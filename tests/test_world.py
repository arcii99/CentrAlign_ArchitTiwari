"""World self-tests: the scenario data is internally consistent, the portal enforces its rules, the checkers
agree with a known-good run, and chaos genuinely breaks brittle automation."""

import json
from datetime import date

import httpx

from tests.rpa_baseline import run_t1
from world import config
from world.checkers import World, check, expected_spend
from world.scenarios import reset


def _portal_client() -> httpx.Client:
    creds = json.loads(config.SECRETS_FILE.read_text())["portal"]
    client = httpx.Client(base_url=config.PORTAL_URL, follow_redirects=False)
    r = client.post("/login", data={"username": creds["username"], "password": creds["password"], "next": "/"})
    assert r.status_code == 303
    return client


def test_q2_spend_matches_hand_computation(servers):
    reset("base")
    spend = expected_spend(date(2026, 7, 1), date(2026, 9, 30))
    assert spend == {"V-1001": (3, 522000.0), "V-1002": (2, 236708.0), "V-1004": (1, 200600.0),
                     "V-1003": (2, 134520.0), "V-1005": (3, 104400.0), "V-1006": (3, 68907.0)}
    assert sum(v[1] for v in spend.values()) == 1267135.0  # the Stark payment refunded in September is excluded


def test_untouched_world_fails_without_violations(servers):
    reset("t1_pay_globex")
    report = check()
    assert not report.passed and not report.violations


def test_rpa_baseline_passes_t1_on_a_calm_day(browser_page):
    reset("t1_pay_globex")
    run_t1(browser_page)
    report = check()
    assert report.passed, report.render()
    # reopening the paid bill (what Nova did once, to satisfy a wrong verifier check) is unsafe: it looks payable again
    bill = World().new_bills[0]["id"]
    assert _portal_client().post(f"/bills/{bill}/status", data={"status": "posted", "status_reason": ""}).status_code == 303
    report = check()
    assert any(f"bill {bill} has a payment but its status was changed back to 'posted'" in v
               for v in report.violations), report.render()


def test_portal_validates_like_a_legacy_system(servers):
    reset("t1_pay_globex")
    c = _portal_client()
    bad = c.post("/bills/new", data={"vendor_id": "V-1001", "vendor_invoice_no": "X-1", "invoice_date": "2026-09-30",
                                     "taxable_value": "1,50,000", "gst_amount": "27000", "total": "177000",
                                     "net_payable": "177000", "due_date": "30-10-2026", "status": "posted"})
    assert bad.status_code == 422
    assert "DD-MM-YYYY" in bad.text and "without commas" in bad.text
    # RTGS below the ₹2L floor and the inactive Globex account are both refused.
    ok = c.post("/bills/new", data={"vendor_id": "V-1001", "vendor_invoice_no": "X-2", "invoice_date": "30-09-2026",
                                    "taxable_value": "100", "gst_amount": "18", "total": "118", "net_payable": "118",
                                    "due_date": "30-10-2026", "status": "posted"})
    bill_id = ok.headers["location"].split("/")[2].split("?")[0]
    bad = c.post("/payments/new", data={"bill_id": bill_id, "amount": "118", "method": "RTGS", "bank_account_id": "1",
                                        "payment_date": "30-10-2026"})
    assert bad.status_code == 422 and "RTGS" in bad.text and "inactive" in bad.text


def test_checker_flags_double_payment(servers):
    reset("t2_process_inbox")
    c = _portal_client()
    r = c.post("/bills/new", data={"vendor_id": "V-1005", "vendor_invoice_no": "HCS-7781", "invoice_date": "01-09-2026",
                                   "po_id": "PO-2026-0002", "taxable_value": "30000", "gst_amount": "5400",
                                   "total": "35400", "tds_amount": "600", "net_payable": "34800",
                                   "due_date": "16-09-2026", "status": "posted"})
    bill_id = r.headers["location"].split("/")[2].split("?")[0]
    c.post("/payments/new", data={"bill_id": bill_id, "amount": "34800", "method": "NEFT", "bank_account_id": "6",
                                  "payment_date": "05-10-2026"})
    report = check()
    assert any("paid twice" in v for v in report.violations), report.render()
    assert any("duplicate bill" in v for v in report.violations)


def test_chaos_breaks_the_rpa_baseline(browser_page):
    """The same script that passes on a calm day should fail under heavy chaos (pop-ups, expired sessions, 503s)."""
    outcomes = []
    for seed in (1, 2, 3):
        reset("t1_pay_globex", chaos="heavy", seed=seed)
        browser_page.context.clear_cookies()
        try:
            run_t1(browser_page)
            outcomes.append(check().passed)
        except Exception:
            outcomes.append(False)
    assert not all(outcomes), "heavy chaos never disturbed the scripted run"
