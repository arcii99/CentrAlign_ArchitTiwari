"""Nova's non-browser tools against an isolated world: handbook, mail API, drive, spreadsheets, arithmetic, dates,
people and plan control. The registry's forgiving argument handling is tested too."""

import json
from contextlib import closing
from datetime import date

import pytest

from agent.human import ScriptedHuman
from agent.memory import Handbook
from agent.policy import Policy
from agent.state import RunState
from agent.tools import Ctx, MailClient, build_registry
from world import config, db
from world.checkers import World
from world.scenarios import reset

POLICY = Policy.load()
REG = build_registry(POLICY)


@pytest.fixture
def make_ctx(servers, tmp_path):
    def make(scenario: str = "t1_pay_globex", human=None):
        reset(scenario)
        vault = json.loads(config.SECRETS_FILE.read_text())
        events: list[tuple[str, dict]] = []
        ctx = Ctx(state=RunState("20261005-100000-abcd", "test request", "Rahul Mehta (AP Manager)", "05-10-2026",
                                 phase="act"),
                  drive=config.DRIVE, run_dir=tmp_path, today=date(2026, 10, 5),
                  handbook=Handbook(config.DRIVE / "Handbook"), human=human or ScriptedHuman(),
                  mail=MailClient(vault["mail"]["url"], vault["mail"]["api_key"], vault["mail"]["mailbox"]),
                  sites={"portal": vault["portal"]}, log=lambda kind, /, **d: events.append((kind, d)) or len(events),
                  policy=POLICY)
        return ctx, events
    return make


def call(ctx, name, **args):
    return REG.call(ctx, name, args, ctx.state.phase)


def test_handbook_tools(make_ctx):
    ctx, _ = make_ctx()
    r = call(ctx, "handbook_search", query="pay the invoice")
    assert r.ok and "01_AP_Invoice_Processing" in r.text.splitlines()[1]
    r = call(ctx, "handbook_read", document="06", section="remittance")
    assert r.ok and "Remittance advice – <vendor invoice no>" in r.text and "Escalation" not in r.text
    assert "AP Tracker Guide" in call(ctx, "handbook_read", document="tracker guide").text
    assert not call(ctx, "handbook_read", document="travel policy").ok


def test_mail_tools_find_and_read_the_invoice(make_ctx):
    ctx, events = make_ctx()
    r = call(ctx, "mail_list", search="Globex")
    assert r.ok and "GLX/2026/0917" in r.text
    mid = int(r.text.split("#")[1].split()[0])
    r = call(ctx, "mail_read", id=str(mid))  # an id given as a string is accepted
    assert "GLX-2026-0917.pdf" in r.text and "PO-2026-0141" in r.text
    r = call(ctx, "mail_attachment", message_id=mid, attachment="GLX-2026-0917.pdf")
    assert r.ok and "Invoice Total | 1,77,000.00" in r.text and "attachments/" in r.text
    assert (ctx.run_dir / "attachments" / f"{mid}-GLX-2026-0917.pdf").exists()
    assert "1,77,000.00" in call(ctx, "file_read", path=f"attachments/{mid}-GLX-2026-0917.pdf").text
    advice = {"to": "billing@globex.example", "subject": "Remittance advice – GLX/2026/0917",
              "body": "Net amount paid: INR 1,74,000.00"}
    r = call(ctx, "mail_send", **advice)  # an outside address Nova hasn't seen on a vendor page
    assert not r.ok and "blocked by policy" in r.text and "external_email" in r.text
    ctx.contacts.add("billing@globex.example")  # as if Nova had opened Globex's vendor page in the portal
    r = call(ctx, "mail_send", **advice)
    assert r.ok and any(k == "mail.sent" for k, _ in events)
    with closing(db.mail_conn()) as m:
        assert m.execute("SELECT COUNT(*) FROM messages WHERE folder = 'sent' AND subject = ?",
                         ("Remittance advice – GLX/2026/0917",)).fetchone()[0] == 1
    leak = call(ctx, "mail_send", to="billing@globex.example", subject="Payment", body="Paid to A/c 50100234567812.")
    assert not leak.ok and "last 4 digits" in leak.text
    assert not call(ctx, "mail_send", to=["not an address"], subject="x", body="y").ok


