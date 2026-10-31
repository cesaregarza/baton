"""`wait`: block until something is addressed to a role, then hand it over once.

A role's inbox is every event whose audience is that role: messages to it and
job changes that wait on it. Each role has one cursor, advanced as events are
returned, so two waits never return the same event. Messages returned this
way count as read, and messages already read with `read_messages` are skipped.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from baton import events, messages
from baton.store import transaction

MAX_EVENTS = 100


def wait(conn: sqlite3.Connection, role: str, *, timeout_seconds: float | None = None,
         poll_seconds: float = 0.5, config: Any = None) -> dict[str, Any]:
    """New events for `role`, oldest first; an empty list only once `timeout_seconds` has passed.

    A message event carries the message itself, or `"message": null` if
    retention has already deleted it. `more` is true when a page limit was hit.
    """
    deadline = None if timeout_seconds is None else time.monotonic() + max(0.0, timeout_seconds)
    while True:
        taken = take(conn, role, config=config)
        if taken["events"] or (deadline is not None and time.monotonic() >= deadline):
            return taken
        time.sleep(poll_seconds)


def take(conn: sqlite3.Connection, role: str, *, config: Any = None) -> dict[str, Any]:
    """One non-blocking pass of `wait`, bounded like a message page."""
    name = f"wait:{role}"
    with transaction(conn):
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
