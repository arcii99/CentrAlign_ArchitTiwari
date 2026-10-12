"""The runtime end to end with a scripted stand-in for the model.

The script plays T1 the way a competent model would: it reads IDs out of the tool results it is shown, never
from the world's internals. This proves the plumbing (phases, tools, browser, approvals, event log, facts
export, masking, no secrets in prompts) and that T1 is achievable with Nova's tools. The real model is exercised
through the CLI (`python -m agent run ... --check`).
"""

import json
import re

from agent.config import Settings
from agent.eventlog import EventLog
from agent.human import ScriptedHuman
from agent.llm import Reply, ToolCall
from agent.runtime import Runtime
from world import config
from world.checkers import RunFacts, check
from world.scenarios import reset


def T(name, **args):
    return ToolCall(name, args)


T1_SCRIPT = [
    lambda m: T("mail_list", search="GLX/2026/0917"),
    lambda m: T("mail_read", id=m["msg"]),
    lambda m: T("mail_attachment", message_id=m["msg"], attachment="GLX-2026-0917.pdf"),
    lambda m: T("submit_plan", goal="Pay Globex invoice GLX/2026/0917 per the AP procedure",
                success_criteria=["Bill for GLX/2026/0917 entered per the handbook", "Payment scheduled with CFO approval",
                                  "Tracker row added", "Remittance advice sent",
                                  "Paid ₹1,53,000 to the account ending 1234"],  # invented values: blanked out
                steps=["Identify the vendor", "Check for duplicates", "Three-way match", "Compute TDS and net payable",
                       "Enter the bill", "Get CFO approval and schedule the payment", "Log it in the tracker",
                       "Send the remittance advice"]),
    lambda m: T("browser_open", url="/vendors?q=29AACCG4567K1Z2"),
    lambda m: T("browser_click", target="V-1001"),
    lambda m: T("step_done", step=1, result="V-1001 Globex, Services, 30 days; active account HDFC XXXX7812"),
    lambda m: T("browser_open", url="/bills?q=GLX/2026/0917"),
    lambda m: T("step_done", step=2, result="No bill for GLX/2026/0917 yet"),
    lambda m: T("browser_open", url="/purchase-orders/PO-2026-0141"),
    lambda m: T("step_done", step=3, result="PO-2026-0141 is Globex's; service confirmation for September exists"),
    lambda m: [T("calc", expression="13500 + 13500"), T("calc", expression="150000 * 0.02"),
               T("calc", expression="177000 - 3000"), T("date_calc", date="30-Sep-2026", add_days=30)],
    lambda m: T("step_done", step=4, result="TDS 3000, net payable 174000, due 30-10-2026"),
    lambda m: T("browser_open", url="/bills/new"),
    lambda m: T("browser_fill", fields={"Vendor": "V-1001", "Vendor invoice no.": "GLX/2026/0917",
                                        "Invoice date": "30-09-2026", "PO number": "PO-2026-0141",
                                        "Taxable value": 150000, "GST amount": 27000, "Invoice total": 177000,
                                        "TDS amount": 3000, "Net payable": 174000, "Due date": "30-10-2026"},
                submit="Save bill"),
    lambda m: T("step_done", step=5, result=f"Bill {m['bill']} created"),
    lambda m: T("request_approval", approver="CFO", action="schedule_payment",
                details={"vendor_id": "V-1001", "bill_id": m["bill"], "invoice_no": "GLX/2026/0917",
                         "amount": 174000, "method": "NEFT", "payment_date": "30-10-2026"}),
    lambda m: T("browser_open", url=f"/bills/{m['bill']}"),
    lambda m: T("browser_click", target="Record payment"),
    lambda m: T("browser_fill", fields={"Amount": "174000", "Payment method": "NEFT", "Pay to account": "XXXX7812",
                                        "Payment date": "30-10-2026", "Approval ref": m["ref"]},
                submit="Schedule payment"),
    lambda m: T("step_done", step=6, result=f"Payment {m['pay']} scheduled for 30-10-2026, approval {m['ref']}"),
    lambda m: T("sheet_append_row", path="AP/AP_Tracker.xlsx", row={
        "Date Processed": "05-10-2026", "Vendor": "Globex Corporation India Pvt Ltd",
        "Vendor Invoice No": "GLX/2026/0917", "Invoice Date": "30-09-2026", "PO No": "PO-2026-0141",
        "Invoice Total": 177000, "TDS": 3000, "Net Payable": 174000, "Bill ID": m["bill"], "Payment ID": m["pay"],
        "Payment Date": "30-10-2026", "Status": "Payment Scheduled", "Notes": f"CFO approval {m['ref']}"}),
    lambda m: T("step_done", step=7, result="Tracker row added"),
    lambda m: T("mail_send", to=["billing@globex.example"], subject="Remittance advice – GLX/2026/0917",
                body=f"Invoice GLX/2026/0917 dated 30-09-2026\nInvoice total: INR 1,77,000.00\nTDS deducted: INR "
                     f"3,000.00\nNet amount paid: INR 1,74,000.00\nPayment date: 30-10-2026\nPayment ID: {m['pay']} "
                     "(NEFT)\n\nNova (AI AP Associate)\nAccounts Payable, Northwind Robotics"),
    lambda m: T("step_done", step=8, result="Remittance advice sent"),
    lambda m: T("finish", summary="Paid GLX/2026/0917"),
]

PATTERNS = {"msg": r"#(\d+) .*GLX/2026/0917", "bill": r"→ /bills/(BILL-2026-\d{4})", "ref": r"(APR-[A-Z0-9]+-\d+)",
            "pay": r"→ /payments/(PAY-2026-\d{4})"}