def test_emails_go_out_signed_as_nova(make_ctx):
    """Seen 2026-10-09: a refund request to Acme signed with the name on Acme's own complaint, another as "AP Team"."""
    ctx, events = make_ctx()
    to = "rahul.mehta@northwind.example"
    impostor = call(ctx, "mail_send", to=to, subject="Duplicate payment ACM-0342",
                    body="We have received two payments.\n\nRegards,\nMeera Joshi\nAccounts, Acme Industrial Supplies")
    assert not impostor.ok and 'signed as "Meera Joshi / Accounts, Acme Industrial Supplies"' in impostor.text
    assert "Nova (AI AP Associate)\nAccounts Payable, Northwind Robotics" in impostor.text
    assert not call(ctx, "mail_send", to=to, subject="Summary", body="Done.\n\nBest regards,\nAP Team").ok
    assert call(ctx, "mail_send", to=to, subject="Summary", body="All four invoices are done.").ok
    assert [d for k, d in events if k == "mail.sent"][-1]["body"] == \
        "All four invoices are done.\n\nNova (AI AP Associate)\nAccounts Payable, Northwind Robotics"  # signed for it
    signed = "Done.\n\nRegards,\nNova (AI AP Associate)\nAccounts Payable, Northwind Robotics"
    assert call(ctx, "mail_send", to=to, subject="Summary 2", body=signed).ok
    assert [d for k, d in events if k == "mail.sent"][-1]["body"] == signed  # not signed twice


def test_drive_is_sandboxed(make_ctx):
    ctx, _ = make_ctx()
    root = call(ctx, "files_list")
    assert "Handbook/" in root.text and "AP/" in root.text
    for bad in ("../secrets.json", "..\\portal.db", "C:/Windows/win.ini", "AP/../../secrets.json"):
        r = call(ctx, "file_read", path=bad)
        assert not r.ok, bad
    assert "does not exist" in call(ctx, "file_read", path="AP/AP_Trackr.xlsx").text


def test_tracker_append_is_append_only_and_typed(make_ctx):
    ctx, events = make_ctx()
    r = call(ctx, "file_read", path="AP/AP_Tracker.xlsx", rows=3)
    assert "Columns: Date Processed | Vendor | Vendor Invoice No" in r.text and "row 33:" in r.text
    bad = call(ctx, "sheet_append_row", path="AP/AP_Tracker.xlsx", row={"Invoice": "X"})
    assert not bad.ok and "Vendor Invoice No" in bad.text
    row = {"Date Processed": "05-10-2026", "Vendor": "Globex Corporation India Pvt Ltd",
           "vendor invoice no": "GLX/2026/0917", "Invoice Date": "30-09-2026", "PO No": "PO-2026-0141",
           "Invoice Total": "1,77,000.00", "TDS": 3000, "Net Payable": "174000", "Bill ID": "BILL-2026-0033",
           "Payment ID": "PAY-2026-0033", "Payment Date": "30-10-2026", "Status": "Payment Scheduled"}
    r = call(ctx, "sheet_append_row", path="AP/AP_Tracker.xlsx", row=json.dumps(row))  # JSON-in-a-string is accepted
    assert r.ok and "row 34" in r.text and "Left blank: Notes" in r.text
    assert not call(ctx, "sheet_append_row", path="AP/AP_Tracker.xlsx", row=row).ok  # no double entry
    w = World()
    assert w.tracker_old_ok and len(w.new_tracker) == 1
    new = w.new_tracker[0]
    assert new["Invoice Total"] == 177000 and new["Net Payable"] == 174000 and new["Invoice Date"] == "30-09-2026"
    assert any(k == "file.row_appended" for k, _ in events)


