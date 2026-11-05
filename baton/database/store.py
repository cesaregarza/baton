"""SQLite database lifecycle: connections, transactions, and migrations.

The database is the host's source of truth. transaction() starts a
`BEGIN IMMEDIATE` write transaction, so concurrent writers (MCP tool calls, the local
CLI and the wake dispatcher) queue on SQLite's write lock instead of racing
between a read and the write that depends on it.

schema.py owns the base table and index declarations. migrations.py owns historical
schema upgrades. This module owns connection lifecycle and current schema initialization,
including optional search indexes.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from baton import datadir
from baton.config import ConfigError
from baton.database.files import open_private

from . import migrations as _migrations
from .driver import Connection, OperationalError
from .driver import snapshot as snapshot
from .driver import transaction as transaction
from .schema import _BINDINGS_BY_ISSUE, _SCHEMA
from .schema import _MESSAGE_REQUESTS as _MESSAGE_REQUESTS
from .schema import _MESSAGES as _MESSAGES

# Keep the historical store-level names used by existing callers and tests.
SCHEMA_VERSION = _migrations.SCHEMA_VERSION
MIGRATES_FROM = _migrations.MIGRATES_FROM
_REBUILT = _migrations._REBUILT
_ADDED_COLUMNS = _migrations._ADDED_COLUMNS
_BACKFILL = _migrations._BACKFILL
_ADDED_TABLES = _migrations._ADDED_TABLES
_discover_topics = _migrations._discover_topics
new_topic_id = _migrations.new_topic_id
_give_topic_ids = _migrations._give_topic_ids
_number_bindings = _migrations._number_bindings
_PROCEDURES = _migrations._PROCEDURES
_STEPS = _migrations._STEPS


class SchemaMismatch(ConfigError):
    code = "schema_mismatch"


def connect(path: Path) -> Connection:
    """Open the database in a checked data directory, creating the schema on first use.

    The file is created 0600 before SQLite opens it, and SQLite gives its WAL
    and shared-memory files the database's permissions. The connection uses
    autocommit between explicit transaction blocks. A current schema opens without a write
    transaction; only a new or older database runs initialization and migration.
    """
    conn = open_private(path)
    try:
        version = _refuse_other_schema(conn)
        if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        if version != SCHEMA_VERSION:
            _ensure_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def connection(path: Path, *, read_only: bool = False) -> Iterator[Connection]:
    """Own a connection's lifetime; domain operations still own their transactions."""
    conn = connect_read_only(path) if read_only else connect(path)
    try:
        yield conn
    finally:
        conn.close()


def _refuse_other_schema(conn: Connection) -> int | None:
    """Return the version, refusing unsupported schemas before even the journal mode changes."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'").fetchone():
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is not None and row["version"] != SCHEMA_VERSION and row["version"] not in MIGRATES_FROM:
            raise SchemaMismatch(
                f"the database's schema is version {row['version']}, and this baton needs "
                f"version {SCHEMA_VERSION}. baton migrates only from "
                f"{_listed(MIGRATES_FROM)}: move the old database aside to start fresh."
            )
        return row["version"] if row is not None else None
    return None


def _listed(versions: tuple[int, ...]) -> str:
    names = [str(version) for version in versions]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
    return ("version " if len(names) == 1 else "versions ") + joined


def _ensure_schema(conn: Connection) -> None:
    """Create the schema, or upgrade an older database, in one transaction: a crash leaves the old one intact."""
    with transaction(conn):
        version = _create_or_upgrade_tables(conn)
        conn.execute(_BINDINGS_BY_ISSUE)
        # A random epoch no client can already hold, written once: at creation, or at the upgrade adding the log.
        conn.execute(
            "INSERT OR IGNORE INTO log_meta (key, value) VALUES ('log_epoch', ?), ('pruned_through', '0')",
            (secrets.token_hex(16),),
        )
        _ensure_search_index(conn)
        _ensure_artifact_index(conn)
        _update_schema_version(conn, version)


def _create_or_upgrade_tables(conn: Connection) -> int | None:
    conn.execute(_SCHEMA[0])
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    version = row["version"] if row is not None else None
    if version is not None and version != SCHEMA_VERSION and version not in MIGRATES_FROM:
        raise SchemaMismatch(f"schema version {version}, expected {SCHEMA_VERSION}")  # a second line of defence
    rebuilding = version == 2
    _create_schema_tables(conn, rebuilding)
    _apply_schema_steps(conn, version)
    return version


def _update_schema_version(conn: Connection, version: int | None) -> None:
    if version is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    elif version != SCHEMA_VERSION:
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))


def _create_schema_tables(conn: Connection, rebuilding: bool) -> None:
    # Version 3 needs two message tables rebuilt because SQLite cannot change their identity constraints in place.
    if rebuilding:
        for table in _REBUILT:
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_v2")
    for statement in _SCHEMA[1:]:
        conn.execute(statement)
    if rebuilding:
        for table, columns in _REBUILT.items():
            conn.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {table}_v2")
            conn.execute(f"DROP TABLE {table}_v2")


def _apply_schema_steps(conn: Connection, version: int | None) -> None:
    # Fresh schemas already include every step; otherwise run unapplied steps in order.
    for step in _STEPS:
        if version is None or version >= step:
            continue
        _apply_added_columns(conn, step)
        for statement in _BACKFILL.get(step, ()):
            conn.execute(statement)
        for procedure in _PROCEDURES.get(step, ()):
            procedure(conn)


def _apply_added_columns(conn: Connection, step: int) -> None:
    for table, columns in _ADDED_COLUMNS.get(step, {}).items():
        # Newly created or rebuilt tables may already have these columns; inspect before altering.
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column.split()[0] not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")


def fts5_available(conn: Connection) -> bool:
    """Whether this SQLite has FTS5. Without it, search falls back to a bounded LIKE scan (baton.search)."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)")
    except OperationalError:
        return False
    conn.execute("DROP TABLE temp.fts5_probe")
    return True