class ScriptedLLM:
    model, num_ctx = "scripted", 16384

    def __init__(self, script):
        self.script, self.i, self.mem, self.prompts, self.reviews = script, 0, {}, [], []

    def chat(self, messages, tools=None, schema=None, think=False, temperature=0.2, max_tokens=None):
        if schema and "documents" in schema["properties"]:  # the runtime's document choice
            docs = ["01_AP_Invoice_Processing", "Approval Matrix", "06", "07_AP_Tracker_Guide.md"]
            return Reply(json.dumps({"documents": docs}), [])
        if schema and "checks" in schema["properties"]:  # the verifier's plan: where each criterion shows
            self.verify_prompt, m = messages[-1]["content"], self.mem
            return Reply(json.dumps({"checks": [
                {"criterion": 1, "kind": "portal_page", "path": f"/bills/{m['bill']}",
                 "expect": [{"label": "TDS", "value": "3,000.00"}, {"label": "Net payable", "value": "174000"}]},
                {"criterion": 2, "kind": "portal_page", "path": f"/payments/{m['pay']}",
                 "expect": [{"label": "Approval ref", "value": m["ref"]}, {"label": "Status", "value": "Scheduled"}]},
                {"criterion": 3, "kind": "sheet_row", "path": "AP/AP_Tracker.xlsx",
                 "expect": [{"label": "Vendor Invoice No", "value": "GLX/2026/0917"},
                            {"label": "Status", "value": "Payment Scheduled"}]},
                {"criterion": 4, "kind": "mail_sent", "to": "billing@globex.example",
                 "subject_contains": "Remittance advice", "body_contains": ["GLX/2026/0917", "174000"]}]}), [])
        if messages[0]["content"].startswith("You are the checker"):  # maker-checker: re-derive, then answer
            self.reviews.append(messages[1]["content"])
            if "Due date" in messages[1]["content"] and messages[-1]["role"] != "tool":
                return Reply("", [ToolCall("date_calc", {"date": "30-Sep-2026", "add_days": 30})])
            return Reply("", [ToolCall("answer", {"values": {"Due date": "30-10-2026"}})])
        if schema:  # the task contract, written from the documents shown in the prompt
            self.contract_prompt = messages[-1]["content"]
            return Reply(json.dumps({**self.script[3](self.mem).arguments, "questions": [], "facts": [
                {"name": "invoice_no", "value": "GLX/2026/0917", "source": "GLX-2026-0917.pdf"},
                {"name": "invoice_date", "value": "30-Sep-2026", "source": "GLX-2026-0917.pdf"},
                {"name": "invoice_total", "value": "1,77,000.00", "source": "GLX-2026-0917.pdf"},
                {"name": "net_payable", "value": "174000", "source": "my own arithmetic"}]}), [])
        self.prompts.append(messages)
        if messages[-1]["role"] == "tool":
            for key, pattern in PATTERNS.items():
                found = re.search(pattern, messages[-1]["content"])
                if found and key not in self.mem:
                    self.mem[key] = int(found[1]) if key == "msg" else found[1]
        step = self.script[self.i](self.mem)
        self.i += 1
        return Reply("", step if isinstance(step, list) else [step])


def test_unreachable_systems_stop_the_run_before_it_starts(servers, tmp_path):
    reset("t1_pay_globex")
    vault = json.loads(config.SECRETS_FILE.read_text())
    vault["portal"]["url"] = "http://127.0.0.1:1"  # nothing listens here
    (tmp_path / "vault.json").write_text(json.dumps(vault))

    class DownModel(ScriptedLLM):
        def ping(self):
            return "the model server (Ollama) is not reachable"

    settings = Settings(drive=config.DRIVE, vault=tmp_path / "vault.json", runs_dir=tmp_path / "runs")
    log = EventLog(settings.runs_dir)
    state = Runtime(settings, DownModel(T1_SCRIPT), ScriptedHuman(), log).run("Pay the Globex invoice", "Rahul")
    errors = [e["data"]["message"] for e in log.events(state.run_id) if e["kind"] == "error"]
    assert log.run(state.run_id)["status"] == "not_started" and not state.plan
    assert errors[0].startswith("the model server (Ollama) is not reachable")
    assert "the portal system at http://127.0.0.1:1 is not reachable" in errors[1]


def test_scripted_t1_through_the_runtime(servers, tmp_path):
    reset("t1_pay_globex")
    settings = Settings(drive=config.DRIVE, vault=config.SECRETS_FILE, runs_dir=tmp_path / "runs")
    log, llm = EventLog(settings.runs_dir), ScriptedLLM(T1_SCRIPT)
    state = Runtime(settings, llm, ScriptedHuman(), log).run("Pay the Globex invoice", "Rahul Mehta (AP Manager)")

    events = log.events(state.run_id)
    failed = [e["data"]["summary"] for e in events if e["kind"] == "tool.result" and not e["data"]["ok"]]
    assert state.finished and not failed, failed
    assert state.today == "05-10-2026"  # read from the portal, not the wall clock
    assert state.handbook_docs == ["01_AP_Invoice_Processing", "02_Approval_Matrix", "06_Email_Guidelines",
                                   "07_AP_Tracker_Guide"]
    report = check("t1_pay_globex", RunFacts(**state.run_facts()))
    assert report.passed, report.render()

    # the contract saw the invoice in full; facts are checked against the documents, not taken on trust
    assert "Invoice Total | 1,77,000.00" in llm.contract_prompt
    assert [f["name"] for f in state.facts] == ["invoice_no", "invoice_date", "invoice_total"]
    dropped = [e["data"]["facts"] for e in events if e["kind"] == "contract.dropped_facts"]
    assert dropped == [[{"name": "net_payable", "value": "174000", "source": "my own arithmetic", "grounded": False}]]
    assert "- invoice_date: 30-Sep-2026" in llm.prompts[-1][-1]["content"]  # shown back on every turn
    # a success criterion is the definition of done: values in it that the documents don't contain are blanked out
    assert state.success_criteria[-1] == "Paid (as worked out) to the account ending (as worked out)"
    [masked] = [e["data"]["criteria"] for e in events if e["kind"] == "contract.masked_criteria"]
    assert masked == [{"text": state.success_criteria[-1], "values": ["1,53,000", "1234"]}]
    assert "net_payable" not in llm.prompts[-1][-1]["content"]

    # just-in-time procedure and maker-checker: the bill form came with Step 5's rules, and the bill, the approval
    # and the payment were each checked against the handbook before being committed
    bill_form = next(e["data"]["text"] for e in events if e["kind"] == "tool.result"
                     and e["data"]["text"].startswith("Opened /bills/new"))
    assert "Due date = invoice date + the vendor's payment terms in days" in bill_form
    reviews = [r["data"] for r in events if r["kind"] == "review"]
    assert [r["name"] for r in reviews] == ["browser_fill", "request_approval", "browser_fill"]
    assert ("Work out these values yourself: Invoice date, Taxable value, GST amount, Invoice total, TDS amount, "
            "Net payable, Due date.") in llm.reviews[0]  # dates and amounts only: not the vendor, PO or invoice no.
    assert "Step 5 — Enter the bill" in llm.reviews[0] and "Due date = invoice date + the vendor's" in llm.reviews[0]
    assert reviews[0]["trace"] == ["30-09-2026 + 30 days = 30-10-2026 (Friday), 25 day(s) after today (05-10-2026)"]
    assert not any(r["problems"] for r in reviews)  # the maker's values agreed with the checker's derivation

    actions = [e["data"]["action"] for e in events if e["kind"] == "browser.post" and e["data"]["action"]]
    assert actions == ["portal.bill.create", "portal.payment.create"]

    # the policy gate let the payment through on its approval, which is now spent
    decisions = [(e["data"]["action"], e["data"]["effect"], e["data"]["approval"]) for e in events
                 if e["kind"] == "policy"]
    assert decisions == [("portal.bill.create", "allow", ""), ("portal.payment.create", "allow", llm.mem["ref"]),
                         ("file.append", "allow", ""),  # the tracker row
                         ("mail.send", "allow", "")]  # the remittance goes to the contact on Globex's vendor page
    assert state.approvals[0].consumed_by.endswith(f"/payments/{llm.mem['pay']}")

    # independent verification: every action read back, every criterion checked in the systems
    v = next(e["data"] for e in events if e["kind"] == "verify")
    assert v["verdict"] == "verified" and v["counts"] == {"pass": 8, "fail": 0, "unverified": 1}, v
    assert [a["what"].split(" ")[0] for a in v["actions"]] == ["portal.bill.create", "portal.payment.create", "row",
                                                               "email"]
    assert f"portal.bill.create: page /bills/{llm.mem['bill']}" in llm.verify_prompt  # the planner saw the record
    run_dir = log.run_dir(state.run_id)
    assert log.run(state.run_id)["status"] == "verified"
    assert json.loads((run_dir / "facts.json").read_text())["approvals"][0]["status"] == "approved"
    md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "Done, and confirmed by an independent read of the systems." in md and "✓ confirmed" in md
    assert json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))["verification"]["verdict"] == "verified"

    # the model never sees credentials, and old observations shrink to one line
    secrets = json.loads(config.SECRETS_FILE.read_text())
    every_prompt = json.dumps(llm.prompts, default=str) + json.dumps(events, default=str)
    assert secrets["portal"]["password"] not in every_prompt and secrets["mail"]["api_key"] not in every_prompt
    late = llm.prompts[-1]
    assert sum("(older result; details hidden)" in m["content"] for m in late if m["role"] == "tool") >= 10
    assert "[status] Goal: Pay Globex invoice GLX/2026/0917" in late[-1]["content"]


