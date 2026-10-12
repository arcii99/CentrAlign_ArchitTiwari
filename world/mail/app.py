"""Northwind Mail: the company mail server.

Unlike the finance portal, this system has a proper REST API (`/api/...`, documented at /docs) — the case
where an AI employee should use the API rather than click through a UI. A bare-bones webmail view (`/`) lets
humans watch what the AI employee sends during a demo.
"""

from __future__ import annotations

import base64
import html
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from world import config, db
from world.mail.store import deliver

EMAIL_OK = lambda a: "@" in a and "." in a.split("@")[-1] and " " not in a.strip()  # noqa: E731

app = FastAPI(title="Northwind Mail API", version="1.0",
              description="Mailbox access for Northwind applications. Authenticate with the X-API-Key header.")


def _mailbox(c: sqlite3.Connection, key: str | None) -> str:
    row = c.execute("SELECT mailbox FROM api_keys WHERE key = ?", (key or "",)).fetchone()
    if not row:
        raise HTTPException(401, "Missing or invalid X-API-Key")
    return row["mailbox"]


def _summary(m: sqlite3.Row, n_att: int) -> dict:
    return {"id": m["id"], "folder": m["folder"], "from": m["from_addr"], "to": m["to_addrs"], "cc": m["cc_addrs"],
            "subject": m["subject"], "date": m["sent_at"], "is_read": bool(m["is_read"]),
            "attachment_count": n_att, "snippet": m["body"][:140].replace("\n", " ")}


def _attachments(c: sqlite3.Connection, message_id: str) -> list[sqlite3.Row]:
    return c.execute("SELECT * FROM attachments WHERE message_id = ? ORDER BY id", (message_id,)).fetchall()


def _own(c: sqlite3.Connection, mailbox: str, mid: int) -> sqlite3.Row:
    m = c.execute("SELECT * FROM messages WHERE id = ? AND mailbox = ?", (mid, mailbox)).fetchone()
    if not m:
        raise HTTPException(404, f"Message {mid} not found in {mailbox}")
    return m


