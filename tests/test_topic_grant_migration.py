"""Schema 17: the two topic grant tables, on a fresh database and on every upgrade.

`tests/fixtures/schema_v16.sql` is the schema as the stable-topic-ids change (head 41a8a87f) created it, with
rows: three topics that have ids, a guest alias and four conversations filed in them or in none. Every older
fixture must still upgrade, and every upgrade must leave `topic_grants` and `topic_grant_events` empty and
everything else as it was.

The current schema version is never written here as a number. The fixture's own version is, and everything
about the current one goes through `store.SCHEMA_VERSION` and `store.MIGRATES_FROM`, so another change
landing first cannot make these wrong.

Two existing checks follow from this and are the code writer's to update:
* `test_migration_matrix.py::WHOLE_TABLES` must list `topic_grants` and `topic_grant_events` for the new step;
* the new fixture needs 16 in `MIGRATES_FROM` (`test_the_upgrade_steps_cover_every_version_there_is_a_fixture_for`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_migration_matrix import FIXTURE_VERSIONS
from test_topic_containers import topic_config
from test_topic_id_migration import install, snapshot, snapshot_plan
from topic_grant_support import GRANT_COLUMNS, grants_module, table_exists

from baton.database import store
from baton.database.store import connect

FIXTURES = Path(__file__).parent / "fixtures"
V16_VERSION = 16  # the version schema_v16.sql holds, not an assertion about the current one
NEW_STEP = V16_VERSION + 1  # the step right above that fixture: the one that adds the grant tables
NEW_TABLES = {"topic_grants", "topic_grant_events"}


def columns_of(db: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {row["name"]: row for row in db.execute(f"PRAGMA table_info({table})")}


def tables_of(db: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


# 1. The fixture and the version relationship ----------------------------------------------------------------

def test_the_v16_fixture_has_the_topics_and_threads_it_is_documented_to_have():  # GUARD
    raw = sqlite3.connect(":memory:")
    raw.executescript((FIXTURES / "schema_v16.sql").read_text())
    assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == V16_VERSION
    topics = {path: topic_id for path, topic_id in raw.execute("SELECT path, id FROM topics")}
    assert set(topics) == {"baton", "baton/development", "reviews"}
    assert all(topic_id and topic_id.startswith("topic_") for topic_id in topics.values())
    assert len(set(topics.values())) == 3
    assert [row[0] for row in raw.execute("SELECT name FROM aliases")] == ["lane-a"]
    filed = {title: topic for title, topic in raw.execute("SELECT title, topic FROM conversations")}
    assert filed == {"Release plan": "baton", "A guest question": "baton", "Schema work": "baton/development",
                     "No topic": None}
    assert {"topic_changes"} <= {row[0] for row in raw.execute("SELECT name FROM sqlite_master")}
    assert not NEW_TABLES & {row[0] for row in raw.execute("SELECT name FROM sqlite_master")}, "the fixture is 16"
    raw.close()


def test_the_version_the_v16_fixture_holds_is_one_the_database_migrates_from():
    assert V16_VERSION in store.MIGRATES_FROM, "16 is not a version baton migrates from"
    assert store.SCHEMA_VERSION > V16_VERSION, "the schema version did not rise above 16"
    assert store.SCHEMA_VERSION - 1 == max(store.MIGRATES_FROM), "the schema version rises by one"


def test_the_grant_tables_are_declared_as_the_step_above_the_v16_fixture():
    assert set(store._ADDED_TABLES.get(NEW_STEP, ())) == NEW_TABLES, f"step {NEW_STEP} does not add the grant tables"
    assert NEW_STEP in store._STEPS


# 2. Every older fixture reaches the current schema with both tables, empty ---------------------------------

# Only the fixtures below the step that adds the grant tables: a fixture from that step on carries grants.
@pytest.mark.parametrize("version", [version for version in FIXTURE_VERSIONS if version < NEW_STEP])
def test_every_older_schema_gains_both_grant_tables_empty(tmp_path, version):
    config = topic_config(tmp_path)
    install(config, f"schema_v{version}.sql")
    db = connect(config.db_path)
    try:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == store.SCHEMA_VERSION
        missing = NEW_TABLES - tables_of(db)
        assert not missing, f"version {version} did not gain {sorted(missing)}"
        for name in sorted(NEW_TABLES):
            assert db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0, name
    finally:
        db.close()


def test_the_v16_fixture_upgrades_with_nothing_but_the_grant_tables_changing(tmp_path):
    config = topic_config(tmp_path)
    install(config, "schema_v16.sql")
    raw = sqlite3.connect(config.db_path)
    plan = snapshot_plan(raw)
    before = snapshot(raw, plan)
    raw.close()
    assert before["topics"] and before["conversations"] and before["aliases"] and before["topic_changes"] is not None
    db = connect(config.db_path)
    try:
        assert NEW_TABLES <= tables_of(db), "the grant tables were not added"
        assert snapshot(db, plan) == before
    finally:
        db.close()
    connect(config.db_path).close()  # a second open changes nothing and adds no row
    again = connect(config.db_path)
    try:
        assert snapshot(again, plan) == before
        assert all(again.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0 for name in NEW_TABLES)
    finally:
        again.close()


def test_an_upgraded_database_takes_a_grant_for_the_fixtures_own_alias_and_topic(tmp_path, monkeypatch):
    """The upgraded database is a working one: the fixture's alias and topic can be granted on."""
    tg = grants_module()
    config = topic_config(tmp_path)
    install(config, "schema_v16.sql")
    db = connect(config.db_path)
    try:
        topic_id = db.execute("SELECT id FROM topics WHERE path = 'baton/development'").fetchone()["id"]
        proposal = tg.propose(db, "lane-a", "baton/development", ["reference.edit"], False, 3)
        assert proposal["topic_id"] == topic_id and proposal["state"] == "pending"
        assert tg.authorize(db, proposal["id"], proposal["digest"])["grant_key"]
        found = tg.covering_grant(db, proposal["alias_id"], "baton/development", "reference.edit")
        assert found["id"] == proposal["id"]
    finally:
        db.close()


