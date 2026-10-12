"""SQLite schemas for the two systems of record: the finance portal and the mail server.

Dates are stored ISO (YYYY-MM-DD); the portal displays them Indian-style (DD-MM-YYYY).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from world.config import MAIL_DB, PORTAL_DB

PORTAL_SCHEMA = """
CREATE TABLE users (
    username TEXT PRIMARY KEY,
    password TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL
);
CREATE TABLE sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL REFERENCES users(username),
    created_at TEXT NOT NULL,
    notice_acked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE vendors (
    id TEXT PRIMARY KEY,
    legal_name TEXT NOT NULL,
    trade_name TEXT,
    gstin TEXT NOT NULL UNIQUE,
    pan TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('goods', 'services')),
    email TEXT NOT NULL,
    phone TEXT,
    address TEXT NOT NULL,
    state_code TEXT NOT NULL,
    payment_terms_days INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE bank_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id TEXT NOT NULL REFERENCES vendors(id),
    holder_name TEXT NOT NULL,
    account_number TEXT NOT NULL,
    ifsc TEXT NOT NULL,
    bank_name TEXT NOT NULL,
    branch TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE purchase_orders (
    id TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL REFERENCES vendors(id),
    po_date TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE po_lines (
    po_id TEXT NOT NULL REFERENCES purchase_orders(id),
    line_no INTEGER NOT NULL,
    item TEXT NOT NULL,
    hsn_sac TEXT NOT NULL,
    qty REAL NOT NULL,
    unit TEXT NOT NULL,
    unit_price REAL NOT NULL,
    gst_rate REAL NOT NULL,
    PRIMARY KEY (po_id, line_no)
);
CREATE TABLE receipts (
    id TEXT PRIMARY KEY,
    po_id TEXT NOT NULL REFERENCES purchase_orders(id),
    receipt_date TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('goods', 'service')),
    received_by TEXT NOT NULL,
    notes TEXT
);
CREATE TABLE receipt_lines (
    receipt_id TEXT NOT NULL REFERENCES receipts(id),
    line_no INTEGER NOT NULL,
    qty_received REAL NOT NULL,
    PRIMARY KEY (receipt_id, line_no)
);
CREATE TABLE bills (
    id TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL REFERENCES vendors(id),
    vendor_invoice_no TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    po_id TEXT REFERENCES purchase_orders(id),
    taxable_value REAL NOT NULL,
    gst_amount REAL NOT NULL,
    total REAL NOT NULL,
    tds_amount REAL NOT NULL DEFAULT 0,
    net_payable REAL NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT,
    notes TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE payments (
    id TEXT PRIMARY KEY,
    bill_id TEXT NOT NULL REFERENCES bills(id),
    vendor_id TEXT NOT NULL REFERENCES vendors(id),
    amount REAL NOT NULL,
    method TEXT NOT NULL,
    bank_account_id INTEGER NOT NULL REFERENCES bank_accounts(id),
    payment_date TEXT NOT NULL,
    status TEXT NOT NULL,
    utr TEXT,
    approval_ref TEXT,
    reference_note TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    username TEXT,
    action TEXT NOT NULL,
    entity TEXT,
    detail TEXT
);
CREATE TABLE counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

MAIL_SCHEMA = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox TEXT NOT NULL,          -- whose copy this is
    folder TEXT NOT NULL,           -- inbox | sent | archive
    message_id TEXT NOT NULL,       -- shared by every copy of the same email
    from_addr TEXT NOT NULL,
    to_addrs TEXT NOT NULL,         -- comma-separated
    cc_addrs TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    sent_at TEXT NOT NULL,          -- ISO datetime
    in_reply_to TEXT,
    is_read INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL
);
CREATE TABLE api_keys (
    key TEXT PRIMARY KEY,
    mailbox TEXT NOT NULL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # autocommit; use explicit BEGIN
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def portal_conn() -> sqlite3.Connection:
    return connect(PORTAL_DB)


def mail_conn() -> sqlite3.Connection:
    return connect(MAIL_DB)


def create(path: Path, schema: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    conn.executescript(schema)
    return conn


def next_id(conn: sqlite3.Connection, name: str, fmt: str) -> str:
    """Allocate the next human-readable ID, e.g. next_id(c, 'bill', 'BILL-2026-{:04d}')."""
    row = conn.execute(
        "INSERT INTO counters(name, value) VALUES (?, 1) "
        "ON CONFLICT(name) DO UPDATE SET value = value + 1 RETURNING value",
        (name,),
    ).fetchone()
    return fmt.format(row["value"])
