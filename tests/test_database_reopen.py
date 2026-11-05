"""Database reopen behavior for optional indexes and reusable SQLite engines."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from baton.database import driver, store


def test_current_schema_gains_search_indexes_when_fts5_becomes_available(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "baton.db"
    real_probe = store.fts5_available
    monkeypatch.setattr(store, "fts5_available", lambda conn: False)
    with store.connect(path) as conn:
        conn.execute(
            "INSERT INTO messages (id, recipient, seq, text, at) VALUES ('m1', 'worker', 1, 'findable phrase', 'now')"
        )
        conn.execute(
            "INSERT INTO artifact_versions (id, producer, scope_id, generation, conversation_id, kind, version, body,"
            " citations, covered_through_seq, source_revision, state, batch_id, created_at)"
            " VALUES ('a1', 'planner', 'scope', 'g1', 'c1', 'summary', 1, 'artifact phrase', '[]', 1, 1,"
            " 'valid', 'b1', 'now')"
        )

    monkeypatch.setattr(store, "fts5_available", real_probe)
    with store.connect(path) as conn:
        assert conn.execute("SELECT text FROM messages_fts WHERE messages_fts MATCH 'findable'").one()[0] == (
            "findable phrase"
        )
        assert conn.execute("SELECT body FROM artifacts_fts WHERE artifacts_fts MATCH 'artifact'").one()[0] == (
            "artifact phrase"
        )


def test_reopens_reuse_engine_but_own_separate_connections_and_rollback(tmp_path: Path) -> None:
    path = tmp_path / "baton.db"
    with store.connect(path):
        pass

    first = store.connect(path)
    second = store.connect(path)
    assert first._engine is second._engine
    assert first._driver_connection() is not second._driver_connection()
    first.execute("BEGIN IMMEDIATE")
    first.execute(
        "INSERT INTO messages (id, recipient, seq, text, at) VALUES ('m1', 'worker', 1, 'rolled back', 'now')"
    )
    first.close()
    assert second.execute("SELECT count(*) FROM messages").one()[0] == 0
    second.close()


def test_read_only_and_memory_engines_are_not_shared(tmp_path: Path) -> None:
    path = tmp_path / "baton.db"
    with store.connect(path):
        pass
    writable = store.connect(path)
    readonly = store.connect_read_only(path)
    assert writable._engine is not readonly._engine
    writable.close()
    readonly.close()

    first, second = driver.memory(), driver.memory()
    assert first._engine is not second._engine
    first.execute("CREATE TABLE isolated (value TEXT)")
    assert not second.table_exists("isolated")
    raw_connection = first._driver_connection()
    first.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        raw_connection.execute("SELECT 1")
    second.close()


def test_file_engine_cache_is_bounded(tmp_path: Path) -> None:
    for index in range(driver._ENGINE_CACHE_SIZE + 1):
        with store.connect(tmp_path / f"db-{index}.sqlite"):
            pass
    assert len(driver._ENGINE_CACHE) == driver._ENGINE_CACHE_SIZE
