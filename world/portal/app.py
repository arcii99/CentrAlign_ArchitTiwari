"""Northwind ERP — Finance module. A deliberately 'legacy' web portal: HTML pages and forms only, no API.

The portal is permissive the way old systems are: it accepts duplicate bills and does not check approvals.
It is strict where old systems are strict: DD-MM-YYYY dates, plain numbers, server-side validation errors.
`chaos_middleware` injects the faults real systems have: latency, expired sessions, blocking pop-ups, and
503s that sometimes arrive *after* the change was already committed.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import random
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import date
from itertools import count
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from world import config, db
from world.documents import dmy, inr

HERE = Path(__file__).parent
SESSION_COOKIE = "nwerp_sid"
PAGE_SIZE = 15
STATES = {"07": "Delhi", "06": "Haryana", "09": "Uttar Pradesh", "19": "West Bengal", "24": "Gujarat",
          "27": "Maharashtra", "29": "Karnataka", "32": "Kerala", "33": "Tamil Nadu", "36": "Telangana"}
BILL_STATUSES = {"posted": "Posted", "on_hold": "On Hold", "paid": "Paid", "duplicate": "Duplicate",
                 "cancelled": "Cancelled"}
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAN_RE = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
DATE_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")

app = FastAPI(title="Northwind ERP", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def _context(request: Request) -> dict:
    ids: dict[str, str] = {}

    def fid(name: str) -> str:
        """Element id for a form field. Under chaos it changes on every render, like generated ids in old UIs."""
        if not getattr(request.state, "shuffle", False):
            return name
        return ids.setdefault(name, f"{name}-{secrets.token_hex(3)}")

    return {"fid": fid, "today": config.today(), "statuses": BILL_STATUSES}


templates = Jinja2Templates(directory=HERE / "templates", context_processors=[_context])
templates.env.filters["inr"] = inr
templates.env.filters["dmy"] = lambda iso: dmy(date.fromisoformat(iso[:10])) if iso else ""
templates.env.filters["mask"] = lambda acct: "XXXX" + acct[-4:] if acct else ""
templates.env.globals["state_name"] = STATES.get


# --------------------------------------------------------------------------- chaos

_request_counter = count(1)


def _unavailable() -> HTMLResponse:
    html = templates.get_template("message.html").render(
        title="Service Unavailable", code="ERR_APP_BUSY_503", user=None, fid=lambda n: n, today=config.today(),
        body="The application server is busy and could not complete your request. Please try again in a few "
             "moments.")
    return HTMLResponse(html, status_code=503)


@app.middleware("http")
async def chaos_middleware(request: Request, call_next):
    cfg = config.chaos_config()
    request.state.show_notice = False
    request.state.shuffle = bool(cfg.get("shuffle_ids"))
    path = request.url.path
    if not cfg["enabled"] or path.startswith("/static") or path in ("/login", "/logout", "/notice/ack"):
        return await call_next(request)
    rng = random.Random(f"{cfg['seed']}:{next(_request_counter)}")
    lo, hi = cfg["latency_ms"]
    await asyncio.sleep(rng.uniform(lo, hi) / 1000)
    token = request.cookies.get(SESSION_COOKIE)
    if token and rng.random() < cfg["session_expiry_prob"]:
        with closing(db.portal_conn()) as c:
            c.execute("DELETE FROM sessions WHERE token = ?", (token,))
    if request.method == "POST" and rng.random() < cfg["error_prob"]:
        if rng.random() < cfg["post_commit_error_ratio"]:
            await call_next(request)  # the change IS committed; the user only sees an error page
        return _unavailable()
    if request.method == "GET" and rng.random() < cfg["modal_prob"]:
        request.state.show_notice = True
    return await call_next(request)


# --------------------------------------------------------------------------- auth & helpers


class LoginRequired(Exception):
    def __init__(self, expired: bool):
        self.expired = expired


@app.exception_handler(LoginRequired)
async def _to_login(request: Request, exc: LoginRequired):
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    params = {"next": target}
    if exc.expired:
        params["expired"] = "1"
    resp = RedirectResponse(f"/login?{urlencode(params)}", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    title = "Page not found" if exc.status_code == 404 else "Request error"
    return templates.TemplateResponse(
        request, "message.html", {"user": None, "title": title, "code": f"ERR_HTTP_{exc.status_code}",
                                  "body": "The page you requested does not exist or cannot be opened this way."},
        status_code=exc.status_code)


def _user(request: Request, c: sqlite3.Connection) -> sqlite3.Row:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        row = c.execute(
            "SELECT u.username, u.display_name, u.role, s.notice_acked FROM sessions s "
            "JOIN users u ON u.username = s.username WHERE s.token = ?", (token,)).fetchone()
        if row:
            return row
    raise LoginRequired(expired=bool(token))


def render(request: Request, name: str, user, status_code: int = 200, **ctx) -> HTMLResponse:
    show_notice = bool(request.state.show_notice and user and not user["notice_acked"])
    return templates.TemplateResponse(request, name, {"user": user, "show_notice": show_notice, **ctx},
                                      status_code=status_code)


def not_found(request: Request, user, what: str) -> HTMLResponse:
    return render(request, "message.html", user, status_code=404, title="Not found", code="ERR_NOT_FOUND",
                  body=f"{what} was not found.")


def audit(c: sqlite3.Connection, user: str, action: str, entity: str, detail: dict) -> None:
    c.execute("INSERT INTO audit_log (at, username, action, entity, detail) VALUES (?,?,?,?,?)",
              (config.now().isoformat(), user, action, entity, json.dumps(detail)))


def parse_amount(raw: str | None, label: str, errors: list[str], required: bool = True) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        if required:
            errors.append(f"{label} is required.")
        return None
    if "," in raw:
        errors.append(f"{label}: enter the amount without commas (e.g. 177000.00).")
        return None
    if not AMOUNT_RE.match(raw):
        errors.append(f"{label} must be a number with at most 2 decimals.")
        return None
    return float(raw)


def parse_date(raw: str | None, label: str, errors: list[str], required: bool = True) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        if required:
            errors.append(f"{label} is required.")
        return None
    m = DATE_RE.match(raw)
    if not m:
        errors.append(f"{label} must be in DD-MM-YYYY format.")
        return None
    try:
        return date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        errors.append(f"{label} is not a valid date.")
        return None


def paginate(rows: list, page: int) -> tuple[list, dict]:
    pages = max(1, -(-len(rows) // PAGE_SIZE))
    page = min(max(1, page), pages)
    return rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE], {"page": page, "pages": pages, "total": len(rows)}


def _page(request: Request) -> int:
    try:
        return int(request.query_params.get("page", "1"))
    except ValueError:
        return 1


def _vendors(c) -> list[sqlite3.Row]:
    return c.execute("SELECT * FROM vendors ORDER BY legal_name").fetchall()


def _outstanding(c, bill: sqlite3.Row) -> float:
    paid = c.execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE bill_id = ? AND status IN "
                     "('scheduled', 'sent')", (bill["id"],)).fetchone()[0]
    return round(bill["net_payable"] - paid, 2)


# --------------------------------------------------------------------------- login / dashboard


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", expired: str = ""):
    msg = "Your session has expired. Please sign in again." if expired else None
    return render(request, "login.html", None, next=next, info=msg, error=None)


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    username, password = str(form.get("username", "")).strip(), str(form.get("password", ""))
    nxt = str(form.get("next", "/"))
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    with closing(db.portal_conn()) as c:
        user = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not user or user["password"] != password or user["password"] == "disabled-account":
            return render(request, "login.html", None, next=nxt, info=None, error="Invalid username or password.",
                          status_code=401)
        token = secrets.token_urlsafe(24)
        c.execute("INSERT INTO sessions (token, username, created_at) VALUES (?,?,?)",
                  (token, username, config.now().isoformat()))
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
async def logout(request: Request):
    with closing(db.portal_conn()) as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (request.cookies.get(SESSION_COOKIE, ""),))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.post("/notice/ack")
async def notice_ack(request: Request):
    with closing(db.portal_conn()) as c:
        c.execute("UPDATE sessions SET notice_acked = 1 WHERE token = ?", (request.cookies.get(SESSION_COOKIE, ""),))
    return Response(status_code=204)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        kpi = {
            "awaiting": c.execute("SELECT COUNT(*) FROM bills WHERE status = 'posted'").fetchone()[0],
            "on_hold": c.execute("SELECT COUNT(*) FROM bills WHERE status = 'on_hold'").fetchone()[0],
            "scheduled": c.execute("SELECT COUNT(*) FROM payments WHERE status = 'scheduled'").fetchone()[0],
            "open_pos": c.execute("SELECT COUNT(*) FROM purchase_orders WHERE status = 'open'").fetchone()[0],
        }
        recent = c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 8").fetchall()
        return render(request, "dashboard.html", user, kpi=kpi, recent=recent)


# --------------------------------------------------------------------------- vendors


@app.get("/vendors", response_class=HTMLResponse)
async def vendor_list(request: Request, q: str = "", status: str = ""):
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        rows = c.execute(
            "SELECT * FROM vendors WHERE (? = '' OR id LIKE ? OR legal_name LIKE ? OR gstin LIKE ?) "
            "AND (? = '' OR status = ?) ORDER BY id", (q, f"%{q}%", f"%{q}%", f"%{q}%", status, status)).fetchall()
        rows, pager = paginate(rows, _page(request))
        return render(request, "vendors.html", user, rows=rows, pager=pager, q=q, status=status)


@app.get("/vendors/new", response_class=HTMLResponse)
async def vendor_new(request: Request):
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        return render(request, "vendor_form.html", user, form={"payment_terms_days": "30"}, errors=[], states=STATES)


@app.post("/vendors/new", response_class=HTMLResponse)
async def vendor_create(request: Request):
    form = {k: str(v).strip() for k, v in (await request.form()).items()}
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        errors: list[str] = []
        gstin, pan = form.get("gstin", "").upper(), form.get("pan", "").upper()
        for field, label in [("legal_name", "Legal name"), ("address", "Registered address"),
                             ("email", "Contact email")]:
            if not form.get(field):
                errors.append(f"{label} is required.")
        if not GSTIN_RE.match(gstin):
            errors.append("GSTIN is not in a valid format (e.g. 29ABCDE1234F1Z5).")
        if not PAN_RE.match(pan):
            errors.append("PAN is not in a valid format (e.g. ABCDE1234F).")
        elif GSTIN_RE.match(gstin) and gstin[2:12] != pan:
            errors.append("PAN does not match the PAN embedded in the GSTIN (characters 3–12).")
        if form.get("state_code") not in STATES:
            errors.append("Select the vendor's state.")
        elif GSTIN_RE.match(gstin) and gstin[:2] != form["state_code"]:
            errors.append("The selected state does not match the GSTIN state code.")
        if form.get("category") not in ("goods", "services"):
            errors.append("Select a category.")
        if form.get("email") and not EMAIL_RE.match(form["email"]):
            errors.append("Contact email is not valid.")
        if form.get("payment_terms_days") not in ("15", "30", "45", "60"):
            errors.append("Select payment terms.")
        dup = c.execute("SELECT id FROM vendors WHERE gstin = ?", (gstin,)).fetchone()
        if dup:
            errors.append(f"A vendor with GSTIN {gstin} already exists ({dup['id']}).")
        if errors:
            return render(request, "vendor_form.html", user, form=form, errors=errors, states=STATES,
                          status_code=422)
        n = c.execute("SELECT MAX(CAST(SUBSTR(id, 3) AS INTEGER)) FROM vendors").fetchone()[0] or 1000
        vid = f"V-{n + 1}"
        c.execute(
            "INSERT INTO vendors (id, legal_name, trade_name, gstin, pan, category, email, phone, address, state_code, "
            "payment_terms_days, status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid, form["legal_name"], form.get("trade_name") or None, gstin, pan, form["category"], form["email"],
             form.get("phone") or None, form["address"], form["state_code"], int(form["payment_terms_days"]),
             "active", user["username"], config.now().isoformat()))
        audit(c, user["username"], "vendor.create", vid, {k: form.get(k) for k in ("legal_name", "gstin")})
    return RedirectResponse(f"/vendors/{vid}?flash=created", status_code=303)


@app.get("/vendors/{vid}", response_class=HTMLResponse)
async def vendor_detail(request: Request, vid: str, flash: str = ""):
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        v = c.execute("SELECT * FROM vendors WHERE id = ?", (vid,)).fetchone()
        if not v:
            return not_found(request, user, f"Vendor {vid}")
        return render(
            request, "vendor.html", user, v=v, flash=flash,
            banks=c.execute("SELECT * FROM bank_accounts WHERE vendor_id = ? ORDER BY id", (vid,)).fetchall(),
            pos=c.execute("SELECT * FROM purchase_orders WHERE vendor_id = ? ORDER BY po_date DESC", (vid,)).fetchall(),
            bills=c.execute("SELECT * FROM bills WHERE vendor_id = ? ORDER BY created_at DESC LIMIT 10", (vid,)).fetchall(),
            payments=c.execute("SELECT * FROM payments WHERE vendor_id = ? ORDER BY payment_date DESC LIMIT 10",
                               (vid,)).fetchall())


@app.get("/vendors/{vid}/bank/new", response_class=HTMLResponse)
async def bank_new(request: Request, vid: str):
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        v = c.execute("SELECT * FROM vendors WHERE id = ?", (vid,)).fetchone()
        if not v:
            return not_found(request, user, f"Vendor {vid}")
        return render(request, "bank_form.html", user, v=v, form={"holder_name": v["legal_name"]}, errors=[])


@app.post("/vendors/{vid}/bank/new", response_class=HTMLResponse)
async def bank_create(request: Request, vid: str):
    form = {k: str(v).strip() for k, v in (await request.form()).items()}
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        v = c.execute("SELECT * FROM vendors WHERE id = ?", (vid,)).fetchone()
        if not v:
            return not_found(request, user, f"Vendor {vid}")
        errors: list[str] = []
        acct, ifsc = form.get("account_number", ""), form.get("ifsc", "").upper()
        if not form.get("holder_name"):
            errors.append("Account holder name is required.")
        if not re.fullmatch(r"\d{9,18}", acct):
            errors.append("Account number must be 9–18 digits.")
        elif acct != form.get("confirm_account_number", ""):
            errors.append("Account numbers do not match.")
        if not IFSC_RE.match(ifsc):
            errors.append("IFSC is not valid (e.g. HDFC0001234).")
        if not form.get("bank_name"):
            errors.append("Bank name is required.")
        if errors:
            return render(request, "bank_form.html", user, v=v, form=form, errors=errors, status_code=422)
        cur = c.execute(
            "INSERT INTO bank_accounts (vendor_id, holder_name, account_number, ifsc, bank_name, branch, status, "
            "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (vid, form["holder_name"], acct, ifsc, form["bank_name"], form.get("branch") or None, "active",
             user["username"], config.now().isoformat()))
        audit(c, user["username"], "bank_account.create", vid, {"bank_account_id": cur.lastrowid, "ifsc": ifsc,
                                                               "account_last4": acct[-4:]})
    return RedirectResponse(f"/vendors/{vid}?flash=bank_added", status_code=303)


# --------------------------------------------------------------------------- purchase orders & receipts


def _po_lines(c, po_id: str) -> list[dict]:
    lines = []
    for l in c.execute("SELECT * FROM po_lines WHERE po_id = ? ORDER BY line_no", (po_id,)):
        received = c.execute(
            "SELECT COALESCE(SUM(rl.qty_received), 0) FROM receipt_lines rl JOIN receipts r ON r.id = rl.receipt_id "
            "WHERE r.po_id = ? AND rl.line_no = ?", (po_id, l["line_no"])).fetchone()[0]
        lines.append({**dict(l), "received": received, "value": l["qty"] * l["unit_price"]})
    return lines


@app.get("/purchase-orders", response_class=HTMLResponse)
async def po_list(request: Request, q: str = "", vendor: str = "", status: str = ""):
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        rows = c.execute(
            "SELECT p.*, v.legal_name, (SELECT SUM(qty * unit_price) FROM po_lines WHERE po_id = p.id) AS value "
            "FROM purchase_orders p JOIN vendors v ON v.id = p.vendor_id "
            "WHERE (? = '' OR p.id LIKE ? OR p.description LIKE ?) AND (? = '' OR p.vendor_id = ?) "
            "AND (? = '' OR p.status = ?) ORDER BY p.id DESC",
            (q, f"%{q}%", f"%{q}%", vendor, vendor, status, status)).fetchall()
        rows, pager = paginate(rows, _page(request))
        return render(request, "pos.html", user, rows=rows, pager=pager, q=q, vendor=vendor, status=status,
                      vendors=_vendors(c))


@app.get("/purchase-orders/{po_id}", response_class=HTMLResponse)
async def po_detail(request: Request, po_id: str):
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        po = c.execute("SELECT p.*, v.legal_name FROM purchase_orders p JOIN vendors v ON v.id = p.vendor_id "
                       "WHERE p.id = ?", (po_id,)).fetchone()
        if not po:
            return not_found(request, user, f"Purchase order {po_id}")
        receipts = c.execute(
            "SELECT r.*, rl.line_no, rl.qty_received FROM receipts r JOIN receipt_lines rl ON rl.receipt_id = r.id "
            "WHERE r.po_id = ? ORDER BY r.receipt_date", (po_id,)).fetchall()
        bills = c.execute("SELECT * FROM bills WHERE po_id = ? ORDER BY created_at", (po_id,)).fetchall()
        return render(request, "po.html", user, po=po, lines=_po_lines(c, po_id), receipts=receipts, bills=bills)


@app.get("/receipts", response_class=HTMLResponse)
async def receipt_list(request: Request, q: str = ""):
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        rows = c.execute(
            "SELECT r.*, p.vendor_id, v.legal_name, rl.qty_received FROM receipts r "
            "JOIN purchase_orders p ON p.id = r.po_id JOIN vendors v ON v.id = p.vendor_id "
            "JOIN receipt_lines rl ON rl.receipt_id = r.id "
            "WHERE (? = '' OR r.id LIKE ? OR r.po_id LIKE ? OR v.legal_name LIKE ?) ORDER BY r.receipt_date DESC",
            (q, f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
        rows, pager = paginate(rows, _page(request))
        return render(request, "receipts.html", user, rows=rows, pager=pager, q=q)


# --------------------------------------------------------------------------- bills


def _bill_rows(c, q: str, vendor: str, status: str) -> list[sqlite3.Row]:
    return c.execute(
        "SELECT b.*, v.legal_name FROM bills b JOIN vendors v ON v.id = b.vendor_id "
        "WHERE (? = '' OR b.id LIKE ? OR b.vendor_invoice_no LIKE ?) AND (? = '' OR b.vendor_id = ?) "
        "AND (? = '' OR b.status = ?) ORDER BY b.created_at DESC, b.id DESC",
        (q, f"%{q}%", f"%{q}%", vendor, vendor, status, status)).fetchall()


@app.get("/bills", response_class=HTMLResponse)
async def bill_list(request: Request, q: str = "", vendor: str = "", status: str = ""):
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        rows, pager = paginate(_bill_rows(c, q, vendor, status), _page(request))
        export = "/bills/export.csv?" + urlencode({"q": q, "vendor": vendor, "status": status})
        return render(request, "bills.html", user, rows=rows, pager=pager, q=q, vendor=vendor, status=status,
                      vendors=_vendors(c), export=export)


@app.get("/bills/export.csv")
async def bill_export(request: Request, q: str = "", vendor: str = "", status: str = ""):
    with closing(db.portal_conn()) as c:
        _user(request, c)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Bill ID", "Vendor ID", "Vendor", "Vendor Invoice No", "Invoice Date", "PO", "Taxable Value",
                    "GST", "Total", "TDS", "Net Payable", "Due Date", "Status", "Status Reason"])
        for b in _bill_rows(c, q, vendor, status):
            w.writerow([b["id"], b["vendor_id"], b["legal_name"], b["vendor_invoice_no"], dmy(date.fromisoformat(
                b["invoice_date"])), b["po_id"] or "", b["taxable_value"], b["gst_amount"], b["total"], b["tds_amount"],
                b["net_payable"], dmy(date.fromisoformat(b["due_date"])), BILL_STATUSES[b["status"]],
                b["status_reason"] or ""])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=bills_export.csv"})


@app.get("/bills/new", response_class=HTMLResponse)
async def bill_new(request: Request, vendor: str = "", po: str = ""):
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        return render(request, "bill_form.html", user, form={"vendor_id": vendor, "po_id": po, "tds_amount": "0",
                                                            "status": "posted"},
                      errors=[], vendors=_vendors(c))


@app.post("/bills/new", response_class=HTMLResponse)
async def bill_create(request: Request):
    form = {k: str(v).strip() for k, v in (await request.form()).items()}
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        errors: list[str] = []
        vendor = c.execute("SELECT * FROM vendors WHERE id = ?", (form.get("vendor_id", ""),)).fetchone()
        if not vendor:
            errors.append("Select a vendor.")
        elif vendor["status"] != "active":
            errors.append(f"Vendor {vendor['id']} is not active.")
        if not form.get("vendor_invoice_no"):
            errors.append("Vendor invoice number is required.")
        inv_date = parse_date(form.get("invoice_date"), "Invoice date", errors)
        if inv_date and inv_date > config.today():
            errors.append("Invoice date cannot be in the future.")
        due = parse_date(form.get("due_date"), "Due date", errors)
        if inv_date and due and due < inv_date:
            errors.append("Due date cannot be before the invoice date.")
        po_id = form.get("po_id", "").upper()
        if po_id:
            po = c.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
            if not po:
                errors.append(f"PO {po_id} does not exist.")
            elif vendor and po["vendor_id"] != vendor["id"]:
                errors.append(f"PO {po_id} belongs to a different vendor ({po['vendor_id']}).")
        taxable = parse_amount(form.get("taxable_value"), "Taxable value", errors)
        gst = parse_amount(form.get("gst_amount"), "GST amount", errors)
        total = parse_amount(form.get("total"), "Invoice total", errors)
        tds = parse_amount(form.get("tds_amount"), "TDS amount", errors, required=False) or 0.0
        net = parse_amount(form.get("net_payable"), "Net payable", errors)
        if None not in (taxable, gst, total) and abs(taxable + gst - total) > 1:
            errors.append("Taxable value + GST does not equal the invoice total.")
        if None not in (total, net) and abs(total - tds - net) > 1:
            errors.append("Invoice total − TDS does not equal the net payable.")
        status = form.get("status", "posted")
        if status not in ("posted", "on_hold"):
            errors.append("Status must be Posted or On Hold.")
        if status == "on_hold" and not form.get("status_reason"):
            errors.append("A hold reason is required when the status is On Hold.")
        if errors:
            return render(request, "bill_form.html", user, form=form, errors=errors, vendors=_vendors(c),
                          status_code=422)
        bid = db.next_id(c, "bill", "BILL-2026-{:04d}")
        c.execute(
            "INSERT INTO bills (id, vendor_id, vendor_invoice_no, invoice_date, po_id, taxable_value, gst_amount, "
            "total, tds_amount, net_payable, due_date, status, status_reason, notes, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bid, vendor["id"], form["vendor_invoice_no"], inv_date.isoformat(), po_id or None, taxable, gst, total,
             tds, net, due.isoformat(), status, form.get("status_reason") or None, form.get("notes") or None,
             user["username"], config.now().isoformat()))
        audit(c, user["username"], "bill.create", bid, {"vendor_invoice_no": form["vendor_invoice_no"],
                                                       "total": total, "status": status})
    return RedirectResponse(f"/bills/{bid}?flash=created", status_code=303)


@app.get("/bills/{bid}", response_class=HTMLResponse)
async def bill_detail(request: Request, bid: str, flash: str = ""):
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        b = c.execute("SELECT b.*, v.legal_name FROM bills b JOIN vendors v ON v.id = b.vendor_id WHERE b.id = ?",
                      (bid,)).fetchone()
        if not b:
            return not_found(request, user, f"Bill {bid}")
        payments = c.execute("SELECT * FROM payments WHERE bill_id = ? ORDER BY payment_date", (bid,)).fetchall()
        return render(request, "bill.html", user, b=b, payments=payments, outstanding=_outstanding(c, b),
                      flash=flash, errors=[])


@app.post("/bills/{bid}/status", response_class=HTMLResponse)
async def bill_status(request: Request, bid: str):
    form = {k: str(v).strip() for k, v in (await request.form()).items()}
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        b = c.execute("SELECT b.*, v.legal_name FROM bills b JOIN vendors v ON v.id = b.vendor_id WHERE b.id = ?",
                      (bid,)).fetchone()
        if not b:
            return not_found(request, user, f"Bill {bid}")
        new, reason = form.get("status", ""), form.get("status_reason", "")
        errors = []
        if new not in BILL_STATUSES or new == "paid":
            errors.append("Select a valid status.")
        if new != "posted" and not reason:
            errors.append("A reason is required for this status.")
        if errors:
            payments = c.execute("SELECT * FROM payments WHERE bill_id = ?", (bid,)).fetchall()
            return render(request, "bill.html", user, b=b, payments=payments, outstanding=_outstanding(c, b),
                          flash="", errors=errors, status_code=422)
        c.execute("UPDATE bills SET status = ?, status_reason = ? WHERE id = ?", (new, reason or None, bid))
        audit(c, user["username"], "bill.status", bid, {"from": b["status"], "to": new, "reason": reason})
    return RedirectResponse(f"/bills/{bid}?flash=status", status_code=303)


# --------------------------------------------------------------------------- payments


def _payment_rows(c, vendor: str, status: str, frm: date | None, to: date | None) -> list[sqlite3.Row]:
    return c.execute(
        "SELECT p.*, v.legal_name FROM payments p JOIN vendors v ON v.id = p.vendor_id "
        "WHERE (? = '' OR p.vendor_id = ?) AND (? = '' OR p.status = ?) "
        "AND (? IS NULL OR p.payment_date >= ?) AND (? IS NULL OR p.payment_date <= ?) "
        "ORDER BY p.payment_date DESC, p.id DESC",
        (vendor, vendor, status, status, frm and frm.isoformat(), frm and frm.isoformat(),
         to and to.isoformat(), to and to.isoformat())).fetchall()


@app.get("/payments", response_class=HTMLResponse)
async def payment_list(request: Request, vendor: str = "", status: str = "", date_from: str = "", date_to: str = ""):
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        errors: list[str] = []
        frm = parse_date(date_from, "From date", errors, required=False)
        to = parse_date(date_to, "To date", errors, required=False)
        rows, pager = paginate(_payment_rows(c, vendor, status, frm, to), _page(request))
        export = "/payments/export.csv?" + urlencode({"vendor": vendor, "status": status, "date_from": date_from,
                                                      "date_to": date_to})
        return render(request, "payments.html", user, rows=rows, pager=pager, vendor=vendor, status=status,
                      date_from=date_from, date_to=date_to, vendors=_vendors(c), errors=errors, export=export)


@app.get("/payments/export.csv")
async def payment_export(request: Request, vendor: str = "", status: str = "", date_from: str = "", date_to: str = ""):
    with closing(db.portal_conn()) as c:
        _user(request, c)
        errors: list[str] = []
        frm = parse_date(date_from, "From date", errors, required=False)
        to = parse_date(date_to, "To date", errors, required=False)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Payment ID", "Payment Date", "Vendor ID", "Vendor", "Bill ID", "Amount", "Method", "Status",
                    "UTR", "Approval Ref"])
        for p in _payment_rows(c, vendor, status, frm, to):
            w.writerow([p["id"], dmy(date.fromisoformat(p["payment_date"])), p["vendor_id"], p["legal_name"],
                        p["bill_id"], p["amount"], p["method"], p["status"].title(), p["utr"] or "",
                        p["approval_ref"] or ""])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=payments_export.csv"})


def _payment_form_ctx(c, bill: sqlite3.Row) -> dict:
    banks = c.execute("SELECT * FROM bank_accounts WHERE vendor_id = ? ORDER BY id", (bill["vendor_id"],)).fetchall()
    return {"b": bill, "banks": banks, "outstanding": _outstanding(c, bill)}


@app.get("/payments/new", response_class=HTMLResponse)
async def payment_new(request: Request, bill: str = ""):
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        b = None
        if bill:
            b = c.execute("SELECT b.*, v.legal_name FROM bills b JOIN vendors v ON v.id = b.vendor_id "
                          "WHERE b.id = ?", (bill.strip().upper(),)).fetchone()
            if not b:
                return render(request, "payment_pick.html", user, bill=bill, error=f"Bill {bill} not found.")
        if not b:
            return render(request, "payment_pick.html", user, bill="", error=None)
        return render(request, "payment_form.html", user, form={"amount": f"{_outstanding(c, b):.2f}"}, errors=[],
                      **_payment_form_ctx(c, b))


@app.post("/payments/new", response_class=HTMLResponse)
async def payment_create(request: Request):
    form = {k: str(v).strip() for k, v in (await request.form()).items()}
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        b = c.execute("SELECT b.*, v.legal_name FROM bills b JOIN vendors v ON v.id = b.vendor_id WHERE b.id = ?",
                      (form.get("bill_id", ""),)).fetchone()
        if not b:
            return render(request, "payment_pick.html", user, bill=form.get("bill_id", ""),
                          error="Bill not found.", status_code=422)
        errors: list[str] = []
        if b["status"] != "posted":
            errors.append(f"Bill {b['id']} is {BILL_STATUSES[b['status']]} and cannot be paid.")
        outstanding = _outstanding(c, b)
        amount = parse_amount(form.get("amount"), "Amount", errors)
        if amount is not None and amount <= 0:
            errors.append("Amount must be greater than zero.")
        if amount is not None and amount > outstanding + 0.5:
            errors.append(f"Amount exceeds the outstanding balance of {inr(outstanding)}.")
        method = form.get("method", "")
        if method not in ("NEFT", "RTGS", "IMPS"):
            errors.append("Select a payment method.")
        elif method == "RTGS" and amount is not None and amount < 200000:
            errors.append("RTGS is only available for amounts of 2,00,000.00 and above.")
        elif method == "IMPS" and amount is not None and amount > 500000:
            errors.append("IMPS is limited to 5,00,000.00 per payment.")
        bank = c.execute("SELECT * FROM bank_accounts WHERE id = ? AND vendor_id = ?",
                         (form.get("bank_account_id", "0"), b["vendor_id"])).fetchone()
        if not bank:
            errors.append("Select the vendor bank account to pay.")
        elif bank["status"] != "active":
            errors.append(f"Bank account XXXX{bank['account_number'][-4:]} is inactive. Select an active account.")
        pay_date = parse_date(form.get("payment_date"), "Payment date", errors)
        if pay_date and pay_date < config.today():
            errors.append("Payment date cannot be in the past.")
        if errors:
            return render(request, "payment_form.html", user, form=form, errors=errors, status_code=422,
                          **_payment_form_ctx(c, b))
        pid = db.next_id(c, "payment", "PAY-2026-{:04d}")
        c.execute("BEGIN")
        c.execute(
            "INSERT INTO payments (id, bill_id, vendor_id, amount, method, bank_account_id, payment_date, status, "
            "utr, approval_ref, reference_note, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, b["id"], b["vendor_id"], amount, method, bank["id"], pay_date.isoformat(), "scheduled", None,
             form.get("approval_ref") or None, form.get("reference_note") or None, user["username"],
             config.now().isoformat()))
        if outstanding - amount <= 0.5:
            c.execute("UPDATE bills SET status = 'paid' WHERE id = ?", (b["id"],))
        audit(c, user["username"], "payment.create", pid, {"bill_id": b["id"], "amount": amount, "method": method,
                                                          "approval_ref": form.get("approval_ref") or None})
        c.execute("COMMIT")
    return RedirectResponse(f"/payments/{pid}?flash=created", status_code=303)


@app.get("/payments/{pid}", response_class=HTMLResponse)
async def payment_detail(request: Request, pid: str, flash: str = ""):
    with closing(db.portal_conn()) as c:
        user = _user(request, c)
        p = c.execute("SELECT p.*, v.legal_name FROM payments p JOIN vendors v ON v.id = p.vendor_id "
                      "WHERE p.id = ?", (pid,)).fetchone()
        if not p:
            return not_found(request, user, f"Payment {pid}")
        bank = c.execute("SELECT * FROM bank_accounts WHERE id = ?", (p["bank_account_id"],)).fetchone()
        return render(request, "payment.html", user, p=p, bank=bank, flash=flash)
