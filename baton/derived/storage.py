"""Shared artifact-version lookups, persistence, and SQL timestamps.

These operations use the caller's connection and transaction; they do not start or commit one."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Any

from baton.database import Connection, Row

from .constants import SCOPE


def _current(conn: Connection, producer: str, generation: str | None, conversation_id: str, kind: str) -> Row | None:
    if generation is None:
        return None
    return conn.execute(
        "SELECT * FROM artifact_versions WHERE producer = ? AND scope_id = ? AND generation = ?"
        " AND conversation_id = ? AND kind = ? ORDER BY version DESC LIMIT 1",
        (producer, SCOPE, generation, conversation_id, kind),
    ).fetchone()


def _inputs(conn: Connection, version_id: str) -> list[list[int]]:
    return [
        [row[0], row[1]]
        for row in conn.execute(
            "SELECT from_seq, to_seq FROM artifact_inputs WHERE version_id = ? ORDER BY from_seq", (version_id,)
        )
    ]


def _state(conn: Connection, version_id: str) -> str:
    """`invalid`; `behind`, valid but not covering the whole conversation; or `valid`, as things stand now."""
    row = conn.execute(
        "SELECT version.state, version.covered_through_seq, conversation.last_seq,"
        " generation.dropped_at FROM artifact_versions AS version"
        " JOIN conversations AS conversation ON conversation.id = version.conversation_id"
        " JOIN generations AS generation ON generation.id = version.generation"
        " WHERE version.id = ?",
        (version_id,),
    ).fetchone()
    # A dropped generation's versions are invalid at once.
    if row is None or row["state"] == "invalid" or row["dropped_at"] is not None:
        return "invalid"
    return "behind" if row["covered_through_seq"] < row["last_seq"] else "valid"


def _write_version(
    conn: Connection,
    manifest: dict[str, Any],
    artifact: dict[str, Any],
    version: int,
    merged: list[list[int]],
    prior_id: str | None,
) -> str:
    version_id = "art_" + secrets.token_hex(8)
    conn.execute(
        "INSERT INTO artifact_versions (id, producer, scope_id, generation, conversation_id, kind, version,"
        " body, citations, covered_through_seq, source_revision, state, batch_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'valid', ?, ?)",
        (
            version_id,
            manifest["producer"],
            SCOPE,
            manifest["generation"],
            manifest["conversation_id"],
            artifact["kind"],
            version,
            artifact["body"],
            json.dumps(artifact["citations"]),
            manifest["window"]["to_seq"],
            manifest["source_revision"],
            manifest["batch_id"],
            _now(),
        ),
    )
    conn.executemany(
        "INSERT INTO artifact_inputs (version_id, conversation_id, from_seq, to_seq) VALUES (?, ?, ?, ?)",
        [(version_id, manifest["conversation_id"], start, end) for start, end in merged],
    )
    if prior_id is not None:
        conn.execute("INSERT INTO artifact_depends_on (version_id, prior_id) VALUES (?, ?)", (version_id, prior_id))
    if "graph_snapshot" in artifact:  # validated by the commit before this write; stored in the same transaction
        snapshot = artifact["graph_snapshot"]
        conn.execute(
            "INSERT INTO graph_snapshots (version_id, conversation_id, covered_through_seq, graph_digest, revision,"
            " snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                version_id,
                snapshot["conversation_id"],
                snapshot["covered_through_seq"],
                snapshot["graph_digest"],
                snapshot["revision"],
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                _now(),
            ),
        )
    return version_id


def _now() -> str:
    return datetime.now(UTC).isoformat()
