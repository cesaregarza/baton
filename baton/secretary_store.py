"""Private, append-only attention records, separate from Baton's authoritative database."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from baton import datadir
from baton.config import ConfigError
from baton.store import transaction

SCHEMA_VERSION = 1

SCHEMA = (
    "CREATE TABLE schema_version (version INTEGER NOT NULL)",
    "CREATE TABLE sources (principal TEXT PRIMARY KEY, log_epoch TEXT NOT NULL, through_pos INTEGER NOT NULL)",
    """CREATE TABLE items (
        principal TEXT NOT NULL, seq INTEGER NOT NULL, source_key TEXT NOT NULL,
        event_pos INTEGER, envelope_digest TEXT NOT NULL, envelope TEXT NOT NULL,
        PRIMARY KEY(principal, source_key), UNIQUE(principal, seq))""",
    """CREATE TABLE expectations (
        principal TEXT NOT NULL, id TEXT NOT NULL, direction TEXT NOT NULL,
        requester TEXT, responder TEXT NOT NULL, task_ref TEXT, artifact_key TEXT, artifact_rev TEXT,
        expected_kind TEXT NOT NULL, opening_source TEXT NOT NULL, opening_message_id TEXT, state TEXT NOT NULL,
        closed_source TEXT, PRIMARY KEY(principal, id))""",
    """CREATE TABLE expectation_evidence (
        principal TEXT NOT NULL, expectation_id TEXT NOT NULL, source_key TEXT NOT NULL,
        kind TEXT NOT NULL, rule_id TEXT NOT NULL, artifact_key TEXT, artifact_rev TEXT,
        observed_kind TEXT,
        PRIMARY KEY(principal, expectation_id, source_key, kind))""",
    """CREATE TABLE dispositions (
        principal TEXT NOT NULL, source_key TEXT NOT NULL, policy_digest TEXT NOT NULL,
        kind TEXT NOT NULL, reason TEXT NOT NULL, rules_version INTEGER NOT NULL,
        envelope_version INTEGER NOT NULL, brief_work TEXT, brief_version INTEGER,
        PRIMARY KEY(principal, source_key, policy_digest))""",
    """CREATE TABLE briefs (
        principal TEXT NOT NULL, work_key TEXT NOT NULL, version INTEGER NOT NULL,
        content TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(principal, work_key, version))""",
    "CREATE INDEX items_by_principal_pos ON items(principal, event_pos)",
    "CREATE INDEX evidence_by_source ON expectation_evidence(principal, source_key)",
)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _init(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables:
        if "schema_version" not in tables or not {"sources", "items", "expectations", "expectation_evidence",
                                                   "dispositions", "briefs"} <= tables:
            raise ConfigError("secretary state path contains another or incomplete database")
        version = conn.execute("SELECT version FROM schema_version").fetchone()
        if version is None or version["version"] != SCHEMA_VERSION:
            raise ConfigError("unsupported secretary state schema version")
        required = {"expectations": "expected_kind", "expectation_evidence": "observed_kind"}
        if any(column not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
               for table, column in required.items()):
            raise ConfigError("secretary state schema is incomplete")
    conn.execute("PRAGMA foreign_keys=ON")
    if not tables:
        with transaction(conn):
            for statement in SCHEMA:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))


def memory() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    _init(conn)
    return conn


def open_state(path: Path, data_dir: Path) -> sqlite3.Connection:
    """Open a 0600 sidecar like Baton opens its own file, without migrating anything."""
    path = datadir.canonical(path)
    data_dir = datadir.canonical(data_dir)
    if data_dir in path.parents and path.parent != data_dir:
        raise ConfigError("secretary state inside data_dir must be a direct child file")
    with datadir.opened(path.parent) as dir_fd:
        fd = datadir.open_file(dir_fd, path.name, os.O_RDWR | os.O_CREAT, directory=path.parent)
        try:
            file_info = os.fstat(fd)
        finally:
            os.close(fd)
        dir_info = os.fstat(dir_fd)
        conn = sqlite3.connect(path, isolation_level=None, timeout=30)
        if not (datadir.same_file(path, file_info) and datadir.same_file(path.parent, dir_info)):
            conn.close()
            raise ConfigError("secretary state path changed while opening")
    try:
        _init(conn)
        conn.execute("PRAGMA journal_mode=WAL")
    except BaseException:
        conn.close()
        raise
    return conn


def source_position(conn: sqlite3.Connection, principal: str, epoch: str) -> int:
    row = conn.execute("SELECT log_epoch, through_pos FROM sources WHERE principal=?", (principal,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO sources VALUES (?, ?, 0)", (principal, epoch))
        return 0
    if row["log_epoch"] != epoch:
        raise ConfigError("secretary state log_epoch differs from Baton")
    return row["through_pos"]


def set_position(conn: sqlite3.Connection, principal: str, pos: int) -> None:
    conn.execute("UPDATE sources SET through_pos=? WHERE principal=? AND through_pos <= ?", (pos, principal, pos))


def put_item(conn: sqlite3.Connection, principal: str, envelope: dict) -> bool:
    key = envelope["source_key"]
    before = conn.execute("SELECT envelope_digest FROM items WHERE principal=? AND source_key=?",
                          (principal, key)).fetchone()
    fingerprint = digest(envelope)
    if before is not None:
        # A conversation message has one ID but several recipient copies and event positions.
        if not key.startswith("message:") and before["envelope_digest"] != fingerprint:
            raise ConfigError("source identity now has different metadata")
        return False
    order = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM items WHERE principal=?", (principal,)).fetchone()[0]
    conn.execute("INSERT INTO items (principal, seq, source_key, event_pos, envelope_digest, envelope)"
                 " VALUES (?, ?, ?, ?, ?, ?)", (principal, order, key, envelope.get("event_pos"), fingerprint,
                                                canonical(envelope)))
    return True


def add_evidence(conn: sqlite3.Connection, principal: str, expectation_id: str, source_key: str,
                 kind: str, rule_id: str, artifact: tuple[str | None, str | None],
                 observed_kind: str | None = None) -> None:
    conn.execute("INSERT OR IGNORE INTO expectation_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (principal, expectation_id, source_key, kind, rule_id, *artifact, observed_kind))


def export(conn: sqlite3.Connection, principal: str) -> dict:
    """A deterministic export for restart, replay, and isolation comparisons."""
    result = {}
    for table, order in (("sources", "principal"), ("items", "seq"), ("expectations", "id"),
                         ("expectation_evidence", "expectation_id, source_key, kind"),
                         ("dispositions", "source_key, policy_digest"), ("briefs", "work_key, version")):
        result[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table} WHERE principal=? ORDER BY {order}",
                                                    (principal,))]
    return result


def export_digest(conn: sqlite3.Connection, principal: str) -> str:
    return digest(export(conn, principal))