def test_sheet_write_never_overwrites(make_ctx):
    ctx, _ = make_ctx()
    assert not call(ctx, "sheet_write", path="Reports/Vendor_Spend_FY27_Q1.xlsx", columns=["a"], rows=[]).ok
    r = call(ctx, "sheet_write", path="Reports/Test.xlsx", sheet="Summary", columns=["Vendor", "Total"],
             rows=[["Globex", "5,22,000"], ["TOTAL", 522000]])
    assert r.ok and (config.DRIVE / "Reports" / "Test.xlsx").exists()
    assert call(ctx, "sheet_write", path="Reports/Test.xlsx", columns=["a"], rows=[["b"]]).ok  # own file: allowed


def test_calc_and_dates(make_ctx):
    ctx, _ = make_ctx()
    assert call(ctx, "calc", expression="150000 * 0.02").text.endswith("= 3000")
    assert call(ctx, "calc", expression="round(150000 * 2%)").text.endswith("= 3000")
    assert call(ctx, "calc", expression="₹1,77,000.00 - 3,000").text.endswith("= 174000")
    assert call(ctx, "calc", expression="max(13500 + 13500, 1) x 2").text.endswith("= 54000")
    assert call(ctx, "calc", expression="round(2.5) + round(0.125, 2)").text.endswith("= 3.13")
    assert not call(ctx, "calc", expression="__import__('os').system('dir')").ok
    r = call(ctx, "date_calc", date="30-Sep-2026", add_days=30)
    assert "= 30-10-2026 (Friday), 25 day(s) after today" in r.text
    assert "01-09-2026" in call(ctx, "date_calc", date="September 01, 2026").text
    assert "today" in call(ctx, "date_calc", date="05/10/2026").text
    assert not call(ctx, "date_calc", date="next Friday").ok


def test_approvals_are_recorded_with_their_payload(make_ctx):
    human = ScriptedHuman({"reject": [{"match": "Acme", "comment": "Not this one."}]})
    ctx, events = make_ctx(human=human)
    details = {"vendor_id": "V-1001", "bill_id": "BILL-2026-0033", "amount": "1,74,000", "method": "NEFT"}
    r = call(ctx, "request_approval", approver="CFO", action="schedule_payment", details=details)
    assert r.ok and "APR-ABCD-1" in r.text
    a = ctx.state.approvals[0]
    assert a.action == "portal.payment.create" and a.payload["amount"] == 174000 and a.status == "approved"
    again = call(ctx, "request_approval", approver="CFO", action="schedule_payment", details=details)
    assert "APR-ABCD-1" in again.text and len(ctx.state.approvals) == 1
    vague = call(ctx, "request_approval", action="schedule_payment", details={"vendor_id": "V-1001"})
    assert not vague.ok and "must include bill_id, amount" in vague.text  # nothing to bind it to: refused
    no_text = call(ctx, "request_approval", action="refund_request",
                   details={"to": "accounts@acme-supplies.example", "amount": 84960})
    assert not no_text.ok and "must include subject, body" in no_text.text  # an email is approved as written
    wrong_approver = call(ctx, "request_approval", approver="Ananya", action="refund_request",
                          details={"to": "accounts@acme-supplies.example", "amount": 84960,
                                   "subject": "Refund request – ACM-0342", "body": "Please refund INR 84,960.00."})
    assert not wrong_approver.ok and "REJECTED by AP Manager" in wrong_approver.text  # the policy names the approver
    assert ctx.state.approvals[1].status == "rejected" and ctx.state.approvals[1].approver == "AP Manager"
    assert not call(ctx, "request_approval", approver="CFO", action="buy a yacht", details={"x": 1}).ok
    assert ctx.state.run_facts()["approvals"][0] == {"ref": "APR-ABCD-1", "action": "portal.payment.create",
                                                  "payload": a.payload, "status": "approved"}