def _step(script, index, *extra):
    """The T1 script with extra moves inserted before step `index` (they don't change what the model reads)."""
    return script[:index] + list(extra) + script[index:]


def _index(script, name: str) -> int:
    """Where in the script the model first calls a tool."""
    for i, move in enumerate(script):
        calls = move({"msg": 1, "bill": "B", "ref": "R", "pay": "P"})
        if any(c.name == name for c in (calls if isinstance(calls, list) else [calls])):
            return i
    raise ValueError(name)


def test_the_policy_gate_holds_inside_a_run(servers, tmp_path):
    """A payment submitted without an approval is stopped at the network layer, whatever led to the click; an email
    with a full account number never leaves. Nova then does it properly and the run still passes."""
    reset("t1_pay_globex")
    approval, mail = _index(T1_SCRIPT, "request_approval"), _index(T1_SCRIPT, "mail_send")
    script = _step(T1_SCRIPT, mail, lambda m: T("mail_send", to=["billing@globex.example"], subject="Payment details",
                                                  body="We paid your account 50100234567812 today."))
    script = _step(script, approval,
                   lambda m: T("browser_open", url=f"/bills/{m['bill']}"),
                   lambda m: T("browser_click", target="Record payment"),
                   lambda m: T("browser_fill", fields={"Amount": "174000", "Payment method": "NEFT",
                                                       "Pay to account": "XXXX7812", "Payment date": "30-10-2026"},
                               submit="Schedule payment"))
    settings = Settings(drive=config.DRIVE, vault=config.SECRETS_FILE, runs_dir=tmp_path / "runs")
    log, llm = EventLog(settings.runs_dir), ScriptedLLM(script)
    state = Runtime(settings, llm, ScriptedHuman(), log).run("Pay the Globex invoice", "Rahul Mehta (AP Manager)")
    events = log.events(state.run_id)
    stopped = [(e["data"]["action"], e["data"]["effect"]) for e in events if e["kind"] == "policy"
               and e["data"]["effect"] != "allow"]
    assert stopped == [("portal.payment.create", "approval"), ("mail.send", "deny")]
    blocked = next(e["data"] for e in events if e["kind"] == "tool.result" and "BLOCKED by policy" in e["data"]["summary"])
    assert "needs CFO approval" in blocked["text"] and "request_approval(action=\"schedule_payment\"" in blocked["text"]
    assert log.run(state.run_id)["status"] == "verified"
    assert check("t1_pay_globex", RunFacts(**state.run_facts())).passed  # exactly one payment, with its approval


def test_a_run_waits_for_the_cfo_and_resumes_after_a_restart(servers, tmp_path):
    """The approval goes to the inbox and the run pauses (the process could exit). The CFO approves later, a fresh
    runtime resumes the run from its saved state, and it finishes as if it had never stopped."""
    from agent.human import Inbox, InboxHuman

    reset("t1_pay_globex")
    settings = Settings(drive=config.DRIVE, vault=config.SECRETS_FILE, runs_dir=tmp_path / "runs")
    log, llm = EventLog(settings.runs_dir), ScriptedLLM(T1_SCRIPT)
    state = Runtime(settings, llm, InboxHuman(Inbox(log.db), prompt=False), log).run(
        "Pay the Globex invoice", "Rahul Mehta (AP Manager)")
    run = log.run(state.run_id)
    assert run["status"] == "waiting" and run["snapshot"] and state.waiting_for == f"APR-{state.run_id[-4:].upper()}-1"
    [item] = Inbox(log.db).pending(state.run_id)
    assert item["kind"] == "approval" and item["who"] == "CFO" and item["payload"]["details"]["amount"] == 174000
    paused = (log.run_dir(state.run_id) / "report.md").read_text(encoding="utf-8")
    assert "Paused: waiting for a person" in paused
    assert f"| {item['id']} | schedule_payment |" in paused and "**waiting for a decision**" in paused

    Inbox(log.db).decide(item["id"], "approved", "Fine.", "Priya Nair (CFO)")  # hours later, from the inbox
    log2 = EventLog(settings.runs_dir)  # a new process
    state = Runtime(settings, llm, InboxHuman(Inbox(log2.db), prompt=False), log2).resume(state.run_id)
    assert log2.run(state.run_id)["status"] == "verified" and not state.waiting_for
    assert state.approvals[0].by == "Priya Nair (CFO)" and state.approvals[0].consumed_by
    kinds = [e["kind"] for e in log2.events(state.run_id)]
    assert kinds.count("approval.request") == 1 and "run.pause" in kinds and "run.resume" in kinds
    assert check("t1_pay_globex", RunFacts(**state.run_facts())).passed


