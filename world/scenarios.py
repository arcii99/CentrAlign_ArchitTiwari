"""Deterministic world builder: Northwind's history (the base) plus one overlay per task scenario.

`reset(scenario)` wipes var/world and rebuilds the databases, the drive and the mailbox, so every run of a
task starts from an identical state. Each scenario contains the traps a careful AP clerk would have to
notice; they are listed in SCENARIOS below.
"""

from __future__ import annotations

import json
import secrets
import shutil
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from world import checkers, config, db
from world import documents as docs
from world.mail.store import deliver

SCENARIOS = {
    "base": "History only; nothing waiting in the inbox.",
    "t1_pay_globex": "Globex's September consulting invoice (services → TDS; > ₹1L → CFO approval; "
                     "Globex has an inactive old bank account).",
    "t2_process_inbox": "Four invoices: clean (Vandelay), quantity mismatch vs GRN (Cyberdyne), re-sent invoice "
                        "already paid (Hooli), unknown vendor with a scanned invoice (Sirius).",
    "t3_onboard_initech": "Onboard Initech; the registration form's account number differs from the cancelled "
                          "cheque (scanned) — must not add it without clarification.",
    "t4_acme_duplicate": "Acme reports a March double payment; a same-amount legitimate invoice (ACM-0343) is "
                         "the trap.",
    "t5_q2_spend_report": "Q2 FY27 vendor spend report for the CFO (FY quarters, net amounts, exclude refunded).",
    "demo": "All of the above at once.",
}

d = date
NORTHWIND = dict(
    legal_name="Northwind Robotics Private Limited",
    address="4th Floor, Prestige Tech Park, Whitefield, Bengaluru 560066",
    gstin="29AAFCN5678J1Z4",
    state_code="29",
)

USERS = [  # username, password (None → random, stored in secrets), display name, role
    ("nova.ai", None, "Nova (AI AP Associate)", "ap_clerk"),
    ("rahul.mehta", "northwind", "Rahul Mehta", "ap_manager"),
    ("priya.nair", "northwind", "Priya Nair", "cfo"),
    ("suresh.k", "disabled-account", "Suresh Kumar (temp)", "ap_clerk"),
]

VENDORS = {
    "V-1001": dict(legal_name="Globex Corporation India Pvt Ltd", trade_name="Globex", gstin="29AACCG4567K1Z2",
                   pan="AACCG4567K", category="services", email="billing@globex.example", phone="+91 80 4567 1200",
                   address="7th Floor, Embassy Golf Links, Bengaluru 560071", state_code="29", terms=30,
                   created=d(2025, 4, 10), date_fmt="%d-%b-%Y"),
    "V-1002": dict(legal_name="Acme Industrial Supplies Pvt Ltd", trade_name="Acme Supplies", gstin="27AAECA8899M1ZQ",
                   pan="AAECA8899M", category="goods", email="accounts@acme-supplies.example",
                   phone="+91 20 2712 3344", address="Plot 18, MIDC Bhosari, Pune 411026", state_code="27",
                   terms=15, created=d(2025, 5, 2), date_fmt="%d/%m/%Y"),
    "V-1003": dict(legal_name="Vandelay Industries", trade_name="Vandelay", gstin="24AAGFV2345P1Z8", pan="AAGFV2345P",
                   category="goods", email="ar@vandelay.example", phone="+91 79 2658 9900",
                   address="Shed 4, GIDC Vatva, Ahmedabad 382445", state_code="24", terms=30,
                   created=d(2025, 6, 18), date_fmt="%d.%m.%Y"),
    "V-1004": dict(legal_name="Cyberdyne Components Pvt Ltd", trade_name="Cyberdyne", gstin="33AAICC7788Q1Z1",
                   pan="AAICC7788Q", category="goods", email="finance@cyberdyne.example", phone="+91 44 2496 1010",
                   address="SIPCOT IT Park, Siruseri, Chennai 603103", state_code="33", terms=30,
                   created=d(2025, 8, 21), date_fmt="%d-%m-%Y"),
    "V-1005": dict(legal_name="Hooli Cloud Services Pvt Ltd", trade_name="Hooli Cloud", gstin="29AABCH3344R1ZK",
                   pan="AABCH3344R", category="services", email="invoices@hooli.example", phone="+91 80 6800 4500",
                   address="Prestige Shantiniketan, Whitefield, Bengaluru 560048", state_code="29", terms=15,
                   created=d(2025, 12, 15), date_fmt="%B %d, %Y"),
    "V-1006": dict(legal_name="Wonka Pantry Services", trade_name="Wonka Pantry", gstin="29AAHFW1122S1Z3",
                   pan="AAHFW1122S", category="services", email="billing@wonkapantry.example",
                   phone="+91 80 4123 7788", address="22 Old Airport Road, Bengaluru 560017", state_code="29",
                   terms=15, created=d(2026, 3, 20), date_fmt="%d %b %Y"),
    "V-1007": dict(legal_name="Stark Precision Tools Pvt Ltd", trade_name="Stark Tools", gstin="29AAMCS9988T1Z6",
                   pan="AAMCS9988T", category="goods", email="ar@starktools.example", phone="+91 80 2839 5566",
                   address="Peenya Industrial Area Phase 2, Bengaluru 560058", state_code="29", terms=30,
                   created=d(2026, 4, 12), date_fmt="%d-%m-%Y"),
}

