"""Derived data from the host owner's local indexing worker: read batches, artifact versions and receipts.

The worker supplies only text, each artifact's body and the messages it cites. Everything else is Baton's: the
producer (a name listed in `[indexer] producers`), which messages a batch covers (the manifest Baton issued),
the version a write builds on, and the provenance recorded with it. A version is valid until a message it was
made from loses its last copy, which flips it to invalid in that same transaction (`invalidate_lost`); an
invalid kind is rebuilt by a rebase, a fresh first version from seq 1.

Every writer runs in one transaction and checks its producer before anything else. Nothing here logs a
change, adds an event or wakes anyone, and nothing here is authority: an artifact is a derived reading of
messages, never a decision. Roles read it only through `baton.search`, under the reader's own visibility.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import struct
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from baton.config import Config
from baton.errors import (
    BatchClosed,
    BatchExpired,
    BatchIncomplete,
    BatchSuperseded,
    CheckpointGap,
    Forbidden,
    GenerationActive,
    GenerationBehind,
    GenerationDropped,
    GenerationIncomplete,
    IdempotencyConflict,
    InvalidRequest,
    LimitExceeded,
    NothingToIndex,
    ProvenanceLimitExceeded,
    ReconcileRequired,
    UnknownBatch,
    UnknownConversation,
    UnknownGeneration,
    UpToDate,
    VectorConflict,
    VectorsIncomplete,
    VersionConflict,
)
from baton.store import transaction

KINDS = ("summary", "decisions", "open_questions")
# A summary of only the latest messages, off unless `[search] rolling_window_messages` is set. Its window starts
# from the end of the conversation, so it never counts toward any kind's coverage.
RECENT = "summary_recent"
SCOPE = "all"  # a local producer reads the host's own database: every conversation
ITEM_REVISION = 1
# Why an item may go without a vector. `empty_text` is checked; the others are the producer's word.
SKIP_REASONS = ("empty_text", "over_token_limit", "excluded_kind")
UNIT_TOLERANCE = 1e-3  # messages are immutable; a later edit would raise this
MAX_REQUEST_ID = 200


def begin_batch(conn: sqlite3.Connection, config: Config, producer: str, conversation_id: str,
                kinds: list[str], generation: str | None = None) -> dict[str, Any]:
    """Issue a manifest naming exactly which messages the batch reads, and what each kind builds on."""
    _require_producer(config, producer)
    if isinstance(kinds, list) and RECENT in kinds:
        if not config.search.rolling_window_messages:
            raise InvalidRequest(f"{RECENT} needs [search] rolling_window_messages")
        if kinds != [RECENT]:
            raise InvalidRequest(f"{RECENT} is a batch of its own")
    elif not isinstance(kinds, list) or not kinds or len(set(kinds)) != len(kinds) or not set(kinds) <= set(KINDS):
        raise InvalidRequest(f"kinds is a non-empty list of distinct kinds from {', '.join(KINDS)}")
    limits = config.indexer
    with transaction(conn):
        _cleanup(conn, config)
        if generation is not None:  # after the sweep, so a generation it just retired fails closed
            generation = _owned_generation(conn, producer, generation)["id"]
        conversation = conn.execute("SELECT last_seq, revision FROM conversations WHERE id = ?",
                                    (str(conversation_id),)).fetchone()
        if conversation is None:
            raise UnknownConversation(f"there is no conversation {conversation_id}")
        if not _has_copies(conn, conversation_id):  # a summary of nothing is never required, so never made
            raise NothingToIndex(f"no message in {conversation_id} has a copy left")
        open_batches = conn.execute("SELECT COUNT(*) FROM batches WHERE producer = ? AND state = 'open'"
                                    " AND expires_at > ?", (producer, time.time())).fetchone()[0]
        if open_batches >= limits.max_open_batches:
            raise LimitExceeded(f"{producer} already has {open_batches} open batches", "max_open_batches")
        if generation is None:
            generation = _generation(conn, producer)
        if kinds == [RECENT]:
            plans = {RECENT: _recent_plan(conn, config, producer, generation, conversation_id,
                                          conversation["last_seq"])}
        else:
            plans = {kind: _kind_plan(conn, producer, generation, conversation_id, kind) for kind in kinds}
        window = _window(conn, config, conversation_id, plans, conversation["last_seq"])
        if generation is None:  # the first batch makes the producer's one generation; `dirty` never does
            _require_slot(conn, config, producer)
            generation = "gen_" + secrets.token_hex(8)
            conn.execute("INSERT INTO generations (id, producer, scope_id, profile, active, created_at, activated_at,"
                         " last_write_at) VALUES (?, ?, ?, '{}', 1, ?, ?, ?)",
                         (generation, producer, SCOPE, _now(), _now(), _now()))
        batch_id = "batch_" + secrets.token_hex(8)
        manifest = {
            "batch_id": batch_id, "producer": producer, "scope_id": SCOPE, "generation": generation,
            "conversation_id": conversation_id, "source_revision": conversation["revision"], **window,
            "pages": math.ceil(window["item_count"] / limits.page_items), "page_items": limits.page_items,
            "kinds": plans,
        }
        manifest["manifest_digest"] = _digest(manifest)
        conn.execute("INSERT INTO batches (id, producer, scope_id, generation, conversation_id, manifest,"
                     " manifest_digest, pages, state, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?,"
                     " 'open', ?, ?)", (batch_id, producer, SCOPE, generation, conversation_id, json.dumps(manifest),
                                        manifest["manifest_digest"], manifest["pages"], _now(),
                                        time.time() + limits.batch_ttl_seconds))
        conn.execute("UPDATE generations SET last_write_at = ? WHERE id = ?", (_now(), generation))
    return manifest


def read_batch(conn: sqlite3.Connection, config: Config, producer: str, batch_id: str, page: int) -> dict[str, Any]:
    """One page of the batch's messages, recorded as delivered: a commit needs every page delivered first."""
    _require_producer(config, producer)
    with transaction(conn):
        batch = _open_batch(conn, producer, batch_id)
        manifest = json.loads(batch["manifest"])
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= manifest["pages"]:
            raise InvalidRequest(f"page is 1 to {manifest['pages']} for this batch")
        size = manifest["page_items"]
        items = [item for segment in manifest["segments"] for item in segment["items"]][(page - 1) * size:page * size]
        served = []
        for item in items:
            copy = conn.execute("SELECT sender, at, text FROM messages WHERE id = ? AND conversation_id = ?"
                                " AND conversation_seq = ? LIMIT 1",
                                (item["message_id"], manifest["conversation_id"], item["seq"])).fetchone()
            if copy is None:
                raise BatchSuperseded(f"message {item['message_id']} at seq {item['seq']} has no copy left")
            served.append({"seq": item["seq"], "message_id": item["message_id"], "from_role": copy["sender"],
                           "at": copy["at"], "text": copy["text"]})
        conn.execute("INSERT OR IGNORE INTO batch_pages (batch_id, page, delivered_at) VALUES (?, ?, ?)",
                     (batch_id, page, _now()))
    return {"batch_id": batch_id, "page": page, "pages": manifest["pages"], "items": served}


