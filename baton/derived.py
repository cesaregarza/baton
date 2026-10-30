"""Derived data from the host owner's local indexing worker: read batches, artifact versions and receipts.

The worker supplies only text, each artifact's body and the messages it cites. Everything else is Baton's: the
producer (a name listed in `[indexer] producers`), which messages a batch covers (the manifest Baton issued),
the version a write builds on, and the provenance recorded with it. A version is valid until a message it was
made from loses its last copy, which flips it to invalid in that same transaction (`invalidate_lost`); an
invalid kind is rebuilt by a rebase, a fresh first version from seq 1.

Every writer runs in one transaction and checks its producer before anything else. Nothing here logs a
change, adds an event or wakes anyone, and nothing here is authority: an artifact is a derived reading of
messages, never a decision. Roles can't reach any of it yet.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
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
    IdempotencyConflict,
    InvalidRequest,
    LimitExceeded,
    ProvenanceLimitExceeded,
    UnknownBatch,
    UnknownConversation,
    UpToDate,
    VersionConflict,
)
from baton.store import transaction

KINDS = ("summary", "decisions", "open_questions")
SCOPE = "all"  # a local producer reads the host's own database: every conversation
ITEM_REVISION = 1  # messages are immutable; a later edit would raise this
MAX_REQUEST_ID = 200


def begin_batch(conn: sqlite3.Connection, config: Config, producer: str, conversation_id: str,
                kinds: list[str]) -> dict[str, Any]:
    """Issue a manifest naming exactly which messages the batch reads, and what each kind builds on."""
    _require_producer(config, producer)
    if not isinstance(kinds, list) or not kinds or len(set(kinds)) != len(kinds) or not set(kinds) <= set(KINDS):
        raise InvalidRequest(f"kinds is a non-empty list of distinct kinds from {', '.join(KINDS)}")
    limits = config.indexer
    with transaction(conn):
        _cleanup(conn, config)
        conversation = conn.execute("SELECT last_seq, revision FROM conversations WHERE id = ?",
                                    (str(conversation_id),)).fetchone()
        if conversation is None:
            raise UnknownConversation(f"there is no conversation {conversation_id}")
        open_batches = conn.execute("SELECT COUNT(*) FROM batches WHERE producer = ? AND state = 'open'"
                                    " AND expires_at > ?", (producer, time.time())).fetchone()[0]
        if open_batches >= limits.max_open_batches:
            raise LimitExceeded(f"{producer} already has {open_batches} open batches", "max_open_batches")
        generation = _generation(conn, producer)
        plans = {kind: _kind_plan(conn, producer, generation, conversation_id, kind) for kind in kinds}
        window = _window(conn, config, conversation_id, plans, conversation["last_seq"])
        if generation is None:  # the first batch makes the producer's one generation; `dirty` never does
            generation = "gen_" + secrets.token_hex(8)
            conn.execute("INSERT INTO generations (id, producer, scope_id, profile, active, created_at)"
                         " VALUES (?, ?, ?, '{}', 1, ?)", (generation, producer, SCOPE, _now()))
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
    request_id, artifacts = _commit_request(payload)
    digest = _digest({"operation": "commit_batch", "batch_id": str(batch_id), "artifacts": artifacts})
    limits = config.indexer
    with transaction(conn):
        receipt = conn.execute("SELECT * FROM commit_requests WHERE producer = ? AND request_id = ?"
                               " AND expires_at > ?", (producer, request_id, _now())).fetchone()
        if receipt is not None:
            if receipt["digest"] != digest:
                raise IdempotencyConflict(f"request_id {request_id} was used for a different commit")
            return _replay(conn, receipt)
        _require_producer(config, producer)
        batch = _open_batch(conn, producer, batch_id)
        _cleanup(conn, config)
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
        window_ranges = [[segment["from_seq"], segment["to_seq"]] for segment in manifest["segments"]]
        written = []
        for artifact in artifacts:
            kind, plan = artifact["kind"], manifest["kinds"][artifact["kind"]]
            current = _current(conn, producer, manifest["generation"], manifest["conversation_id"], kind)
            current_version = current["version"] if current else 0
            if artifact["expected_version"] != current_version or plan["prior_version"] != current_version:
                raise VersionConflict(f"{kind} is at version {current_version}, not {artifact['expected_version']}")
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
                    segments=len(merged))
            version_id = _write_version(conn, manifest, artifact, current_version + 1, merged, prior_id)
            written.append({"kind": kind, "version_id": version_id, "version": current_version + 1})
        conn.execute("UPDATE batches SET state = 'committed' WHERE id = ?", (batch_id,))
        committed_at = _now()
        stored = {"operation": "commit_batch", "request_id": request_id, "committed_at": committed_at,
                  "versions": written}
        expires = (datetime.fromisoformat(committed_at) + timedelta(days=limits.receipt_retention_days)).isoformat()
        conn.execute("INSERT INTO commit_requests (producer, request_id, digest, receipt, committed_at, expires_at)"
                     " VALUES (?, ?, ?, ?, ?, ?)", (producer, request_id, digest, json.dumps(stored), committed_at,
                                                    expires))
    return {"created": True, "request_id": request_id, "committed_at": committed_at,
            "versions": [{**version, "state": "valid"} for version in written]}


def dirty(conn: sqlite3.Connection, config: Config, producer: str) -> dict[str, Any]:
    """Each (conversation, kind) `producer` has no version of, or whose version is behind or invalid. Read-only.

    `producer` only chooses whose set to compute; it isn't checked against the config, and an unknown name
    simply has nothing yet. This never creates the producer's generation.
    """
    generation = _generation(conn, producer)
    pairs = []
    for conversation in conn.execute("SELECT id, last_seq FROM conversations ORDER BY created_at, id").fetchall():
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


def invalidate_lost(conn: sqlite3.Connection, lost: list[tuple[str, int]]) -> None:
    """Flip every valid version made from a message that has just lost its last copy. In the caller's transaction.

    A version's inputs include its prior's (checked at commit), so one match on the seq reaches every version
    of the chain without walking it.
    """
    for conversation_id, seq in sorted(set(lost)):
        conn.execute("UPDATE artifact_versions SET state = 'invalid' WHERE state = 'valid' AND id IN"
                     " (SELECT version_id FROM artifact_inputs WHERE conversation_id = ? AND from_seq <= ?"
                     " AND to_seq >= ?)", (conversation_id, seq, seq))


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
        if not isinstance(artifact, dict) or artifact.get("kind") not in KINDS:
            raise InvalidRequest(f"each artifact has a kind from {', '.join(KINDS)}")
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
    return request_id, sorted(canonical, key=lambda artifact: artifact["kind"])


def _replay(conn: sqlite3.Connection, receipt: sqlite3.Row) -> dict[str, Any]:
    """The stored receipt with each version's state now: no batch, no revalidation, no write, no content."""
    stored = json.loads(receipt["receipt"])
    versions = []
    for version in stored["versions"]:
        row = conn.execute("SELECT state FROM artifact_versions WHERE id = ?", (version["version_id"],)).fetchone()
        versions.append({**version, "state": row["state"] if row else "invalid"})  # only invalid ones are cleaned
    return {"created": False, "request_id": stored["request_id"], "committed_at": stored["committed_at"],
            "versions": versions}


