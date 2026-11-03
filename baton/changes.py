"""Conversation revisions and the change log: what changed in each conversation's source, in order.

Every message stored in a conversation and every participant added or removed bumps that conversation's
`revision` by one and appends one `change_log` row, in the same transaction as the change itself. Reading never
logs anything. An indexer follows the log with `read` and `wait` through a cursor, `<log_epoch>:<change_seq>`,
that it keeps itself: baton holds no position for it, so following the log moves nothing anyone else depends
on.

The log is an optimisation, never the only source of truth. A cursor from another log entirely, or older than
the oldest change still kept, is refused as `cursor_expired` with a position to start again from.

Every read names its scope. There is one scope today, `Scope.ALL`, for the host's own local worker; no read
falls back to it implicitly.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from baton.errors import CursorExpired, InvalidRequest
from baton.store import snapshot

MAX_SCAN = 1000  # rows one read looks at, so a long stretch is paged and never loaded at once
MAX_LIMIT = 1000
KINDS = ("message_added", "participant_added", "participant_removed")
FIELDS = ("change_seq", "conversation_id", "revision", "kind", "message_id", "role", "at")


class Scope(Enum):
    """What a read of the log may see. `ALL` is the host's own local worker reading the host's own database."""

    ALL = "all"


def record(conn: sqlite3.Connection, conversation_id: str, kind: str, *, message_id: str | None = None,
           role: str | None = None) -> int:
    """Bump the conversation's revision and log the change; the caller holds the write lock. Returns the revision."""
    if kind not in KINDS:
        raise ValueError(f"unknown change kind {kind!r}")
    conn.execute("UPDATE conversations SET revision = revision + 1 WHERE id = ?", (conversation_id,))
    revision = conn.execute("SELECT revision FROM conversations WHERE id = ?", (conversation_id,)).fetchone()[0]
    now = datetime.now(UTC)
    conn.execute("INSERT INTO change_log (conversation_id, revision, kind, message_id, role, at)"
                 " VALUES (?, ?, ?, ?, ?, ?)", (conversation_id, revision, kind, message_id, role, now.isoformat()))
    return revision


def epoch(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT value FROM log_meta WHERE key = 'log_epoch'").fetchone()[0]


def read(conn: sqlite3.Connection, scope: Scope, *, cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
    """The changes after `cursor`, oldest first, metadata only, and `next_cursor` to continue from.

    `next_cursor` is past every row looked at, so a page always advances. Without a cursor the read starts at
    the oldest change still kept.
    """
    _require(scope)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise InvalidRequest(f"limit must be 1-{MAX_LIMIT}")
    with snapshot(conn):  # a prune between checking the cursor and scanning would skip rows silently
        after = _position(conn, cursor)
        rows = conn.execute(f"SELECT {', '.join(FIELDS)} FROM change_log WHERE change_seq > ? ORDER BY change_seq"
                            " LIMIT ?", (after, min(limit, MAX_SCAN))).fetchall()
        entries = [{field: row[field] for field in FIELDS} for row in rows]
        last = entries[-1]["change_seq"] if entries else after
        return {"entries": entries, "next_cursor": f"{epoch(conn)}:{last}"}


def wait(conn: sqlite3.Connection, scope: Scope, *, cursor: str, timeout_seconds: float,
         poll_seconds: float = 0.5) -> dict[str, Any]:
    """Block until a change after `cursor` is logged, then return only its `change_seq`; or time out. No content."""
    _require(scope)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        with snapshot(conn):
            after = _position(conn, cursor)
            row = conn.execute("SELECT MIN(change_seq) FROM change_log WHERE change_seq > ?", (after,)).fetchone()
        if row[0] is not None:
            return {"change_seq": row[0]}
        if time.monotonic() >= deadline:
            return {"timed_out": True}
        time.sleep(poll_seconds)


def _require(scope: Scope) -> None:
    if not isinstance(scope, Scope):
        raise TypeError("every read of the change log names its scope")


def _position(conn: sqlite3.Connection, cursor: str | None) -> int:
    pruned = int(conn.execute("SELECT value FROM log_meta WHERE key = 'pruned_through'").fetchone()[0])
    current = epoch(conn)
    if cursor is None:
        return pruned
    stamp, _, number = str(cursor).partition(":")
    invalid_detail = "a cursor is <log_epoch>:<change_seq>, as next_cursor gave it"
    if not number.isdigit():
        raise InvalidRequest(invalid_detail)
    if stamp == current:
        try:
            position = int(number)
        except ValueError:
            raise InvalidRequest(invalid_detail) from None
        if position >= pruned:
            if position > 2**63 - 1:  # SQLite stores change_seq as a signed 64-bit integer.
                raise InvalidRequest(invalid_detail)
            return position
    raise CursorExpired("that cursor is from another log or older than the oldest change kept; rebuild from "
                        "state, then continue from reset_cursor", f"{current}:{pruned}")

