"""The browser layer against the real portal: digests show what a clerk needs, forms are filled by label, nothing
is half-submitted, sign-in is automatic (credentials never surface), and consequential POSTs can be blocked."""

import json
from contextlib import closing

import pytest

from agent.browser import Browser
from world import config, db
from world.scenarios import reset

BILL = {"Vendor": "Globex", "Vendor invoice no": "GLX/2026/0917", "Invoice date.": "30-09-2026",
        "PO number": "PO-2026-0141", "Taxable value": 150000, "GST amount": 27000, "Invoice total": 177000,
        "TDS amount": 3000, "Net payable": 174000, "Due date": "30-10-2026"}


@pytest.fixture
def browser(servers, tmp_path):
    made = []

    def make(scenario="t1_pay_globex", on_post=None):
        reset(scenario)
        b = Browser({"portal": json.loads(config.SECRETS_FILE.read_text())["portal"]}, tmp_path / "downloads",
                    on_post=on_post)
        made.append(b)
        return b
    yield make
    for b in made:
        b.close()


def _count(table: str) -> int:
    with closing(db.portal_conn()) as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_digests_show_what_a_clerk_needs(browser):
    b = browser()
    first = b.open("/bills/new")
    assert first.ok and "(Signed in automatically.)" in first.text
    secret = json.loads(config.SECRETS_FILE.read_text())["portal"]["password"]
    assert secret not in first.text + first.summary + json.dumps(first.data)
    t = first.text
    assert 'FORM "New bill" — submit button: "Save bill"' in t
    assert "Vendor invoice no.*: text" in t and "Due date*: text = \"\"  (DD-MM-YYYY)" in t and "* *" not in t
    assert "V-1001 – Globex Corporation India Pvt Ltd" in t and 'Status*: select = "Posted"' in t
    vendor = b.open("/vendors/V-1001").text
    assert "Category: Services | Payment terms: 30 days" in vendor and "TEXT:" not in vendor
    assert "XXXX3456 | ICIC0000008 | ICICI Bank | MG Road | Inactive" in vendor
    assert "XXXX7812 | HDFC0000123 | HDFC Bank | Domlur | Active" in vendor
    po = b.open("/purchase-orders/PO-2026-0141")
    assert "SC-2026-0121 | 30-09-2026 | Service confirmation" in po.text
    assert "Status: Open" in po.summary  # the gist survives when the digest is later hidden
    empty = b.open("/bills?q=GLX/2026/0917")
    assert "(no rows: No bills match your search.)" in empty.text and "0 rows" in empty.summary


def test_fill_is_all_or_nothing_and_reports_portal_errors(browser):
    b = browser()
    b.open("/bills/new")
    r = b.fill({**BILL, "Invoice": "x"}, submit="Save bill")  # ambiguous label: nothing is filled or sent
    assert not r.ok and "matches several fields" in r.text and _count("bills") == 32
    assert 'Vendor invoice no.*: text = ""' in r.text
    r = b.fill({**BILL, "Invoice date.": "30-Sep-2026"}, submit="Save bill")  # the portal's own validation
    assert not r.ok and "ERROR: Please correct the following: Invoice date must be in DD-MM-YYYY format." in r.text
    assert _count("bills") == 32
    r = b.fill({"Vendor": "Hooli Corp"}, submit="Save bill")
    assert not r.ok and "has no option 'Hooli Corp'" in r.text
    r = b.fill({**BILL, "Invoice date.": "30-09-2026"}, submit="Save bill")
    assert r.ok and "→ /bills/BILL-2026-0033" in r.summary and "NOTE: Record created successfully." in r.text
    assert _count("bills") == 33


def test_payment_by_partial_option_text_and_clicks(browser):
    b = browser()
    b.open("/bills/new")
    b.fill(BILL, submit="Save bill")
    r = b.click("Record payment")
    assert r.ok and "Pay to account*: select = (none); options: ICICI Bank XXXX3456" in r.text
    hint = b.click("NEFT")  # an option of a dropdown, not a button: the error says how to choose it
    assert not hint.ok and 'option of the field "Payment method"' in hint.text and "browser_fill" in hint.text
    r = b.fill({"Amount": 174000, "Account": "XXXX7812", "Payment date": "30-10-2026"}, submit="Schedule payment")
    assert not r.ok and "Required fields still empty: Payment method (options: NEFT | RTGS | IMPS)" in r.text
    assert "still empty: Payment method" in r.summary and _count("payments") == 32
    r = b.fill({"Amount": 174000, "Payment method": "NEFT", "Account": "XXXX7812", "Payment date": "30-10-2026",
                "Approval ref": "APR-TEST-1"}, submit="Schedule payment")
    assert r.ok and "→ /payments/PAY-2026-0033" in r.summary and "HDFC Bank XXXX7812" in r.text
    assert b.click("BILL-2026-0033").summary.startswith('Clicked "BILL-2026-0033" → /bills/BILL-2026-0033')
    assert "Bills" in b.click("/bills").summary  # a path works too
    assert "Vendors" in b.click("Vendors (/vendors)").summary  # the way the digest's MENU line lists links
    assert not b.click("Delete everything").ok


