"""Mail delivery shared by the seeder and the mail API.

One logical email gets one `message_id` and one copy per internal mailbox involved (the sender's "sent"
copy plus each internal recipient's "inbox" copy). Mail to external addresses leaves only the sender's
copy, which is exactly what checkers inspect.
"""

from __future__ import annotations

import mimetypes
import sqlite3
from datetime import datetime
from pathlib import Path

from world.config import COMPANY_DOMAIN, MAIL_FILES


def is_internal(addr: str) -> bool:
    return addr.strip().lower().endswith("@" + COMPANY_DOMAIN)


def _new_message_id(conn: sqlite3.Connection, sender: str) -> str:
    n = conn.execute("SELECT COUNT(DISTINCT message_id) FROM messages").fetchone()[0] + 1
    return f"<m{n:05d}.{sender.split('@')[-1]}>"


def deliver(
    conn: sqlite3.Connection,
    *,
    sender: str,
    to: list[str],
    subject: str,
    body: str,
    sent_at: datetime,
    cc: list[str] | None = None,
    attachments: list[tuple[str, bytes]] | None = None,
    in_reply_to: str | None = None,
    recipient_folder: str = "inbox",
    recipient_read: bool = False,
) -> str:
    """Store one email; returns its message_id. `attachments` is a list of (filename, bytes)."""
    cc = cc or []
    message_id = _new_message_id(conn, sender)
    copies: list[tuple[str, str, int]] = []
    if is_internal(sender):
        copies.append((sender.lower(), "sent", 1))
    for rcpt in dict.fromkeys(a.lower() for a in to + cc):
        if is_internal(rcpt) and rcpt != sender.lower():
            copies.append((rcpt, recipient_folder, int(recipient_read)))
    conn.execute("BEGIN")
    for mailbox, folder, read in copies:
        conn.execute(
            "INSERT INTO messages (mailbox, folder, message_id, from_addr, to_addrs, cc_addrs, subject, body, "
            "sent_at, in_reply_to, is_read) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (mailbox, folder, message_id, sender, ", ".join(to), ", ".join(cc), subject, body,
             sent_at.isoformat(timespec="seconds"), in_reply_to, read),
        )
    for filename, data in attachments or []:
        folder_path = MAIL_FILES / message_id.strip("<>").replace("@", "_")
        folder_path.mkdir(parents=True, exist_ok=True)
        path = folder_path / Path(filename).name
        path.write_bytes(data)
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        conn.execute(
            "INSERT INTO attachments (message_id, filename, content_type, path, size) VALUES (?,?,?,?,?)",
            (message_id, Path(filename).name, ctype, str(path), len(data)),
        )
    conn.execute("COMMIT")
    return message_id
