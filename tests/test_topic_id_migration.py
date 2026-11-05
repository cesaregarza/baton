"""Topic ids on upgrade: every older database gains `topics.id` and `topic_changes`.

`tests/fixtures/schema_v15.sql` is the schema as main e17bbfb created it, with rows: three topics (a root
with a subtopic, and another root) and four conversations filed in them or in none. Every older
fixture must still upgrade. After an upgrade every existing topic has its own non-empty id and nothing
else about the database has changed.

The schema version is never written here as a number: the fixture's own version is, as
test_topic_migration.py does, and everything about the current one goes through `store.SCHEMA_VERSION`
and `store.MIGRATES_FROM`, so another change landing first cannot make these wrong.

Two checks in `test_migration_matrix.py` follow from this: the new fixture needs 15 in `MIGRATES_FROM`,
and `WHOLE_TABLES`, which pins the declared steps exactly, lists `topic_changes` for this version.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from test_migration_matrix import FIXTURE_VERSIONS
from test_topic_containers import make_topic, topic_config
from test_topic_migration import v14_database

from baton import conversations, topics
from baton.config import Config
from baton.database import store
from baton.database.store import connect

FIXTURES = Path(__file__).parent / "fixtures"
V15_VERSION = 15  # the version schema_v15.sql holds, not an assertion about the current one
O = "orchestrator"  # noqa: E741
# Tables whose contents change on every open for reasons that have nothing to do with topics: the log
# epoch is written when absent, and the version row is what an upgrade is for.
NOT_COMPARED = {"schema_version", "log_meta"}


def install(config: Config, fixture: str) -> None:
    config.data_dir.mkdir(mode=0o700, parents=True)
    raw = sqlite3.connect(config.db_path)
    raw.executescript((FIXTURES / fixture).read_text())
    raw.close()
    os.chmod(config.db_path, 0o600)


def snapshot_plan(raw: sqlite3.Connection) -> dict[str, list[str]]:
    """Every ordinary table in the database and its columns, as the old schema had them."""
    plan = {}
    for (name,) in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                               " AND name NOT LIKE '%\\_fts%' ESCAPE '\\' ORDER BY name"):
        if name not in NOT_COMPARED:
            plan[name] = [row[1] for row in raw.execute(f"PRAGMA table_info({name})")]
    return plan


def snapshot(conn: sqlite3.Connection, plan: dict[str, list[str]]) -> dict[str, list[tuple]]:
    return {table: [tuple(row) for row in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid")]
            for table, cols in plan.items()}


def ids_in(db: sqlite3.Connection) -> dict[str, str]:
    assert "id" in {row["name"] for row in db.execute("PRAGMA table_info(topics)")}, "topics has no id column"
    return {row["path"]: row["id"] for row in db.execute("SELECT path, id FROM topics")}


def assert_good_ids(found: dict[str, str]) -> None:
    assert all(isinstance(one, str) and one for one in found.values()), f"an empty or missing id in {found}"
    assert len(set(found.values())) == len(found), f"two topics share an id: {found}"


# 1. The version relationship ----------------------------------------------------------------------------

def test_the_version_the_v15_fixture_holds_is_one_the_database_migrates_from():  # FAILS-ON-MAIN
    raw = sqlite3.connect(":memory:")
    raw.executescript((FIXTURES / "schema_v15.sql").read_text())
    assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == V15_VERSION
    raw.close()
    assert V15_VERSION in store.MIGRATES_FROM, "the previous schema is not a version baton migrates from"
    assert store.SCHEMA_VERSION > V15_VERSION
    assert store.SCHEMA_VERSION - 1 == max(store.MIGRATES_FROM), "the schema version rises by one"


# 2. Every fixture reaches the current schema with ids and history ---------------------------------------

@pytest.mark.parametrize("version", FIXTURE_VERSIONS)
def test_every_older_schema_gains_the_topic_id_and_the_history_table(tmp_path, version):  # FAILS-ON-MAIN
    config = topic_config(tmp_path)
    install(config, f"schema_v{version}.sql")
    db = connect(config.db_path)
    try:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == store.SCHEMA_VERSION
        found = ids_in(db)
        assert_good_ids(found)
        tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "topic_changes" in tables, f"version {version} did not gain topic_changes"
    finally:
        db.close()


# 3. The v15 fixture: ids are new, everything else is as it was ----------------------------------------------

def test_the_v15_fixture_has_the_topics_and_threads_it_is_documented_to_have():  # GUARD
    raw = sqlite3.connect(":memory:")
    raw.executescript((FIXTURES / "schema_v15.sql").read_text())
    assert [row[0] for row in raw.execute("SELECT path FROM topics ORDER BY path")] == \
        ["baton", "baton/development", "reviews"]
    assert "id" not in {row[1] for row in raw.execute("PRAGMA table_info(topics)")}, "the fixture is schema 15"
    filed = {title: topic for title, topic in raw.execute("SELECT title, topic FROM conversations")}
    assert filed == {"Release plan": "baton", "A guest question": "baton", "Schema work": "baton/development",
                     "No topic": None}
    raw.close()


def test_every_existing_topic_gets_its_own_id_on_upgrade(tmp_path):  # FAILS-ON-MAIN
    config = topic_config(tmp_path)
    install(config, "schema_v15.sql")
    db = connect(config.db_path)
    try:
        found = ids_in(db)
        assert set(found) == {"baton", "baton/development", "reviews"}
        assert_good_ids(found)
        assert not set(found.values()) & set(found), "an id is not the path"
    finally:
        db.close()


def test_nothing_but_the_new_id_and_tables_changes_on_upgrade(tmp_path):  # FAILS-ON-MAIN
    config = topic_config(tmp_path)
    install(config, "schema_v15.sql")
    raw = sqlite3.connect(config.db_path)
    plan = snapshot_plan(raw)
    before = snapshot(raw, plan)
    raw.close()
    assert before["topics"] and before["conversations"] and before["participants"] and before["aliases"]
    db = connect(config.db_path)
    try:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == store.SCHEMA_VERSION
        assert "id" in {row["name"] for row in db.execute("PRAGMA table_info(topics)")}, "topics has no id column"
        assert snapshot(db, plan) == before
    finally:
        db.close()


def test_the_upgraded_database_reads_as_the_old_one_did(tmp_path):  # GUARD
    config = topic_config(tmp_path)
    install(config, "schema_v15.sql")
    db = connect(config.db_path)
    try:
        listed = {entry["path"]: entry for entry in topics.listing(db, config, reader=O)}
        assert {path: (entry["title"], entry["description"], entry["members"], entry["threads"],
                       entry["created_by"]) for path, entry in listed.items()} == {
            "baton": ("Baton", "The relay itself", [O, "assistant"], 3, O),
            "baton/development": ("development", None, [O, "outsider"], 1, O),
            "reviews": ("reviews", "Reviews, one thread each", [], 0, O)}
        assert all(set(entry) == {"path", "title", "description", "members", "created_by", "created_at", "threads",
                                  "last_activity_at"} for entry in listed.values()), "no id in a topic's response"
        assert set(topics.get(db, config, reader=O, path="baton")) == {
            "path", "title", "description", "members", "created_by", "created_at", "threads", "subtopics"}
        assert {c["title"]: c["topic"] for c in conversations.list_all(db)} == {
            "Release plan": "baton", "A guest question": "baton", "Schema work": "baton/development",
            "No topic": None}
        assert [c["title"] for c in conversations.list_all(db, "baton/development")] == ["Schema work"]
    finally:
        db.close()


def test_a_second_open_keeps_every_id(tmp_path):  # FAILS-ON-MAIN
    config = topic_config(tmp_path)
    install(config, "schema_v15.sql")
    first = connect(config.db_path)
    try:
        found = ids_in(first)
    finally:
        first.close()
    second = connect(config.db_path)
    try:
        assert ids_in(second) == found, "reopening an upgraded database re-ran the id backfill"
    finally:
        second.close()


def test_a_migrated_topic_keeps_its_id_through_a_rename_and_a_new_topic_gets_another(tmp_path):  # FAILS-ON-MAIN
    config = topic_config(tmp_path)
    install(config, "schema_v15.sql")
    db = connect(config.db_path)
    try:
        before = ids_in(db)
        topics.rename(db, config, actor=O, path="baton", new_path="platform")
        after = ids_in(db)
        assert after == {"platform": before["baton"], "platform/development": before["baton/development"],
                         "reviews": before["reviews"]}
        make_topic(db, config, "baton")
        made = ids_in(db)["baton"]
        assert isinstance(made, str) and made, "the new topic has no id"
        assert made not in before.values(), "a new topic took a migrated topic's id"
    finally:
        db.close()


# 4. A database that has to discover its topics, as the version-15 upgrade did ---------------------------------

def test_discovered_topics_each_get_an_id_including_implicit_parents(tmp_path):  # FAILS-ON-MAIN
    config = topic_config(tmp_path)
    labels = [f"area-{number:02d}" for number in range(25)]
    threads = [(f"conv_{number}", label, [O, "reviewer"]) for number, label in enumerate(labels)]
    threads += [("conv_sub_a", "wide/sub-a", [O, "reviewer"]), ("conv_sub_b", "wide/sub-b", [O, "assistant"])]
    v14_database(config, threads)
    db = connect(config.db_path)
    try:
        found = ids_in(db)
        assert set(found) == {*labels, "wide", "wide/sub-a", "wide/sub-b"}, "'wide' is an implicit parent"
        assert_good_ids(found)
    finally:
        db.close()


def test_a_database_with_no_topics_upgrades_with_none(tmp_path):  # FAILS-ON-MAIN
    config = topic_config(tmp_path)
    v14_database(config, [("conv_1", None, [O, "reviewer"])])
    db = connect(config.db_path)
    try:
        assert ids_in(db) == {}
        assert db.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 0
    finally:
        db.close()


def test_the_backfilled_members_and_titles_survive(tmp_path):  # FAILS-ON-MAIN
    """The v14 to current climb passes through the version-15 discovery and then the id step."""
    config = topic_config(tmp_path)
    v14_database(config, [("conv_1", "baton", [O, "reviewer"]), ("conv_2", "baton/development", [O, "assistant"])])
    db = connect(config.db_path)
    try:
        assert_good_ids(ids_in(db))
        found = {row["path"]: (row["title"], json.loads(row["members"]), row["created_by"])
                 for row in db.execute("SELECT * FROM topics")}
        assert found == {"baton": ("baton", [O, "reviewer"], "migration"),
                         "baton/development": ("development", ["assistant", O], "migration")}
    finally:
        db.close()