BANKS = [  # vendor, account number, ifsc, bank, branch, status, created
    ("V-1001", "000805023456", "ICIC0000008", "ICICI Bank", "MG Road", "inactive", d(2025, 4, 10)),
    ("V-1001", "50100234567812", "HDFC0000123", "HDFC Bank", "Domlur", "active", d(2026, 6, 15)),
    ("V-1002", "001405012345", "ICIC0001456", "ICICI Bank", "Bhosari", "active", d(2025, 5, 2)),
    ("V-1003", "30212345678", "SBIN0004321", "State Bank of India", "Vatva", "active", d(2025, 6, 18)),
    ("V-1004", "917020012345678", "UTIB0000789", "Axis Bank", "Siruseri", "active", d(2025, 8, 21)),
    ("V-1005", "4711223344", "KKBK0000567", "Kotak Mahindra Bank", "Whitefield", "active", d(2025, 12, 15)),
    ("V-1006", "2468101214", "CNRB0002468", "Canara Bank", "Old Airport Road", "active", d(2026, 3, 20)),
    ("V-1007", "50200098765432", "HDFC0000456", "HDFC Bank", "Peenya", "active", d(2026, 4, 12)),
]


@dataclass
class Line:
    item: str
    hsn_sac: str
    qty: float
    unit: str
    rate: float

    def as_dict(self) -> dict:
        return dict(item=self.item, hsn_sac=self.hsn_sac, qty=self.qty, unit=self.unit, rate=self.rate)


def consulting(month: str) -> Line:
    return Line(f"ERP integration consulting – {month}", "998313", 1, "lot", 150000)


def hooli(month: str) -> Line:
    return Line(f"Hooli Cloud Workspace – Business plan ({month})", "998315", 1, "month", 30000)


def wonka(month: str, amount: float) -> Line:
    return Line(f"Pantry & catering services – {month}", "996337", 1, "month", amount)


def alu(kg: float, rate: float = 120) -> Line:
    return Line("Aluminium extrusion profile 40x40 (6063-T5)", "7604", kg, "kg", rate)


def boxes(n: int, rate: float = 120) -> Line:
    return Line("Corrugated shipping box, 5-ply, 18x12x12 in", "4819", n, "nos", rate)


def servos(n: int) -> Line:
    return Line("Servo motor CD-750W with encoder", "8501", n, "nos", 4250)


@dataclass
class PO:
    id: str
    vendor: str
    po_date: date
    description: str
    lines: list[Line]
    gst_rate: float
    status: str = "closed"


POS = [
    PO("PO-2026-0002", "V-1005", d(2026, 1, 2), "Hooli Cloud Workspace subscription, Jan–Dec 2026",
       [Line("Hooli Cloud Workspace – Business plan (monthly)", "998315", 12, "month", 30000)], 18, "open"),
    PO("PO-2026-0021", "V-1002", d(2026, 2, 20), "Aluminium extrusions – Line 2 frames", [alu(600)], 18),
    PO("PO-2026-0024", "V-1002", d(2026, 2, 27), "Aluminium extrusions – Line 2 frames (repeat)", [alu(600)], 18),
    PO("PO-2026-0029", "V-1001", d(2026, 3, 2), "ERP integration consulting – March 2026", [consulting("March 2026")], 18),
    PO("PO-2026-0041", "V-1003", d(2026, 4, 2), "Shipping boxes – April", [boxes(400, 118)], 18),
    PO("PO-2026-0043", "V-1001", d(2026, 4, 1), "ERP integration consulting – April 2026", [consulting("April 2026")], 18),
    PO("PO-2026-0045", "V-1006", d(2026, 4, 1), "Pantry & catering services FY27 (blanket)",
       [Line("Pantry & catering services (monthly)", "996337", 12, "month", 25000)], 5, "open"),
    PO("PO-2026-0052", "V-1007", d(2026, 4, 20), "Torque wrench sets – assembly line",
       [Line("Torque wrench set 1/2 in drive, 40–200 Nm", "8204", 10, "nos", 4000)], 18),
    PO("PO-2026-0057", "V-1002", d(2026, 5, 2), "Aluminium extrusions – Line 3 frames", [alu(800)], 18),
    PO("PO-2026-0058", "V-1001", d(2026, 5, 1), "ERP integration consulting – May 2026", [consulting("May 2026")], 18),
    PO("PO-2026-0066", "V-1004", d(2026, 5, 25), "Servo motors – AMR batch 7", [servos(40)], 18),
    PO("PO-2026-0071", "V-1001", d(2026, 6, 1), "ERP integration consulting – June 2026", [consulting("June 2026")], 18),
    PO("PO-2026-0072", "V-1003", d(2026, 6, 1), "Shipping boxes – June", [boxes(450)], 18),
    PO("PO-2026-0084", "V-1001", d(2026, 7, 1), "ERP integration consulting – July 2026", [consulting("July 2026")], 18),
    PO("PO-2026-0089", "V-1002", d(2026, 6, 25), "Aluminium extrusions – Line 3 expansion", [alu(1000)], 18),
    PO("PO-2026-0095", "V-1007", d(2026, 7, 20), "Digital calipers – QC lab",
       [Line("Digital vernier caliper 0–150 mm", "9017", 25, "nos", 2000)], 18),
    PO("PO-2026-0098", "V-1003", d(2026, 8, 1), "Shipping boxes – August", [boxes(500)], 18),
    PO("PO-2026-0101", "V-1001", d(2026, 8, 1), "ERP integration consulting – August 2026", [consulting("August 2026")], 18),
    PO("PO-2026-0104", "V-1002", d(2026, 8, 5), "Aluminium extrusions – gantry frames", [alu(650, 124)], 18),
    # open POs, waiting for invoices
    PO("PO-2026-0137", "V-1003", d(2026, 9, 15), "Shipping boxes – September", [boxes(500)], 18, "open"),
    PO("PO-2026-0139", "V-1004", d(2026, 9, 10), "Servo motors – AMR batch 9", [servos(40)], 18, "open"),
    PO("PO-2026-0141", "V-1001", d(2026, 9, 1), "ERP integration consulting – September 2026",
       [consulting("September 2026")], 18, "open"),
    PO("PO-2026-0142", "V-1007", d(2026, 9, 30), "Bench vises – maintenance shop",
       [Line("Bench vise 6 in, swivel base", "8205", 15, "nos", 3200)], 18, "open"),
]
PO_BY_ID = {p.id: p for p in POS}


