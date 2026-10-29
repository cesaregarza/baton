"""Jobs: a title, a body and a state that moves through the configured state graph.

The graph comes from `[jobs]` in the config. Each state may name the role it
waits on; entering a state appends an event addressed to that role, so the
role's `wait` returns and its wake hook fires. A job that reaches a terminal
state (one with no `next`) tells the role that created it.

Every state change adds one to the job's `revision`; nothing else does, so a
message about a job leaves it alone. Every operation is one transaction: a
reader never sees a state without its event, or the reverse.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from baton import events, messages
from baton.config import Jobs
from baton.errors import IllegalTransition, InvalidRequest, StaleRevision, UnknownJob
from baton.store import transaction

MAX_TITLE = 200
MAX_BODY_BYTES = 64 * 1024
MAX_PAGE_EVENTS = 100


def create(conn: sqlite3.Connection, graph: Jobs, *, request_id: str, actor: str, title: str,
           body: str = "") -> dict[str, Any]:
    """Open a job in the initial state. Retrying with the same request_id and body returns the same job."""
    if not 1 <= len(request_id) <= 200:
        raise InvalidRequest("request_id is 1-200 characters")
    if not 1 <= len(title) <= MAX_TITLE or len(body.encode()) > MAX_BODY_BYTES:
        raise InvalidRequest(f"title is 1-{MAX_TITLE} characters and body at most {MAX_BODY_BYTES} bytes")
    digest = events.request_digest("create_job", {"actor": actor, "title": title, "body": body})
    initial = graph.states[graph.initial]
    with transaction(conn):
        existing = events.replayed(conn, "jobs", request_id, digest)
        if existing is not None:
            return {"job_id": existing["id"], "state": existing["state"], "revision": existing["revision"],
                    "created": False}
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        stamp = events.now()
        conn.execute("INSERT INTO jobs (id, request_id, request_digest, title, body, state, waiting_on, revision,"
                     " created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                     (job_id, request_id, digest, title, body, initial.name, initial.waiting_on, actor, stamp, stamp))
        events.append(conn, job_id=job_id, actor=actor, audience=initial.waiting_on, kind="job.created",
                      summary=title, data={"state": initial.name})
    return {"job_id": job_id, "state": initial.name, "revision": 1, "created": True}


def move(conn: sqlite3.Connection, graph: Jobs, job_id: str, to_state: str, *, actor: str, note: str = "",
         request_id: str | None = None, expected_revision: int | None = None) -> dict[str, Any]:
    """Move a job along one edge of the graph, with an optional short note for whoever acts next.

    `request_id` makes the move safe to retry: a retry with the same body
    returns the original outcome (`created: false`) and changes nothing, even
    if the job has moved on since. `expected_revision` refuses the move as
    `stale_revision` if the job's state has changed at all since the caller
    read that revision, including a change that came back to the same state.
    """
    if len(note) > 2000:
        raise InvalidRequest("a note is at most 2000 characters; send a message for anything longer")
    request_id = request_id or messages.new_request_id()
    created_at = messages.request_time(request_id)
    digest = events.request_digest("move_job", {"job_id": job_id, "to_state": to_state, "actor": actor,
                                                "note": note, "expected_revision": expected_revision})
    with transaction(conn):
        _forget_old_moves(conn)
        replay = events.replayed(conn, "job_moves", request_id, digest)
        if replay is not None:
            return {**json.loads(replay["outcome"]), "request_id": request_id, "created": False}
        job = _load(conn, job_id)
        if expected_revision is not None and expected_revision != job["revision"]:
            raise StaleRevision(f"{job_id} is at revision {job['revision']}, not {expected_revision}; "
                                "read it again before moving it")
        current = graph.states.get(job["state"])
        if current is None or to_state not in current.next:
            allowed = sorted(current.next) if current else []
            raise IllegalTransition(f"{job_id}: {job['state']} -> {to_state} is not allowed; next: {allowed}")
        target = graph.states[to_state]
        conn.execute("UPDATE jobs SET state = ?, waiting_on = ?, revision = revision + 1, updated_at = ?"
                     " WHERE id = ?", (target.name, target.waiting_on, events.now(), job_id))
        audience = target.waiting_on or (job["created_by"] if target.terminal else None)
        events.append(conn, job_id=job_id, actor=actor, audience=audience, kind="job.moved",
                      summary=note or f"{job['state']} -> {to_state}",
                      data={"from": job["state"], "to": to_state, "note": note, "revision": job["revision"] + 1})
        outcome = _as_dict(_load(conn, job_id))
        conn.execute("INSERT INTO job_moves (request_id, request_digest, job_id, outcome, created_at)"
                     " VALUES (?, ?, ?, ?, ?)", (request_id, digest, job_id, json.dumps(outcome),
                                                 created_at.isoformat()))
    return {**outcome, "request_id": request_id, "created": True}


def get(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    return _as_dict(_load(conn, job_id))


def list_jobs(conn: sqlite3.Connection, *, state: str | None = None, waiting_on: str | None = None,
              limit: int = 50) -> list[dict[str, Any]]:
    """Recent jobs, newest first, without their bodies."""
    query, params = "SELECT * FROM jobs WHERE 1 = 1", []
    for column, value in (("state", state), ("waiting_on", waiting_on)):
        if value is not None:
            query += f" AND {column} = ?"
            params.append(value)
    rows = conn.execute(query + " ORDER BY created_at DESC LIMIT ?", (*params, max(1, min(limit, 200)))).fetchall()
    return [{key: value for key, value in _as_dict(row).items() if key != "body"} for row in rows]


def count_jobs(conn: sqlite3.Connection, *, state: str | None = None, waiting_on: str | None = None) -> int:
    """How many jobs `list_jobs` would match with no limit."""
    query, params = "SELECT COUNT(*) FROM jobs WHERE 1 = 1", []
    for column, value in (("state", state), ("waiting_on", waiting_on)):
        if value is not None:
            query += f" AND {column} = ?"
            params.append(value)
    return conn.execute(query, params).fetchone()[0]


def read_events(conn: sqlite3.Connection, job_id: str, after_seq: int = 0, limit: int = 50) -> dict[str, Any]:
    """A job's events after `after_seq`, oldest first, with `next_seq` to continue from."""
    _load(conn, job_id)
    rows = conn.execute("SELECT * FROM events WHERE job_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                        (job_id, max(0, after_seq), max(1, min(limit, MAX_PAGE_EVENTS)))).fetchall()
    page = [events.as_dict(row) for row in rows]
    return {"events": page, "next_seq": page[-1]["seq"] if page else after_seq}


def require_exists(conn: sqlite3.Connection, job_id: str) -> None:
    """Refuse an unknown job. A finished job still accepts messages about it."""
    _load(conn, job_id)


def _forget_old_moves(conn: sqlite3.Connection) -> None:
    """Retry identities expire a day after their request_id would be refused as too old anyway."""
    cutoff = datetime.now(UTC) - messages.IDENTITY_RETENTION
    conn.execute("DELETE FROM job_moves WHERE created_at < ?", (cutoff.isoformat(),))


def _load(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise UnknownJob(job_id)
    return row


def _as_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys() if key not in ("request_digest", "request_id")}