def _open_batch(conn: sqlite3.Connection, producer: str, batch_id: str) -> sqlite3.Row:
    batch = conn.execute("SELECT * FROM batches WHERE id = ?", (str(batch_id),)).fetchone()
    if batch is None:
        raise UnknownBatch(f"there is no batch {batch_id}")
    if batch["producer"] != producer:
        raise Forbidden(f"batch {batch_id} belongs to another producer")
    if batch["state"] != "open":
        raise BatchClosed(f"batch {batch_id} is already committed")
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


def _window(conn: sqlite3.Connection, config: Config, conversation_id: str, plans: dict[str, dict[str, Any]],
            last_seq: int) -> dict[str, Any]:
    """The seqs this batch covers, from the lowest checkpoint asked for, as far as the bounds allow.

    A seq with no copy left is an absent hole: covered, never served, never an input. The window stops before
    the item that would break a bound; the provenance bound is checked for each kind that can commit from
    here, and if not even one item fits, nothing can advance for that kind.
    """
    limits = config.indexer
    from_seq = min(plan["start_seq"] for plan in plans.values())
    if from_seq > last_seq:
        raise UpToDate("every kind asked for already covers the whole conversation")
    eligible = {kind: plan for kind, plan in plans.items() if plan["start_seq"] == from_seq}
    present = _present(conn, conversation_id, from_seq, last_seq)
    items: list[tuple[int, str]] = []
    ranges: list[list[int]] = []
    absent: list[int] = []
    pending: list[int] = []
    seq = from_seq
    for seq in range(from_seq, last_seq + 1):
        message_id = present.get(seq)
        if message_id is None:
            pending.append(seq)
            continue
        extended = _extend(ranges, seq)
        if len(items) >= limits.max_manifest_items or len(extended) > limits.max_manifest_segments:
            break
        over = [kind for kind, plan in eligible.items()
                if len(_merge(plan["prior_inputs"] + extended)) > limits.max_input_segments]
        if over:
            if not items:
                raise ProvenanceLimitExceeded(
                    f"{over[0]}'s inputs can't take another range within {limits.max_input_segments}",
                    limit="max_input_segments", conversation_id=conversation_id, kind=over[0],
                    segments=len(_merge(eligible[over[0]]["prior_inputs"] + extended)))
            break
        absent += pending
        pending = []
        items.append((seq, message_id))
        ranges = extended
    else:
        seq = last_seq + 1
    # It covers up to the first item it couldn't take, the holes before that item included.
    absent += pending
    to_seq = seq - 1
    segments = []
    for start, end in ranges:
        run = [(seq, message_id) for seq, message_id in items if start <= seq <= end]
        segments.append({"from_seq": start, "to_seq": end, "count": len(run), "digest": hashlib.sha256(
            "".join(f"{seq}:{message_id}:{ITEM_REVISION}\n" for seq, message_id in run).encode()).hexdigest(),
            "items": [{"seq": seq, "message_id": message_id, "item_revision": ITEM_REVISION}
                      for seq, message_id in run]})
    return {"window": {"from_seq": from_seq, "to_seq": to_seq}, "segments": segments,
            "absent": [{"seq": seq, "reason": "expired"} for seq in absent], "item_count": len(items)}