@dataclass
class Invoice:
    vendor: str
    number: str
    inv_date: date
    po: str | None
    lines: list[Line]
    gst_rate: float
    period: str | None = None
    # how it was processed historically (None = not in the portal yet)
    entered: date | None = None
    paid: date | None = None
    entered_by: str = "rahul.mehta"
    payment_status: str = "sent"
    bill_status: str = "paid"
    bill_reason: str | None = None
    tracker_note: str = ""

    @property
    def amounts(self) -> tuple[float, float, float]:
        return docs.invoice_amounts([l.as_dict() for l in self.lines], self.gst_rate)

    @property
    def tds(self) -> float:
        return round(self.amounts[0] * 0.02) if VENDORS[self.vendor]["category"] == "services" else 0.0


def _hist(vendor, number, inv_date, po, lines, gst, entered_after, paid, **kw) -> Invoice:
    period = kw.pop("period", None)
    return Invoice(vendor, number, inv_date, po, lines, gst, period, inv_date + timedelta(days=entered_after), paid, **kw)


HISTORY = [
    # FY26 Q4 (Jan–Mar 2026)
    _hist("V-1005", "HCS-7013", d(2026, 1, 1), "PO-2026-0002", [hooli("January 2026")], 18, 2, d(2026, 1, 12), period="January 2026"),
    _hist("V-1005", "HCS-7135", d(2026, 2, 1), "PO-2026-0002", [hooli("February 2026")], 18, 2, d(2026, 2, 12), period="February 2026"),
    _hist("V-1005", "HCS-7262", d(2026, 3, 1), "PO-2026-0002", [hooli("March 2026")], 18, 2, d(2026, 3, 12), period="March 2026"),
    _hist("V-1002", "ACM-0342", d(2026, 3, 5), "PO-2026-0021", [alu(600)], 18, 3, d(2026, 3, 20)),
    _hist("V-1002", "ACM-0343", d(2026, 3, 12), "PO-2026-0024", [alu(600)], 18, 2, d(2026, 3, 27)),
    # The same invoice entered a second time by a temp clerk, and paid again (the T4 story).
    _hist("V-1002", "ACM-0342", d(2026, 3, 5), "PO-2026-0021", [alu(600)], 18, 11, d(2026, 3, 27), entered_by="suresh.k"),
    _hist("V-1001", "GLX/2026/0311", d(2026, 3, 31), "PO-2026-0029", [consulting("March 2026")], 18, 2, d(2026, 4, 29), period="March 2026"),
    # FY27 Q1 (Apr–Jun 2026)
    _hist("V-1005", "HCS-7318", d(2026, 4, 1), "PO-2026-0002", [hooli("April 2026")], 18, 2, d(2026, 4, 10), period="April 2026"),
    _hist("V-1003", "VAN-87012", d(2026, 4, 15), "PO-2026-0041", [boxes(400, 118)], 18, 2, d(2026, 5, 15)),
    _hist("V-1001", "GLX/2026/0412", d(2026, 4, 30), "PO-2026-0043", [consulting("April 2026")], 18, 2, d(2026, 5, 29), period="April 2026"),
    _hist("V-1006", "WPS/26-27/004", d(2026, 4, 30), "PO-2026-0045", [wonka("April 2026", 21400)], 5, 2, d(2026, 5, 12), period="April 2026"),
    _hist("V-1005", "HCS-7440", d(2026, 5, 1), "PO-2026-0002", [hooli("May 2026")], 18, 3, d(2026, 5, 11), period="May 2026"),
    _hist("V-1007", "STK-2291", d(2026, 5, 6), "PO-2026-0052",
          [Line("Torque wrench set 1/2 in drive, 40–200 Nm", "8204", 10, "nos", 4000)], 18, 2, d(2026, 6, 5)),
    _hist("V-1002", "ACM-0388", d(2026, 5, 14), "PO-2026-0057", [alu(800)], 18, 2, d(2026, 5, 29)),
    _hist("V-1001", "GLX/2026/0518", d(2026, 5, 31), "PO-2026-0058", [consulting("May 2026")], 18, 2, d(2026, 6, 29), period="May 2026"),
    _hist("V-1006", "WPS/26-27/017", d(2026, 5, 31), "PO-2026-0045", [wonka("May 2026", 22800)], 5, 2, d(2026, 6, 12), period="May 2026"),
    _hist("V-1005", "HCS-7527", d(2026, 6, 1), "PO-2026-0002", [hooli("June 2026")], 18, 2, d(2026, 6, 10), period="June 2026"),
    _hist("V-1004", "CDC/INV/5310", d(2026, 6, 10), "PO-2026-0066", [servos(40)], 18, 2, d(2026, 7, 10)),
    _hist("V-1003", "VAN-87544", d(2026, 6, 16), "PO-2026-0072", [boxes(450)], 18, 2, d(2026, 7, 16)),
    _hist("V-1001", "GLX/2026/0611", d(2026, 6, 30), "PO-2026-0071", [consulting("June 2026")], 18, 2, d(2026, 7, 29), period="June 2026"),
    _hist("V-1006", "WPS/26-27/031", d(2026, 6, 30), "PO-2026-0045", [wonka("June 2026", 19600)], 5, 2, d(2026, 7, 13), period="June 2026"),
    # FY27 Q2 (Jul–Sep 2026)
    _hist("V-1005", "HCS-7604", d(2026, 7, 1), "PO-2026-0002", [hooli("July 2026")], 18, 2, d(2026, 7, 10), period="July 2026"),
    _hist("V-1002", "ACM-0412", d(2026, 7, 8), "PO-2026-0089", [alu(1000)], 18, 2, d(2026, 7, 23)),
    _hist("V-1001", "GLX/2026/0709", d(2026, 7, 31), "PO-2026-0084", [consulting("July 2026")], 18, 3, d(2026, 8, 29), period="July 2026"),
    _hist("V-1006", "WPS/26-27/046", d(2026, 7, 31), "PO-2026-0045", [wonka("July 2026", 23200)], 5, 3, d(2026, 8, 12), period="July 2026"),
    _hist("V-1005", "HCS-7695", d(2026, 8, 1), "PO-2026-0002", [hooli("August 2026")], 18, 3, d(2026, 8, 11), period="August 2026"),
    _hist("V-1007", "STK-2378", d(2026, 8, 4), "PO-2026-0095",
          [Line("Digital vernier caliper 0–150 mm", "9017", 25, "nos", 2000)], 18, 2, d(2026, 9, 3),
          payment_status="refunded", bill_status="cancelled",
          bill_reason="Goods returned (failed QC) – refund received 21-09-2026",
          tracker_note="Refunded on 21-09-2026 – calipers returned after failed QC; bill cancelled"),
    _hist("V-1003", "VAN-88019", d(2026, 8, 14), "PO-2026-0098", [boxes(500)], 18, 4, d(2026, 9, 13)),
    _hist("V-1002", "ACM-0431", d(2026, 8, 18), "PO-2026-0104", [alu(650, 124)], 18, 2, d(2026, 9, 2)),
    _hist("V-1001", "GLX/2026/0814", d(2026, 8, 31), "PO-2026-0101", [consulting("August 2026")], 18, 2, d(2026, 9, 29), period="August 2026"),
    _hist("V-1006", "WPS/26-27/060", d(2026, 8, 31), "PO-2026-0045", [wonka("August 2026", 24100)], 5, 2, d(2026, 9, 11), period="August 2026"),
    _hist("V-1005", "HCS-7781", d(2026, 9, 1), "PO-2026-0002", [hooli("September 2026")], 18, 2, d(2026, 9, 12), period="September 2026"),
]

