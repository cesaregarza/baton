"""Batch issuance, delivery, commit receipts, and outstanding indexing work.

This module owns batch transactions and their validation order. It calls generation,
provenance, vector, and artifact storage operations within those transactions."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from baton.config import Config
from baton.database import Connection, Row
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
    NothingToIndex,
    ProvenanceLimitExceeded,
    UnknownBatch,
    UnknownConversation,
    VersionConflict,
)

from . import generations, provenance, storage
from . import vectors as vector_module
from .constants import KINDS, MAX_REQUEST_ID, RECENT, SCOPE


def begin_batch(conn: Connection, config: Config, producer: str, conversation_id: str,
                kinds: list[str], generation: str | None = None) -> dict[str, Any]:
    """Issue a manifest naming exactly which messages the batch reads, and what each kind builds on."""
    generations._require_producer(config, producer)
    if isinstance(kinds, list) and RECENT in kinds:
        if not config.search.rolling_window_messages:
            raise InvalidRequest(f"{RECENT} needs [search] rolling_window_messages")
        if kinds != [RECENT]:
            raise InvalidRequest(f"{RECENT} is a batch of its own")
    elif not isinstance(kinds, list) or not kinds or len(set(kinds)) != len(kinds) or not set(kinds) <= set(KINDS):
        raise InvalidRequest(f"kinds is a non-empty list of distinct kinds from {', '.join(KINDS)}")
    limits = config.indexer
    with conn.transaction():
        generations._retire_generations(conn, config)
        if generation is not None:  # after retiring, so a generation it just retired fails closed
            generation = generations._owned_generation(conn, producer, generation)["id"]
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
            generation = generations._generation(conn, producer)
        if kinds == [RECENT]:
            plans = {RECENT: provenance._recent_plan(conn, config, producer, generation, conversation_id,
                                          conversation["last_seq"])}
        else:
            plans = {kind: provenance._kind_plan(conn, producer, generation, conversation_id, kind) for kind in kinds}
        window = provenance._window(conn, config, conversation_id, plans, conversation["last_seq"])
        if generation is None:  # the first batch makes the producer's one generation; `dirty` never does
            generations._require_slot(conn, config, producer)
            generation = "gen_" + secrets.token_hex(8)
            conn.execute("INSERT INTO generations (id, producer, scope_id, profile, active, created_at, activated_at,"
                         " last_write_at) VALUES (?, ?, ?, '{}', 1, ?, ?, ?)",
                         (generation, producer, SCOPE, storage._now(), storage._now(), storage._now()))
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
                                        manifest["manifest_digest"], manifest["pages"], storage._now(),
                                        time.time() + limits.batch_ttl_seconds))
        conn.execute("UPDATE generations SET last_write_at = ? WHERE id = ?", (storage._now(), generation))
    return manifest


def read_batch(conn: Connection, config: Config, producer: str, batch_id: str, page: int) -> dict[str, Any]:
    """One page of the batch's messages, recorded as delivered: a commit needs every page delivered first."""
    generations._require_producer(config, producer)
    with conn.transaction():
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
                     (batch_id, page, storage._now()))
    return {"batch_id": batch_id, "page": page, "pages": manifest["pages"], "items": served}


