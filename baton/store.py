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

SCHEMA_VERSION = 6
# The older versions `connect` upgrades in place, oldest first; any other is refused.
MIGRATES_FROM = (2, 3, 4, 5)

# A message's retry identity outlives its text: retention may delete the
# message, but a retry of its request still replays from here. A direct send
# records its `seq`; a conversation send records each recipient's in `seqs`.
_MESSAGE_REQUESTS = """CREATE TABLE IF NOT EXISTS message_requests (
        request_id TEXT PRIMARY KEY,
        request_digest TEXT NOT NULL,
        message_id TEXT NOT NULL,
        seq INTEGER,
        seqs TEXT,
        created_at TEXT NOT NULL
    )"""
# One row per copy: `seq` numbers each recipient's messages without gaps, and
# a conversation message's copies share its `id` and its `conversation_seq`,
# the conversation's own gap-free order. A host notice has no sender. `author_kind` (`human` or
# `agent`, NULL for a host notice) is fixed when the message is sent, from the sender role's config.
_MESSAGES = """CREATE TABLE IF NOT EXISTS messages (
        id TEXT NOT NULL,
        job_id TEXT REFERENCES jobs(id),
        conversation_id TEXT REFERENCES conversations(id),
        conversation_seq INTEGER,
        sender TEXT,
        author_kind TEXT,
        recipient TEXT NOT NULL,
        seq INTEGER NOT NULL,
        text TEXT NOT NULL,
        relayed TEXT,
        notice TEXT,
        reply_to TEXT,
        attention TEXT,
        needs_decision INTEGER NOT NULL DEFAULT 0,
        at TEXT NOT NULL,
        PRIMARY KEY (recipient, seq)
    )"""

# The version-2 tables a migration rebuilds, with the columns they keep.
_REBUILT = {
    "messages": "id, job_id, sender, recipient, seq, text, at",
    "message_requests": "request_id, request_digest, message_id, seq, created_at",
}
# What each version above 3 adds, by table, and the data each fixes up, keyed by the version that
# introduced it. `_ensure_schema` applies every step above the database's own version, in order, so one
# entry here is all a later version needs. SQLite adds columns in place, so no rebuild is involved.
_ADDED_COLUMNS = {
    # A reply's parent, the roles a message singles out, and whether it asks for a decision.
    # No release ever created a version-4 database: the work that added these columns reached a release
    # already combined with version 5's. The step stays so that a data directory left by a build of that
    # work on its own is upgraded rather than refused.
    4: {"messages": ("reply_to TEXT", "attention TEXT", "needs_decision INTEGER NOT NULL DEFAULT 0")},
    # Whether a human or an agent wrote it, fixed when it was sent.
    5: {"messages": ("author_kind TEXT",)},
    # A conversation's optional readable topic. The first step to alter another table, which is why each
    # one names its own.
    6: {"conversations": ("topic TEXT",)},
}
_BACKFILL = {
    # Before version 5 no role could be human, so every message a role sent was an agent's.
    5: ("UPDATE messages SET author_kind = 'agent' WHERE sender IS NOT NULL",),
}
_STEPS = tuple(sorted(_ADDED_COLUMNS.keys() | _BACKFILL.keys()))