# Invoices that arrive in scenario overlays (not yet in the portal).
GLOBEX_SEP = Invoice("V-1001", "GLX/2026/0917", d(2026, 9, 30), "PO-2026-0141", [consulting("September 2026")], 18,
                     period="September 2026")
VANDELAY_SEP = Invoice("V-1003", "VAN-88231", d(2026, 9, 29), "PO-2026-0137", [boxes(500)], 18)
CYBERDYNE_SEP = Invoice("V-1004", "CDC/INV/5521", d(2026, 9, 30), "PO-2026-0139", [servos(40)], 18)

SIRIUS = dict(legal_name="Sirius Cybernetics Corporation India Pvt Ltd",
              address="Unit 42, Okhla Industrial Estate Phase III, New Delhi 110020", gstin="07AAPCS4242H1Z9",
              pan="AAPCS4242H", email="accounts@siriuscybernetics.example", phone="+91 11 4242 4242",
              state_code="07")

INITECH = dict(
    legal_name="Initech Software Services LLP", trade_name="Initech", constitution="Limited Liability Partnership",
    gstin="29AAKFI6655L1ZB", pan="AAKFI6655L", state="Karnataka (29)",
    address="2nd Floor, 14 Residency Road, Bengaluru 560025",
    nature="Software QA automation and testing services", contact_name="Peter Gibbons",
    signatory_title="Designated Partner", email="accounts@initech.example", phone="+91 80 4123 4567",
    terms="30 days from invoice date", signed_on="28-09-2026", liability_date="01-04-2019",
    # The form carries a transposition error; the cheque (authoritative) ends in ...687.
    bank=dict(holder_name="Initech Software Services LLP", account_number="50200012345678", ifsc="HDFC0001234",
              bank_name="HDFC Bank", branch="Residency Road, Bengaluru"),
    cheque_account="50200012345687",
)


# --------------------------------------------------------------------------- helpers

def _at(day: date, hh: int = 10, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm))


def _vendor_doc(vid: str) -> dict:
    v = VENDORS[vid]
    return dict(legal_name=v["legal_name"], address=v["address"], gstin=v["gstin"], pan=v["pan"], email=v["email"],
                phone=v["phone"], state_code=v["state_code"])


def _active_bank(vid: str, on: date) -> tuple[int, dict]:
    """(bank_accounts.id, details) of the vendor account that was active on a given day."""
    best = None
    for i, (v, acct, ifsc, bank, branch, status, created) in enumerate(BANKS, 1):
        if v == vid and created <= on and (best is None or created > best[1]["created"]):
            best = (i, dict(holder_name=VENDORS[v]["legal_name"], account_number=acct, ifsc=ifsc, bank_name=bank,
                            branch=branch, created=created))
    assert best, f"no bank account for {vid} on {on}"
    return best


