"""SQLite database lifecycle: connections, transactions, and migrations.

The database is the host's source of truth. transaction() starts a
`BEGIN IMMEDIATE` write transaction, so concurrent writers (MCP tool calls, the local
CLI and the wake dispatcher) queue on SQLite's write lock instead of racing
between a read and the write that depends on it.

schema.py owns the base table and index declarations. This module owns version policy,
migration steps, and runtime operations, including optional search indexes.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from baton import datadir
from baton.config import ConfigError

from .schema import _MESSAGE_REQUESTS as _MESSAGE_REQUESTS
from .schema import _MESSAGES as _MESSAGES
from .schema import _SCHEMA

SCHEMA_VERSION = 19
# The older versions `connect` upgrades in place, oldest first; any other is refused.
MIGRATES_FROM = (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18)


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
    # A generation's watermark, when it was switched to and from, and whether its owner dropped it.
    11: {"generations": ("watermark_epoch TEXT", "watermark_seq INTEGER", "activated_at TEXT", "deactivated_at TEXT",
                         "dropped_at TEXT", "last_write_at TEXT")},
    13: {"messages": ("alias_id TEXT REFERENCES aliases(id)", "alias TEXT", "alias_code TEXT")},
    # Who started a guest thread, recorded the way a guest post is attributed. A thread a role started
    # leaves all three NULL.
    15: {"conversations": ("created_by_alias_id TEXT REFERENCES aliases(id)", "created_by_alias TEXT",
                           "created_by_alias_code TEXT")},
    # A topic's stable id, beside its path. SQLite cannot add a UNIQUE or NOT NULL column in place, so the
    # column is added bare here, and `_give_topic_ids` gives every existing topic its own id and the index.
    16: {"topics": ("id TEXT",)},
    # When and how a seat row ended. A row that ends is kept, and every check refuses it by this state.
    19: {table: ("ended_at REAL", "end_reason TEXT")
         for table in ("seat_sessions", "seat_prelogin", "seat_receipts", "seat_requests")},
}
_BACKFILL = {
    # Before version 5 no role could be human, so every message a role sent was an agent's.
    5: ("UPDATE messages SET author_kind = 'agent' WHERE sender IS NOT NULL",),
    # Revisions start at 0 for every existing conversation; activity is the newest message still held.
    9: ("UPDATE conversations SET last_activity_at = COALESCE((SELECT MAX(at) FROM messages m"
        " WHERE m.conversation_id = conversations.id), created_at)",),
    # A version-10 producer's one generation was active from creation.
    11: ("UPDATE generations SET activated_at = created_at WHERE active = 1 AND activated_at IS NULL",
         "UPDATE generations SET last_write_at = created_at WHERE last_write_at IS NULL"),
}
# Tables a version adds whole. `_SCHEMA` creates them (IF NOT EXISTS) on every open, so an upgrade needs no
# statement of its own for them; naming them here makes the version a step, and the tests check each exists.
_ADDED_TABLES = {
    # The browser seat (`baton.seat`).
    8: ("seat_sessions", "seat_prelogin", "seat_receipts", "seat_requests"),
    # The conversation change log, with its epoch and the floor an earlier prune left (`baton.changes`). The
    # search index is not listed: it exists only where SQLite has FTS5.
    9: ("change_log", "log_meta"),
    # The local indexing writer's derived data (`baton.derived`). Its keyword index is not listed, for the
    # same reason as the messages'.
    10: ("generations", "batches", "batch_pages", "artifact_versions", "artifact_inputs", "artifact_depends_on",
         "commit_requests"),
    # Vectors a producer supplies for a generation's artifacts and messages, and the items it skipped.
    11: ("vectors", "vector_skips"),
    12: ("aliases", "alias_bindings", "alias_claims"),
    13: ("alias_posts",),
    # Per-call identity (`baton.aliases`): owner arms, core enrolment and the current alias-to-role mapping.
    14: ("alias_arms", "core_enrolment", "alias_roles", "events_targets"),
    # Topics as containers (`baton.topics`).
    15: ("topics",),
    # Every change a topic undergoes, one row each (`baton.topics`).
    16: ("topic_changes",),
    # The owner's topic grant records and their event log (`baton.topic_grants`).
    17: ("topic_grants", "topic_grant_events"),
    # A topic's reference documents and every revision of them (`baton.references`).
    18: ("reference_docs", "reference_revisions"),
    # Records kept where something used to be overwritten or expired: each alias rekey and each MCP Events secret
    # rotation (neither with a secret), and each MCP Events wake's occurrence, outcomes and delivery receipts.
    19: ("alias_rekeys", "events_rotations", "events_occurrences", "events_outcomes", "events_receipts"),
}


def _discover_topics(conn: sqlite3.Connection) -> None:
    """Record a topic for every label the conversations already carry, and for each missing parent.

    Data-driven rather than a fixed list, so a database holding labels this release never saw still
    upgrades. A stored label that fails the topic grammar stops the upgrade and names itself: dropping
    it would leave its threads pointing at a topic that does not exist, and the whole upgrade is one
    transaction, so refusing here leaves the old database exactly as it was.

    Members come from the threads filed directly under a path, never from a subtopic's, so a parent
    never gains a recipient who was not already in one of its own threads. A membership too small to
    receive a guest thread is stored empty instead, for a core role to set with `update_topic`; the
    checks that need the config -- a role it no longer has, or more members than it allows -- belong
    to admission, which can see it, and the schema stays independent of any one host's config.
    """
    from baton import conversations, events  # lazily: both import this module

    stored = [row[0] for row in conn.execute(
        "SELECT DISTINCT topic FROM conversations WHERE topic IS NOT NULL ORDER BY topic")]
    for label in stored:
        conversations.check_topic(label, field="the stored conversation topic")
    stamp = events.now()
    for path in sorted({part for label in stored for part in (label, label.split("/")[0])}):
        members = [row[0] for row in conn.execute(
            "SELECT DISTINCT p.role FROM participants p JOIN conversations c ON c.id = p.conversation_id"
            " WHERE c.topic = ? AND p.role != 'guest' ORDER BY p.role", (path,))]
        if len(members) < conversations.MIN_PARTICIPANTS:
            members = []
        conn.execute("INSERT OR IGNORE INTO topics (id, path, title, members, created_by, created_at)"
                     " VALUES (?, ?, ?, ?, 'migration', ?)",
                     (new_topic_id(), path, path.split("/")[-1], json.dumps(members), stamp))


def new_topic_id() -> str:
    """A topic's id. Random, never derived from the path: a path a rename frees and a new topic reuses gets a new id."""
    return "topic_" + secrets.token_hex(8)