def commit_batch(conn: sqlite3.Connection, config: Config, producer: str, batch_id: str,
                 payload: dict[str, Any]) -> dict[str, Any]:
    """Write a new version of each kind given, built only from what the batch delivered.

    The checks run in this order: delivery, revalidation of everything the batch rests on, the version each
    kind builds on, its checkpoint, its citations, and its provenance bound. An identical retry is answered
    from its receipt alone, even after the batch is gone or the producer was removed from the config.
    """
    request_id, artifacts, vectors, skips = _commit_request(payload)
    request = {"operation": "commit_batch", "batch_id": str(batch_id), "artifacts": artifacts}
    if vectors or skips:  # a commit without them keeps the digest it always had
        request |= {"vectors": vectors, "skips": skips}
    digest = _digest(request)
    limits = config.indexer
    with transaction(conn):
        receipt = conn.execute("SELECT * FROM commit_requests WHERE producer = ? AND request_id = ?"
                               " AND expires_at > ?", (producer, request_id, _now())).fetchone()
        if receipt is not None:
            if receipt["digest"] != digest:
                raise IdempotencyConflict(f"request_id {request_id} was used for a different commit")
            return _replay(conn, receipt)
        _require_producer(config, producer)
        _open_batch(conn, producer, batch_id)
        _cleanup(conn, config)
        batch = _open_batch(conn, producer, batch_id)  # again: the sweep may have closed it
        manifest = json.loads(batch["manifest"])
        hour_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        recent = conn.execute("SELECT COUNT(*) FROM commit_requests WHERE producer = ? AND committed_at > ?",
                              (producer, hour_ago)).fetchone()[0]
        if recent >= limits.max_commits_per_hour:
            raise LimitExceeded(f"{producer} has committed {recent} times in the last hour", "max_commits_per_hour")
        for artifact in artifacts:
            if artifact["kind"] not in manifest["kinds"]:
                raise InvalidRequest(f"this batch was begun for {', '.join(manifest['kinds'])}, not "
                                     f"{artifact['kind']}")
            if len(artifact["body"].encode()) > limits.max_artifact_bytes:
                raise LimitExceeded(f"the {artifact['kind']} body is over {limits.max_artifact_bytes} bytes",
                                    "max_artifact_bytes")
            if len(artifact["citations"]) > limits.max_citations:
                raise LimitExceeded(f"the {artifact['kind']} cites more than {limits.max_citations} messages",
                                    "max_citations")
        delivered = conn.execute("SELECT COUNT(*) FROM batch_pages WHERE batch_id = ?", (batch_id,)).fetchone()[0]
        if delivered < manifest["pages"]:
            raise BatchIncomplete(f"{manifest['pages'] - delivered} of {manifest['pages']} pages not yet read")
        _revalidate(conn, manifest, [artifact["kind"] for artifact in artifacts])
        # Checked whole before anything is written: a commit either carries every vector it owes, or nothing.
        embedding = _embedding(conn, manifest["generation"])
        owed = _check_vectors(conn, config, manifest, artifacts, vectors, skips, embedding)
        window_ranges = [[segment["from_seq"], segment["to_seq"]] for segment in manifest["segments"]]
        written = []
        for artifact in artifacts:
            kind, plan = artifact["kind"], manifest["kinds"][artifact["kind"]]
            current = _current(conn, producer, manifest["generation"], manifest["conversation_id"], kind)
            current_version = current["version"] if current else 0
            if artifact["expected_version"] != current_version or plan["prior_version"] != current_version:
                raise VersionConflict(f"{kind} is at version {current_version}, not {artifact['expected_version']}")
            if kind == RECENT:  # anchored to the end of the conversation, not to its previous coverage
                start = plan["start_seq"]
            else:
                start = 1 if current is None or current["state"] == "invalid" else current["covered_through_seq"] + 1
            if start != manifest["window"]["from_seq"]:
                raise CheckpointGap(f"{kind} continues from seq {start}, and this batch starts at "
                                    f"{manifest['window']['from_seq']}")
            prior_id = plan["prior_version_id"]
            prior = _inputs(conn, prior_id) if prior_id else []
            merged = _merge(prior + window_ranges)
            _check_containment(prior, merged)
            for message_id in artifact["citations"]:
                cited = conn.execute("SELECT conversation_seq FROM messages WHERE id = ? AND conversation_id = ?"
                                     " LIMIT 1", (message_id, manifest["conversation_id"])).fetchone()
                if cited is None or not _within(cited[0], merged):
                    raise InvalidRequest(f"citation {message_id} is not among this {kind}'s inputs")
            if len(merged) > limits.max_input_segments:
                raise ProvenanceLimitExceeded(
                    f"{kind}'s inputs would be {len(merged)} ranges, over {limits.max_input_segments}",
                    limit="max_input_segments", conversation_id=manifest["conversation_id"], kind=kind,
                    segments=len(merged), items=_count(merged))
            version_id = _write_version(conn, manifest, artifact, current_version + 1, merged, prior_id)
            written.append({"kind": kind, "version_id": version_id, "version": current_version + 1})
            if embedding:
                _write_vector(conn, manifest["generation"], embedding, "artifact", version_id, owed[("kind", kind)])
        for (field, value), entry in owed.items():
            if field == "message_id" and entry is not None:
                _write_vector(conn, manifest["generation"], embedding, "message", value, entry)
        conn.execute("UPDATE batches SET state = 'committed' WHERE id = ?", (batch_id,))
        conn.execute("UPDATE generations SET last_write_at = ? WHERE id = ?", (_now(), manifest["generation"]))
        committed_at = _now()
        stored = {"operation": "commit_batch", "request_id": request_id, "committed_at": committed_at,
                  "versions": written}
        expires = (datetime.fromisoformat(committed_at) + timedelta(days=limits.receipt_retention_days)).isoformat()
        conn.execute("INSERT INTO commit_requests (producer, request_id, digest, receipt, committed_at, expires_at)"
                     " VALUES (?, ?, ?, ?, ?, ?)", (producer, request_id, digest, json.dumps(stored), committed_at,
                                                    expires))
    return {"created": True, **_with_states(conn, stored)}


