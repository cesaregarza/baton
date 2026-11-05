"""Secretary source queries and text-free event envelopes.

The engine and read interface share these visibility checks and source positions.
Helpers use the caller's database connection and snapshot; they never advance a cursor
or persist the authored text they return transiently."""

from __future__ import annotations

import hashlib
import json
import re

from baton import board_read, messages
from baton.config import Config, ConfigError
from baton.database import Connection, Row
from baton.errors import InvalidRequest

ENVELOPE_VERSION = 1
DEFAULT_BATCH = 100
MAX_CITATIONS = 64
ARTIFACT = re.compile(r"(?<![\w/.-])(?P<key>[\w./-]+#\d+)@(?P<rev>[0-9a-fA-F]{7,40})(?![0-9a-fA-F])"
                      r"|(?<![0-9a-fA-F])(?P<sha>[0-9a-fA-F]{40})(?![0-9a-fA-F])")
SUPERSEDES = re.compile(r"\bsupersedes\s+(?P<key>[\w./-]+#\d+)@(?P<rev>[0-9a-fA-F]{7,40})"
                        r"(?![0-9a-fA-F])", re.IGNORECASE)


def _digest(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _epoch(baton: Connection) -> str:
    row = baton.execute("SELECT value FROM log_meta WHERE key='log_epoch'").fetchone()
    if row is None:
        raise ConfigError("Baton log_epoch is missing")
    return row["value"]


def _sha_text(text: str | None) -> str | None:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None


def _artifacts(text: str) -> tuple[list[dict], bool]:
    found = []
    for match in ARTIFACT.finditer(text):
        if len(found) == MAX_CITATIONS:
            return found, True
        found.append({"key": match["key"] or "sha", "rev": (match["rev"] or match["sha"]).lower()})
    return found, False


def _supersedes(text: str) -> dict | None:
    matches = list(SUPERSEDES.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    return {"key": match["key"], "rev": match["rev"].lower()}


def _request_artifact(meta: dict) -> dict | None:
    predecessor = meta["supersedes"]
    candidates = [item for item in meta["artifacts"] if item != predecessor]
    return candidates[0] if len(candidates) == 1 else None


def _visible_message(baton: Connection, config: Config, principal: str,
                     message_id: str) -> Row | None:
    condition, params = board_read.message_condition(config, principal)
    return baton.execute(f"SELECT m.* FROM messages m WHERE m.id=:message_id AND {condition}"
                         " ORDER BY (m.recipient = :reader) DESC, m.seq LIMIT 1",
                         {**params, "message_id": message_id}).fetchone()


def _event_message(baton: Connection, config: Config, principal: str,
                   message_id: str, seq: int, recipient: str) -> Row | None:
    condition, params = board_read.message_condition(config, principal)
    return baton.execute(f"SELECT m.* FROM messages m WHERE m.id=:message_id AND m.seq=:seq"
                         f" AND m.recipient=:recipient AND {condition}",
                         {**params, "message_id": message_id, "seq": seq, "recipient": recipient}).fetchone()


def _job(config: Config, job_id: str | None, event: Row, data: dict) -> dict | None:
    if job_id is None:
        return None
    event_state = (data.get("state") if event["kind"] == "job.created" else data.get("to")
                   if event["kind"] == "job.moved" else None)
    entered = config.jobs.states.get(event_state)
    return {"id": job_id, "event_state": event_state, "event_revision": data.get("revision"),
            "event_from_state": data.get("from") if event["kind"] == "job.moved" else None,
            "event_waiting_on": event["audience"] if entered is not None and not entered.terminal else None}


def _conversation(baton: Connection, conversation_id: str | None, message_id: str | None) -> dict | None:
    if conversation_id is None:
        return None
    change = baton.execute("SELECT revision FROM change_log WHERE conversation_id=? AND message_id=?"
                           " ORDER BY revision DESC LIMIT 1", (conversation_id, message_id)).fetchone()
    return {"id": conversation_id, "message_revision": change["revision"] if change else None}


def _envelope(baton: Connection, config: Config, principal: str,
              event: Row) -> tuple[dict, str] | None:
    """Map one real Baton event to metadata plus transient authored text. Never persist the latter."""
    data = json.loads(event["data"])
    message = None
    text = ""
    if event["kind"] == "message":
        message_id = data.get("message_id")
        message_seq = data.get("seq")
        if not isinstance(message_id, str) or type(message_seq) is not int:
            raise InvalidRequest("message event has no message_id or seq")
        message = _event_message(baton, config, principal, message_id, message_seq, event["audience"])
        if message is None:
            if baton.execute("SELECT 1 FROM messages WHERE id=? AND seq=? AND recipient=?",
                             (message_id, message_seq, event["audience"])).fetchone() is None:
                raise InvalidRequest("message event has no corresponding message copy")
            return None  # visibility may have narrowed since this event was appended
        shaped = messages.as_dict(message)
        text = shaped["text"]
        source_key = f"message:{message_id}"
        identity = {key: shaped.get(key) for key in ("from_role", "author_kind", "alias_id", "alias", "code")}
        relayed, notice = shaped["relayed"], shaped["notice"]
        conversation_id = message["conversation_id"]
        job_id = message["job_id"]
        detail = {"message_id": message_id, "seq": message["seq"], "recipient": message["recipient"],
                  "conversation_seq": message["conversation_seq"], "reply_to": message["reply_to"],
                  "attention": shaped["attention"], "needs_decision": shaped["needs_decision"],
                  "text_sha256": _sha_text(text),
                  "relayed_sha256": _digest(relayed) if relayed is not None else None,
                  "notice_sha256": _digest(notice) if notice is not None else None,
                  "relayed_count": len(relayed) if relayed else 0, "has_notice": notice is not None}
    else:
        source_key = f"event:{event['pos']}"
        identity = {"from_role": event["actor"], "author_kind": None, "alias_id": None,
                    "alias": None, "code": None}
        job_id = event["job_id"]
        conversation_id = data.get("conversation_id") if event["kind"] == "conversation.rate_limited" else None
        if conversation_id and not board_read.may_read_conversation(baton, config, principal, conversation_id):
            return None
        detail = {"message_id": None, "seq": event["seq"], "recipient": event["audience"],
                  "conversation_seq": None, "reply_to": None, "attention": None, "needs_decision": False,
                  "text_sha256": None, "relayed_sha256": None, "notice_sha256": None,
                  "relayed_count": 0, "has_notice": False}
    artifacts, too_many = _artifacts(text)
    meta = {"version": ENVELOPE_VERSION, "source_key": source_key, "event_pos": event["pos"],
            "event_kind": event["kind"], "event_actor": event["actor"], "event_audience": event["audience"],
            "at": event["at"], "identity": identity, "conversation_id": conversation_id, "job_id": job_id,
            "job": _job(config, job_id, event, data),
            "conversation": _conversation(baton, conversation_id, detail["message_id"]),
            "can_view_all_conversations": board_read.sees_all(config, principal),
            "artifacts": artifacts, "supersedes": _supersedes(text),
            "artifact_over_limit": too_many, **detail}
    return meta, text


def _source_rows(baton: Connection, principal: str, after: int, limit: int) -> list[Row]:
    # An earlier job event authored by P is in P's stream and seeds job tracking. The correlated
    # source-event predicate rebuilds that set across batch boundaries without mutable job state.
    return baton.execute("SELECT e.* FROM events e WHERE e.pos>? AND (e.audience=? OR e.actor=? OR"
                         " (e.job_id IS NOT NULL AND EXISTS (SELECT 1 FROM events seed WHERE seed.job_id=e.job_id"
                         " AND seed.pos<e.pos AND seed.actor=? AND seed.kind IN ('job.created','job.moved'))))"
                         " ORDER BY e.pos LIMIT ?", (after, principal, principal, principal, limit)).fetchall()


def delivered_to(principal: str, audience: str | None) -> bool:
    """Whether a row of the principal's stream is a delivery: an event addressed to the principal, whatever its kind.

    Every delivery lands here, so a new kind reaches the read and the wake decision with no change to the secretary.
    `_source_rows` also selects the principal's own sends and later events on jobs it moved: those are context, which
    the secretary may read but never treats as a delivery or an alert."""
    return audience == principal


def _actor(meta: dict) -> str | None:
    alias_id = meta["identity"]["alias_id"]
    return f"guest:{alias_id}" if alias_id else meta["identity"]["from_role"]