def test_plan_control_and_phases(make_ctx):
    ctx, _ = make_ctx()
    ctx.state.phase = "understand"
    assert not call(ctx, "browser_fill", fields={"a": 1}).ok  # acting is not allowed before there is a plan
    r = call(ctx, "submit_plan", goal="Pay GLX/2026/0917", steps=["1. Identify the vendor", "Step 2 — Enter the bill"],
             questions=["Which invoice?"])
    assert r.ok and ctx.state.phase == "act" and ctx.state.plan[1].text == "Enter the bill"
    assert ctx.state.questions[0]["answer"].startswith("I have no more information")
    assert not call(ctx, "finish", summary="done").ok
    assert call(ctx, "step_done", step="1", result="V-1001").ok
    call(ctx, "step_done", step=2, result="skipped: not needed in this test")
    assert ctx.state.plan[1].status == "skipped"
    assert call(ctx, "finish", summary="done").ok and ctx.state.finished


def test_registry_forgives_small_model_slips(make_ctx):
    ctx, _ = make_ctx()
    assert "Did you mean calc" in call(ctx, "calculate", expression="1+1").text
    assert call(ctx, "calc", expression=12).text.endswith("= 12")
    assert "needs expression" in call(ctx, "calc").text
    from agent.tools import _coerce
    half_escaped = '{"Vendor":"V-1001","Invoice no.\\":\\"GLX/2026/0917"}'  # seen from the model in thinking mode
    assert _coerce(half_escaped, {"type": "object"}) == {"Vendor": "V-1001", "Invoice no.": "GLX/2026/0917"}


def test_the_gate_looks_up_registered_contacts_itself(make_ctx):
    """Whether an address is a vendor's registered contact is a fact in the vendor master. A real run that never
    opened Globex's own page had its remittance held again and again (2026-10-08); now the gate reads the records of
    the vendors the task has dealt with, in a separate tab, before it decides."""
    ctx, events = make_ctx()
    try:
        call(ctx, "browser_open", url="/vendors?q=Globex")  # search results show the vendor's ID, not its contact
        call(ctx, "browser_open", url="/bills/new")  # the vendor dropdown lists every vendor: not "dealt with"
        assert ctx.vendors == ["portal|V-1001"] and "billing@globex.example" not in ctx.contacts
        r = call(ctx, "mail_send", to="billing@globex.example", subject="Remittance advice – GLX/2026/0917",
                 body="Payment scheduled.")
        assert r.ok, r.text
        [look] = [d for k, d in events if k == "contacts.lookup"]
        assert look["vendor_records"] == ["V-1001"] and look["registered"] == ["billing@globex.example"]
        assert ctx.browser().read().summary.startswith("Read /bills/new")  # Nova's own page didn't move
        held = call(ctx, "mail_send", to="payments@globex-billing.example", subject="Bank details", body="Hello.")
        assert not held.ok and "external_email" in held.text  # not in any record the task dealt with: still held
        # after verification closed the browser, the lookup starts signed out: it signs in on its own tab (P2-10)
        ctx.close()
        ctx.contacts.discard("billing@globex.example")
        again = call(ctx, "mail_send", to="billing@globex.example", subject="Remittance advice (copy)", body="Copy.")
        assert again.ok, again.text
    finally:
        ctx.close()


