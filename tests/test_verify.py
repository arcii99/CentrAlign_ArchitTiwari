"""The verifier's comparisons: labels as different screens write them, and only a contradiction fails. The page data
below is what the portal's bill, vendor and payment pages showed after a real T1 run (2026-10-07), where loose
matching produced four false failures and Nova "fixed" one of them by reopening the paid bill."""

from agent.verify import check_value, label_match, record_text


def _page(h1: str, pairs: list[tuple[str, str]]) -> tuple[list, str]:
    snap = {"h1": h1, "title": h1, "tables": [{"kind": "kv", "pairs": [list(p) for p in pairs]}],
            "forms": [{"fields": [{"label": "Status", "options": ["Posted", "On hold", "Cancelled"]}]}]}
    return pairs, record_text(snap)


BILL = _page("Bill BILL-2026-0033 Paid", [
    ("Vendor", "Globex Corporation India Pvt Ltd (V-1001)"), ("PO", "PO-2026-0141"),
    ("Vendor invoice no.", "GLX/2026/0917"), ("Invoice date", "30-09-2026"), ("Taxable value", "1,50,000.00"),
    ("GST", "27,000.00"), ("Invoice total", "1,77,000.00"), ("TDS", "3,000.00"), ("Net payable", "1,74,000.00"),
    ("Due date", "30-10-2026"), ("Outstanding", "0.00"), ("Status reason", "—"), ("Notes", "—")])
VENDOR = _page("Globex Corporation India Pvt Ltd (V-1001)", [
    ("Trade name", "Globex"), ("Status", "Active"), ("GSTIN", "29AACCG4567K1Z2"),
    ("Contact email", "billing@globex.example")])
PAYMENT = _page("Payment PAY-2026-0033", [
    ("Amount", "1,74,000.00"), ("Method", "NEFT"), ("Payment date", "30-10-2026"), ("Status", "Scheduled"),
    ("Approval ref", "APR-BCB7-1")])


def test_labels_as_different_screens_write_them():
    assert label_match("TDS amount", "TDS") == "exact"  # the form's label against the record's
    assert label_match("PO number", "PO") == "exact" and label_match("PO No.", "PO") == "exact"
    assert label_match("Due Date", "Due date") == "exact"
    assert label_match("Payment method", "Method") == "close" and label_match("Name", "Trade name") == "close"
    assert label_match("CFO Approval Reference", "Approval ref") == "close"
    assert label_match("Status", "Status reason") == ""  # the match that got a paid bill reopened
    assert label_match("Due date", "Payment date") == ""


def test_only_a_contradiction_fails():
    # the real run's false failures: none of them is a contradiction any more
    assert check_value("Status", "Posted", *BILL)[0] == "missing"  # the bill's only "Posted" is in a form's options
    assert check_value("Name", "Globex Corporation India Pvt Ltd", *VENDOR)[0] == "ok"  # shown in the heading
    assert check_value("Billed Quantity", "1 lot", *BILL)[0] == "missing"
    # real contradictions still fail
    assert check_value("TDS amount", "3540", *BILL) == ("contradicts", "3,000.00")
    assert check_value("Payment method", "RTGS", *PAYMENT) == ("contradicts", "NEFT (as Method)")
    assert check_value("Status", "Sent", *PAYMENT) == ("contradicts", "Scheduled")
    # and agreement passes, whatever the formatting
    assert check_value("Net payable", "174000", *BILL) == ("ok", "1,74,000.00")
    assert check_value("Due date", "30-Oct-2026", *BILL)[0] == "ok"
    assert check_value("CFO Approval Reference", "APR-BCB7-1", *PAYMENT)[0] == "ok"


def test_the_same_value_said_more_briefly():
    from agent.verify import same

    assert same("30 days from invoice date", "30 days")  # task 3, 2026-10-08: not a contradiction
    assert not same("30 days", "45 days") and not same("Net 30", "Net 45")


def test_an_email_must_reach_whoever_the_criterion_names():
    """Seen 2026-10-09: the vendor ID went to "anya.iyer@…"; the probe's planner copied that address from the sent
    email, the guess was set aside, and with no recipient left to check the email "passed" a criterion naming
    ananya.iyer@…. Now the criterion's own addresses are always checked."""
    from agent.verify import Verifier

    class Mail:
        mailbox = "ap@northwind.example"
        sent = [{"id": 13, "to": "anya.iyer@northwind.example", "cc": "rahul.mehta@northwind.example",
                 "subject": "New vendor: Initech Software Services (V-1008)"}]

        def list(self, folder, q, limit):
            return self.sent

        def read(self, mid):
            return {"body": "Vendor ID: V-1008\n\nNova (AI AP Associate)"}

    v = Verifier.__new__(Verifier)
    v.mail = Mail()
    criterion = ("Email sent from ap@northwind.example to ananya.iyer@northwind.example containing the new vendor ID, "
                 "cc Rahul Mehta")
    probe = {"kind": "mail_sent", "path": "email #13", "expect": [
        {"label": "to", "value": "anya.iyer@northwind.example"}, {"label": "body_contains", "value": "V-1008"}]}
    r = v._probe(probe, (criterion, "Sent message #13 to anya.iyer@northwind.example (V-1008)"))
    assert r["status"] == "fail" and "went to anya.iyer@northwind.example" in r["detail"]
    assert "the criterion names ananya.iyer@northwind.example" in r["detail"]
    Mail.sent[0]["to"] = "ananya.iyer@northwind.example"  # the right person: it passes
    assert v._probe(probe, (criterion, "Sent message #13 to ananya.iyer@northwind.example (V-1008)"))["status"] == "pass"


def test_an_email_is_checked_for_the_values_a_phrase_carries():
    """Seen 2026-10-09: the probe expected "Invoice No VAN-88231" in a remittance that said "Invoice number:
    VAN-88231", failed a correct email, and the model spent its last calls "fixing" it."""
    from agent.verify import Verifier, values_in

    assert values_in("Invoice No VAN-88231") == ["VAN-88231"] and values_in("Total 70,800.00") == ["70,800.00"]
    assert values_in("VAN-88231") == ["VAN-88231"] and values_in("Paid in full") == ["Paid in full"]

    class Mail:
        mailbox = "ap@northwind.example"

        def list(self, folder, q, limit):
            return [{"id": 20, "to": "ar@vandelay.example", "cc": "", "subject": "Remittance advice – VAN-88231"}]

        def read(self, mid):
            return {"body": "Invoice number: VAN-88231\nInvoice total: INR 70,800.00\nPayment ID: PAY-2026-0033"}

    v = Verifier.__new__(Verifier)
    v.mail = Mail()
    criterion = "Remittance advice email sent to ar@vandelay.example containing VAN-88231, 70800 and PAY-2026-0033"
    probe = {"kind": "mail_sent", "path": "email #20", "to": "ar@vandelay.example",
             "body_contains": ["Invoice No VAN-88231", "Total 70800", "Payment ID PAY-2026-0033"]}
    assert v._probe(probe, (criterion, ""))["status"] == "pass"