def test_the_verifier_fails_only_what_is_wrong(servers, tmp_path):
    """Probes real runs' planners chose (2026-10-07), against correct T1 work. Loose matching once turned four of them
    into failures, and Nova "fixed" one by reopening the paid bill. Now what the pages don't show is "unverified",
    a value the planner made up (in neither the criterion nor the run record) is set aside, and only a contradiction,
    or an email that was never sent, fails."""
    from agent.tools import MailClient
    from agent.verify import Verifier

    reset("t1_pay_globex")
    settings = Settings(drive=config.DRIVE, vault=config.SECRETS_FILE, runs_dir=tmp_path / "runs")
    log, llm = EventLog(settings.runs_dir), ScriptedLLM(T1_SCRIPT)
    state = Runtime(settings, llm, ScriptedHuman(), log).run("Pay the Globex invoice", "Rahul")
    record = Runtime._record(state, log.events(state.run_id))
    bill, pay, ref = llm.mem["bill"], llm.mem["pay"], llm.mem["ref"]
    probes = [  # (criterion, probe kind, path, expect, the status it must get)
        ("Vendor Globex Corporation India Pvt Ltd (GSTIN 29AACCG4567K1Z2) is active in the vendor master",
         "portal_page", "/vendors/V-1001", [("Name", "Globex Corporation India Pvt Ltd"),
                                            ("GSTIN", "29AACCG4567K1Z2"), ("Status", "Active")], "pass"),
        ("No duplicate bill for GLX/2026/0917", "none", "", [], "unverified"),
        ("Three-way match for PO-2026-0141 confirmed", "portal_page", f"/bills/{bill}",
         [("PO No.", "PO-2026-0141"), ("Billed Quantity", "1 lot"), ("Unit Price", "₹1,50,000")], "pass"),  # guesses
        ("Bill for GLX/2026/0917 entered with status Posted", "portal_page", f"/bills/{bill}",
         [("Status", "Posted"), ("Due Date", "30-10-2026")], "unverified"),  # paid since: the page shows no "Posted"
        ("Payment scheduled with CFO approval", "portal_page", f"/payments/{pay}",
         [("Amount", "₹1,74,000"), ("Method", "NEFT"), ("Due Date", "30-10-2026"), ("CFO Approval Reference", ref)],
         "pass"),
        ("Tracker row added", "sheet_row", "/drive/AP/AP_Tracker.xlsx",
         [("Status", "Payment Scheduled"), ("Vendor Invoice No", "GLX/2026/0917"), ("Net Payable", "174000")], "pass"),
        ("Remittance advice sent to billing@globex.example", "mail_sent", "",
         [("to", "billing@globex.example"), ("subject_contains", "GLX/2026/0917"), ("body_contains", "TDS: ₹3,000")],
         "pass"),  # the probe's own fields, put inside expect
        ("AP tracker has a row for GLX/2026/0917", "sheet_row", "AP/AP_Tracker.xlsx",
         [("Vendor Invoice No", "GLX/2026/0917"), ("Net Payable", '{"amount": 174000.0, "currency": "INR"}'),
          ("Status", "Paid")], "pass"),  # an amount as an object, and a status nobody said: set aside
        # what must still fail, or never be read
        ("Payment PAY-2026-0033 made by RTGS", "portal_page", f"/payments/{pay}", [("Payment method", "RTGS")], "fail"),
        ("Tracker row for GLX/2026/0917 with net payable 177000", "sheet_row", "AP/AP_Tracker.xlsx",
         [("Vendor Invoice No", "GLX/2026/0917"), ("Net Payable", "177000")], "fail"),
        ("Remittance advice sent to cfo@globex.example", "mail_sent", "", [("to", "cfo@globex.example")], "fail"),
        ("Windows settings checked", "sheet_row", "C:/Windows/win.ini", [("x", "y")], "unverified"),
        ("An email went out", "mail_sent", "", [], "unverified"),  # names nothing to look for: must not match any
        ("No duplicate bill for GLX/2026/0917", "portal_page", "/bills/search?invoice_no=GLX/2026/0917",
         [("Bill", "none")], "unverified"),  # an invented page
        ("Remittance advice sent to billing@globex.example", "mail_sent", "",
         [("to", "billing@globex.example"), ("body_contains", "GLX/2026/0917, 30-10-2026, 1,74,000.00")],
         "pass"),  # several values listed in one string (2026-10-08)
        ("Remittance advice sent to billing@globex.example containing invoice number, date, total, and method",
         "mail_sent", "", [("to", "billing@globex.example"),
                           ("body_contains", "invoice number, date, total, TDS, payment ID, and method")],
         "pass"),  # descriptions of what the email holds, not values (2026-10-08)
    ]
    checks = [{"criterion": i, "kind": k, "path": path, "expect": [{"label": a, "value": b} for a, b in expect]}
              for i, (_, k, path, expect, _) in enumerate(probes, 1)]

    class Planner:
        def chat(self, messages, **kw):
            return Reply(json.dumps({"checks": checks}), [])

    vault = json.loads(config.SECRETS_FILE.read_text())
    mail = MailClient(vault["mail"]["url"], vault["mail"]["api_key"], vault["mail"]["mailbox"])
    v = Verifier({"portal": vault["portal"]}, mail, config.DRIVE, tmp_path / "verify", Planner())
    try:
        got = v.criteria([p[0] for p in probes], record)
    finally:
        v.close()
    assert [c["status"] for c in got] == [p[4] for p in probes], [(c["status"], c["detail"]) for c in got]
    assert "set aside, not from the criterion or the record: Billed Quantity=1 lot" in got[2]["detail"]
    assert "Status=Paid" in got[7]["detail"] and "row 34" in got[7]["detail"]
    assert "closest is row 34: Net Payable is 174000" in got[9]["detail"]


def test_finish_counts_again_after_progress(servers, tmp_path):
    """Seen in a real run: finish was refused twice for an open step; once that step was done, the identical finish
    was refused as a "repeat" and the run ended stuck. Progress (a step marked done) restarts the repeat window."""
    reset("t1_pay_globex")
    done = lambda m: T("finish", summary="Paid GLX/2026/0917")  # the same call as the script's own last move
    script = _step(T1_SCRIPT, len(T1_SCRIPT) - 2, done, done)  # twice, while step 8 is still open
    settings = Settings(drive=config.DRIVE, vault=config.SECRETS_FILE, runs_dir=tmp_path / "runs")
    log = EventLog(settings.runs_dir)
    state = Runtime(settings, ScriptedLLM(script), ScriptedHuman(), log).run("Pay the Globex invoice", "Rahul")
    results = [e["data"] for e in log.events(state.run_id) if e["kind"] == "tool.result" and e["data"]["name"] == "finish"]
    assert [r["ok"] for r in results] == [False, False, True], [r["summary"] for r in results]
    assert log.run(state.run_id)["status"] == "verified"