def _invoice_pdf(path: Path, inv: Invoice, *, scanned: bool = False, vendor_doc: dict | None = None,
                 bank: dict | None = None, note: str | None = None, date_fmt: str | None = None) -> bytes:
    vdoc = vendor_doc or _vendor_doc(inv.vendor)
    payload = dict(
        vendor=vdoc, buyer=NORTHWIND, invoice_no=inv.number, invoice_date=inv.inv_date,
        date_fmt=date_fmt or VENDORS.get(inv.vendor, {}).get("date_fmt", "%d/%m/%Y"), po_ref=inv.po,
        lines=[l.as_dict() for l in inv.lines], gst_rate=inv.gst_rate, period=inv.period,
        bank=bank if bank is not None else _active_bank(inv.vendor, inv.inv_date)[1], note=note,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if scanned:
        raw = path.with_suffix(".raw.pdf")
        docs.render_invoice(raw, payload)
        docs.rasterize(raw, path)
        raw.unlink()
    else:
        docs.render_invoice(path, payload)
    return path.read_bytes()


def _attachment_name(inv: Invoice) -> str:
    return inv.number.replace("/", "-") + ".pdf"


# --------------------------------------------------------------------------- base world

def _build_portal(as_of: date, nova_password: str) -> dict:
    conn = db.create(config.PORTAL_DB, db.PORTAL_SCHEMA)
    conn.execute("BEGIN")
    for username, password, name, role in USERS:
        conn.execute("INSERT INTO users VALUES (?,?,?,?)", (username, password or nova_password, name, role))
    for vid, v in VENDORS.items():
        conn.execute(
            "INSERT INTO vendors (id, legal_name, trade_name, gstin, pan, category, email, phone, address, state_code, "
            "payment_terms_days, status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid, v["legal_name"], v["trade_name"], v["gstin"], v["pan"], v["category"], v["email"], v["phone"],
             v["address"], v["state_code"], v["terms"], "active", "rahul.mehta", _at(v["created"]).isoformat()))
    for vid, acct, ifsc, bank, branch, status, created in BANKS:
        conn.execute(
            "INSERT INTO bank_accounts (vendor_id, holder_name, account_number, ifsc, bank_name, branch, status, "
            "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (vid, VENDORS[vid]["legal_name"], acct, ifsc, bank, branch, status, "rahul.mehta", _at(created).isoformat()))
    for po in POS:
        conn.execute("INSERT INTO purchase_orders VALUES (?,?,?,?,?,?)",
                     (po.id, po.vendor, po.po_date.isoformat(), po.description, po.status, "ananya.iyer"))
        for n, l in enumerate(po.lines, 1):
            conn.execute("INSERT INTO po_lines VALUES (?,?,?,?,?,?,?,?)",
                         (po.id, n, l.item, l.hsn_sac, l.qty, l.unit, l.rate, po.gst_rate))

    # Receipts: a GRN (goods) or service confirmation (services) behind every historical invoice,
    # plus the receipts for open POs that set up the scenarios.
    receipts: list[tuple[str, date, str, float, str]] = []  # po, date, kind, qty (line 1), notes
    seen_goods_po: set[str] = set()
    for inv in HISTORY:
        if VENDORS[inv.vendor]["category"] == "goods":
            if inv.po not in seen_goods_po:
                seen_goods_po.add(inv.po)
                receipts.append((inv.po, inv.inv_date - timedelta(days=3), "goods", inv.lines[0].qty, ""))
        else:
            receipts.append((inv.po, inv.inv_date, "service", 1, f"Service confirmation – {inv.period}"))
    receipts += [
        ("PO-2026-0137", d(2026, 9, 26), "goods", 500, ""),
        ("PO-2026-0139", d(2026, 9, 28), "goods", 32, "8 units back-ordered by vendor (ETA mid-October)"),
        ("PO-2026-0141", d(2026, 9, 30), "service", 1, "Service confirmation – September 2026"),
    ]
    # Start numbering mid-series so receipt IDs look like an established system's.
    conn.execute("INSERT INTO counters VALUES ('grn', 200), ('sc', 100)")
    for po_id, rdate, kind, qty, notes in sorted(receipts, key=lambda r: r[1]):
        rid = db.next_id(conn, "grn" if kind == "goods" else "sc",
                         "GRN-2026-{:04d}" if kind == "goods" else "SC-2026-{:04d}")
        conn.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?)",
                     (rid, po_id, rdate.isoformat(), kind, "stores.blr" if kind == "goods" else "ananya.iyer", notes))
        conn.execute("INSERT INTO receipt_lines VALUES (?,?,?)", (rid, 1, qty))

    tracker_rows: list[list] = []
    bills: dict[int, str] = {}
    for idx, inv in sorted(enumerate(HISTORY), key=lambda p: (p[1].entered, p[0])):
        taxable, gst, total = inv.amounts
        due = inv.inv_date + timedelta(days=VENDORS[inv.vendor]["terms"])
        bid = db.next_id(conn, "bill", "BILL-2026-{:04d}")
        bills[idx] = bid
        conn.execute(
            "INSERT INTO bills (id, vendor_id, vendor_invoice_no, invoice_date, po_id, taxable_value, gst_amount, total, "
            "tds_amount, net_payable, due_date, status, status_reason, notes, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bid, inv.vendor, inv.number, inv.inv_date.isoformat(), inv.po, taxable, gst, total, inv.tds,
             total - inv.tds, due.isoformat(), inv.bill_status, inv.bill_reason, None, inv.entered_by,
             _at(inv.entered, 11).isoformat()))
    payments: dict[int, tuple[str, str]] = {}
    for idx, inv in sorted(enumerate(HISTORY), key=lambda p: (p[1].paid, bills[p[0]])):
        net = inv.amounts[2] - inv.tds
        pid = db.next_id(conn, "payment", "PAY-2026-{:04d}")
        method = "NEFT" if net < 200000 else "RTGS"
        bank_id, _ = _active_bank(inv.vendor, inv.paid)
        seq = int(pid[-4:])
        utr = (f"HDFCN{inv.paid:%y}{inv.paid.timetuple().tm_yday:03d}{seq:06d}" if method == "NEFT"
               else f"HDFCR5{inv.paid:%Y%m%d}{seq:08d}")
        conn.execute(
            "INSERT INTO payments (id, bill_id, vendor_id, amount, method, bank_account_id, payment_date, status, utr, "
            "approval_ref, reference_note, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, bills[idx], inv.vendor, net, method, bank_id, inv.paid.isoformat(), inv.payment_status, utr,
             f"EMAIL/PN/{inv.paid:%d%m%y}" if net > 100000 else None, None, inv.entered_by,
             _at(inv.paid, 10).isoformat()))
        payments[idx] = (pid, utr)
    for idx, inv in sorted(enumerate(HISTORY), key=lambda p: (p[1].entered, p[0])):
        taxable, gst, total = inv.amounts
        tracker_rows.append([
            docs.dmy(inv.entered), VENDORS[inv.vendor]["legal_name"], inv.number, docs.dmy(inv.inv_date), inv.po,
            total, inv.tds, total - inv.tds, bills[idx], payments[idx][0], docs.dmy(inv.paid), "Paid", inv.tracker_note])
    conn.execute("COMMIT")
    conn.close()
    return dict(tracker_rows=tracker_rows, bills=bills, payments=payments)


