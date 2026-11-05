"""`wait`: block until something is addressed to a role, then hand it over once.

A role's inbox is every event whose audience is that role: messages to it and
job changes that wait on it. Each role has one cursor, advanced as events are
returned, so two waits never return the same event. Messages returned this
way count as read, and messages already read with `read_messages` are skipped.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from baton import events, messages
from baton.database import Connection

MAX_EVENTS = 100


@dataclass
class Inbox:
    """A role's read cursor and event stream over one database connection."""

    connection: Connection
    role: str
    config: Any = None

    def read(self, after_seq: int | None = None, limit: int = 50) -> dict[str, Any]:
        """The role's messages after a sequence cursor, oldest first."""
        if not 1 <= limit <= messages.MAX_PAGE_MESSAGES or (after_seq is not None and after_seq < 0):
            from baton.errors import InvalidRequest

            raise InvalidRequest(f"limit must be 1-{messages.MAX_PAGE_MESSAGES} and after_seq at least 0")
        conn, recipient = self.connection, self.role
        with conn.transaction():
            last_seq, read_seq = messages._mailbox(conn, recipient)
            start = read_seq if after_seq is None else after_seq
            rows = conn.execute("SELECT * FROM messages WHERE recipient = ? AND seq > ? ORDER BY seq LIMIT ?",
                                (recipient, start, limit)).fetchall()
            oldest = conn.execute("SELECT MIN(seq) FROM messages WHERE recipient = ?", (recipient,)).fetchone()[0]
            page = messages.as_page_for(conn, recipient, rows, config=self.config)
            if page:
                messages.mark_read(conn, recipient, page[-1]["seq"])
            unread = last_seq - messages._mailbox(conn, recipient)[1]
        truncated = start + 1 < (oldest if oldest is not None else last_seq + 1)
        return {"messages": page, "next_seq": page[-1]["seq"] if page else start, "unread": unread,
                "truncated": truncated}

    def wait(self, *, timeout_seconds: float | None = None, poll_seconds: float = 0.5) -> dict[str, Any]:
        """Wait until an event is addressed to this role, then hand it over once."""
        deadline = None if timeout_seconds is None else time.monotonic() + max(0.0, timeout_seconds)
        while True:
            taken = self.take()
            if taken["events"] or (deadline is not None and time.monotonic() >= deadline):
                return taken
            time.sleep(poll_seconds)

    def take(self) -> dict[str, Any]:
        """One non-blocking event pass, bounded like a message page."""
        conn, role, config = self.connection, self.role, self.config
        name = f"wait:{role}"
        with conn.transaction():
            rows = conn.execute("SELECT * FROM events WHERE audience = ? AND pos > ? ORDER BY pos LIMIT ?",
                                (role, events.cursor(conn, name), MAX_EVENTS)).fetchall()
            page: list[dict[str, Any]] = []
            size, full = 0, len(rows) == MAX_EVENTS
            read_seq = messages.read_seq(conn, role)
            for row in rows:
                event = events.as_dict(row)
                if event["kind"] == "message" and event["data"]["seq"] <= read_seq:
                    events.set_cursor(conn, name, row["pos"])  # already delivered by read_messages
                    continue
                if event["kind"] == "message":
                    found = conn.execute("SELECT * FROM messages WHERE recipient = ? AND seq = ?",
                                         (role, event["data"]["seq"])).fetchone()
                    event["message"] = messages.as_page_for(conn, role, [found], config=config)[0] if found else None
                size += 2 * len(json.dumps(event))
                if page and size > messages.MAX_PAGE_BYTES:
                    full = True
                    break
                page.append(event)
                if event["kind"] == "message":
                    messages.mark_read(conn, role, event["data"]["seq"])
                events.set_cursor(conn, name, row["pos"])
        return {"events": page, "more": full}


def wait(conn: Connection, role: str, *, timeout_seconds: float | None = None,
         poll_seconds: float = 0.5, config: Any = None) -> dict[str, Any]:
    """Compatibility entrypoint for the role's blocking inbox read."""
    return Inbox(conn, role, config).wait(timeout_seconds=timeout_seconds, poll_seconds=poll_seconds)


def take(conn: Connection, role: str, *, config: Any = None) -> dict[str, Any]:
    """Compatibility entrypoint for one non-blocking inbox pass."""
    return Inbox(conn, role, config).take()