def test_nudges_for_a_stalled_small_model():
    """Two stalls from real runs (2026-10-07): the model wrote "Step 1 is complete" without calling step_done, and it
    reopened a page five times without filling the form waiting there. The runtime names the missing move."""
    from agent.runtime import Loop
    from agent.state import RunState, Step

    st = RunState("r", "x", "y", "05-10-2026", plan=[Step("Identify the vendor"), Step("Check for duplicates")])
    said = Runtime._said_done("Step 1 is complete. Now I need to check for duplicates.", st, [{"name": "browser_open"}])
    assert said.startswith("You wrote that step 1 is complete") and "step_done for step 1" in said
    assert not Runtime._said_done("Step 2 is not done yet.", st, [])
    assert not Runtime._said_done("Step 1 is complete.", st, [{"name": "step_done"}])
    loop = Loop(system="", msgs=[])
    loop.page_url = "http://127.0.0.1:8101/payments/new"
    loop.page_text = ('PAGE "Record payment" — /payments/new\nFORM "Record payment" — submit button: "Continue"\n'
                      '  Bill ID: text = ""  (BILL-2026-0000)\nMENU: Dashboard (/)')
    assert Runtime._form_hint(loop) == (' You are on /payments/new, and its form "Record payment" is waiting to be '
                                        'filled: call browser_fill(fields={"Bill ID": ...}, submit="Continue").')


def test_a_form_filled_again_on_a_page_opened_again_is_a_new_try():
    """Seen 2026-10-09 (heavy chaos): a 503 left the browser on the error page, two fills failed there; the model
    opened the form again, dismissed a pop-up and filled the same values, and that was stopped as a fifth repeat."""
    from types import SimpleNamespace

    from agent.provenance import Evidence
    from agent.result import Result
    from agent.runtime import Loop
    from agent.state import RunState

    rt = Runtime.__new__(Runtime)
    rt.evidence = Evidence()
    st, loop = RunState("r", "x", "y", "05-10-2026", phase="act"), Loop(system="", msgs=[])
    ctx, reg = SimpleNamespace(state=st, log=lambda *a, **k: None), SimpleNamespace(get=lambda name: None)

    def account(name, ok=True, **args):
        rt._account(ctx, reg, loop, ToolCall(name=name, arguments=args), Result("…", "…", ok=ok), True)
        return name + json.dumps(args, sort_keys=True)

    fill = account("browser_fill", ok=False, fields={"Invoice total": "177000"})  # on the 503 page: no form
    assert loop.stamps[fill] == rt._stamp(st, loop, "browser_fill")  # unchanged: trying it again is a repeat
    opened = account("browser_open", url="/bills/new")
    assert loop.stamps[fill] != rt._stamp(st, loop, "browser_fill")  # the form is back: filling it is a new try
    assert loop.stamps[opened] == rt._stamp(st, loop, "browser_open")  # but opening it again and again repeats
    before = rt._stamp(st, loop, "browser_fill")
    account("browser_click", ok=False, target="Nothing")  # a click that changed nothing changes nothing
    assert rt._stamp(st, loop, "browser_fill") == before
    account("browser_click", target="Acknowledge")  # a pop-up dismissed: the page changed
    assert rt._stamp(st, loop, "browser_fill") != before
    # a failed submission stays the open problem until a submission succeeds (wandering elsewhere doesn't settle it)
    assert loop.open_problem == "…"  # from the first fill
    account("browser_open", url="/vendors")
    assert loop.open_problem == "…"
    account("browser_fill", fields={"Invoice total": "177000", "Due date": "30-10-2026"})
    assert loop.open_problem == ""


def test_a_settled_objection_and_an_echo_leave_the_models_view():
    """Seen 2026-10-08: a mistaken checker objection stayed in view after the next request was accepted, and the
    model re-explained it word for word on every call until it stalled."""
    from agent.runtime import Loop

    loop = Loop(system="", msgs=[
        {"role": "assistant", "content": "I see the issue now. The approval system is checking TDS on net payable.",
         "tool_calls": []},
        {"role": "tool", "tool_name": "request_approval", "content": "ERROR: an independent check that worked the "
         "values out again from the handbook disagrees, so nothing was submitted", "summary": "ERROR: ...", "ok": False},
        {"role": "tool", "tool_name": "browser_fill", "content": "ERROR: an independent check that worked the values "
         "out again ...", "summary": "ERROR: ...", "ok": False}])
    Runtime._settle_objections(loop, "request_approval")
    assert loop.msgs[1]["content"].startswith("(settled: an independent check objected here")
    assert "independent check that worked" in loop.msgs[2]["content"]  # another kind of submission: still open
    assert Runtime._echo("I see the issue now.  The approval system is checking TDS on net payable.", loop.msgs)
    assert not Runtime._echo("Now I will fill in the payment form for BILL-2026-0033 with NEFT.", loop.msgs)


def test_a_blocked_call_may_be_retried_once_something_changed(servers, tmp_path):
    """Seen 2026-10-08: a bill fill blocked twice by provenance (the GST wasn't computed yet) was refused as a "repeat"
    right after the model computed it. A repeat is refused only when nothing its outcome depends on has changed."""
    reset("t1_pay_globex")
    fill, calcs = _index(T1_SCRIPT, "browser_fill"), _index(T1_SCRIPT, "calc")
    script = _step(T1_SCRIPT, calcs, T1_SCRIPT[fill], T1_SCRIPT[fill])  # twice, before anything is computed
    settings = Settings(drive=config.DRIVE, vault=config.SECRETS_FILE, runs_dir=tmp_path / "runs")
    log = EventLog(settings.runs_dir)
    state = Runtime(settings, ScriptedLLM(script), ScriptedHuman(), log).run("Pay the Globex invoice", "Rahul")
    events = log.events(state.run_id)
    assert [e["data"]["name"] for e in events if e["kind"] == "provenance.blocked"] == ["browser_fill"] * 2
    assert not any(e["kind"] == "runtime.refused" for e in events)  # the third try comes after the calculations
    assert log.run(state.run_id)["status"] == "verified"


