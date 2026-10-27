"""Messages between roles: sending, reading by sequence cursor, bounds and retention.

Each recipient's messages carry a gap-free `seq`. A reader passes the last
`seq` it has seen and gets what follows; the host also remembers the last seq
it delivered (`read_seq`), so a reader that passes nothing gets only unread
messages. A message may name a job it is about.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from baton import events
from baton.errors import InvalidRequest, MailboxFull, RequestTooOld
from baton.store import transaction

# Text is measured as it travels: JSON with every non-ASCII character escaped.
MAX_MESSAGE_BYTES = 64 * 1024
# A page's estimated wire size. A tool result carries its data twice (the
# structured result and a text copy), so the estimate doubles the JSON size.
MAX_PAGE_BYTES = 512 * 1024
MAX_PAGE_MESSAGES = 100
# Past this many unread messages a sender is refused until the recipient reads.
MAX_UNREAD = 500
READ_RETENTION = timedelta(days=30)

# A request_id starts with its UTC creation time, so the host can refuse an old
# one and therefore forget retry identities after a fixed time without ever
# admitting a stale retry twice.
REQUEST_ID = re.compile(r"(\d{8}T\d{6}Z)-[A-Za-z0-9._:-]{1,100}")
REQUEST_MAX_AGE = timedelta(days=30)
REQUEST_MAX_SKEW = timedelta(minutes=5)
IDENTITY_RETENTION = REQUEST_MAX_AGE + timedelta(days=1)


def new_request_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"


def send(conn: sqlite3.Connection, *, sender: str, recipient: str, text: str, request_id: str | None = None,
         job_id: str | None = None) -> dict[str, Any]:
    """Store a message as the recipient's next seq. Retrying with the same request_id is safe."""
    request_id = request_id or new_request_id()
    created_at = request_time(request_id)
    if wire_bytes(text) > MAX_MESSAGE_BYTES:
        raise InvalidRequest(f"a message is at most {MAX_MESSAGE_BYTES} bytes once JSON-escaped")
    digest = events.request_digest("send", {"sender": sender, "recipient": recipient, "text": text,
                                            "job_id": job_id})
    with transaction(conn):
        existing = events.replayed(conn, "message_requests", request_id, digest)
        if existing is not None:
            return {"message_id": existing["message_id"], "seq": existing["seq"], "request_id": request_id,
                    "created": False}
        if job_id is not None:
            from baton import jobs  # lazily: jobs imports this module

            jobs.require_exists(conn, job_id)
        last_seq, read_seq = _mailbox(conn, recipient)
        if last_seq - read_seq >= MAX_UNREAD:
            raise MailboxFull(f"{recipient} has {MAX_UNREAD} unread messages; retry after it reads")
        seq = last_seq + 1
        message_id = f"msg_{uuid.uuid4().hex[:16]}"
        conn.execute("INSERT INTO mailboxes (recipient, last_seq, read_seq) VALUES (?, ?, 0)"
                     " ON CONFLICT (recipient) DO UPDATE SET last_seq = excluded.last_seq", (recipient, seq))
        conn.execute("INSERT INTO messages (id, job_id, sender, recipient, seq, text, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (message_id, job_id, sender, recipient, seq, text, events.now()))
        conn.execute("INSERT INTO message_requests (request_id, request_digest, message_id, seq, created_at)"
                     " VALUES (?, ?, ?, ?, ?)", (request_id, digest, message_id, seq, created_at.isoformat()))
        # The event carries no text, so retention deleting the message leaves no copy behind.
        # A job-less event uses the message's seq as its own.
        events.append(conn, job_id=job_id, seq=None if job_id else seq, actor=sender, audience=recipient,
                      kind="message", summary=f"message {seq} from {sender}",
                      data={"message_id": message_id, "seq": seq})
        _forget_old(conn, recipient, read_seq)
    return {"message_id": message_id, "seq": seq, "request_id": request_id, "created": True}


def read(conn: sqlite3.Connection, recipient: str, after_seq: int | None = None, limit: int = 50) -> dict[str, Any]:
    """The recipient's messages after `after_seq` (default: after the last one delivered), oldest first.

    `next_seq` is where the next page starts, so a short page is not the end.
    `truncated` is true when retention already deleted messages after `after_seq`.
    """
    if not 1 <= limit <= MAX_PAGE_MESSAGES or (after_seq is not None and after_seq < 0):
        raise InvalidRequest(f"limit must be 1-{MAX_PAGE_MESSAGES} and after_seq at least 0")
    with transaction(conn):
        last_seq, read_seq = _mailbox(conn, recipient)
        _forget_old(conn, recipient, read_seq)
        start = read_seq if after_seq is None else after_seq
        rows = conn.execute("SELECT * FROM messages WHERE recipient = ? AND seq > ? ORDER BY seq LIMIT ?",
                            (recipient, start, limit)).fetchall()
        oldest = conn.execute("SELECT MIN(seq) FROM messages WHERE recipient = ?", (recipient,)).fetchone()[0]
        page = _page(rows)
        if page:
            mark_read(conn, recipient, page[-1]["seq"])
        unread = last_seq - _mailbox(conn, recipient)[1]
    truncated = start + 1 < (oldest if oldest is not None else last_seq + 1)
    return {"messages": page, "next_seq": page[-1]["seq"] if page else start, "unread": unread,
            "truncated": truncated}


def first_unread_seq(conn: sqlite3.Connection, recipient: str) -> int | None:
    last_seq, read_seq = _mailbox(conn, recipient)
    return read_seq + 1 if read_seq < last_seq else None


def read_seq(conn: sqlite3.Connection, recipient: str) -> int:
    return _mailbox(conn, recipient)[1]


def mark_read(conn: sqlite3.Connection, recipient: str, seq: int) -> None:
    """Advance the delivered position; a pending wake for messages now read would only replay them."""
    conn.execute("UPDATE mailboxes SET read_seq = MAX(read_seq, ?) WHERE recipient = ?", (seq, recipient))
    conn.execute("UPDATE wakes SET status = 'superseded', finished_at = ? WHERE role = ? AND status = 'pending'"
                 " AND key LIKE 'messages:%' AND CAST(substr(key, 10) AS INTEGER) <= ?",
                 (events.now(), recipient, seq))


def sweep(conn: sqlite3.Connection) -> None:
    """Apply retention to every mailbox, including ones nobody reads or writes any more."""
    with transaction(conn):
        for row in conn.execute("SELECT recipient, read_seq FROM mailboxes").fetchall():
            _forget_old(conn, row["recipient"], row["read_seq"])


def wire_bytes(text: str) -> int:
    return len(json.dumps(text)) - 2


def as_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {"seq": row["seq"], "message_id": row["id"], "sender": row["sender"], "job_id": row["job_id"],
            "text": row["text"], "at": row["at"]}


def _page(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    page: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        message = as_dict(row)
        size += 2 * len(json.dumps(message))
        # Always return at least one message: admission keeps any single message far below the page budget.
        if page and size > MAX_PAGE_BYTES:
            break
        page.append(message)
    return page


def _mailbox(conn: sqlite3.Connection, recipient: str) -> tuple[int, int]:
    row = conn.execute("SELECT last_seq, read_seq FROM mailboxes WHERE recipient = ?", (recipient,)).fetchone()
    return (row["last_seq"], row["read_seq"]) if row else (0, 0)


def request_time(request_id: str) -> datetime:
    """The UTC creation time a request_id starts with; refuse a malformed, future or too-old one."""
    match = REQUEST_ID.fullmatch(request_id)
    if match is None:
        raise InvalidRequest("a request_id is YYYYMMDDTHHMMSSZ-<suffix>: its UTC creation time, then up "
                             "to 100 of [A-Za-z0-9._:-]")
    try:
        created_at = datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise InvalidRequest(f"request_id {request_id} does not carry a real UTC time") from None
    current = datetime.now(UTC)
    if created_at < current - REQUEST_MAX_AGE:
        raise RequestTooOld(f"request_id {request_id} is more than {REQUEST_MAX_AGE.days} days old")
    if created_at > current + REQUEST_MAX_SKEW:
        raise InvalidRequest(f"request_id {request_id} is dated in the future")
    return created_at


def _forget_old(conn: sqlite3.Connection, recipient: str, read_seq: int) -> None:
    """Delete read messages, and their job-less events, once past retention; expire old retry identities."""
    current = datetime.now(UTC)
    cutoff = (current - READ_RETENTION).isoformat()
    conn.execute("DELETE FROM messages WHERE recipient = ? AND seq <= ? AND at < ?", (recipient, read_seq, cutoff))
    conn.execute("DELETE FROM events WHERE job_id IS NULL AND kind = 'message' AND audience = ? AND seq <= ?"
                 " AND at < ?", (recipient, read_seq, cutoff))
    conn.execute("DELETE FROM message_requests WHERE created_at < ?", ((current - IDENTITY_RETENTION).isoformat(),))