# 3. The tables a fresh database declares -------------------------------------------------------------------

def test_a_fresh_database_declares_topic_grants_as_the_spec_lists_it(tmp_path):
    db = connect(topic_config(tmp_path).db_path)
    try:
        assert "topic_grants" in tables_of(db), "there is no topic_grants table"
        columns = columns_of(db, "topic_grants")
        assert set(columns) == GRANT_COLUMNS
        assert columns["id"]["pk"] == 1
        nullable = {"authorized_by", "authorized_at", "authorized_digest", "proof_digest",
                    "revoked_at", "revoked_by", "revoke_reason"}
        assert {name for name, row in columns.items() if not row["notnull"] and not row["pk"]} == nullable
        assert columns["inherit"]["type"] == "INTEGER"
        assert {row["type"] for name, row in columns.items() if name != "inherit"} == {"TEXT"}
    finally:
        db.close()


def test_a_fresh_database_declares_topic_grant_events_as_an_autoincrementing_log(tmp_path):
    db = connect(topic_config(tmp_path).db_path)
    try:
        assert "topic_grant_events" in tables_of(db), "there is no topic_grant_events table"
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name = 'topic_grant_events'").fetchone()["sql"]
        assert "AUTOINCREMENT" in sql.upper()
        columns = columns_of(db, "topic_grant_events")
        assert {name: row["notnull"] for name, row in columns.items() if name != "seq"} == \
            {"grant_id": 1, "event": 1, "actor": 1, "detail": 0, "at": 1}
    finally:
        db.close()


@pytest.mark.parametrize("table", sorted(NEW_TABLES))
def test_the_grant_tables_have_no_foreign_keys(tmp_path, table):
    """Like `topic_changes`: the history must be creatable before the things it names, and nothing deletes."""
    db = connect(topic_config(tmp_path).db_path)
    try:
        assert table in tables_of(db), f"there is no {table} table"
        assert list(db.execute(f"PRAGMA foreign_key_list({table})")) == []
    finally:
        db.close()


@pytest.mark.parametrize("version", [V16_VERSION, min(FIXTURE_VERSIONS)])
def test_an_upgraded_database_declares_the_same_grant_tables_as_a_fresh_one(tmp_path, version):
    fresh = connect(topic_config(tmp_path, data_dir="fresh").db_path)
    config = topic_config(tmp_path, data_dir="old")
    install(config, f"schema_v{version}.sql")
    upgraded = connect(config.db_path)
    try:
        for name in sorted(NEW_TABLES):
            assert table_exists(fresh, name) and table_exists(upgraded, name), f"{name} is missing"
            assert [tuple(row) for row in upgraded.execute(f"PRAGMA table_info({name})")] == \
                [tuple(row) for row in fresh.execute(f"PRAGMA table_info({name})")]
    finally:
        fresh.close()
        upgraded.close()