def test_a_stalled_fix_round_ends_with_a_final_check(servers, tmp_path):
    """Seen 2026-10-08: Nova marked "remittance advice sent" without sending it. The verifier found no email, the fix
    round sent it, then the model kept re-reading it until the loop guard stopped the run. The systems decide: one
    more check, and the run is verified."""
    reset("t1_pay_globex")
    mail = _index(T1_SCRIPT, "mail_send")
    reread = lambda m: T("mail_read", id=m["msg"])  # noqa: E731
    script = T1_SCRIPT[:mail] + T1_SCRIPT[mail + 1:] + [T1_SCRIPT[mail]] + [reread] * 5
    settings = Settings(drive=config.DRIVE, vault=config.SECRETS_FILE, runs_dir=tmp_path / "runs")
    log = EventLog(settings.runs_dir)
    state = Runtime(settings, ScriptedLLM(script), ScriptedHuman(), log).run("Pay the Globex invoice", "Rahul")
    events = log.events(state.run_id)
    first, final = [e["data"] for e in events if e["kind"] == "verify"]
    assert first["verdict"] == "failed" and any("no email in Sent" in c["detail"] for c in first["criteria"])
    assert any(e["kind"] == "runtime.stop" and "stuck" in e["data"]["reason"] for e in events)
    assert final["verdict"] == "verified" and log.run(state.run_id)["status"] == "verified"
    assert check("t1_pay_globex", RunFacts(**state.run_facts())).passed


def test_a_run_that_stalls_after_the_work_is_checked_by_the_systems(servers, tmp_path):
    """Seen 2026-10-09: the last email sent, then date_calc of today five times, and a correct run ended "stuck". After
    a stall the systems are read: verified only if every criterion is confirmed."""
    reread = lambda m: T("mail_read", id=m["msg"])  # noqa: E731
    plan = _index(T1_SCRIPT, "submit_plan")

    def checkable(m):  # the contract without its last criterion, the one no probe can check
        call = T1_SCRIPT[plan](m)
        call.arguments["success_criteria"] = call.arguments["success_criteria"][:4]
        return call

    for n, (script, end) in enumerate([(T1_SCRIPT[:-1] + [reread] * 5, "stuck"),
                                       (T1_SCRIPT[:plan] + [checkable] + T1_SCRIPT[plan + 1:-1] + [reread] * 5,
                                        "verified")]):
        reset("t1_pay_globex")
        settings = Settings(drive=config.DRIVE, vault=config.SECRETS_FILE, runs_dir=tmp_path / f"runs{n}")
        log = EventLog(settings.runs_dir)
        state = Runtime(settings, ScriptedLLM(script), ScriptedHuman(), log).run("Pay the Globex invoice", "Rahul")
        events = log.events(state.run_id)
        assert any(e["kind"] == "runtime.stop" and "stuck" in e["data"]["reason"] for e in events)
        [final] = [e["data"] for e in events if e["kind"] == "verify"]
        assert final["verdict"] == "verified"  # no contradiction either time ...
        # ... but after a stall that is not enough: every criterion must be confirmed (one not checkable keeps "stuck")
        assert log.run(state.run_id)["status"] == end, [c["status"] for c in final["criteria"]]


def test_invented_values_are_blanked_out_of_the_plan():
    """Seen 2026-10-08: the contract put "net payable 1,48,000" (it is 1,74,000) into four plan steps, and the model,
    shown that plan on every turn, kept re-calculating until the run stopped."""
    from agent.provenance import Evidence

    known = Evidence()
    known.add("Invoice GLX/2026/0917 | Taxable value 1,50,000.00 | Invoice Total 1,77,000.00")
    steps, masked = Runtime._mask(["Compute TDS at 2% of 1,50,000.00 and net payable as 1,48,000.00",
                                   "Log it in the tracker"], known)
    assert steps == ["Compute TDS at 2% of 1,50,000.00 and net payable as (as worked out)", "Log it in the tracker"]
    assert masked == [{"text": steps[0], "values": ["1,48,000.00"]}]


def test_a_misspelt_recipient_becomes_the_address_seen():
    """Seen 2026-10-09: "anya.iyer@" and "anyanya.iyer@" for ananya.iyer@, the same misspelling resent four times
    after the refusal named the right address. A plain misspelling of one seen address is corrected (and logged);
    an address nobody mentioned and nothing like a seen one is still refused by provenance."""
    from agent.provenance import Evidence, unsupported

    rt = Runtime.__new__(Runtime)
    rt.evidence = Evidence()
    rt.evidence.add("Read message #12 from ananya.iyer@northwind.example; AP Manager: rahul.mehta@northwind.example")
    logged = []
    ctx = type("Ctx", (), {"log": lambda self, kind, **d: logged.append((kind, d))})()
    call = ToolCall("mail_send", {"to": ["anyanya.iyer@northwind.example"], "cc": "rahul.mehta@northwind.example, "
                                  "priya.nair@northwind.example", "subject": "x", "body": "y"})
    rt._fix_recipients(ctx, call)
    assert call.arguments["to"] == ["ananya.iyer@northwind.example"]
    assert call.arguments["cc"] == "rahul.mehta@northwind.example, priya.nair@northwind.example"  # left as given
    assert logged == [("recipient.corrected", {"given": "anyanya.iyer@northwind.example",
                                               "used": "ananya.iyer@northwind.example"})]
    assert unsupported("mail_send", call.arguments, rt.evidence) == ["recipient priya.nair@northwind.example"]


def test_a_step_shows_the_part_of_the_handbook_it_refers_to():
    """Seen 2026-10-09: the refund step says "to Northwind's bank account (see the Company Overview)"; the overview
    was in the prompt, far above, and four refund requests of five went out without the account number."""
    from agent.memory import Handbook

    reset("t4_acme_duplicate")
    rt = Runtime.__new__(Runtime)
    rt._handbook = Handbook(config.DRIVE / "Handbook")
    sec = next(s for s in rt._handbook.sections if s.doc == "04_Duplicate_Payments" and "Recovery" in s.heading)
    [ref] = rt._referenced(sec, "Email the vendor's registered address asking for a refund")
    assert ref.doc == "00_Company_Overview" and ref.heading.startswith("Our bank account") and "50200077889900" in ref.text