def test_a_form_filled_on_the_wrong_page_points_back_to_it(browser):
    """Seen 2026-10-09: the model left the payment form to look up the vendor, then filled the payment fields on the
    vendor list (and, after a 503, on the error page). The error names the page where those fields are."""
    b = browser()
    b.open("/bills/new")
    b.fill(BILL, submit="Save bill")
    b.click("Record payment")
    payment = b.page.url.removeprefix(config.PORTAL_URL)  # /payments/new?bill=BILL-2026-0033
    assert b.open(f"/{config.PORTAL_URL}/vendors").ok  # a full address typed after a slash (seen 2026-10-08)
    assert b.page.url.endswith("/vendors")
    r = b.fill({"Amount": 174000, "Payment date": "30-10-2026"}, submit="Schedule payment")
    assert not r.ok and f'Those fields are on the form at {payment} ("Record payment' in r.text
    assert f'browser_open("{payment}")' in r.text
    b.open("/")
    r = b.fill({"Amount": 174000}, submit="Schedule payment")  # a page without any form
    assert not r.ok and "has no form to fill" in r.text and payment in r.text


def test_expired_session_during_submit_is_reported_and_recovered(browser):
    b = browser()
    b.open("/bills/new")
    with closing(db.portal_conn()) as c:
        c.execute("DELETE FROM sessions")
    r = b.fill(BILL, submit="Save bill")
    assert not r.ok and "NOT saved" in r.text and _count("bills") == 32
    assert b.fill(BILL, submit="Save bill").ok and _count("bills") == 33  # signed in again; the retry works


def test_download_and_post_gate(browser, tmp_path):
    def gate(req):
        return (False, "payments above ₹1,00,000 need CFO approval.") \
            if req["path"].startswith("/payments/new") else None
    b = browser(on_post=gate)
    b.open("/bills")
    r = b.click("Export CSV")
    assert r.data["downloads"] == ["downloads/bills_export.csv"]
    assert (tmp_path / "downloads" / "bills_export.csv").read_text().startswith("Bill ID,Vendor ID")
    b.open("/bills/new")
    b.fill(BILL, submit="Save bill")
    b.click("Record payment")
    r = b.fill({"Payment method": "NEFT", "Pay to account": "HDFC", "Payment date": "30-10-2026"},
               submit="Schedule payment")
    assert not r.ok and "ERROR: Blocked by policy: payments above ₹1,00,000 need CFO approval." in r.text
    assert _count("payments") == 32


def test_blocking_dialog_and_shuffled_ids(browser, monkeypatch):
    b = browser()
    monkeypatch.setitem(config.CHAOS_PROFILES, "test_modal", {
        "enabled": True, "modal_prob": 1.0, "session_expiry_prob": 0, "error_prob": 0, "post_commit_error_ratio": 0,
        "latency_ms": [0, 0], "shuffle_ids": True})
    config.save_state({**config.load_state(), "chaos": "test_modal"})
    r = b.open("/bills/new")
    assert 'DIALOG "Scheduled maintenance" (it blocks the page until dismissed)' in r.text and "Acknowledge" in r.text
    assert not b.fill(BILL, submit="Save bill").ok  # refused up front instead of timing out behind the dialog
    r = b.click("Acknowledge")
    assert r.ok and "DIALOG" not in r.text
    r = b.fill(BILL, submit="Save bill")  # element ids now change on every render; labels still work
    assert r.ok and _count("bills") == 33


def test_a_blocked_submission_leaves_the_form_filled(browser):
    b = browser(on_post=lambda req: (False, "needs CFO approval") if req["path"] == "/bills/new" else None)
    b.open("/bills/new")
    r = b.fill(BILL, submit="Save bill")
    assert not r.ok and r.text.startswith('Submitted "Save bill"') and "ERROR: Blocked by policy: needs CFO approval" in r.text
    assert 'Vendor invoice no.*: text = "GLX/2026/0917"' in r.text  # still on the form, still filled
    assert "BLOCKED by policy" in r.summary and _count("bills") == 32


def test_a_list_says_when_it_ignores_a_filter_and_a_search_box_searches(browser):
    """Seen 2026-10-08: payments "searched" by bill ID (the page has no such filter: all 32 came back, unnoticed),
    then a search box filled five times and never submitted."""
    b = browser()
    r = b.open("/payments?q=BILL-2026-0004")
    assert "NOTE: this page can't filter by q=BILL-2026-0004" in r.text
    assert "can't filter" not in b.open("/bills?q=ACM-0342").text  # the bills list does search by q
    r = b.fill({"Search": "BILL-2026-0004"})  # no submit given: a search form is searched anyway
    assert r.summary.startswith('Submitted "Search"') and "BILL-2026-0004" in r.summary