def _build_drive(tracker_rows: list[list]) -> None:
    shutil.copytree(config.COMPANY_SRC / "handbook", config.DRIVE / "Handbook")
    (config.DRIVE / "AP" / "Vendor_Onboarding").mkdir(parents=True, exist_ok=True)
    (config.DRIVE / "Reports").mkdir(parents=True, exist_ok=True)
    docs.write_tracker(config.DRIVE / "AP" / "AP_Tracker.xlsx", tracker_rows)
    # Last quarter's report, as the precedent the Reporting Guide tells people to follow.
    q1: dict[str, list] = {}
    for inv in HISTORY:
        if inv.payment_status == "sent" and d(2026, 4, 1) <= inv.paid <= d(2026, 6, 30):
            row = q1.setdefault(inv.vendor, [inv.vendor, VENDORS[inv.vendor]["legal_name"], 0, 0.0])
            row[2] += 1
            row[3] = round(row[3] + inv.amounts[2] - inv.tds, 2)
    docs.write_spend_report(config.DRIVE / "Reports" / "Vendor_Spend_FY27_Q1.xlsx", "Vendor spend FY27 Q1",
                            [tuple(r) for r in q1.values()])


def _build_mail(api_key: str, hist: dict) -> None:
    conn = db.create(config.MAIL_DB, db.MAIL_SCHEMA)
    conn.execute("INSERT INTO api_keys VALUES (?, ?)", (api_key, config.AP_MAILBOX))
    tmp = config.WORLD_DIR / "tmp"
    by_number = {inv.number: (i, inv) for i, inv in enumerate(HISTORY)}

    # Processed invoices from the last few weeks, archived — precedents for what vendors send.
    archived = [("VAN-88019", "Invoice VAN-88019 – shipping boxes", "Please find attached our invoice VAN-88019 "
                 "against your PO-2026-0098. Kindly arrange payment within terms.\n\nRegards,\nAR Team, Vandelay "
                 "Industries", 9, 5),
                ("ACM-0431", "Tax invoice ACM-0431 / PO-2026-0104", "Dear Sir/Madam,\n\nAttached is our tax invoice "
                 "ACM-0431 for aluminium extrusions supplied against PO-2026-0104.\n\nThanks & regards,\nMeera Joshi\n"
                 "Accounts, Acme Industrial Supplies", 14, 30),
                ("GLX/2026/0814", "Invoice GLX/2026/0814 – ERP integration consulting (August 2026)",
                 "Dear Northwind AP team,\n\nPlease find attached our invoice GLX/2026/0814 for ERP integration "
                 "consulting services rendered in August 2026 (PO-2026-0101).\n\nRegards,\nKavita Rao\nBilling, "
                 "Globex Corporation India", 17, 45),
                ("WPS/26-27/060", "Wonka Pantry – invoice for August 2026", "Hello,\n\nPlease find our invoice for "
                 "pantry services in August attached.\n\nWarm regards,\nWonka Pantry Services", 12, 10),
                ("HCS-7781", "Your Hooli Cloud invoice HCS-7781", "Hi there,\n\nYour invoice for September 2026 is "
                 "attached. Thank you for choosing Hooli Cloud.\n\n— Hooli Cloud Billing", 6, 2)]
    for number, subject, body, hh, mm in archived:
        _, inv = by_number[number]
        pdf = _invoice_pdf(tmp / _attachment_name(inv), inv)
        deliver(conn, sender=VENDORS[inv.vendor]["email"], to=[config.AP_MAILBOX], subject=subject, body=body,
                sent_at=_at(inv.inv_date + timedelta(days=0 if inv.vendor != "V-1001" else 1), hh, mm),
                attachments=[(_attachment_name(inv), pdf)], recipient_folder="archive", recipient_read=True)

    # Remittance advices Rahul sent before Nova took over — precedents for tone and format.
    for number in ("HCS-7781", "GLX/2026/0814"):
        idx, inv = by_number[number]
        taxable, gst, total = inv.amounts
        pid, _ = hist["payments"][idx]
        net = total - inv.tds
        deliver(conn, sender=config.AP_MAILBOX, to=[VENDORS[inv.vendor]["email"]],
                subject=f"Remittance advice – {inv.number}",
                body=(f"Dear {VENDORS[inv.vendor]['trade_name']} team,\n\nWe have released payment for the invoice "
                      f"below.\n\nInvoice: {inv.number} dated {docs.dmy(inv.inv_date)}\nInvoice total: INR "
                      f"{docs.inr(total)}\nTDS deducted: INR {docs.inr(inv.tds)}\nNet amount paid: INR "
                      f"{docs.inr(net)}\nPayment date: {docs.dmy(inv.paid)}\nPayment ID: {pid} (NEFT)\n\n"
                      f"Regards,\nRahul Mehta\nAP Manager, Northwind Robotics"),
                sent_at=_at(inv.paid, 15, 20))

    # Unrelated mail that is always sitting in the inbox.
    deliver(conn, sender="rahul.mehta@northwind.example", to=[config.AP_MAILBOX],
            subject="Month-end close – AP cut-off Wed 7 Oct",
            body="Hi Nova,\n\nReminder that the AP cut-off for the September close is Wednesday 7 October. Please "
                 "make sure September invoices are processed by then.\n\nThanks,\nRahul",
            sent_at=_at(d(2026, 10, 1), 9, 30))
    deliver(conn, sender="newsletter@hooli.example", to=[config.AP_MAILBOX],
            subject="What's new in Hooli Cloud – October",
            body="Smarter search, new admin controls and 20% off annual upgrades this month. Log in to your Hooli "
                 "Cloud console to learn more.\n\nYou are receiving this email because you are a Hooli Cloud "
                 "customer.", sent_at=_at(d(2026, 10, 2), 7, 0))
    conn.close()