def test_a_misspelt_address_in_the_contract_is_put_right():
    """Seen 2026-10-09, twice: the 4B model writes "anya.iyer@…" for ananya.iyer@…. In the criteria it made the
    verifier confirm an email to the wrong person; as a fact it passed the grounding check as a substring."""
    from datetime import date

    from agent.contract import grounded
    from agent.provenance import Evidence

    sources = "Read message #12 from ananya.iyer@northwind.example: \"New vendor: Initech Software Services\""
    known = Evidence()
    known.add(sources)
    criteria, masked = Runtime._mask(["Email sent to anya.iyer@northwind.example with the new vendor ID"], known)
    assert criteria == ["Email sent to ananya.iyer@northwind.example with the new vendor ID"]
    assert masked[0]["values"] == ["anya.iyer@northwind.example → ananya.iyer@northwind.example"]
    people = Evidence()  # the company's directory: the whole handbook, not only the task's documents
    people.add("| Rahul Mehta | AP Manager | rahul.mehta@northwind.example |")
    [cc], _ = Runtime._mask(["An email to ananya.iyer@northwind.example with Rahul Mehta in cc"], known, people)
    assert cc == "An email to ananya.iyer@northwind.example with Rahul Mehta (rahul.mehta@northwind.example) in cc"
    # a role the criterion tells something gets its person's address (r2: the Procurement Lead never told, unseen)
    roles = Runtime._roles("| Rahul Mehta | AP Manager | rahul.mehta@northwind.example | Runs AP; approves refunds |\n"
                           "| Ananya Iyer | Procurement Lead | ananya.iyer@northwind.example | Owns vendors and POs |")
    assert roles["Procurement Lead"] == "ananya.iyer@northwind.example" and "Runs AP; approves refunds" not in roles
    people.add("ananya.iyer@northwind.example")
    told, by = Runtime._mask(["The Procurement Lead informed of the quantity mismatch",
                              "Refund request to Acme, approved by the AP Manager"], known, people, roles)[0]
    assert told == "The Procurement Lead (ananya.iyer@northwind.example) informed of the quantity mismatch"
    assert by == "Refund request to Acme, approved by the AP Manager"  # an approver isn't a recipient
    assert not grounded("anya.iyer@northwind.example", sources, date(2026, 10, 5))  # a fact like it is dropped
    assert grounded("ananya.iyer@northwind.example", sources, date(2026, 10, 5))
    assert known.missing("Contact: anya.iyer@northwind.example") == ["anya.iyer@northwind.example"]


def test_a_value_worked_out_in_the_head_gets_the_calc_that_proves_it():
    """Seen 2026-10-08: the model added CGST and SGST in its head, provenance refused 27000 ten times, and the model
    never called calc. The refusal now names the calc; the model still has to make it."""
    from agent.provenance import Evidence

    rt = Runtime.__new__(Runtime)
    rt.evidence = Evidence()
    rt.evidence.add("Taxable value | 1,50,000.00 | CGST @ 9% | 13,500.00 | SGST @ 9% | 13,500.00 | Total | 1,77,000.00")
    hint = rt._derivation_hint(["GST amount = 27000", "Due date = 10-10-2026"])
    assert hint.startswith(' 27000 = 13500 + 13500? If that is how you worked it out, call '
                           'calc(expression="13500 + 13500") first: its result counts as seen.')
    assert '10-10-2026 is a date nothing has shown' in hint and 'date_calc(date="<that date>", add_days=<days>)' in hint
    assert rt._derivation_hint(["Net payable = 148000"]) == ""  # nothing seen adds up to it: no hint


def test_a_split_request_runs_each_item_and_carries_on_after_a_pause(tmp_path, monkeypatch):
    """"Process the AP inbox" covers several invoices: each becomes its own run, one after another (own contract, plan
    and verification). A waiting item pauses the whole request; when it is done, the rest follow, and the request's
    record gathers every item's approvals and questions for the grader."""
    from agent.state import RunState

    settings = Settings(drive=config.DRIVE, vault=config.SECRETS_FILE, runs_dir=tmp_path / "runs")
    log = EventLog(settings.runs_dir)
    pid = log.new_run("Process the AP inbox", "Rahul", "scripted")
    parent = RunState(pid, "Process the AP inbox", "Rahul", "05-10-2026",
                      work_items=[{"request": r, "run_id": "", "status": "todo"} for r in ("A", "B", "C")])
    log.update_run(pid, status="split", state=parent.to_dict())
    outcomes, made = iter(["verified", "waiting", "verified"]), []

    def item_run(self, request, requester, parent=""):  # stands in for a whole item run
        assert self.s.today == "05-10-2026"  # the request's business date: an item doesn't read it again
        rid = self.log.new_run(request, requester, "scripted")
        st = RunState(rid, request, requester, "05-10-2026", parent=parent)
        self.log.update_run(rid, status=next(outcomes), state=st.to_dict())
        (self.log.run_dir(rid) / "facts.json").write_text(json.dumps(
            {"approvals": [{"ref": f"APR-{request}", "action": "x", "payload": {}, "status": "approved"}],
             "questions": [f"question about {request}"]}))
        made.append(st)
        return st

    monkeypatch.setattr(Runtime, "run", item_run)
    rt = Runtime(settings, ScriptedLLM([]), ScriptedHuman(), log)
    st = rt._run_items(parent)
    assert [i["status"] for i in st.work_items] == ["verified", "waiting", "todo"]  # C waits for B
    assert log.run(pid)["status"] == "waiting"
    log.update_run(made[1].run_id, status="verified")  # B's approval came, B was resumed and finished
    st = rt._continue_parent(made[1])
    assert [i["status"] for i in st.work_items] == ["verified", "verified", "verified"]
    assert log.run(pid)["status"] == "verified"
    facts = json.loads((log.run_dir(pid) / "facts.json").read_text())
    assert [a["ref"] for a in facts["approvals"]] == ["APR-A", "APR-B", "APR-C"] and len(facts["questions"]) == 3
    md = (log.run_dir(pid) / "report.md").read_text(encoding="utf-8")
    assert "## Work items" in md and f"[{made[2].run_id}](../{made[2].run_id}/report.md)" in md


def test_a_contract_that_names_several_documents_splits_the_request(servers, tmp_path, monkeypatch):
    """Seen 2026-10-08: asked to process the inbox, the model wrote one plan for four invoices (43 facts). The
    contract now says first whether the request covers several documents; if so, each becomes its own task."""
    reset("t1_pay_globex")
    items = ["Process the invoice in email #11 (Globex, GLX/2026/0917)", "Process the invoice in email #12"]

    class SplitLLM(ScriptedLLM):
        def chat(self, messages, tools=None, schema=None, **kw):
            if schema and "work_items" in schema["properties"]:
                assert "one self-contained request per document" in messages[-1]["content"]
                return Reply(json.dumps({"work_items": items, "goal": "", "facts": [], "success_criteria": [],
                                         "steps": [], "questions": []}), [])
            return super().chat(messages, tools, schema, **kw)

    split = []
    monkeypatch.setattr(Runtime, "_run_items", lambda self, st: split.append([i["request"] for i in st.work_items]) or st)
    settings = Settings(drive=config.DRIVE, vault=config.SECRETS_FILE, runs_dir=tmp_path / "runs")
    log = EventLog(settings.runs_dir)
    state = Runtime(settings, SplitLLM(T1_SCRIPT[:4]), ScriptedHuman(), log).run("Process the AP inbox", "Rahul")
    assert log.run(state.run_id)["status"] == "split" and not state.plan
    [(first, second)] = split  # described by the emails themselves, not by the model's words
    assert re.fullmatch(r'Process email #\d+ from billing@globex\.example: "Invoice GLX/2026/0917.*"', first), first
    assert second == "Process email #12"  # not read during understanding: its number only
    assert any(e["kind"] == "work.split" and e["data"]["via"] == "contract" for e in log.events(state.run_id))


