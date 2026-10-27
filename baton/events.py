"""The event log: one ordered record of everything addressed to a role.

Job changes and messages both append here. `pos` orders all events; `wait`
and the wake dispatcher each keep a cursor into it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from baton.errors import IdempotencyConflict


def now() -> str:
    return datetime.now(UTC).isoformat()


def append(conn: sqlite3.Connection, *, job_id: str | None, seq: int | None = None, actor: str,
           audience: str | None, kind: str, summary: str, data: dict[str, Any] | None = None) -> int:
    """Append one event; the caller holds the write lock. A job event without `seq` takes the job's next one."""
    if seq is None:
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE job_id = ?", (job_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO events (job_id, seq, at, actor, audience, kind, summary, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, seq, now(), actor, audience, kind, summary[:200], json.dumps(data or {})),
    )
    return seq


def as_dict(row: sqlite3.Row) -> dict[str, Any]:
    event = {key: row[key] for key in row.keys() if key != "pos"}
    event["data"] = json.loads(row["data"])
    return event


def cursor(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT pos FROM cursors WHERE name = ?", (name,)).fetchone()
    return row["pos"] if row else 0


def set_cursor(conn: sqlite3.Connection, name: str, pos: int) -> None:
    conn.execute("INSERT INTO cursors (name, pos) VALUES (?, ?) ON CONFLICT (name) DO UPDATE SET pos = excluded.pos",
                 (name, pos))


def request_digest(operation: str, body: dict[str, Any]) -> str:
    canonical = json.dumps({"operation": operation, **body}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def replayed(conn: sqlite3.Connection, table: str, request_id: str, digest: str) -> sqlite3.Row | None:
    """The row an earlier request with this id created, or None; a different body under the same id is refused."""
    row = conn.execute(f"SELECT * FROM {table} WHERE request_id = ?", (request_id,)).fetchone()
    if row is not None and row["request_digest"] != digest:
        raise IdempotencyConflict(f"request_id {request_id} was already used with a different body")
    return row