def dirty(conn: sqlite3.Connection, config: Config, producer: str, generation: str | None = None) -> dict[str, Any]:
    """Each (conversation, kind) `producer` has no version of, or whose version is behind or invalid. Read-only.

    `producer` only chooses whose set to compute, and `generation` which of its generations (the active one by
    default); neither is checked, and an unknown one simply has nothing yet. This never creates a generation,
    and a conversation with no copy left needs nothing, so it isn't listed.
    """
    generation = generation if generation is not None else _generation(conn, producer)
    pairs = []
    for conversation in conn.execute("SELECT id, last_seq FROM conversations WHERE EXISTS (SELECT 1 FROM messages m"
                                     " WHERE m.conversation_id = conversations.id) ORDER BY created_at, id").fetchall():
        for kind in KINDS:
            current = _current(conn, producer, generation, conversation["id"], kind) if generation else None
            if current is None:
                state, covered = "none", 0
            elif current["state"] == "invalid":
                state, covered = "invalid", current["covered_through_seq"]
            elif current["covered_through_seq"] < conversation["last_seq"]:
                state, covered = "behind", current["covered_through_seq"]
            else:
                continue
            stuck = state != "invalid" and _cannot_advance(conn, config, conversation["id"], current)
            pairs.append({"conversation_id": conversation["id"], "kind": kind, "state": state,
                          "covered_through_seq": covered, "last_seq": conversation["last_seq"],
                          "provenance_limit_exceeded": stuck})
    return {"producer": producer, "pairs": pairs}