def _present(conn: sqlite3.Connection, conversation_id: str, from_seq: int, to_seq: int) -> dict[int, str]:
    """Conversation seq to message id, for each seq in range that still has a copy."""
    return {row[0]: row[1] for row in conn.execute(
        "SELECT conversation_seq, MIN(id) FROM messages WHERE conversation_id = ? AND conversation_seq BETWEEN ?"
        " AND ? GROUP BY conversation_seq", (conversation_id, from_seq, to_seq))}


def _revalidate(conn: sqlite3.Connection, manifest: dict[str, Any], kinds: list[str]) -> None:
    """Everything the batch rests on still holds, or the batch is superseded, naming the first thing that failed."""
    for segment in manifest["segments"]:
        for item in segment["items"]:
            if conn.execute("SELECT 1 FROM messages WHERE id = ? AND conversation_id = ? AND conversation_seq = ?"
                            " LIMIT 1", (item["message_id"], manifest["conversation_id"], item["seq"])).fetchone() \
                    is None:
                raise BatchSuperseded(f"message {item['message_id']} at seq {item['seq']} has no copy left")
    for hole in manifest["absent"]:
        if _present(conn, manifest["conversation_id"], hole["seq"], hole["seq"]):
            raise BatchSuperseded(f"seq {hole['seq']}, absent when the batch began, now has a message")
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
    """Free space, a bounded amount per writer call: expired batches, aged-out receipts, and invalid versions a
    newer version has replaced. Never needed for correctness; the latest version of a kind always stays."""
    limit = config.indexer.cleanup_batch
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


def _within(seq: int, ranges: list[list[int]]) -> bool:
    return any(start <= seq <= end for start, end in ranges)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()