@app.get("/api/messages", summary="List messages in a folder (newest first)")
def list_messages(folder: str = "inbox", q: str = "", unread_only: bool = False, limit: int = Query(50, le=200),
                  x_api_key: str | None = Header(None)) -> list[dict]:
    with closing(db.mail_conn()) as c:
        mailbox = _mailbox(c, x_api_key)
        rows = c.execute(
            "SELECT * FROM messages WHERE mailbox = ? AND (? = 'all' OR folder = ?) AND (? = 0 OR is_read = 0) "
            "AND (? = '' OR subject LIKE ? OR body LIKE ? OR from_addr LIKE ? OR to_addrs LIKE ?) "
            "ORDER BY sent_at DESC LIMIT ?",
            (mailbox, folder, folder, int(unread_only), q, f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", limit)).fetchall()
        return [_summary(m, len(_attachments(c, m["message_id"]))) for m in rows]


@app.get("/api/messages/{mid}", summary="Read a message (marks it read)")
def read_message(mid: int, x_api_key: str | None = Header(None)) -> dict:
    with closing(db.mail_conn()) as c:
        mailbox = _mailbox(c, x_api_key)
        m = _own(c, mailbox, mid)
        c.execute("UPDATE messages SET is_read = 1 WHERE id = ?", (mid,))
        atts = [{"id": a["id"], "filename": a["filename"], "content_type": a["content_type"], "size": a["size"]}
                for a in _attachments(c, m["message_id"])]
        return {**_summary(m, len(atts)), "message_id": m["message_id"], "in_reply_to": m["in_reply_to"],
                "body": m["body"], "attachments": atts}


@app.get("/api/messages/{mid}/attachments/{att_id}", summary="Download an attachment")
def download_attachment(mid: int, att_id: int, x_api_key: str | None = Header(None)):
    with closing(db.mail_conn()) as c:
        mailbox = _mailbox(c, x_api_key)
        m = _own(c, mailbox, mid)
        a = c.execute("SELECT * FROM attachments WHERE id = ? AND message_id = ?", (att_id, m["message_id"])).fetchone()
        if not a:
            raise HTTPException(404, "Attachment not found")
        return FileResponse(a["path"], media_type=a["content_type"], filename=a["filename"])


class OutAttachment(BaseModel):
    filename: str
    content_base64: str


class SendRequest(BaseModel):
    to: list[str] = Field(min_length=1)
    cc: list[str] = []
    subject: str = Field(min_length=1)
    body: str
    in_reply_to: int | None = Field(None, description="id of the message being replied to")
    attachments: list[OutAttachment] = []


def _check_addresses(addrs: list[str]) -> None:
    bad = [a for a in addrs if not EMAIL_OK(a)]
    if bad:
        raise HTTPException(422, f"Invalid email address: {', '.join(bad)}")


@app.post("/api/messages", summary="Send an email from your mailbox")
def send_message(req: SendRequest, x_api_key: str | None = Header(None)) -> dict:
    _check_addresses(req.to + req.cc)
    with closing(db.mail_conn()) as c:
        mailbox = _mailbox(c, x_api_key)
        reply_to = None
        if req.in_reply_to is not None:
            reply_to = _own(c, mailbox, req.in_reply_to)["message_id"]
        try:
            atts = [(a.filename, base64.b64decode(a.content_base64, validate=True)) for a in req.attachments]
        except ValueError:
            raise HTTPException(422, "Attachment content_base64 is not valid base64")
        message_id = deliver(c, sender=mailbox, to=req.to, cc=req.cc, subject=req.subject, body=req.body,
                             sent_at=config.now(), attachments=atts, in_reply_to=reply_to)
        sent = c.execute("SELECT id FROM messages WHERE message_id = ? AND mailbox = ?", (message_id, mailbox)).fetchone()
        return {"status": "sent", "id": sent["id"], "message_id": message_id}


class ForwardRequest(BaseModel):
    to: list[str] = Field(min_length=1)
    cc: list[str] = []
    body: str = ""


@app.post("/api/messages/{mid}/forward", summary="Forward a message, including its attachments")
def forward_message(mid: int, req: ForwardRequest, x_api_key: str | None = Header(None)) -> dict:
    _check_addresses(req.to + req.cc)
    with closing(db.mail_conn()) as c:
        mailbox = _mailbox(c, x_api_key)
        m = _own(c, mailbox, mid)
        atts = [(a["filename"], Path(a["path"]).read_bytes()) for a in _attachments(c, m["message_id"])]
        body = (f"{req.body}\n\n---------- Forwarded message ----------\nFrom: {m['from_addr']}\nDate: {m['sent_at']}\n"
                f"Subject: {m['subject']}\nTo: {m['to_addrs']}\n\n{m['body']}")
        message_id = deliver(c, sender=mailbox, to=req.to, cc=req.cc, subject=f"Fwd: {m['subject']}", body=body,
                             sent_at=config.now(), attachments=atts, in_reply_to=m["message_id"])
        sent = c.execute("SELECT id FROM messages WHERE message_id = ? AND mailbox = ?", (message_id, mailbox)).fetchone()
        return {"status": "sent", "id": sent["id"], "message_id": message_id}


class MoveRequest(BaseModel):
    folder: str = Field(pattern="^(inbox|archive)$")


@app.post("/api/messages/{mid}/move", summary="Move a message to inbox or archive")
def move_message(mid: int, req: MoveRequest, x_api_key: str | None = Header(None)) -> dict:
    with closing(db.mail_conn()) as c:
        mailbox = _mailbox(c, x_api_key)
        m = _own(c, mailbox, mid)
        if m["folder"] == "sent":
            raise HTTPException(422, "Sent messages cannot be moved")
        c.execute("UPDATE messages SET folder = ? WHERE id = ?", (req.folder, mid))
        return {"status": "moved", "id": mid, "folder": req.folder}


# --------------------------------------------------------------------------- webmail for humans watching a demo

_CSS = ("body{font:13px/1.5 system-ui,sans-serif;margin:0;background:#f4f5f7;color:#222}header{background:#3b3f8f;"
        "color:#fff;padding:8px 16px}header a{color:#dfe0ff;margin-right:12px}main{padding:12px 16px}table{border-collapse:"
        "collapse;width:100%;background:#fff}td,th{padding:5px 8px;border-bottom:1px solid #e3e4ea;text-align:left}"
        "pre{white-space:pre-wrap;background:#fff;padding:12px;border:1px solid #e3e4ea}.unread{font-weight:600}")


def _page(title: str, body: str, mailbox: str) -> HTMLResponse:
    boxes = "".join(f'<a href="/?mailbox={b}">{b}</a>' for b in (
        config.AP_MAILBOX, "rahul.mehta@northwind.example", "priya.nair@northwind.example",
        "ananya.iyer@northwind.example"))
    return HTMLResponse(f"<!doctype html><title>{html.escape(title)}</title><style>{_CSS}</style>"
                        f"<header><b>Northwind Mail</b> · {html.escape(mailbox)}<br>{boxes}</header><main>{body}</main>")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def webmail(mailbox: str = config.AP_MAILBOX, folder: str = "inbox"):
    with closing(db.mail_conn()) as c:
        rows = c.execute("SELECT * FROM messages WHERE mailbox = ? AND folder = ? ORDER BY sent_at DESC",
                         (mailbox, folder)).fetchall()
        tabs = " · ".join(f'<a href="/?mailbox={mailbox}&folder={f}">{f.title()}</a>' for f in ("inbox", "sent", "archive"))
        trs = "".join(
            f'<tr class="{"" if m["is_read"] else "unread"}"><td>{html.escape(m["from_addr"])}</td>'
            f'<td>{html.escape(m["to_addrs"])}</td><td><a href="/view/{m["id"]}">{html.escape(m["subject"])}</a>'
            f'{" 📎" if _attachments(c, m["message_id"]) else ""}</td><td>{m["sent_at"].replace("T", " ")}</td></tr>'
            for m in rows) or '<tr><td colspan="4">No messages.</td></tr>'
        return _page(f"{folder} – {mailbox}", f"<p>{tabs}</p><table><tr><th>From</th><th>To</th><th>Subject</th>"
                                              f"<th>Date</th></tr>{trs}</table>", mailbox)


@app.get("/view/{mid}", response_class=HTMLResponse, include_in_schema=False)
def webmail_view(mid: int):
    with closing(db.mail_conn()) as c:
        m = c.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
        if not m:
            raise HTTPException(404)
        atts = ", ".join(html.escape(a["filename"]) for a in _attachments(c, m["message_id"])) or "none"
        meta = (f"<p><b>From:</b> {html.escape(m['from_addr'])}<br><b>To:</b> {html.escape(m['to_addrs'])}<br>"
                f"<b>Cc:</b> {html.escape(m['cc_addrs'] or '—')}<br><b>Date:</b> {m['sent_at']}<br>"
                f"<b>Attachments:</b> {atts}</p>")
        return _page(m["subject"], f"<h3>{html.escape(m['subject'])}</h3>{meta}<pre>{html.escape(m['body'])}</pre>",
                     m["mailbox"])