def commit_batch(conn: Connection, config: Config, producer: str, batch_id: str,
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
    with conn.transaction():
        # A request_id names one commit forever, so an aged-out receipt still refuses a different commit.
        receipt = conn.execute("SELECT * FROM commit_requests WHERE producer = ? AND request_id = ?",
                               (producer, request_id)).fetchone()
        if receipt is not None:
            if receipt["digest"] != digest:
                raise IdempotencyConflict(f"request_id {request_id} was used for a different commit")
            if receipt["expires_at"] > storage._now():
                return _replay(conn, receipt)
            # Aged out: the batch this commit made is committed, so the checks below refuse it as batch_closed.
        generations._require_producer(config, producer)
        _open_batch(conn, producer, batch_id)
        generations._retire_generations(conn, config)
        batch = _open_batch(conn, producer, batch_id)  # again: retiring may have closed it
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
        provenance._revalidate(conn, manifest, [artifact["kind"] for artifact in artifacts])
        # Checked whole before anything is written: a commit either carries every vector it owes, or nothing.
        embedding = vector_module._embedding(conn, manifest["generation"])
        owed = vector_module._check_vectors(conn, config, manifest, artifacts, vectors, skips, embedding)
        window_ranges = [[segment["from_seq"], segment["to_seq"]] for segment in manifest["segments"]]
        written = []
        for artifact in artifacts:
            kind, plan = artifact["kind"], manifest["kinds"][artifact["kind"]]
            current = storage._current(conn, producer, manifest["generation"], manifest["conversation_id"], kind)
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
            prior = storage._inputs(conn, prior_id) if prior_id else []
            merged = provenance._merge(prior + window_ranges)
            provenance._check_containment(prior, merged)
            for message_id in artifact["citations"]:
                cited = conn.execute("SELECT conversation_seq FROM messages WHERE id = ? AND conversation_id = ?"
                                     " LIMIT 1", (message_id, manifest["conversation_id"])).fetchone()
                if cited is None or not provenance._within(cited[0], merged):
                    raise InvalidRequest(f"citation {message_id} is not among this {kind}'s inputs")
            if len(merged) > limits.max_input_segments:
                raise ProvenanceLimitExceeded(
                    f"{kind}'s inputs would be {len(merged)} ranges, over {limits.max_input_segments}",
                    limit="max_input_segments", conversation_id=manifest["conversation_id"], kind=kind,
                    segments=len(merged), items=provenance._count(merged))
            version_id = storage._write_version(conn, manifest, artifact, current_version + 1, merged, prior_id)
            written.append({"kind": kind, "version_id": version_id, "version": current_version + 1})
            if embedding:
                vector_module._write_vector(conn, manifest["generation"], embedding, "artifact", version_id,
                                            owed[("kind", kind)])
        for (field, value), entry in owed.items():
            if field == "message_id" and entry is not None:
                vector_module._write_vector(conn, manifest["generation"], embedding, "message", value, entry)
        conn.execute("UPDATE batches SET state = 'committed' WHERE id = ?", (batch_id,))
        conn.execute("UPDATE generations SET last_write_at = ? WHERE id = ?", (storage._now(), manifest["generation"]))
        committed_at = storage._now()
        stored = {"operation": "commit_batch", "request_id": request_id, "committed_at": committed_at,
                  "versions": written}
        expires = (datetime.fromisoformat(committed_at) + timedelta(days=limits.receipt_retention_days)).isoformat()
        conn.execute("INSERT INTO commit_requests (producer, request_id, digest, receipt, committed_at, expires_at)"
                     " VALUES (?, ?, ?, ?, ?, ?)", (producer, request_id, digest, json.dumps(stored), committed_at,
                                                    expires))
    return {"created": True, **_with_states(conn, stored)}


def dirty(conn: Connection, config: Config, producer: str, generation: str | None = None) -> dict[str, Any]:
    """Each (conversation, kind) `producer` has no version of, or whose version is behind or invalid. Read-only.

    `producer` only chooses whose set to compute, and `generation` which of its generations (the active one by
    default); neither is checked, and an unknown one simply has nothing yet. This never creates a generation,
    and a conversation with no copy left needs nothing, so it isn't listed.
    """
    generation = generation if generation is not None else generations._generation(conn, producer)
    pairs = []
    for conversation in conn.execute("SELECT id, last_seq FROM conversations WHERE EXISTS (SELECT 1 FROM messages m"
                                     " WHERE m.conversation_id = conversations.id) ORDER BY created_at, id").fetchall():
        for kind in KINDS:
            current = storage._current(conn, producer, generation, conversation["id"], kind) if generation else None
            if current is None:
                state, covered = "none", 0
            elif current["state"] == "invalid":
                state, covered = "invalid", current["covered_through_seq"]
            elif current["covered_through_seq"] < conversation["last_seq"]:
                state, covered = "behind", current["covered_through_seq"]
            else:
                continue
            stuck = state != "invalid" and provenance._cannot_advance(conn, config, conversation["id"], current)
            pairs.append({"conversation_id": conversation["id"], "kind": kind, "state": state,
                          "covered_through_seq": covered, "last_seq": conversation["last_seq"],
                          "provenance_limit_exceeded": stuck})
    return {"producer": producer, "pairs": pairs}


def _has_copies(conn: Connection, conversation_id: str) -> bool:
    return conn.execute("SELECT 1 FROM messages WHERE conversation_id = ? LIMIT 1", (conversation_id,)).fetchone() \
        is not None


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
    vectors, skips = (vector_module._vector_entries(payload.get("vectors", []), "vectors"),
                      vector_module._vector_entries(payload.get("skips", []), "skips"))
    seen = [vector_module._item_key(entry["item"]) for entry in vectors + skips]
    if len(set(seen)) != len(seen):
        raise InvalidRequest("each item has one vector or one skip, not both and not twice")
    return request_id, sorted(canonical, key=lambda artifact: artifact["kind"]), vectors, skips


def _replay(conn: Connection, receipt: Row) -> dict[str, Any]:
    """The stored receipt with each version's state now: no batch, no revalidation, no write, no content."""
    return {"created": False, **_with_states(conn, json.loads(receipt["receipt"]))}


def _with_states(conn: Connection, receipt: dict[str, Any]) -> dict[str, Any]:
    return {**receipt, "versions": [{**version, "state": storage._state(conn, version["version_id"])}
                                    for version in receipt["versions"]]}


def _open_batch(conn: Connection, producer: str, batch_id: str) -> Row:
    batch = conn.execute("SELECT * FROM batches WHERE id = ?", (str(batch_id),)).fetchone()
    if batch is None:
        raise UnknownBatch(f"there is no batch {batch_id}")
    if batch["producer"] != producer:
        raise Forbidden(f"batch {batch_id} belongs to another producer")
    dropped = conn.execute("SELECT dropped_at FROM generations WHERE id = ?", (batch["generation"],)).fetchone()
    # Checked before the expiry: retiring or dropping a generation leaves its expired batches open, so this closes them.
    if batch["state"] != "open" or dropped is None or dropped[0] is not None:
        raise BatchClosed(f"batch {batch_id} is closed: committed, or its generation was dropped")
    if batch["expires_at"] <= time.time():
        raise BatchExpired(f"batch {batch_id} has expired; begin another")
    return batch


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
