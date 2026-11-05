"""Generation ownership, activation watermarks, and retirement.

Generation transitions own their transactions. Batch writers also call the ownership,
slot, and retirement checks inside their existing transaction."""

from __future__ import annotations

import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from baton.config import Config
from baton.database import Connection, Row
from baton.errors import (
    Forbidden,
    GenerationActive,
    GenerationBehind,
    GenerationDropped,
    GenerationIncomplete,
    LimitExceeded,
    ReconcileRequired,
    UnknownGeneration,
)

from . import storage, vectors
from .constants import KINDS, SCOPE


def generation_create(conn: Connection, config: Config, producer: str,
                      profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """A new, inactive generation for `producer` to fill, with an immutable profile and a watermark: the change
    log's position now, which activation checks every later change against."""
    _require_producer(config, producer)
    profile = vectors._profile(config, profile)
    with conn.transaction():
        _retire_generations(conn, config)
        _require_slot(conn, config, producer)
        epoch, seq = _log_position(conn)
        generation = "gen_" + secrets.token_hex(8)
        conn.execute("INSERT INTO generations (id, producer, scope_id, profile, active, created_at, watermark_epoch,"
                     " watermark_seq, last_write_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
                     (generation, producer, SCOPE, json.dumps(profile, sort_keys=True), storage._now(), epoch, seq,
                      storage._now()))
    return {"generation": generation, "profile": profile, "active": False, "watermark": {"epoch": epoch, "seq": seq}}


def generation_activate(conn: Connection, config: Config, producer: str, generation: str) -> dict[str, Any]:
    """Switch `producer`'s readers to `generation`, whole, only if every counted pair is current in it.

    A pair is counted for every kind of every conversation with a copy left, and current when its latest version
    in the generation is valid and covers through the conversation's last seq (Delta r4.1). A pair that isn't
    current is `generation_incomplete` if its conversation already owed it at the watermark (it had a copy
    then) and the pair didn't meet that; otherwise, when only messages or expiries after the watermark are
    uncovered, or the obligation itself arose after it, `generation_behind`. Participant changes count for
    neither. When the log no longer holds every change after the watermark (pruned, or another epoch), the same
    check runs from state and any shortfall is `generation_behind`. Over `max_activation_reconcile` pairs, on
    either path, it is `reconcile_required`.
    """
    _require_producer(config, producer)
    with conn.transaction():
        target = _owned_generation(conn, producer, generation)
        previous = _generation(conn, producer)
        if previous == target["id"]:
            return {"generation": target["id"], "previous": previous, "mode": "unchanged"}
        vectors._require_one_signature(conn, producer, target)
        epoch, _ = _log_position(conn)
        pruned = int(conn.execute("SELECT value FROM log_meta WHERE key = 'pruned_through'").fetchone()[0])
        reconcile = (target["watermark_epoch"] != epoch or target["watermark_seq"] is None
                     or target["watermark_seq"] < pruned)
        counted = conn.execute("SELECT id, last_seq FROM conversations WHERE EXISTS (SELECT 1 FROM messages m"
                               " WHERE m.conversation_id = conversations.id) ORDER BY created_at, id").fetchall()
        bound, pairs = config.indexer.max_activation_reconcile, len(counted) * len(KINDS)
        if pairs > bound:  # both paths check every counted pair
            raise ReconcileRequired(f"{pairs} pairs is more than the {bound} one activation checks: raise"
                                    " [indexer] max_activation_reconcile and activate again; no refill helps",
                                    pairs, bound)
        added, expired = ({}, set()) if reconcile else _after_watermark(conn, target["watermark_seq"])
        incomplete, behind = [], []
        for conversation in counted:
            for kind in KINDS:
                version = storage._current(conn, producer, target["id"], conversation["id"], kind)
                if _is_current(version, conversation):
                    continue
                pair = {"conversation_id": conversation["id"], "kind": kind}
                if reconcile:
                    behind.append(pair)
                elif not _owed_at_watermark(conn, conversation, added.get(conversation["id"], 0),
                                            target["watermark_seq"]):
                    behind.append(pair)  # nothing was owed at the watermark: the whole obligation came after
                elif _met_at_watermark(version, conversation, added.get(conversation["id"], 0),
                                       conversation["id"] in expired):
                    behind.append(pair)  # it met what the watermark asked; a change after it is uncovered
                else:
                    incomplete.append(pair)
        if incomplete:
            raise GenerationIncomplete(f"{len(incomplete)} pairs have no current version in {target['id']}",
                                       incomplete)
        if behind:
            raise GenerationBehind(f"{len(behind)} pairs changed since {target['id']} was filled", behind)
        if json.loads(target["profile"]).get("embedding"):
            missing = vectors._unvectored(conn, producer, target["id"], counted)
            if missing:  # every commit carries what it owes, so this is a guard, not a path
                raise GenerationIncomplete(f"{len(missing)} items in {target['id']} have no vector or skip", missing)
        now = storage._now()
        conn.execute("UPDATE generations SET active = 0, deactivated_at = ? WHERE producer = ? AND active = 1",
                     (now, producer))
        conn.execute("UPDATE generations SET active = 1, activated_at = ?, deactivated_at = NULL WHERE id = ?",
                     (now, target["id"]))
    return {"generation": target["id"], "previous": previous, "mode": "reconcile" if reconcile else "watermark"}


def generation_drop(conn: Connection, config: Config, producer: str, generation: str) -> dict[str, Any]:
    """Drop one of `producer`'s inactive generations: its slot frees at once, its open batches close, and its rows
    stay, invalid from now on. Receipts for commits into it stay, reporting those versions invalid."""
    _require_producer(config, producer)
    with conn.transaction():
        row = conn.execute("SELECT * FROM generations WHERE id = ?", (str(generation),)).fetchone()
        if row is None:
            raise UnknownGeneration(f"there is no generation {generation}")
        if row["producer"] != producer:
            raise Forbidden(f"generation {generation} belongs to another producer")
        if row["dropped_at"] is None:
            if row["active"]:
                raise GenerationActive(f"{generation} is serving; activate another before dropping it")
            conn.execute("UPDATE generations SET dropped_at = ? WHERE id = ?", (storage._now(), row["id"]))
            # Only the unexpired ones, which hold the producer's open-batch slots; an expired one is left as it is.
            conn.execute("UPDATE batches SET state = 'discarded' WHERE generation = ? AND state = 'open'"
                         " AND expires_at > ?", (row["id"], time.time()))
    return {"generation": row["id"], "dropped": True}


def _owned_generation(conn: Connection, producer: str, generation: str) -> Row:
    row = conn.execute("SELECT * FROM generations WHERE id = ?", (str(generation),)).fetchone()
    if row is None:
        raise UnknownGeneration(f"there is no generation {generation}")
    if row["producer"] != producer:
        raise Forbidden(f"generation {generation} belongs to another producer")
    if row["dropped_at"] is not None:
        raise GenerationDropped(f"generation {generation} was dropped")
    return row


def _after_watermark(conn: Connection, watermark: int) -> tuple[dict[str, int], set[str]]:
    """Per conversation, how many messages were added after the watermark; and which lost a copy after it.
    Participant changes count for neither: at scope `all` they change no coverage and no validity."""
    # Logs written before copies stopped expiring still hold `message_expired` rows, and count the same.
    added = {row[0]: row[1] for row in conn.execute(
        "SELECT conversation_id, COUNT(*) FROM change_log WHERE change_seq > ? AND kind = 'message_added'"
        " GROUP BY conversation_id", (watermark,))}
    expired = {row[0] for row in conn.execute(
        "SELECT DISTINCT conversation_id FROM change_log WHERE change_seq > ? AND kind = 'message_expired'",
        (watermark,))}
    return added, expired


def _owed_at_watermark(conn: Connection, conversation: Row, added_after: int,
                       watermark: int) -> bool:
    """Whether the conversation owed a version at the watermark: it had messages then, and a copy of one.

    A copy existed at the watermark if a message from then still has one, or if one of them lost its last copy
    after the watermark (an expiry whose message wasn't itself added after it). A conversation created after
    the watermark, or empty at it and refilled later, owed nothing then.
    """
    then = conversation["last_seq"] - added_after
    if then < 1:
        return False
    if conn.execute("SELECT 1 FROM messages WHERE conversation_id = ? AND conversation_seq <= ? LIMIT 1",
                    (conversation["id"], then)).fetchone():
        return True
    return conn.execute(
        "SELECT 1 FROM change_log WHERE change_seq > ? AND conversation_id = ? AND kind = 'message_expired'"
        " AND message_id NOT IN (SELECT message_id FROM change_log WHERE change_seq > ? AND conversation_id = ?"
        " AND kind = 'message_added') LIMIT 1",
        (watermark, conversation["id"], watermark, conversation["id"])).fetchone() is not None


def _met_at_watermark(version: Row | None, conversation: Row, added_after: int,
                      expired_after: bool) -> bool:
    """Whether a pair met its watermark obligation: covering through the last seq the conversation had then,
    and valid, or made invalid only by an expiry after the watermark."""
    if version is None or version["covered_through_seq"] < conversation["last_seq"] - added_after:
        return False
    return version["state"] == "valid" or expired_after


def _require_slot(conn: Connection, config: Config, producer: str) -> None:
    held = conn.execute("SELECT COUNT(*) FROM generations WHERE producer = ? AND dropped_at IS NULL",
                        (producer,)).fetchone()[0]
    if held >= config.indexer.max_generations:
        raise LimitExceeded(f"{producer} already has {held} generations; drop an inactive one first",
                            "max_generations")


def _is_current(version: Row | None, conversation: Row) -> bool:
    """Delta r4.1: valid, and covering through the conversation's last seq. The source revision is a label."""
    return version is not None and version["state"] == "valid" and \
        version["covered_through_seq"] >= conversation["last_seq"]


def _log_position(conn: Connection) -> tuple[str, int]:
    """The change log's epoch and its newest position, retained or pruned."""
    epoch = conn.execute("SELECT value FROM log_meta WHERE key = 'log_epoch'").fetchone()[0]
    pruned = int(conn.execute("SELECT value FROM log_meta WHERE key = 'pruned_through'").fetchone()[0])
    newest = conn.execute("SELECT MAX(change_seq) FROM change_log").fetchone()[0]
    return epoch, max(pruned, newest or 0)


def _require_producer(config: Config, producer: str) -> None:
    if producer not in config.indexer.producers:
        raise Forbidden(f"{producer!r} is not listed in [indexer] producers")


def _generation(conn: Connection, producer: str) -> str | None:
    row = conn.execute("SELECT id FROM generations WHERE producer = ? AND active = 1", (producer,)).fetchone()
    return row["id"] if row else None


def _retire_generations(conn: Connection, config: Config) -> None:
    """Retire a generation replaced longer ago than its retention: mark it dropped.

    Its rows stay, so anything naming it fails closed. The active generation never retires, and one with an open
    batch still running waits for it, so the batches it leaves open have all expired; none is rewritten.
    """
    retired = (datetime.now(UTC) - timedelta(days=config.indexer.generation_retention_days)).isoformat()
    # A replaced generation ages from when it was replaced, one never activated from its last write.
    for (generation,) in conn.execute(
            "SELECT id FROM generations WHERE active = 0 AND dropped_at IS NULL"
            " AND COALESCE(deactivated_at, CASE WHEN activated_at IS NULL THEN last_write_at END) < ?"
            " AND NOT EXISTS (SELECT 1 FROM batches WHERE generation = generations.id AND state = 'open'"
            " AND expires_at > ?)", (retired, time.time())).fetchall():
        conn.execute("UPDATE generations SET dropped_at = ? WHERE id = ?", (storage._now(), generation))