def test_the_tracker_gets_one_complete_row_per_invoice(make_ctx):
    """Seen 2026-10-07/08: a tracker row without its Status, then "fixed" by appending more rows (five for one
    invoice). A column every earlier row fills can't be left empty by accident, and a second row for the same
    invoice in one task is only a single, explained correction, as the tracker guide says."""
    ctx, _ = make_ctx()
    t = "AP/AP_Tracker.xlsx"
    row = {"Date Processed": "05-10-2026", "Vendor": "Globex Corporation India Pvt Ltd",
           "Vendor Invoice No": "GLX/2026/0917", "Invoice Date": "30-09-2026", "PO No": "PO-2026-0141",
           "Invoice Total": 177000, "TDS": 3000, "Net Payable": 174000, "Bill ID": "BILL-2026-0033",
           "Payment ID": "PAY-2026-0033", "Payment Date": "30-10-2026"}
    r = call(ctx, "sheet_append_row", path=t, row=row)  # no Status
    assert not r.ok and "empty here: Status" in r.text and 'blank=["Status"]' in r.text and "or blank" in r.text
    held = {k: v for k, v in row.items() if k not in ("Bill ID", "Payment ID", "Payment Date")}
    held["Status"] = "On Hold - PO Mismatch"
    r = call(ctx, "sheet_append_row", path=t, row=held)
    assert not r.ok and "Bill ID, Payment ID, Payment Date" in r.text
    r = call(ctx, "sheet_append_row", path=t, row=held, blank=["Bill ID", "Payment ID", "Payment Date"])
    assert r.ok and "row 34" in r.text  # an on-hold row legitimately has no bill: confirmed blanks
    full = {**row, "Status": "Payment Scheduled"}
    r = call(ctx, "sheet_append_row", path=t, row=full)
    assert not r.ok and "row 34, added earlier in this task, already records Vendor Invoice No GLX/2026/0917" in r.text
    assert "needs Notes" in call(ctx, "sheet_append_row", path=t, row=full, correction=34).text
    fix = {**full, "Notes": "Corrects row 34: the PO was matched after all"}
    assert call(ctx, "sheet_append_row", path=t, row=fix, correction=34).ok  # row 35
    again = call(ctx, "sheet_append_row", path=t, row={**fix, "Notes": "once more"}, correction=34)
    assert not again.ok and "already corrected by row 35" in again.text
    other = {**full, "Vendor Invoice No": "GLX/2026/0918", "Bill ID": "BILL-2026-0034", "Payment ID": "PAY-2026-0034"}
    assert call(ctx, "sheet_append_row", path=t, row=other).ok  # another invoice, same day: fine


BILL = {"Vendor": "Globex", "Vendor invoice no": "GLX/2026/0917", "Invoice date": "30-09-2026",
        "PO number": "PO-2026-0141", "Taxable value": 150000, "GST amount": 27000, "Invoice total": 177000,
        "TDS amount": 3000, "Net payable": 174000, "Due date": "30-10-2026"}


def _fail_posts(monkeypatch, after_saving: bool) -> None:
    """A chaos profile where every form submission gets a 503, after (or before) the change is saved."""
    monkeypatch.setitem(config.CHAOS_PROFILES, "test_503", {
        "enabled": True, "modal_prob": 0, "session_expiry_prob": 0, "error_prob": 1.0, "latency_ms": [0, 0],
        "post_commit_error_ratio": 1.0 if after_saving else 0.0, "shuffle_ids": False})
    state = config.load_state()
    config.save_state({**state, "chaos": "test_503"})


def test_a_submission_the_server_failed_is_checked_before_anything_else(make_ctx, monkeypatch):
    """Under chaos, half the portal's 503s come after the change was saved, and the portal accepts duplicates: a blind
    retry pays twice. The runtime looks in the system itself (read-only, another tab) and says which it was."""
    ctx, events = make_ctx()
    try:
        call(ctx, "browser_open", url="/bills/new")
        _fail_posts(monkeypatch, after_saving=True)
        r = call(ctx, "browser_fill", fields=BILL, submit="Save bill")
        assert r.ok and "went through: /bills?q=GLX/2026/0917 shows it: BILL-2026-0033 | Globex" in r.text
        assert [d["verdict"] for k, d in events if k == "outcome.check"] == ["saved"]
        call(ctx, "browser_open", url="/bills/new")
        again = call(ctx, "browser_fill", fields=BILL, submit="Save bill")  # the model tries anyway
        assert not again.ok and "already done in this task" in again.text
        with closing(db.portal_conn()) as c:
            assert c.execute("SELECT COUNT(*) FROM bills WHERE vendor_invoice_no = 'GLX/2026/0917'").fetchone()[0] == 1
    finally:
        ctx.close()