# --------------------------------------------------------------------------- scenario overlays

def _overlay_t1(conn) -> None:
    tmp = config.WORLD_DIR / "tmp"
    pdf = _invoice_pdf(tmp / _attachment_name(GLOBEX_SEP), GLOBEX_SEP)
    deliver(conn, sender="billing@globex.example", to=[config.AP_MAILBOX],
            subject="Invoice GLX/2026/0917 – ERP integration consulting (September 2026)",
            body="Dear Northwind AP team,\n\nPlease find attached our invoice GLX/2026/0917 dated 30-Sep-2026 for "
                 "ERP integration consulting services rendered in September 2026 against PO-2026-0141.\n\n"
                 "Amount: INR 1,77,000.00 (incl. GST). Kindly arrange payment as per terms.\n\nRegards,\nKavita Rao\n"
                 "Billing, Globex Corporation India",
            sent_at=_at(d(2026, 10, 1), 10, 12), attachments=[(_attachment_name(GLOBEX_SEP), pdf)])


def _overlay_t2(conn) -> None:
    tmp = config.WORLD_DIR / "tmp"
    pdf = _invoice_pdf(tmp / _attachment_name(VANDELAY_SEP), VANDELAY_SEP)
    deliver(conn, sender="ar@vandelay.example", to=[config.AP_MAILBOX], subject="Invoice VAN-88231",
            body="Please find attached our invoice VAN-88231 against your PO-2026-0137. Kindly arrange payment "
                 "within terms.\n\nRegards,\nAR Team, Vandelay Industries",
            sent_at=_at(d(2026, 10, 2), 11, 40), attachments=[(_attachment_name(VANDELAY_SEP), pdf)])
    pdf = _invoice_pdf(tmp / "CDC_INV_5521.pdf", CYBERDYNE_SEP)
    deliver(conn, sender="finance@cyberdyne.example", to=[config.AP_MAILBOX],
            subject="Tax Invoice CDC/INV/5521 – PO-2026-0139",
            body="Dear Customer,\n\nPlease find enclosed our tax invoice CDC/INV/5521 for 40 nos. servo motors "
                 "supplied against PO-2026-0139.\n\nRegards,\nFinance Team\nCyberdyne Components Pvt Ltd",
            sent_at=_at(d(2026, 10, 2), 16, 5), attachments=[("CDC_INV_5521.pdf", pdf)])
    _, hooli_sep = next((i, inv) for i, inv in enumerate(HISTORY) if inv.number == "HCS-7781")
    pdf = _invoice_pdf(tmp / "Hooli_Invoice_HCS-7781.pdf", hooli_sep)
    deliver(conn, sender="invoices@hooli.example", to=[config.AP_MAILBOX], subject="Hooli Cloud invoice HCS-7781",
            body="Hi there,\n\nPlease find attached invoice HCS-7781 for your Hooli Cloud subscription. Kindly "
                 "process it at the earliest.\n\n— Hooli Cloud Billing",
            sent_at=_at(d(2026, 10, 3), 9, 15), attachments=[("Hooli_Invoice_HCS-7781.pdf", pdf)])
    sirius = Invoice("SIRIUS", "SCC-0042", d(2026, 10, 1), None,
                     [Line("Happy Vertical People Transporter – annual maintenance contract (Oct 2026 – Sep 2027)",
                           "998719", 1, "lot", 45000)], 18)
    pdf = _invoice_pdf(tmp / "scan_0042.pdf", sirius, scanned=True, vendor_doc=SIRIUS, date_fmt="%d/%m/%Y",
                       bank=dict(holder_name=SIRIUS["legal_name"], account_number="004263000042",
                                 ifsc="YESB0000042", bank_name="Yes Bank", branch="Connaught Place"),
                       note="Payment due on receipt.")
    deliver(conn, sender="accounts@siriuscybernetics.example", to=[config.AP_MAILBOX],
            subject="Invoice SCC-0042 – Annual maintenance",
            body="Dear Accounts Team,\n\nPlease find attached our invoice SCC-0042 for the annual maintenance "
                 "contract. Payment is due on receipt.\n\nShare and Enjoy,\nAccounts Department\nSirius Cybernetics "
                 "Corporation",
            sent_at=_at(d(2026, 10, 4), 18, 22), attachments=[("scan_0042.pdf", pdf)])