def generation_create(conn: sqlite3.Connection, config: Config, producer: str,
                      profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """A new, inactive generation for `producer` to fill, with an immutable profile and a watermark: the change
    log's position now, which activation checks every later change against."""
    _require_producer(config, producer)
    profile = _profile(config, profile)
    with transaction(conn):
        _cleanup(conn, config)
        _require_slot(conn, config, producer)
        epoch, seq = _log_position(conn)
        generation = "gen_" + secrets.token_hex(8)
        conn.execute("INSERT INTO generations (id, producer, scope_id, profile, active, created_at, watermark_epoch,"
                     " watermark_seq, last_write_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
                     (generation, producer, SCOPE, json.dumps(profile, sort_keys=True), _now(), epoch, seq, _now()))
    return {"generation": generation, "profile": profile, "active": False, "watermark": {"epoch": epoch, "seq": seq}}


def generation_activate(conn: sqlite3.Connection, config: Config, producer: str, generation: str) -> dict[str, Any]:
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
    with transaction(conn):
        target = _owned_generation(conn, producer, generation)
        previous = _generation(conn, producer)
        if previous == target["id"]:
            return {"generation": target["id"], "previous": previous, "mode": "unchanged"}
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
                version = _current(conn, producer, target["id"], conversation["id"], kind)
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
            missing = _unvectored(conn, producer, target["id"], counted)
            if missing:  # every commit carries what it owes, so this is a guard, not a path
                raise GenerationIncomplete(f"{len(missing)} items in {target['id']} have no vector or skip", missing)
        now = _now()
        conn.execute("UPDATE generations SET active = 0, deactivated_at = ? WHERE producer = ? AND active = 1",
                     (now, producer))
        conn.execute("UPDATE generations SET active = 1, activated_at = ?, deactivated_at = NULL WHERE id = ?",
                     (now, target["id"]))
    return {"generation": target["id"], "previous": previous, "mode": "reconcile" if reconcile else "watermark"}


def generation_drop(conn: sqlite3.Connection, config: Config, producer: str, generation: str) -> dict[str, Any]:
    """Drop one of `producer`'s inactive generations: its slot frees at once, its open batches close, and its rows
    go in bounded sweeps. Receipts for commits into it stay, reporting those versions invalid."""
    _require_producer(config, producer)
    with transaction(conn):
        row = conn.execute("SELECT * FROM generations WHERE id = ?", (str(generation),)).fetchone()
        if row is None:
            raise UnknownGeneration(f"there is no generation {generation}")
        if row["producer"] != producer:
            raise Forbidden(f"generation {generation} belongs to another producer")
        if row["dropped_at"] is None:
            if row["active"]:
                raise GenerationActive(f"{generation} is serving; activate another before dropping it")
            conn.execute("UPDATE generations SET dropped_at = ? WHERE id = ?", (_now(), row["id"]))
            conn.execute("UPDATE batches SET state = 'discarded' WHERE generation = ? AND state = 'open'",
                         (row["id"],))
    return {"generation": row["id"], "dropped": True}


def invalidate_lost(conn: sqlite3.Connection, lost: list[tuple[str, int]]) -> None:
    """Flip every valid version made from a message that has just lost its last copy. In the caller's transaction.

    A version's inputs include its prior's (checked at commit), so one match on the seq reaches every version
    of the chain without walking it.
    """
    for conversation_id, seq in sorted(set(lost)):
        conn.execute("UPDATE artifact_versions SET state = 'invalid' WHERE state = 'valid' AND id IN"
                     " (SELECT version_id FROM artifact_inputs WHERE conversation_id = ? AND from_seq <= ?"
                     " AND to_seq >= ?)", (conversation_id, seq, seq))


def _owned_generation(conn: sqlite3.Connection, producer: str, generation: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM generations WHERE id = ?", (str(generation),)).fetchone()
    if row is None:
        raise UnknownGeneration(f"there is no generation {generation}")
    if row["producer"] != producer:
        raise Forbidden(f"generation {generation} belongs to another producer")
    if row["dropped_at"] is not None:
        raise GenerationDropped(f"generation {generation} was dropped")
    return row


def _after_watermark(conn: sqlite3.Connection, watermark: int) -> tuple[dict[str, int], set[str]]:
    """Per conversation, how many messages were added after the watermark; and which lost a copy after it.
    Participant changes count for neither: at scope `all` they change no coverage and no validity."""
    added = {row[0]: row[1] for row in conn.execute(
        "SELECT conversation_id, COUNT(*) FROM change_log WHERE change_seq > ? AND kind = 'message_added'"
        " GROUP BY conversation_id", (watermark,))}
    expired = {row[0] for row in conn.execute(
        "SELECT DISTINCT conversation_id FROM change_log WHERE change_seq > ? AND kind = 'message_expired'",
        (watermark,))}
    return added, expired


def _owed_at_watermark(conn: sqlite3.Connection, conversation: sqlite3.Row, added_after: int,
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


def _met_at_watermark(version: sqlite3.Row | None, conversation: sqlite3.Row, added_after: int,
                      expired_after: bool) -> bool:
    """Whether a pair met its watermark obligation: covering through the last seq the conversation had then,
    and valid, or made invalid only by an expiry after the watermark."""
    if version is None or version["covered_through_seq"] < conversation["last_seq"] - added_after:
        return False
    return version["state"] == "valid" or expired_after


def _unvectored(conn: sqlite3.Connection, producer: str, generation: str, counted: list) -> list[dict]:
    """Served versions and messages with a copy, in counted conversations, holding no vector or skip."""
    missing = []
    for conversation in counted:
        for kind in KINDS:
            version = _current(conn, producer, generation, conversation["id"], kind)
            if version is not None and _held(conn, generation, "artifact", version["id"]) is None:
                missing.append({"conversation_id": conversation["id"], "item": f"artifact {version['id']}"})
        for (message_id,) in conn.execute("SELECT DISTINCT id FROM messages WHERE conversation_id = ?",
                                          (conversation["id"],)).fetchall():
            if _held(conn, generation, "message", message_id) is None:
                missing.append({"conversation_id": conversation["id"], "item": f"message {message_id}"})
    return missing


def _require_slot(conn: sqlite3.Connection, config: Config, producer: str) -> None:
    held = conn.execute("SELECT COUNT(*) FROM generations WHERE producer = ? AND dropped_at IS NULL",
                        (producer,)).fetchone()[0]
    if held >= config.indexer.max_generations:
        raise LimitExceeded(f"{producer} already has {held} generations; drop an inactive one first",
                            "max_generations")


def _is_current(version: sqlite3.Row | None, conversation: sqlite3.Row) -> bool:
    """Delta r4.1: valid, and covering through the conversation's last seq. The source revision is a label."""
    return version is not None and version["state"] == "valid" and \
        version["covered_through_seq"] >= conversation["last_seq"]


def _has_copies(conn: sqlite3.Connection, conversation_id: str) -> bool:
    return conn.execute("SELECT 1 FROM messages WHERE conversation_id = ? LIMIT 1", (conversation_id,)).fetchone() \
        is not None


def _log_position(conn: sqlite3.Connection) -> tuple[str, int]:
    """The change log's epoch and its newest position, retained or pruned."""
    epoch = conn.execute("SELECT value FROM log_meta WHERE key = 'log_epoch'").fetchone()[0]
    pruned = int(conn.execute("SELECT value FROM log_meta WHERE key = 'pruned_through'").fetchone()[0])
    newest = conn.execute("SELECT MAX(change_seq) FROM change_log").fetchone()[0]
    return epoch, max(pruned, newest or 0)


def _profile(config: Config, profile: Any) -> dict[str, Any]:
    """A generation's profile, checked: an optional summarizer label, and an optional embedding description."""
    if profile is None:
        return {}
    if not isinstance(profile, dict) or not set(profile) <= {"summarizer", "embedding"}:
        raise InvalidRequest("a profile has only summarizer and embedding")
    summarizer = profile.get("summarizer")
    if summarizer is not None and (not isinstance(summarizer, str) or not 0 < len(summarizer) <= 200):
        raise InvalidRequest("profile.summarizer is a label of 1 to 200 characters")
    embedding = profile.get("embedding")
    if embedding is not None and not config.search.embeddings:
        raise InvalidRequest("profile.embedding needs [search] embeddings = true")
    if embedding is not None:
        if not isinstance(embedding, dict) or set(embedding) != {"model", "dim", "normalization", "distance"}:
            raise InvalidRequest("profile.embedding has exactly model, dim, normalization and distance")
        model, dim = embedding["model"], embedding["dim"]
        if not isinstance(model, str) or not 0 < len(model) <= 200:
            raise InvalidRequest("profile.embedding.model is a name of 1 to 200 characters")
        if isinstance(dim, bool) or not isinstance(dim, int) or not 1 <= dim <= config.search.max_vector_dim:
            raise InvalidRequest(f"profile.embedding.dim is 1 to {config.search.max_vector_dim}")
        if embedding["normalization"] not in ("unit", "none") or \
                embedding["distance"] not in ("cosine", "inner_product"):
            raise InvalidRequest("profile.embedding.normalization is unit or none, and distance is cosine or "
                                 "inner_product")
    return profile


def _require_producer(config: Config, producer: str) -> None:
    if producer not in config.indexer.producers:
        raise Forbidden(f"{producer!r} is not listed in [indexer] producers")


def _commit_request(payload: Any) -> tuple[str, list[dict[str, Any]]]:
    """The request in canonical form, or a refusal naming what's wrong with it."""
    if not isinstance(payload, dict):
        raise InvalidRequest("a commit is an object with request_id and artifacts")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not 0 < len(request_id) <= MAX_REQUEST_ID:
        raise InvalidRequest(f"request_id is a string of 1 to {MAX_REQUEST_ID} characters")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise InvalidRequest("artifacts is a non-empty list")
    canonical = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("kind") not in (*KINDS, RECENT):
            raise InvalidRequest(f"each artifact has a kind from {', '.join((*KINDS, RECENT))}")
        body, citations, expected = artifact.get("body"), artifact.get("citations"), artifact.get("expected_version")
        if not isinstance(body, str):
            raise InvalidRequest(f"the {artifact['kind']} body is a string")
        if not isinstance(citations, list) or not all(isinstance(cited, str) for cited in citations):
            raise InvalidRequest(f"the {artifact['kind']} citations are a list of message ids")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise InvalidRequest(f"the {artifact['kind']} expected_version is a whole number")
        canonical.append({"kind": artifact["kind"], "body": body, "citations": citations,
                          "expected_version": expected})
    if len({artifact["kind"] for artifact in canonical}) != len(canonical):
        raise InvalidRequest("each kind appears once in a commit")
    vectors, skips = _vector_entries(payload.get("vectors", []), "vectors"), _vector_entries(payload.get("skips", []),
                                                                                             "skips")
    seen = [_item_key(entry["item"]) for entry in vectors + skips]
    if len(set(seen)) != len(seen):
        raise InvalidRequest("each item has one vector or one skip, not both and not twice")
    return request_id, sorted(canonical, key=lambda artifact: artifact["kind"]), vectors, skips


def _vector_entries(entries: Any, field: str) -> list[dict[str, Any]]:
    """`vectors` or `skips` in canonical form: each names one item, a kind written here or a served message."""
    if not isinstance(entries, list):
        raise InvalidRequest(f"{field} is a list")
    canonical = []
    for entry in entries:
        item = entry.get("item") if isinstance(entry, dict) else None
        if not isinstance(item, dict) or len(item) != 1 or not isinstance(next(iter(item.values())), str) or \
                next(iter(item)) not in ("kind", "message_id"):
            raise InvalidRequest(f"each of {field} names one item: {{\"kind\": ...}} or {{\"message_id\": ...}}")
        if field == "vectors":
            vector = entry.get("vector")
            if not isinstance(vector, list) or not all(
                    isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x) for x in vector):
                raise InvalidRequest(f"the vector for {_item_name(item)} is a list of finite numbers")
            canonical.append({"item": item, "vector": [float(x) for x in vector]})
        else:
            if entry.get("reason") not in SKIP_REASONS:
                raise InvalidRequest(f"a skip's reason is one of {', '.join(SKIP_REASONS)}")
            canonical.append({"item": item, "reason": entry["reason"]})
    return sorted(canonical, key=lambda entry: _item_key(entry["item"]))


def _item_key(item: dict[str, str]) -> tuple[str, str]:
    return next(iter(item.items()))


def _item_name(item: dict[str, str]) -> str:
    field, value = _item_key(item)
    return f"kind {value}" if field == "kind" else f"message {value}"


def _embedding(conn: sqlite3.Connection, generation: str) -> dict[str, Any] | None:
    profile = conn.execute("SELECT profile FROM generations WHERE id = ?", (generation,)).fetchone()[0]
    return json.loads(profile).get("embedding")


def _check_vectors(conn: sqlite3.Connection, config: Config, manifest: dict[str, Any], artifacts: list[dict],
                   vectors: list[dict], skips: list[dict], embedding: dict | None) -> dict:
    """What this commit owes in vectors and skips, checked whole before anything is written.

    Returns, for each kind written and each served message that holds nothing yet, its vector or skip; and
    None for a served message that already holds one (an identical resend is a no-op, a different one a
    conflict). Without an embedding profile, a commit carries none.
    """
    if embedding is None:
        if vectors or skips:
            raise InvalidRequest("this generation's profile has no embedding, so its commits carry no vectors")
        return {}
    given = {_item_key(entry["item"]): entry for entry in vectors + skips}
    served = {item["message_id"]: item["seq"] for segment in manifest["segments"] for item in segment["items"]}
    bodies = {artifact["kind"]: artifact["body"] for artifact in artifacts}
    for (field, value), entry in given.items():
        if (field == "kind" and value not in bodies) or (field == "message_id" and value not in served):
            raise InvalidRequest(f"{_item_name(entry['item'])} isn't written or served by this commit")
        if "vector" in entry:
            _check_fit(entry["vector"], embedding, _item_name(entry["item"]))
        elif entry["reason"] == "empty_text":
            text = bodies[value] if field == "kind" else conn.execute(
                "SELECT text FROM messages WHERE id = ? LIMIT 1", (value,)).fetchone()[0]
            if text.strip():
                raise InvalidRequest(f"{_item_name(entry['item'])} isn't empty, so it can't be skipped as empty_text")
    owed: dict = {}
    for kind in sorted(bodies):
        if ("kind", kind) not in given:
            raise VectorsIncomplete(f"no vector or skip for kind {kind}")
        owed[("kind", kind)] = given[("kind", kind)]
    for message_id in served:
        held = _held(conn, manifest["generation"], "message", message_id)
        entry = given.get(("message_id", message_id))
        if held is None:
            if entry is None:
                raise VectorsIncomplete(f"no vector or skip for message {message_id}")
            owed[("message_id", message_id)] = entry
        elif entry is not None and _stored_form(entry, embedding) != held:
            raise VectorConflict(f"message {message_id} already has a different vector or skip in this generation")
        else:
            owed[("message_id", message_id)] = None  # held already: nothing to write
    adding = sum(1 for entry in owed.values() if entry is not None and "vector" in entry)
    stored = conn.execute("SELECT COUNT(*) FROM vectors WHERE generation = ?", (manifest["generation"],)).fetchone()[0]
    if stored + adding > config.indexer.max_vectors_per_generation:
        raise LimitExceeded(f"this generation would hold {stored + adding} vectors", "max_vectors_per_generation")
    return owed


def fits_float32(value: float) -> bool:
    """Whether a vector component can be stored as it is kept, a float32: finite and within its range."""
    if not math.isfinite(value):
        return False
    try:
        struct.pack("<f", value)
    except OverflowError:
        return False
    return True


def _check_fit(vector: list[float], embedding: dict[str, Any], name: str) -> None:
    if not all(fits_float32(x) for x in vector):
        raise InvalidRequest(f"the vector for {name} has a component that isn't a finite float32")
    if len(vector) != embedding["dim"]:
        raise InvalidRequest(f"the vector for {name} has {len(vector)} dimensions, and the profile {embedding['dim']}")
    if embedding["normalization"] == "unit" and abs(math.sqrt(sum(x * x for x in vector)) - 1) > UNIT_TOLERANCE:
        raise InvalidRequest(f"the vector for {name} isn't unit length, as the profile requires")


def _held(conn: sqlite3.Connection, generation: str, item_kind: str, item_id: str) -> tuple | None:
    vector = conn.execute("SELECT data FROM vectors WHERE generation = ? AND item_kind = ? AND item_id = ?",
                          (generation, item_kind, item_id)).fetchone()
    if vector is not None:
        return ("vector", bytes(vector[0]))
    skip = conn.execute("SELECT reason FROM vector_skips WHERE generation = ? AND item_kind = ? AND item_id = ?",
                        (generation, item_kind, item_id)).fetchone()
    return ("skip", skip[0]) if skip is not None else None


def _stored_form(entry: dict[str, Any], embedding: dict[str, Any]) -> tuple:
    if "vector" in entry:
        return ("vector", struct.pack(f"<{embedding['dim']}f", *entry["vector"]))
    return ("skip", entry["reason"])


def _write_vector(conn: sqlite3.Connection, generation: str, embedding: dict[str, Any], item_kind: str,
                  item_id: str, entry: dict[str, Any]) -> None:
    if "vector" in entry:
        conn.execute("INSERT INTO vectors (generation, item_kind, item_id, model, dim, data, created_at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)", (generation, item_kind, item_id, embedding["model"],
                                                       embedding["dim"], _stored_form(entry, embedding)[1], _now()))
    else:
        conn.execute("INSERT INTO vector_skips (generation, item_kind, item_id, reason) VALUES (?, ?, ?, ?)",
                     (generation, item_kind, item_id, entry["reason"]))


def _replay(conn: sqlite3.Connection, receipt: sqlite3.Row) -> dict[str, Any]:
    """The stored receipt with each version's state now: no batch, no revalidation, no write, no content."""
    return {"created": False, **_with_states(conn, json.loads(receipt["receipt"]))}


def _with_states(conn: sqlite3.Connection, receipt: dict[str, Any]) -> dict[str, Any]:
    return {**receipt, "versions": [{**version, "state": _state(conn, version["version_id"])}
                                    for version in receipt["versions"]]}


def _state(conn: sqlite3.Connection, version_id: str) -> str:
    """`invalid`; `behind`, valid but not covering the whole conversation; or `valid`, as things stand now."""
    row = conn.execute("SELECT version.state, version.covered_through_seq, conversation.last_seq,"
                       " generation.dropped_at FROM artifact_versions AS version"
                       " JOIN conversations AS conversation ON conversation.id = version.conversation_id"
                       " JOIN generations AS generation ON generation.id = version.generation"
                       " WHERE version.id = ?", (version_id,)).fetchone()
    # Cleanup removes only invalid versions and dropped generations' rows; a dropped one's are invalid at once.
    if row is None or row["state"] == "invalid" or row["dropped_at"] is not None:
        return "invalid"
    return "behind" if row["covered_through_seq"] < row["last_seq"] else "valid"


def _open_batch(conn: sqlite3.Connection, producer: str, batch_id: str) -> sqlite3.Row:
    batch = conn.execute("SELECT * FROM batches WHERE id = ?", (str(batch_id),)).fetchone()
    if batch is None:
        raise UnknownBatch(f"there is no batch {batch_id}")
    if batch["producer"] != producer:
        raise Forbidden(f"batch {batch_id} belongs to another producer")
    dropped = conn.execute("SELECT dropped_at FROM generations WHERE id = ?", (batch["generation"],)).fetchone()
    if batch["state"] != "open" or dropped is None or dropped[0] is not None:
        raise BatchClosed(f"batch {batch_id} is closed: committed, or its generation was dropped")
    if batch["expires_at"] <= time.time():
        raise BatchExpired(f"batch {batch_id} has expired; begin another")
    return batch


def _generation(conn: sqlite3.Connection, producer: str) -> str | None:
    row = conn.execute("SELECT id FROM generations WHERE producer = ? AND active = 1", (producer,)).fetchone()
    return row["id"] if row else None


def _current(conn: sqlite3.Connection, producer: str, generation: str | None, conversation_id: str,
             kind: str) -> sqlite3.Row | None:
    if generation is None:
        return None
    return conn.execute("SELECT * FROM artifact_versions WHERE producer = ? AND scope_id = ? AND generation = ?"
                        " AND conversation_id = ? AND kind = ? ORDER BY version DESC LIMIT 1",
                        (producer, SCOPE, generation, conversation_id, kind)).fetchone()


def _kind_plan(conn: sqlite3.Connection, producer: str, generation: str | None, conversation_id: str,
               kind: str) -> dict[str, Any]:
    """What a kind builds on: its current valid version, or nothing (a first version, or a rebase over an
    invalid one, whose number the commit's compare-and-set still names). An invalid version is never a prior."""
    current = _current(conn, producer, generation, conversation_id, kind)
    if current is None:
        return {"prior_version_id": None, "prior_version": 0, "start_seq": 1, "prior_inputs": [], "rebase": False,
                "reason": None}
    if current["state"] == "invalid":
        return {"prior_version_id": None, "prior_version": current["version"], "start_seq": 1, "prior_inputs": [],
                "rebase": True, "reason": "prior_invalid"}
    return {"prior_version_id": current["id"], "prior_version": current["version"],
            "start_seq": current["covered_through_seq"] + 1, "prior_inputs": _inputs(conn, current["id"]),
            "rebase": False, "reason": None}


def _recent_plan(conn: sqlite3.Connection, config: Config, producer: str, generation: str | None,
                 conversation_id: str, last_seq: int) -> dict[str, Any]:
    """A recent window builds on nothing and always reaches the newest message.

    It is chosen from the newest message backwards: as many of the latest messages that still have a copy as
    the window size and every bound allow. Built forwards from the oldest of them instead, a fragmented
    history would truncate the window short of the newest and commit the same old prefix every time.
    """
    current = _current(conn, producer, generation, conversation_id, RECENT)
    if current is not None and current["state"] == "valid" and current["covered_through_seq"] >= last_seq:
        raise UpToDate(f"{RECENT} already covers the latest messages")
    limits = config.indexer
    most = min(config.search.rolling_window_messages, limits.max_manifest_items)
    ranges_allowed = min(limits.max_manifest_segments, limits.max_input_segments)
    start, ranges, previous = last_seq + 1, 0, None
    for (seq,) in conn.execute("SELECT conversation_seq FROM messages WHERE conversation_id = ? GROUP BY"
                               " conversation_seq ORDER BY conversation_seq DESC LIMIT ?", (conversation_id, most)):
        joins = previous is not None and seq == previous - 1  # continues the range the newer message began
        if not joins and ranges == ranges_allowed:
            break
        ranges += 0 if joins else 1
        start = previous = seq
    return {"prior_version_id": None, "prior_version": current["version"] if current else 0, "start_seq": start,
            "prior_inputs": [], "rebase": False, "reason": None}


def _window(conn: sqlite3.Connection, config: Config, conversation_id: str, plans: dict[str, dict[str, Any]],
            last_seq: int) -> dict[str, Any]:
    """The seqs this batch covers, from the lowest checkpoint asked for, as far as the bounds allow.

    A seq with no copy left is an absent hole: covered, never served, never an input. A run of them is one
    absent range, so there are never more ranges than segments plus one, and at most `max_manifest_items` + 1
    rows are read, however long the history. The window stops before the item that would break a bound; the
    provenance bound is checked for each kind that can commit from here, and if not even one item fits,
    nothing can advance for that kind.
    """
    limits = config.indexer
    from_seq = min(plan["start_seq"] for plan in plans.values())
    if from_seq > last_seq:
        raise UpToDate("every kind asked for already covers the whole conversation")
    eligible = {kind: plan for kind, plan in plans.items() if plan["start_seq"] == from_seq}
    items: list[tuple[int, str]] = []
    ranges: list[list[int]] = []
    absent: list[list[int]] = []
    unaccounted = from_seq  # the first seq neither served nor recorded absent yet
    stop = None  # the item the window couldn't take
    for seq, message_id in _present(conn, conversation_id, from_seq, last_seq, limits.max_manifest_items + 1):
        extended = _extend(ranges, seq)
        if len(items) >= limits.max_manifest_items or len(extended) > limits.max_manifest_segments:
            stop = seq
            break
        over = [kind for kind, plan in eligible.items()
                if len(_merge(plan["prior_inputs"] + extended)) > limits.max_input_segments]
        if over:
            if not items:
                merged = _merge(eligible[over[0]]["prior_inputs"] + extended)
                raise ProvenanceLimitExceeded(
                    f"{over[0]}'s inputs can't take another range within {limits.max_input_segments}",
                    limit="max_input_segments", conversation_id=conversation_id, kind=over[0],
                    segments=len(merged), items=_count(merged))
            stop = seq
            break
        if seq > unaccounted:
            absent.append([unaccounted, seq - 1])
        items.append((seq, message_id))
        ranges = extended
        unaccounted = seq + 1
    # It covers up to the first item it couldn't take, or to the end, the holes before either included.
    to_seq = last_seq if stop is None else stop - 1
    if to_seq >= unaccounted:
        absent.append([unaccounted, to_seq])
    segments = []
    for start, end in ranges:
        run = [(seq, message_id) for seq, message_id in items if start <= seq <= end]
        segments.append({"from_seq": start, "to_seq": end, "count": len(run), "digest": hashlib.sha256(
            "".join(f"{seq}:{message_id}:{ITEM_REVISION}\n" for seq, message_id in run).encode()).hexdigest(),
            "items": [{"seq": seq, "message_id": message_id, "item_revision": ITEM_REVISION}
                      for seq, message_id in run]})
    return {"window": {"from_seq": from_seq, "to_seq": to_seq}, "segments": segments,
            "absent": [{"from_seq": start, "to_seq": end, "reason": "expired"} for start, end in absent],
            "item_count": len(items)}


def _present(conn: sqlite3.Connection, conversation_id: str, from_seq: int, to_seq: int,
             limit: int) -> list[tuple[int, str]]:
    """The first `limit` seqs in range that still have a copy, in order, each with its message id."""
    return [(row[0], row[1]) for row in conn.execute(
        "SELECT conversation_seq, MIN(id) FROM messages WHERE conversation_id = ? AND conversation_seq BETWEEN ?"
        " AND ? GROUP BY conversation_seq ORDER BY conversation_seq LIMIT ?",
        (conversation_id, from_seq, to_seq, limit))]


def _revalidate(conn: sqlite3.Connection, manifest: dict[str, Any], kinds: list[str]) -> None:
    """Everything the batch rests on still holds, or the batch is superseded, naming the first thing that failed."""
    for segment in manifest["segments"]:
        for item in segment["items"]:
            if conn.execute("SELECT 1 FROM messages WHERE id = ? AND conversation_id = ? AND conversation_seq = ?"
                            " LIMIT 1", (item["message_id"], manifest["conversation_id"], item["seq"])).fetchone() \
                    is None:
                raise BatchSuperseded(f"message {item['message_id']} at seq {item['seq']} has no copy left")
    for hole in manifest["absent"]:
        if _present(conn, manifest["conversation_id"], hole["from_seq"], hole["to_seq"], 1):
            raise BatchSuperseded(f"seqs {hole['from_seq']}-{hole['to_seq']}, absent when the batch began, now have"
                                  " a message")
    for kind in kinds:
        prior_id = manifest["kinds"][kind]["prior_version_id"]
        if prior_id is not None:
            row = conn.execute("SELECT state FROM artifact_versions WHERE id = ?", (prior_id,)).fetchone()
            if row is None or row["state"] != "valid":
                raise BatchSuperseded(f"{kind} version {prior_id}, which this batch builds on, is no longer valid")


def _check_containment(prior: list[list[int]], merged: list[list[int]]) -> None:
    """A chained version's inputs include all of its prior's, which is what lets one match invalidate a chain."""
    for start, end in prior:
        if not any(low <= start and end <= high for low, high in merged):
            raise BatchSuperseded(f"inputs {start}-{end} of the prior version would be lost from this one")


def _write_version(conn: sqlite3.Connection, manifest: dict[str, Any], artifact: dict[str, Any], version: int,
                   merged: list[list[int]], prior_id: str | None) -> str:
    version_id = "art_" + secrets.token_hex(8)
    conn.execute("INSERT INTO artifact_versions (id, producer, scope_id, generation, conversation_id, kind, version,"
                 " body, citations, covered_through_seq, source_revision, state, batch_id, created_at)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'valid', ?, ?)",
                 (version_id, manifest["producer"], SCOPE, manifest["generation"], manifest["conversation_id"],
                  artifact["kind"], version, artifact["body"], json.dumps(artifact["citations"]),
                  manifest["window"]["to_seq"], manifest["source_revision"], manifest["batch_id"], _now()))
    conn.executemany("INSERT INTO artifact_inputs (version_id, conversation_id, from_seq, to_seq) VALUES (?, ?, ?, ?)",
                     [(version_id, manifest["conversation_id"], start, end) for start, end in merged])
    if prior_id is not None:
        conn.execute("INSERT INTO artifact_depends_on (version_id, prior_id) VALUES (?, ?)", (version_id, prior_id))
    return version_id


def _cannot_advance(conn: sqlite3.Connection, config: Config, conversation_id: str,
                    current: sqlite3.Row | None) -> bool:
    """Whether the next item past a valid version's coverage can't be recorded within the provenance bound."""
    prior = _inputs(conn, current["id"]) if current is not None else []
    start = current["covered_through_seq"] + 1 if current is not None else 1
    row = conn.execute("SELECT MIN(conversation_seq) FROM messages WHERE conversation_id = ? AND conversation_seq >= ?",
                       (conversation_id, start)).fetchone()
    return row[0] is not None and len(_merge(prior + [[row[0], row[0]]])) > config.indexer.max_input_segments


def _cleanup(conn: sqlite3.Connection, config: Config) -> None:
    """Free space, a bounded amount per writer call: expired batches, aged-out receipts, invalid versions a
    newer version has replaced, and the rows of dropped generations, a generation replaced longer ago than its
    retention counting as dropped. Never needed for correctness; the latest version of a kind in a live
    generation always stays, and the active generation is never swept."""
    limit = config.indexer.cleanup_batch
    retired = (datetime.now(UTC) - timedelta(days=config.indexer.generation_retention_days)).isoformat()
    # A replaced generation ages from when it was replaced, one never activated from its last write; the active
    # one never ages, and one with an open batch still running waits for it.
    for (generation,) in conn.execute(
            "SELECT id FROM generations WHERE active = 0 AND dropped_at IS NULL"
            " AND COALESCE(deactivated_at, CASE WHEN activated_at IS NULL THEN last_write_at END) < ?"
            " AND NOT EXISTS (SELECT 1 FROM batches WHERE generation = generations.id AND state = 'open'"
            " AND expires_at > ?)", (retired, time.time())).fetchall():
        conn.execute("UPDATE generations SET dropped_at = ? WHERE id = ?", (_now(), generation))
        conn.execute("UPDATE batches SET state = 'discarded' WHERE generation = ? AND state = 'open'", (generation,))
    dropped = "(SELECT id FROM generations WHERE dropped_at IS NOT NULL)"
    for version_id in [row[0] for row in conn.execute(
            f"SELECT id FROM artifact_versions WHERE generation IN {dropped} LIMIT ?", (limit,))]:
        conn.execute("DELETE FROM artifact_inputs WHERE version_id = ?", (version_id,))
        conn.execute("DELETE FROM artifact_depends_on WHERE version_id = ?", (version_id,))
        conn.execute("DELETE FROM artifact_versions WHERE id = ?", (version_id,))
    for table in ("vectors", "vector_skips"):
        conn.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE generation IN {dropped}"
                     " LIMIT ?)", (limit,))
    for batch_id in [row[0] for row in conn.execute(f"SELECT id FROM batches WHERE generation IN {dropped} LIMIT ?",
                                                    (limit,))]:
        conn.execute("DELETE FROM batch_pages WHERE batch_id = ?", (batch_id,))
        conn.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
    # A dropped generation's own row stays, as a tombstone, so anything naming it later fails closed.
    expired = [row[0] for row in conn.execute("SELECT id FROM batches WHERE expires_at <= ? LIMIT ?",
                                              (time.time(), limit))]
    for batch_id in expired:
        conn.execute("DELETE FROM batch_pages WHERE batch_id = ?", (batch_id,))
        conn.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
    conn.execute("DELETE FROM commit_requests WHERE rowid IN (SELECT rowid FROM commit_requests WHERE expires_at <= ?"
                 " LIMIT ?)", (_now(), limit))
    replaced = [row[0] for row in conn.execute(
        "SELECT id FROM artifact_versions AS old WHERE state = 'invalid' AND EXISTS (SELECT 1 FROM artifact_versions"
        " AS newer WHERE newer.producer = old.producer AND newer.scope_id = old.scope_id AND newer.generation ="
        " old.generation AND newer.conversation_id = old.conversation_id AND newer.kind = old.kind AND"
        " newer.version > old.version) LIMIT ?", (limit,))]
    for version_id in replaced:
        for table in ("vectors", "vector_skips"):
            conn.execute(f"DELETE FROM {table} WHERE item_kind = 'artifact' AND item_id = ?", (version_id,))
        conn.execute("DELETE FROM artifact_inputs WHERE version_id = ?", (version_id,))
        conn.execute("DELETE FROM artifact_depends_on WHERE version_id = ?", (version_id,))
        conn.execute("DELETE FROM artifact_versions WHERE id = ?", (version_id,))


def _inputs(conn: sqlite3.Connection, version_id: str) -> list[list[int]]:
    return [[row[0], row[1]] for row in conn.execute(
        "SELECT from_seq, to_seq FROM artifact_inputs WHERE version_id = ? ORDER BY from_seq", (version_id,))]


def _extend(ranges: list[list[int]], seq: int) -> list[list[int]]:
    """`ranges` with `seq` added: joined to the last range when it follows it directly, else a range of its own."""
    if ranges and ranges[-1][1] == seq - 1:
        return ranges[:-1] + [[ranges[-1][0], seq]]
    return ranges + [[seq, seq]]


def _merge(ranges: list[list[int]]) -> list[list[int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _count(ranges: list[list[int]]) -> int:
    return sum(end - start + 1 for start, end in ranges)


def _within(seq: int, ranges: list[list[int]]) -> bool:
    return any(start <= seq <= end for start, end in ranges)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()
