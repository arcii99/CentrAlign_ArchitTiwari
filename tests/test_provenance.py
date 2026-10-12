"""The provenance check: values typed into the company's systems must come from something observed in the task."""

from agent.provenance import Evidence, unsupported

INVOICE = """Invoice No. | GLX/2026/0917 | Bill To
Invoice Date | 30-Sep-2026 | Northwind Robotics Private Limited
PO Reference | PO-2026-0141
Taxable Value | 1,50,000.00
CGST @ 9% | 13,500.00
SGST @ 9% | 13,500.00
Invoice Total | 1,77,000.00
A/c No: 50100234567812  |  IFSC: HDFC0000123"""


def _evidence() -> Evidence:
    ev = Evidence()
    for text in (INVOICE, "05-10-2026", "150000 * 0.02 = 3000", "177000 - 3000 = 174000",
                 "30-09-2026 + 30 days = 30-10-2026 (Friday), 25 day(s) after today (05-10-2026)",
                 'Pay to account*: select = (none); options: HDFC Bank XXXX7812 – HDFC0000123'):
        ev.add(text)
    return ev


def test_values_seen_or_computed_pass():
    ev = _evidence()
    fields = {"Vendor invoice no.": "GLX/2026/0917", "Invoice date": "30-09-2026", "PO number": "PO-2026-0141",
              "Taxable value": 150000, "Invoice total": "177000.00", "TDS amount": 3000, "Net payable": "1,74,000",
              "Due date": "30-10-2026", "Pay to account": "HDFC Bank XXXX7812 – HDFC0000123", "Payment method": "NEFT"}
    assert unsupported("browser_fill", {"fields": fields}, ev) == []
    body = ("Invoice GLX/2026/0917 dated 30-Sep-2026. Invoice total: INR 1,77,000.00; TDS deducted: INR 3,000.00 "
            "(2% of the taxable value); net amount paid: INR 1,74,000.00 on 30-10-2026, NEFT.")
    assert unsupported("mail_send", {"subject": "Remittance advice – GLX/2026/0917", "body": body}, ev) == []


def test_values_from_nowhere_are_caught():
    ev = _evidence()
    bad = unsupported("browser_fill", {"fields": {"Due date": "10-10-2026", "GST amount": 27000,
                                                  "PO number": "PO-2026-0101", "Vendor": "Globex"}}, ev)
    assert bad == ["Due date = 10-10-2026", "GST amount = 27000", "PO number = PO-2026-0101"]
    ev.add("13500 + 13500 = 27000")  # once it is calculated, it may be used
    assert unsupported("browser_fill", {"fields": {"GST amount": "27,000.00"}}, ev) == []
    assert unsupported("request_approval", {"details": {"bill_id": "BILL-2026-XXXX", "amount": 174000}}, ev) == \
        ["BILL-2026-XXXX"]
    assert unsupported("mail_send", {"subject": "x", "body": "Paid to A/c 50100234567899 on 31-10-2026"}, ev) == \
        ["31-10-2026", "50100234567899"]
    assert unsupported("calc", {"expression": "999 * 2"}, ev) == []  # reading and computing are never blocked
    assert unsupported("sheet_write", {"rows": [["Globex", "1,77,000.00"], ["TOTAL", 999999]]}, ev) == ["999999"]


def test_recipients_must_have_been_seen():
    """Seen 2026-10-09: the new vendor ID went to "anya.iyer@…"; the requester was ananya.iyer@…"""
    ev = _evidence()
    ev.add("Read message #12 from ananya.iyer@northwind.example: \"New vendor: Initech Software Services\"")
    ev.add("Approvals: bank accounts need the AP Manager (Rahul Mehta, rahul.mehta@northwind.example).")
    good = {"to": ["Ananya.Iyer@northwind.example"], "cc": "rahul.mehta@northwind.example", "subject": "x", "body": "y"}
    assert unsupported("mail_send", good, ev) == []
    typo = {"to": ["anya.iyer@northwind.example"], "cc": ["priya.nair@northwind.example"], "subject": "x", "body": "y"}
    assert unsupported("mail_send", typo, ev) == [
        "recipient anya.iyer@northwind.example (did you mean ananya.iyer@northwind.example?)",
        "recipient priya.nair@northwind.example"]
    assert unsupported("mail_forward", {"to": "rahul.mehta@northwind.example", "body": ""}, ev) == []
