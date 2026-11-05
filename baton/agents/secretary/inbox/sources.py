"""Visibility queries shared by the model inbox and source-reading interface."""

from __future__ import annotations

import json

from baton import board_read
from baton.config import Config, ConfigError
from baton.database import Connection, Row
from baton.database.store import connect_read_only
from baton.errors import InvalidRequest

DEFAULT_BATCH = 100


def trigger_position(config: Config, principal: str, trigger: str | None) -> int | None:
    """Resolve a validated trigger to its current visible event, for retry-page selection."""
    if trigger is None or trigger == "conversation":
        return None
    parts = trigger.split(":")
    with connect_read_only(config.db_path) as baton, baton.snapshot():
        if parts[0] == "message":
            row = baton.execute(
                "SELECT pos FROM events WHERE kind='message' AND seq=? AND audience=? ORDER BY pos LIMIT 1",
                (int(parts[1]), principal),
            ).fetchone()
        else:
            row = baton.execute(
                "SELECT pos FROM events WHERE job_id=? AND seq=? ORDER BY pos LIMIT 1", (parts[1], int(parts[2]))
            ).fetchone()
        if row is not None:
            candidates = _source_rows(baton, principal, row["pos"] - 1, 1)
            if candidates and candidates[0]["pos"] == row["pos"]:
                from .read import _entry

                if _entry(baton, config, principal, candidates[0], set()) is not None:
                    return row["pos"]
    raise InvalidRequest("trigger is not a visible event for this principal")


def _epoch(baton: Connection) -> str:
    row = baton.execute("SELECT value FROM log_meta WHERE key='log_epoch'").fetchone()
    if row is None:
        raise ConfigError("Baton log_epoch is missing")
    return row["value"]


def _visible_message(baton: Connection, config: Config, principal: str, message_id: str) -> Row | None:
    condition, params = board_read.message_condition(config, principal)
    return baton.execute(
        f"SELECT m.* FROM messages m WHERE m.id=:message_id AND {condition}"
        " ORDER BY (m.recipient = :reader) DESC, m.seq LIMIT 1",
        {**params, "message_id": message_id},
    ).fetchone()


def _event_message(
    baton: Connection, config: Config, principal: str, message_id: str, seq: int, recipient: str
) -> Row | None:
    condition, params = board_read.message_condition(config, principal)
    return baton.execute(
        f"SELECT m.* FROM messages m WHERE m.id=:message_id AND m.seq=:seq AND m.recipient=:recipient AND {condition}",
        {**params, "message_id": message_id, "seq": seq, "recipient": recipient},
    ).fetchone()


def visible_event(baton: Connection, config: Config, principal: str, event: Row) -> bool:
    """Check thread visibility for non-message events already selected from the principal's stream."""
    if event["kind"] != "conversation.rate_limited":
        return True
    conversation_id = json.loads(event["data"]).get("conversation_id")
    return not conversation_id or board_read.may_read_conversation(baton, config, principal, conversation_id)


def _source_rows(baton: Connection, principal: str, after: int, limit: int) -> list[Row]:
    # An earlier job event authored by P is in P's stream and seeds job tracking. The correlated
    # source-event predicate rebuilds that set across batch boundaries without mutable job state.
    return baton.execute(
        "SELECT e.* FROM events e WHERE e.pos>? AND (e.audience=? OR e.actor=? OR"
        " (e.job_id IS NOT NULL AND EXISTS (SELECT 1 FROM events seed WHERE seed.job_id=e.job_id"
        " AND seed.pos<e.pos AND seed.actor=? AND seed.kind IN ('job.created','job.moved'))))"
        " ORDER BY e.pos LIMIT ?",
        (after, principal, principal, principal, limit),
    ).fetchall()


def delivered_to(principal: str, audience: str | None) -> bool:
    """Whether a row of the principal's stream is a delivery: an event addressed to the principal, whatever its kind.

    Every delivery lands here, so a new kind reaches the read and the wake decision with no change to the secretary.
    `_source_rows` also selects the principal's own sends and later events on jobs it moved: those are context, which
    the secretary may read but never treats as a delivery or an alert."""
    return audience == principal
