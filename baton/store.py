"""SQLite storage: schema, connections and transactions.

The database is the host's source of truth. Every operation runs in one
`BEGIN IMMEDIATE` transaction, so concurrent writers (MCP tool calls, the local
CLI and the wake dispatcher) queue on SQLite's write lock instead of racing
between a read and the write that depends on it.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from baton import datadir
from baton.config import ConfigError

SCHEMA_VERSION = 2

# One statement per entry: executescript() would commit on its own, outside
# the transaction that creates the schema.
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
    # A message's retry identity outlives its text: retention may delete the
    # message, but a retry of its request still replays from here.
    """CREATE TABLE IF NOT EXISTS message_requests (
        request_id TEXT PRIMARY KEY,
        request_digest TEXT NOT NULL,
        message_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )""",
    # `seq` numbers each recipient's messages without gaps.
    """CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        job_id TEXT REFERENCES jobs(id),
        sender TEXT NOT NULL,
        recipient TEXT NOT NULL,
        seq INTEGER NOT NULL,
        text TEXT NOT NULL,
        at TEXT NOT NULL,
        UNIQUE (recipient, seq)
    )""",
    # `last_seq` is the last seq handed out, kept here so deleting old messages
    # never renumbers; `read_seq` is the last one delivered to the recipient.
    """CREATE TABLE IF NOT EXISTS mailboxes (
        recipient TEXT PRIMARY KEY,
        last_seq INTEGER NOT NULL,
        read_seq INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL UNIQUE,
        request_digest TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        state TEXT NOT NULL,
        waiting_on TEXT,
        revision INTEGER NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    # The retry identity of each accepted move_job, and the outcome a retry replays.
    """CREATE TABLE IF NOT EXISTS job_moves (
        request_id TEXT PRIMARY KEY,
        request_digest TEXT NOT NULL,
        job_id TEXT NOT NULL REFERENCES jobs(id),
        outcome TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    # `pos` orders every event for `wait` and the wake dispatcher; (job_id, seq)
    # is the per-job sequence readers page through. A message outside any job
    # is logged with a NULL job_id and its mailbox seq.
    """CREATE TABLE IF NOT EXISTS events (
        pos INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT REFERENCES jobs(id),
        seq INTEGER NOT NULL,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        audience TEXT,
        kind TEXT NOT NULL,
        summary TEXT NOT NULL,
        data TEXT NOT NULL,
        UNIQUE (job_id, seq)
    )""",
    "CREATE INDEX IF NOT EXISTS events_by_audience ON events (audience, pos)",
    # Read positions in the event log: one per role for `wait`, one for the dispatcher.
    "CREATE TABLE IF NOT EXISTS cursors (name TEXT PRIMARY KEY, pos INTEGER NOT NULL)",
    # One row per wake owed to a role. `key` identifies what it is about, so a
    # repeated or replayed event never wakes twice.
    """CREATE TABLE IF NOT EXISTS wakes (
        role TEXT NOT NULL,
        key TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL,
        lease_until REAL NOT NULL,
        attempts INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        finished_at TEXT,
        PRIMARY KEY (role, key)
    )""",
)


class SchemaMismatch(ConfigError):
    code = "schema_mismatch"


def connect(path: Path) -> sqlite3.Connection:
    """Open the database in a checked data directory, creating the schema on first use.

    The file is created 0600 before SQLite opens it, and SQLite gives its WAL
    and shared-memory files the database's permissions. `isolation_level=None`
    turns off the sqlite3 module's implicit transactions, so `transaction()`
    alone decides where each one begins.
    """
    directory = datadir.canonical(path.parent)
    path = directory / path.name
    with datadir.opened(directory) as dir_fd:
        fd = datadir.open_file(dir_fd, path.name, os.O_RDWR | os.O_CREAT, directory=directory)
        try:
            checked_file = os.fstat(fd)
        finally:
            os.close(fd)
        checked_dir = os.fstat(dir_fd)
        conn = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
        # SQLite opened by path: refuse unless the path still names the directory and file just checked.
        if not (datadir.same_file(directory, checked_dir) and datadir.same_file(path, checked_file)):
            conn.close()
            raise datadir.UnsafeDataDir(path, "changed while it was being opened", "find what is changing it")
    conn.row_factory = sqlite3.Row
    try:
        _refuse_other_schema(conn)
    except BaseException:
        conn.close()
        raise
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_schema(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run one operation atomically, holding the write lock from the start."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _refuse_other_schema(conn: sqlite3.Connection) -> None:
    """Refuse a database made by another schema version before anything, even the journal mode, changes it."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'").fetchone():
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is not None and row["version"] != SCHEMA_VERSION:
            raise SchemaMismatch(f"the database's schema is version {row['version']}, and this baton needs "
                                 f"version {SCHEMA_VERSION}. baton doesn't migrate: move the old database aside "
                                 "to start fresh.")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        for statement in _SCHEMA:
            conn.execute(statement)
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        elif row["version"] != SCHEMA_VERSION:  # checked before any change; this is a second line of defence
            raise SchemaMismatch(f"schema version {row['version']}, expected {SCHEMA_VERSION}")