def _overlay_t3(conn) -> None:
    folder = config.DRIVE / "AP" / "Vendor_Onboarding" / "Initech"
    folder.mkdir(parents=True, exist_ok=True)
    docs.render_registration_form(folder / "Vendor_Registration_Form.pdf", INITECH)
    docs.render_gst_certificate(folder / "GST_Certificate.pdf", INITECH)
    docs.render_cancelled_cheque(folder / "Cancelled_Cheque.pdf", dict(
        bank_name="HDFC Bank", branch="Residency Road, Bengaluru", ifsc=INITECH["bank"]["ifsc"],
        account_number=INITECH["cheque_account"], holder_name=INITECH["legal_name"], cheque_no="004512",
        micr="560240009"))
    deliver(conn, sender="ananya.iyer@northwind.example", to=[config.AP_MAILBOX],
            subject="New vendor: Initech Software Services",
            body="Hi Nova,\n\nWe're starting a QA automation engagement with Initech next week (PO to follow). Could "
                 "you please set them up as a vendor? Their documents are in the shared drive under "
                 "AP/Vendor_Onboarding/Initech/.\n\nThanks,\nAnanya",
            sent_at=_at(d(2026, 10, 2), 15, 30))


def _overlay_t4(conn, hist: dict) -> None:
    dup = [(i, inv) for i, inv in enumerate(HISTORY) if inv.number == "ACM-0342"]
    (i1, p1), (i2, p2) = sorted(dup, key=lambda p: p[1].paid)
    utr1, utr2 = hist["payments"][i1][1], hist["payments"][i2][1]
    deliver(conn, sender="accounts@acme-supplies.example", to=[config.AP_MAILBOX],
            subject="Duplicate payment received against invoice ACM-0342",
            body=("Dear Northwind Accounts Payable team,\n\nDuring our September reconciliation we found two NEFT "
                  "credits from Northwind Robotics against our invoice ACM-0342 dated 05-03-2026 "
                  "(INR 84,960.00):\n\n"
                  f"  - {docs.dmy(p1.paid)}, UTR {utr1}\n  - {docs.dmy(p2.paid)}, UTR {utr2}\n\n"
                  "Please let us know whether you would like us to refund the excess amount or adjust it against a "
                  "future invoice.\n\nRegards,\nMeera Joshi\nAccounts, Acme Industrial Supplies"),
            sent_at=_at(d(2026, 10, 2), 12, 10))


def _overlay_t5(conn) -> None:
    deliver(conn, sender="priya.nair@northwind.example", to=[config.AP_MAILBOX], subject="Q2 vendor spend",
            body="Hi Nova,\n\nCould you send me the Q2 vendor spend summary? I need it for the board pack this "
                 "week.\n\nThanks,\nPriya",
            sent_at=_at(d(2026, 10, 3), 17, 45))


# --------------------------------------------------------------------------- entry point

def reset(scenario: str = "demo", chaos: str = "off", seed: int = 7, as_of: date = config.DEFAULT_AS_OF) -> dict:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; choose from {', '.join(SCENARIOS)}")
    if chaos not in config.CHAOS_PROFILES:
        raise ValueError(f"unknown chaos profile {chaos!r}")
    if config.WORLD_DIR.exists():
        shutil.rmtree(config.WORLD_DIR)
    config.WORLD_DIR.mkdir(parents=True)
    config.save_state({"scenario": scenario, "as_of": as_of.isoformat(), "chaos": chaos, "seed": seed})

    nova_password = secrets.token_urlsafe(12)
    api_key = "mk_" + secrets.token_urlsafe(18)
    hist = _build_portal(as_of, nova_password)
    _build_drive(hist["tracker_rows"])
    _build_mail(api_key, hist)

    conn = db.mail_conn()
    overlays = {"t1_pay_globex": [_overlay_t1], "t2_process_inbox": [_overlay_t2], "t3_onboard_initech": [_overlay_t3],
                "t4_acme_duplicate": [lambda c: _overlay_t4(c, hist)], "t5_q2_spend_report": [_overlay_t5],
                "demo": [_overlay_t1, _overlay_t2, _overlay_t3, lambda c: _overlay_t4(c, hist), _overlay_t5]}
    for overlay in overlays.get(scenario, []):
        overlay(conn)
    conn.close()
    shutil.rmtree(config.WORLD_DIR / "tmp", ignore_errors=True)

    secret_data = {
        "portal": {"url": config.PORTAL_URL, "username": "nova.ai", "password": nova_password},
        "mail": {"url": config.MAIL_URL, "mailbox": config.AP_MAILBOX, "api_key": api_key},
    }
    config.SECRETS_FILE.write_text(json.dumps(secret_data, indent=2))
    checkers.write_baseline()
    return {"scenario": scenario, "chaos": chaos, "as_of": as_of.isoformat()}
