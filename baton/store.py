"""SQLite storage: schema, connections and transactions.

The database is the host's source of truth. Every operation runs in one
`BEGIN IMMEDIATE` transaction, so concurrent writers (MCP tool calls, the local
CLI and the wake dispatcher) queue on SQLite's write lock instead of racing
between a read and the write that depends on it.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from baton import datadir
from baton.config import ConfigError

SCHEMA_VERSION = 9
# The older versions `connect` upgrades in place, oldest first; any other is refused.
MIGRATES_FROM = (2, 3, 4, 5, 6, 7, 8)

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
    # What a reminder wake needs and an ordinary wake leaves NULL: when it becomes eligible, the read
    # position the batch had when its original wake was delivered, which of the two it is, and when it
    # first took a cap slot. Added, never rewritten, so an existing wake comes through untouched.
    7: {"wakes": ("due_at REAL", "baseline_seq INTEGER", "ordinal INTEGER", "claimed_at REAL")},
    # Each conversation's source revision, bumped with every change the change log records, and when it
    # last had a message.
    9: {"conversations": ("revision INTEGER NOT NULL DEFAULT 0", "last_activity_at TEXT")},
}
_BACKFILL = {
    # Before version 5 no role could be human, so every message a role sent was an agent's.
    5: ("UPDATE messages SET author_kind = 'agent' WHERE sender IS NOT NULL",),
    # Revisions start at 0 for every existing conversation; activity is the newest message still held.
    9: ("UPDATE conversations SET last_activity_at = COALESCE((SELECT MAX(at) FROM messages m"
        " WHERE m.conversation_id = conversations.id), created_at)",),
}
# Tables a version adds whole. `_SCHEMA` creates them (IF NOT EXISTS) on every open, so an upgrade needs no
# statement of its own for them; naming them here makes the version a step, and the tests check each exists.
_ADDED_TABLES = {
    # The browser seat (`baton.seat`).
    8: ("seat_sessions", "seat_prelogin", "seat_receipts", "seat_requests"),
    # The conversation change log and its epoch and retention floor (`baton.changes`). The search index is
    # not listed: it exists only where SQLite has FTS5.
    9: ("change_log", "log_meta"),
}
_STEPS = tuple(sorted(_ADDED_COLUMNS.keys() | _BACKFILL.keys() | _ADDED_TABLES.keys()))

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
        topic TEXT,
        revision INTEGER NOT NULL DEFAULT 0,
        last_activity_at TEXT
    )""",
    # Every change to a conversation's source, in order: a message added, a copy expired by retention, a
    # participant added or removed. Each carries the revision it bumped the conversation to. Only source
    # changes are logged; reading logs nothing. See baton.changes.
    """CREATE TABLE IF NOT EXISTS change_log (
        change_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        kind TEXT NOT NULL,
        message_id TEXT,
        role TEXT,
        at TEXT NOT NULL
    )""",
    # The log's identity (`log_epoch`) and how far retention has pruned it (`pruned_through`).
    "CREATE TABLE IF NOT EXISTS log_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
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
        due_at REAL,
        baseline_seq INTEGER,
        ordinal INTEGER,
        claimed_at REAL,
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
            for table, columns in _ADDED_COLUMNS.get(step, {}).items():
                # Add only what is missing. Several paths reach this point: a table the version-3
                # rebuild recreated, or one the statements above created for the first time, already
                # has every column of the current schema, while a table carried over from an older
                # database has none of them. Asking the table is simpler, and safer, than tracking
                # which of those happened.
                present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                for column in columns:
                    if column.split()[0] not in present:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
            for statement in _BACKFILL.get(step, ()):
                conn.execute(statement)
        # A random epoch no client can already hold, written once: at creation, or at the upgrade adding the log.
        conn.execute("INSERT OR IGNORE INTO log_meta (key, value) VALUES ('log_epoch', ?), ('pruned_through', '0')",
                     (secrets.token_hex(16),))
        _ensure_search_index(conn)
        if version is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        elif version != SCHEMA_VERSION:
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))


def fts5_available(conn: sqlite3.Connection) -> bool:
    """Whether this SQLite has FTS5. Without it, search falls back to a bounded LIKE scan (baton.search)."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    conn.execute("DROP TABLE temp.fts5_probe")
    return True


def _ensure_search_index(conn: sqlite3.Connection) -> None:
    """The keyword index over message text: created once, filled from what is already stored, kept by triggers.

    It holds its own copy of each text with the copy's real key, (recipient, seq), and search joins on that
    key, never on rowid: `messages` has no INTEGER PRIMARY KEY, so a VACUUM may renumber rowids, and a
    rowid join could then pair one copy's text with another copy's reader. The delete trigger matches the key
    too, so a renumbered rowid can only leave an orphan row, which never joins.
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'messages_fts'").fetchone() or not fts5_available(conn):
        return
    conn.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(text, recipient UNINDEXED, seq UNINDEXED,"
                 " tokenize = 'unicode61 remove_diacritics 2')")
    conn.execute("INSERT INTO messages_fts (rowid, text, recipient, seq)"
                 " SELECT rowid, text, recipient, seq FROM messages")
    conn.execute("CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN"
                 " INSERT INTO messages_fts (rowid, text, recipient, seq) VALUES (new.rowid, new.text, new.recipient,"
                 " new.seq); END")
    conn.execute("CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN"
                 " DELETE FROM messages_fts WHERE rowid = old.rowid AND recipient = old.recipient AND seq = old.seq;"
                 " END")


def connect_read_only(path: Path) -> sqlite3.Connection:
    """Open the database for reading only, as the local `baton index` commands do.

    Read-only twice over: the file is opened `mode=ro`, and `PRAGMA query_only` refuses any write the
    connection is asked for. It never creates or upgrades the schema, so a database another version made is
    refused: start the host once to migrate it.
    """
    directory = datadir.canonical(path.parent)
    path = directory / path.name
    with datadir.opened(directory):
        if not path.exists():
            raise ConfigError(f"no database at {path} yet; start the host once to create it")
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, isolation_level=None, timeout=30,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None or row["version"] != SCHEMA_VERSION:
        conn.close()
        raise SchemaMismatch(f"the database's schema is version {row['version'] if row else 'unknown'}, and this "
                             f"baton reads version {SCHEMA_VERSION}: start the host once to migrate it first")
    return conn
