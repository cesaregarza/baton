"""Persist recipient copies, sequence positions, and acknowledgments of delivery."""

from __future__ import annotations

import json
import uuid

from baton import events
from baton.database import Connection

# Not a valid role name: no role can be mistaken for a host-authored notice.
HOST_ACTOR = "@host"


def store_copy(
    conn: Connection,
    *,
    message_id: str,
    sender: str | None,
    recipient: str,
    text: str,
    job_id: str | None = None,
    conversation_id: str | None = None,
    conversation_seq: int | None = None,
    relayed: list[dict] | None = None,
    notice: dict[str, str] | None = None,
    author_kind: str | None = None,
    reply_to: str | None = None,
    attention: list[str] | None = None,
    needs_decision: bool = False,
    identity: dict[str, str] | None = None,
) -> int:
    """Store one copy as the recipient's next seq, with its event; the caller holds the write lock and checked room."""
    last_seq = positions(conn, recipient)[0]
    seq = last_seq + 1
    conn.execute(
        "INSERT INTO mailboxes (recipient, last_seq, read_seq) VALUES (?, ?, 0)"
        " ON CONFLICT (recipient) DO UPDATE SET last_seq = excluded.last_seq",
        (recipient, seq),
    )
    conn.execute(
        "INSERT INTO messages (id, job_id, conversation_id, conversation_seq, sender, author_kind,"
        " recipient, seq, text, relayed, notice, reply_to, attention, needs_decision, at,"
        " alias_id, alias, alias_code)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            message_id,
            job_id,
            conversation_id,
            conversation_seq,
            sender,
            author_kind,
            recipient,
            seq,
            text,
            json.dumps(relayed) if relayed else None,
            json.dumps(notice) if notice else None,
            reply_to,
            json.dumps(attention) if attention else None,
            int(needs_decision),
            events.now(),
            (identity or {}).get("alias_id"),
            (identity or {}).get("alias"),
            (identity or {}).get("code"),
        ),
    )
    # A job-less event uses the message's seq as its own.
    events.append(
        conn,
        job_id=job_id,
        seq=None if job_id else seq,
        actor=sender or HOST_ACTOR,
        audience=recipient,
        kind="message",
        summary=f"message {seq} from {sender or 'the host'}",
        data={"message_id": message_id, "seq": seq, **({"alias_id": identity["alias_id"]} if identity else {})},
    )
    return seq


def new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex[:16]}"


def unread(conn: Connection, recipient: str) -> int:
    last_seq, read_seq = positions(conn, recipient)
    return last_seq - read_seq


def addressed_to(conn: Connection, recipient: str, seq: int) -> bool:
    """Whether the recipient's message `seq` singles it out, rather than only reaching it.

    A direct message does, by definition. A conversation message does when it names the recipient for
    attention or asks for a decision; one that does neither still arrives, but singles nobody out. A
    message with no copy left singles nobody out either: there is nothing left to look at.
    """
    row = conn.execute(
        "SELECT conversation_id, attention, needs_decision FROM messages WHERE recipient = ? AND seq = ?",
        (recipient, seq),
    ).fetchone()
    if row is None:
        return False
    if row["conversation_id"] is None:
        return True
    return bool(row["needs_decision"]) or recipient in json.loads(row["attention"] or "[]")


def first_unread_seq(conn: Connection, recipient: str) -> int | None:
    last_seq, read_seq = positions(conn, recipient)
    return read_seq + 1 if read_seq < last_seq else None


def read_seq(conn: Connection, recipient: str) -> int:
    return positions(conn, recipient)[1]


def mark_read(conn: Connection, recipient: str, seq: int) -> None:
    """Advance the delivered position, and retire every wake the read has answered.

    A wake for messages now read would only replay them, and that holds for a batch's reminders as
    well: scheduled, deferred over the cap, or already leased by a worker. Every state that is not
    terminal goes, in this transaction, so a send that completes afterwards cannot revive one.

    The two are matched differently on purpose. An original wake is found by the seq in its key, as
    it always was. A reminder is found by the read position its batch had when it was scheduled, so
    nothing here has to parse `messages:9:r2` or rely on how SQLite casts the text after the seq.
    """
    conn.execute("UPDATE mailboxes SET read_seq = MAX(read_seq, ?) WHERE recipient = ?", (seq, recipient))
    conn.execute(
        "UPDATE wakes SET status = 'superseded', finished_at = ? WHERE role = ?"
        " AND status IN ('pending', 'scheduled', 'capped') AND ("
        "   (ordinal IS NULL AND key LIKE 'messages:%' AND CAST(substr(key, 10) AS INTEGER) <= ?)"
        "   OR (ordinal IS NOT NULL AND baseline_seq < ?))",
        (events.now(), recipient, seq, seq),
    )


def positions(conn: Connection, recipient: str) -> tuple[int, int]:
    row = conn.execute("SELECT last_seq, read_seq FROM mailboxes WHERE recipient = ?", (recipient,)).fetchone()
    return (row["last_seq"], row["read_seq"]) if row else (0, 0)