def _give_topic_ids(conn: sqlite3.Connection) -> None:
    """Give every topic that has no id its own, then make the id unique for every topic from here on.

    Each id is drawn in code, so no two topics can share one even on a database that began without the
    column. A fresh database declares the column unique and never runs this.
    """
    for (path,) in conn.execute("SELECT path FROM topics WHERE id IS NULL").fetchall():
        conn.execute("UPDATE topics SET id = ? WHERE path = ?", (new_topic_id(), path))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS topics_by_id ON topics (id)")


# Steps a plain statement cannot express, because they read the data and may refuse the upgrade.
_PROCEDURES: dict[int, tuple[Callable[[sqlite3.Connection], None], ...]] = {
    15: (_discover_topics,),
    16: (_give_topic_ids,),
}
_STEPS = tuple(sorted(_ADDED_COLUMNS.keys() | _BACKFILL.keys() | _ADDED_TABLES.keys() | _PROCEDURES.keys()))


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


@contextmanager
def snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """One read transaction, so every statement inside sees the same state of the database."""
    if conn.in_transaction:  # the caller's transaction is already one snapshot
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
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
            for procedure in _PROCEDURES.get(step, ()):
                procedure(conn)
        # A random epoch no client can already hold, written once: at creation, or at the upgrade adding the log.
        conn.execute("INSERT OR IGNORE INTO log_meta (key, value) VALUES ('log_epoch', ?), ('pruned_through', '0')",
                     (secrets.token_hex(16),))
        _ensure_search_index(conn)
        _ensure_artifact_index(conn)
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


def _ensure_artifact_index(conn: sqlite3.Connection) -> None:
    """The keyword index over artifact bodies, kept by triggers as the messages' is. Nothing reads it yet."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'artifacts_fts'").fetchone() or not fts5_available(conn):
        return
    conn.execute("CREATE VIRTUAL TABLE artifacts_fts USING fts5(body, version_id UNINDEXED,"
                 " tokenize = 'unicode61 remove_diacritics 2')")
    conn.execute("INSERT INTO artifacts_fts (rowid, body, version_id) SELECT rowid, body, id FROM artifact_versions")
    conn.execute("CREATE TRIGGER artifacts_fts_insert AFTER INSERT ON artifact_versions BEGIN"
                 " INSERT INTO artifacts_fts (rowid, body, version_id) VALUES (new.rowid, new.body, new.id); END")
    conn.execute("CREATE TRIGGER artifacts_fts_delete AFTER DELETE ON artifact_versions BEGIN"
                 " DELETE FROM artifacts_fts WHERE rowid = old.rowid AND version_id = old.id; END")


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