def _ensure_search_index(conn: Connection) -> None:
    """The keyword index over message text: created once, filled from what is already stored, kept by triggers.

    It holds its own copy of each text with the copy's real key, (recipient, seq), and search joins on that
    key, never on rowid: `messages` has no INTEGER PRIMARY KEY, so a VACUUM may renumber rowids, and a
    rowid join could then pair one copy's text with another copy's reader. The delete trigger matches the key
    too, so a renumbered rowid can only leave an orphan row, which never joins.
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'messages_fts'").fetchone() or not fts5_available(conn):
        return
    conn.execute(
        "CREATE VIRTUAL TABLE messages_fts USING fts5(text, recipient UNINDEXED, seq UNINDEXED,"
        " tokenize = 'unicode61 remove_diacritics 2')"
    )
    conn.execute(
        "INSERT INTO messages_fts (rowid, text, recipient, seq) SELECT rowid, text, recipient, seq FROM messages"
    )
    conn.execute(
        "CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN"
        " INSERT INTO messages_fts (rowid, text, recipient, seq) VALUES (new.rowid, new.text, new.recipient,"
        " new.seq); END"
    )
    conn.execute(
        "CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN"
        " DELETE FROM messages_fts WHERE rowid = old.rowid AND recipient = old.recipient AND seq = old.seq;"
        " END"
    )


def _ensure_artifact_index(conn: Connection) -> None:
    """The keyword index over artifact bodies, kept by triggers as the messages' is. Nothing reads it yet."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'artifacts_fts'").fetchone() or not fts5_available(conn):
        return
    conn.execute(
        "CREATE VIRTUAL TABLE artifacts_fts USING fts5(body, version_id UNINDEXED,"
        " tokenize = 'unicode61 remove_diacritics 2')"
    )
    conn.execute("INSERT INTO artifacts_fts (rowid, body, version_id) SELECT rowid, body, id FROM artifact_versions")
    conn.execute(
        "CREATE TRIGGER artifacts_fts_insert AFTER INSERT ON artifact_versions BEGIN"
        " INSERT INTO artifacts_fts (rowid, body, version_id) VALUES (new.rowid, new.body, new.id); END"
    )
    conn.execute(
        "CREATE TRIGGER artifacts_fts_delete AFTER DELETE ON artifact_versions BEGIN"
        " DELETE FROM artifacts_fts WHERE rowid = old.rowid AND version_id = old.id; END"
    )


def connect_read_only(path: Path) -> Connection:
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
    conn = open_private(path, read_only=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None or row["version"] != SCHEMA_VERSION:
            raise SchemaMismatch(
                f"the database's schema is version {row['version'] if row else 'unknown'}, and this "
                f"baton reads version {SCHEMA_VERSION}: start the host once to migrate it first"
            )
    except BaseException:
        conn.close()
        raise
    return conn