def test_a_split_item_is_described_by_its_document_not_by_the_model():
    """Seen 2026-10-08: an item described as "ALREADY PAID per tracker row 33" went down the refund procedure, and
    another carried a wrong PO number into its goal. The child's request comes from the email itself."""
    from agent.runtime import Loop

    loop = Loop(msgs=[{"role": "tool", "tool_name": "mail_read", "ok": True, "content": "...",
                       "summary": 'Read message #13 from invoices@hooli.example: "Hooli Cloud invoice HCS-7781" '
                                  '(attachments: 8: Hooli_Invoice_HCS-7781.pdf)'}])
    item = "Process invoice HCS-7781 (Hooli Cloud) from email #13 - ₹35,400 - ALREADY PAID per tracker row 33"
    assert Runtime._item_request(item, loop) == 'Process email #13 from invoices@hooli.example: "Hooli Cloud invoice HCS-7781"'
    assert Runtime._item_request("Process email #14", loop) == "Process email #14"  # not read yet: its number only
    assert Runtime._item_request("Review the Q2 report", loop) == "Review the Q2 report"  # not an email


def test_several_emails_read_lead_to_one_small_decision(tmp_path):
    """Asked inside the big contract prompt, "which documents does this cover?" got four items, then one, then none
    (2026-10-08). With two or more emails read, one small question decides it; the items come from the emails."""
    from agent.runtime import Loop
    from agent.state import RunState
    from agent.tools import Ctx

    summaries = ['Read message #14 from billing@sirius.example: "Invoice SCC-0042"',
                 'Read message #13 from invoices@hooli.example: "Hooli Cloud invoice HCS-7781"',
                 'Read message #11 from ar@vandelay.example: "Invoice VAN-88231"']
    loop = Loop(msgs=[{"role": "tool", "tool_name": "mail_read", "ok": True, "content": "x", "summary": s}
                      for s in summaries])

    class Decider:
        model, num_ctx = "scripted", 16384

        def __init__(self, answer):
            self.answer, self.asked = answer, []

        def chat(self, messages, schema=None, **kw):
            self.asked.append(messages[-1]["content"])
            return Reply(json.dumps({"emails": self.answer}), [])

    settings = Settings(drive=config.DRIVE, vault=config.SECRETS_FILE, runs_dir=tmp_path / "runs")
    log = EventLog(settings.runs_dir)
    st = RunState("r", "Process the AP inbox", "Rahul", "05-10-2026")
    ctx = Ctx(state=st, drive=config.DRIVE, run_dir=tmp_path, today=None, handbook=None, human=None, mail=None,
              sites={}, log=lambda kind, /, **d: 0)
    llm = Decider([14, 13, 11, 99])  # 99 was never read: ignored
    items = Runtime(settings, llm, ScriptedHuman(), log)._decide_split(ctx, loop)
    assert items == ['Process email #14 from billing@sirius.example: "Invoice SCC-0042"',
                     'Process email #13 from invoices@hooli.example: "Hooli Cloud invoice HCS-7781"',
                     'Process email #11 from ar@vandelay.example: "Invoice VAN-88231"']
    assert '- Read message #13 from invoices@hooli.example' in llm.asked[0]
    assert Runtime(settings, Decider([13]), ScriptedHuman(), log)._decide_split(ctx, loop) == []  # one matter
    one = Loop(msgs=loop.msgs[:1])
    assert Runtime(settings, Decider([14, 13]), ScriptedHuman(), log)._decide_split(ctx, one) == []  # one email read


def test_a_remembered_answer_spares_the_person_next_time(tmp_path):
    """MEMORY: a question a person already answered, and asked Nova to remember, is answered by that instruction in a
    later task (asked in other words), after one small call confirms it fits. Anything else still goes to a person."""
    import sqlite3

    from agent.memory import Lessons
    from agent.state import RunState
    from agent.tools import Ctx

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    lessons = Lessons(db)
    lessons.add("answer", "The registration form shows account 50200012345678 but the cancelled cheque shows "
                "50200012345687. How should I proceed?", "Use the cancelled cheque's account; get it approved.",
                "Rahul Mehta (AP Manager)", "run-1", "Set up Initech as a vendor")

    class Matcher:
        model, num_ctx = "scripted", 16384

        def __init__(self, pick):
            self.pick, self.calls = pick, 0

        def chat(self, messages, schema=None, **kw):
            self.calls += 1
            assert "Use the cancelled cheque's account" in messages[-1]["content"]
            return Reply(json.dumps({"answers": self.pick}), [])

    settings = Settings(drive=config.DRIVE, vault=config.SECRETS_FILE, runs_dir=tmp_path / "runs")
    events = []
    ctx = Ctx(state=RunState("run-2", "Set up Initech as a vendor", "Rahul Mehta (AP Manager)", "05-10-2026"),
              drive=config.DRIVE, run_dir=tmp_path, today=None, handbook=None, human=None, mail=None, sites={},
              log=lambda kind, /, **d: events.append((kind, d)) or len(events), lessons=lessons)
    rt = Runtime(settings, Matcher("L-1"), ScriptedHuman(), EventLog(settings.runs_dir))
    res = rt._from_memory(ctx, "The form gives account 50200012345678, the cheque 50200012345687. What should I do "
                               "about this mismatch?")
    assert res.text.startswith("Rahul Mehta (AP Manager) already answered this and asked Nova to remember it (L-1")
    assert ctx.state.questions[0]["by"].startswith("Rahul Mehta (AP Manager), standing instruction L-1")
    assert lessons.all()[0].uses == 1 and [k for k, _ in events][-1] == "lesson.used"
    assert rt._from_memory(ctx, "Which PO should invoice CDC/INV/5521 be matched against?") is None  # no candidate
    doubtful = Runtime(settings, Matcher("none"), ScriptedHuman(), EventLog(settings.runs_dir))
    assert doubtful._from_memory(ctx, "The form's account differs from the cheque, and the cheque is unsigned. What "
                                      "should I do?") is None  # the model says the case differs: ask the person
