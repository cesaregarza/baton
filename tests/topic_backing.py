"""Record the topics a fixture's labels need, for tests that are about something else.

A topic is a container now, so a conversation can only be filed in one that exists. A test about
filtering, paging or a dashboard page says so by backing its labels here, rather than by giving a role
`can_create_topics` and calling the tool: the permission belongs to the tests that are about the
permission. Everything each of those tests asserts is unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from baton.store import connect, new_topic_id


def back_topics(target: Any, *paths: str, members: tuple[str, ...] = ()) -> None:
    """Make every path in `paths` an existing topic, with each implicit parent, in `target`.

    `target` is an open connection or a database path, so an in-process test and one that drives the
    server over MCP can both use it. Writing the row directly is deliberate: it keeps a fixture from
    depending on who may create a topic.
    """
    opened = None
    if isinstance(target, (str, Path)):
        opened = connect(Path(target))
        conn: sqlite3.Connection = opened
    else:
        conn = target
    try:
        wanted = sorted({part for path in paths for part in (path, path.split("/")[0])})
        with conn:
            conn.executemany(
                "INSERT OR IGNORE INTO topics (path, title, members, created_by, created_at, id)"
                " VALUES (?, ?, ?, 'fixture', '2026-01-01T00:00:00+00:00', ?)",
                [(path, path.split("/")[-1], json.dumps(list(members)), new_topic_id()) for path in wanted])
    finally:
        if opened is not None:
            opened.close()