# One statement per entry: executescript() would commit on its own, outside
# the transaction that creates the schema.
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
    _MESSAGE_REQUESTS,
    _MESSAGES,
    "CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages (conversation_id, conversation_seq)",
    # `last_seq` is the last seq handed out, kept here so deleting old messages
    # never renumbers; `read_seq` is the last one delivered to the recipient.
    """CREATE TABLE IF NOT EXISTS mailboxes (
        recipient TEXT PRIMARY KEY,
        last_seq INTEGER NOT NULL,
        read_seq INTEGER NOT NULL
    )""",
    # `last_seq` is the last conversation_seq handed out; `escalated_at` is when
    # the escalation role was last woken about the conversation's hourly limit.
    """CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL UNIQUE,
        request_digest TEXT NOT NULL,
        title TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seq INTEGER NOT NULL DEFAULT 0,
        escalated_at TEXT,
        topic TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS participants (
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        role TEXT NOT NULL,
        added_at TEXT NOT NULL,
        PRIMARY KEY (conversation_id, role)
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
    # The browser seat (`baton.seat`). Nothing secret is stored: a session, a pre-login form and an inbox
    # receipt are kept only as SHA-256 digests of their random values, and a session's credential only as
    # the name, digest and role that `auth.authority_live` checks again on every request.
    """CREATE TABLE IF NOT EXISTS seat_sessions (
        digest TEXT PRIMARY KEY,
        role TEXT NOT NULL,
        authority TEXT NOT NULL,
        created_at REAL NOT NULL,
        last_seen REAL NOT NULL
    )""",
    "CREATE TABLE IF NOT EXISTS seat_prelogin (digest TEXT PRIMARY KEY, expires_at REAL NOT NULL)",
    # One rendered inbox page: acknowledging it advances the cursor from `start_seq` to `last_seq`, or nothing.
    """CREATE TABLE IF NOT EXISTS seat_receipts (
        digest TEXT PRIMARY KEY,
        session TEXT NOT NULL,
        role TEXT NOT NULL,
        start_seq INTEGER NOT NULL,
        last_seq INTEGER NOT NULL,
        expires_at REAL NOT NULL
    )""",
    # A compose form's request id, bound to the session and conversation it was rendered for. `sent_at` is its
    # place in the hourly quota, held while `holders` requests for it are in flight or once it is stored.
    """CREATE TABLE IF NOT EXISTS seat_requests (
        request_id TEXT PRIMARY KEY,
        session TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        sent_at REAL,
        holders INTEGER NOT NULL DEFAULT 0
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
        if row is not None and row["version"] != SCHEMA_VERSION and row["version"] not in MIGRATES_FROM:
            raise SchemaMismatch(f"the database's schema is version {row['version']}, and this baton needs "
                                 f"version {SCHEMA_VERSION}. baton migrates only from "
                                 f"{_listed(MIGRATES_FROM)}: move the old database aside to start fresh.")


def _listed(versions: tuple[int, ...]) -> str:
    names = [str(version) for version in versions]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
    return ("version " if len(names) == 1 else "versions ") + joined


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the schema, or upgrade an older database, in one transaction: a crash leaves the old one intact."""
    with transaction(conn):
        conn.execute(_SCHEMA[0])
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        version = row["version"] if row is not None else None
        if version is not None and version != SCHEMA_VERSION and version not in MIGRATES_FROM:
            raise SchemaMismatch(f"schema version {version}, expected {SCHEMA_VERSION}")  # a second line of defence
        # Version 3 lets a conversation message's copies share one id and a send record several seqs, which
        # SQLite can't change in place: the two tables are set aside, recreated, and every row copied back.
        # A rebuild creates them from the current schema, so it needs the backfills but no column step.
        rebuilding = version == 2
        if rebuilding:
            for table in _REBUILT:
                conn.execute(f"ALTER TABLE {table} RENAME TO {table}_v2")
        for statement in _SCHEMA[1:]:
            conn.execute(statement)
        if rebuilding:
            for table, columns in _REBUILT.items():
                conn.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {table}_v2")
                conn.execute(f"DROP TABLE {table}_v2")
        # Every step above the database's own version, in order: two versions behind needs both.
        # Nothing runs for a database already at the current version, which is every ordinary open.
        for step in _STEPS:
            if version is None or version >= step:
                continue
            if not rebuilding:
                for table, columns in _ADDED_COLUMNS.get(step, {}).items():
                    for column in columns:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
            for statement in _BACKFILL.get(step, ()):
                conn.execute(statement)
        if version is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        elif version != SCHEMA_VERSION:
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