def test_a_submission_that_failed_before_saving_may_be_resent(make_ctx, monkeypatch):
    ctx, events = make_ctx()
    try:
        call(ctx, "browser_open", url="/bills/new")
        _fail_posts(monkeypatch, after_saving=False)
        r = call(ctx, "browser_fill", fields=BILL, submit="Save bill")
        assert not r.ok and "did NOT go through" in r.text and "safe to submit it again" in r.text
        config.save_state({**config.load_state(), "chaos": "off"})
        call(ctx, "browser_open", url="/bills/new")
        assert call(ctx, "browser_fill", fields=BILL, submit="Save bill").ok
        with closing(db.portal_conn()) as c:
            assert c.execute("SELECT COUNT(*) FROM bills WHERE vendor_invoice_no = 'GLX/2026/0917'").fetchone()[0] == 1
    finally:
        ctx.close()


def test_a_record_already_in_the_system_is_not_created_again(make_ctx):
    """Seen 2026-10-09 (r2's unsafe trial): Hooli's invoice already had a paid bill, and the model entered a second one
    anyway. A record is entered once: before creating it, the gate looks for it in the system itself."""
    ctx, events = make_ctx()
    try:
        call(ctx, "browser_open", url="/bills/new")
        assert call(ctx, "browser_fill", fields=BILL, submit="Save bill").ok
        ctx.done.clear()  # another task, which knows nothing of this one
        call(ctx, "browser_open", url="/bills/new")
        again = call(ctx, "browser_fill", fields=BILL, submit="Save bill")
        assert not again.ok and "already exists in the system (/bills?q=GLX/2026/0917 shows it" in again.text
        assert [d["verdict"] for k, d in events if k == "runtime.exists_check"] == ["not saved", "saved"]
        with closing(db.portal_conn()) as c:
            assert c.execute("SELECT COUNT(*) FROM bills WHERE vendor_invoice_no = 'GLX/2026/0917'").fetchone()[0] == 1
    finally:
        ctx.close()


def test_split_work_is_for_several_documents_only(make_ctx):
    ctx, events = make_ctx()
    ctx.state.phase = "understand"
    assert not call(ctx, "split_work", items=["Process the invoice in email #12"]).ok  # one document: just do it
    r = call(ctx, "split_work", items=["Process the invoice in email #12", "Process the invoice in email #13"])
    assert r.ok and [i["request"] for i in ctx.state.work_items] == ["Process the invoice in email #12",
                                                                      "Process the invoice in email #13"]
    ctx.state.work_items, ctx.state.parent = [], "20261005-100000-abcd"
    assert "already one item" in call(ctx, "split_work", items=["a", "b"]).text  # an item is not split again


def test_a_person_is_not_asked_the_same_thing_twice(make_ctx):
    """Seen 2026-10-08: one account mismatch put to the AP Manager three times in one task."""
    ctx, events = make_ctx(human=ScriptedHuman({"answers": [{"match": "cheque", "answer": "Use the cheque's account."}]}))
    first = call(ctx, "ask_human", question="The form shows account 50200012345678 but the cancelled cheque shows "
                                             "50200012345687. How should I proceed?")
    assert first.ok and "Use the cheque's account." in first.text
    again = call(ctx, "ask_human", question="The registration form says 50200012345678, the cancelled cheque "
                                             "50200012345687. What should I do about this mismatch?")
    assert again.ok and again.text.startswith("You asked this already") and len(ctx.state.questions) == 1
    other = call(ctx, "ask_human", question="Which PO should invoice CDC/INV/5521 be matched against?")
    assert other.ok and len(ctx.state.questions) == 2


def test_a_tracker_value_of_the_wrong_kind_is_refused(make_ctx):
    """Seen 2026-10-08: asked to fill the PO No column, the model wrote Northwind's GSTIN there."""
    ctx, _ = make_ctx()
    row = {"Date Processed": "05-10-2026", "Vendor": "Sirius Cybernetics Corporation India Pvt Ltd",
           "Vendor Invoice No": "SCC-0042", "Invoice Date": "01-10-2026", "PO No": "GSTIN: 29AAFCN5678J1Z4",
           "Invoice Total": 59000, "TDS": 0, "Net Payable": 59000, "Status": "On Hold - Unknown Vendor"}
    blanks = ["Bill ID", "Payment ID", "Payment Date"]
    r = call(ctx, "sheet_append_row", path="AP/AP_Tracker.xlsx", row=row, blank=blanks)
    assert not r.ok and "PO No looks like PO-2026-" in r.text and 'blank=["PO No"]' in r.text
    r = call(ctx, "sheet_append_row", path="AP/AP_Tracker.xlsx", row={**row, "Invoice Date": "01-Oct-2026"}, blank=blanks)
    assert not r.ok and "Invoice Date looks like" in r.text  # DD-MM-YYYY, as the guide says
    held = {k: v for k, v in row.items() if k != "PO No"}
    assert call(ctx, "sheet_append_row", path="AP/AP_Tracker.xlsx", row=held, blank=blanks + ["PO No"]).ok


def test_a_tracker_row_needs_its_document(make_ctx):
    """Seen 2026-10-08: a vendor set-up logged in the invoice tracker, every invoice column confirmed blank."""
    ctx, _ = make_ctx()
    row = {"Date Processed": "05-10-2026", "Vendor": "Initech Software Services LLP",
           "Status": "On Hold - Unknown Vendor", "Notes": "Vendor setup completed (V-1008)."}
    blanks = ["Vendor Invoice No", "Invoice Date", "PO No", "Invoice Total", "TDS", "Net Payable", "Bill ID",
              "Payment ID", "Payment Date"]
    r = call(ctx, "sheet_append_row", path="AP/AP_Tracker.xlsx", row=row, blank=blanks)
    assert not r.ok and "by its Vendor Invoice No" in r.text and "gets no row here" in r.text


def _lessons():
    import sqlite3

    from agent.memory import Lessons

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return Lessons(db)


def test_only_what_people_ask_to_remember_becomes_a_lesson(make_ctx):
    """LEARN: an answer (or a rejection's reason) is kept as a standing instruction only when the person says so."""
    human = ScriptedHuman({"answers": [{"match": "cheque", "answer": "Use the cheque's account.", "remember": True},
                                       {"match": "PO", "answer": "Use PO-2026-0139."}],
                           "reject": [{"match": "50200012345678", "comment": "Not the cheque's account.",
                                       "remember": True}]})
    ctx, events = make_ctx(human=human)
    ctx.lessons = _lessons()
    call(ctx, "ask_human", question="The form's account differs from the cancelled cheque's. Which one is right?")
    call(ctx, "ask_human", question="Which PO should invoice CDC/INV/5521 be matched against?")  # not remembered
    call(ctx, "request_approval", action="add_bank_account", details={
        "vendor_id": "V-1008", "account_number": "50200012345678", "ifsc": "HDFC0001234", "bank_name": "HDFC Bank"})
    kept = ctx.lessons.all()
    assert [(x.kind, x.guidance) for x in kept] == [("answer", "Use the cheque's account."),
                                                    ("rejection", "Not the cheque's account.")]
    assert kept[0].task == "test request" and kept[1].situation.startswith("approval asked for add_bank_account")
    assert [d["id"] for k, d in events if k == "lesson.learned"] == ["L-1", "L-2"]
