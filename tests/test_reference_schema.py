"""The two reference tables: what a fresh database declares, how every older database gains them, and that no row of
either is ever changed.

`tests/fixtures/schema_v17.sql` is the schema as the topic grant checks (head ebdf531a) left it, with rows: two topics
that have ids (`job-hunt` and `job-hunt/boards`), one alias, one conversation, and one authorized topic grant with its
events. Every older fixture must still upgrade, and every upgrade must leave `reference_docs` and `reference_revisions`
empty, create no document for any topic, and leave everything else as it was, the grant rows byte for byte.

The current schema version is never written here as a number. The fixture's own version is, and everything about the
current one goes through `store.SCHEMA_VERSION` and `store.MIGRATES_FROM`, so another change landing first cannot make
these wrong. If one does land first and the reference tables take a later version, the fixture's name and `S2_VERSION`
below are what to renumber.

Three existing checks follow from this and are the code writer's to update:
* `test_migration_matrix.py::WHOLE_TABLES` must list the two reference tables for the new step;
* the new fixture's own version must be in `MIGRATES_FROM`
  (`test_the_upgrade_steps_cover_every_version_there_is_a_fixture_for`);
* `test_topic_grant_migration.py::test_every_older_schema_gains_both_grant_tables_empty` runs over every fixture, and
  the new one carries a grant, so it must stop at the versions below this fixture's.

Tests whose docstring starts with GUARD pin behaviour that must not change, and pass before and after the change.
Mutant numbers in comments refer to the spec's list of one-line mutants the tests must kill. Q numbers refer to its open
questions: the test pins the question's default, so a ruling changes only the tests that carry that number.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
import reference_support as refs
import topic_grant_tools_support as support
from reference_support import (
    BOARDS,
    LEADS,
    NAME,
    REFERENCE_TABLES,
    Staging,
    board,
    body_of,
    core_edit,
    grantee_edit,
    request_id,
    require_reference_tables,
    tables_of,
    write_revisions,
)
from test_migration_matrix import FIXTURE_VERSIONS, WHOLE_TABLES
from test_topic_containers import topic_config
from test_topic_id_migration import install, snapshot, snapshot_plan
from topic_grant_tools_support import play, refused, succeeded

from baton.database import store
from baton.database.store import connect

build_scene = refs.build_scene  # the fixture, under the name the tests ask for

FIXTURES = Path(__file__).parent / "fixtures"
S2_FIXTURE = "schema_v17.sql"  # the topic grant checks' schema, which this change climbs from
# The version that fixture holds, read from the fixture itself: it is the fixture's, not a claim about the current one.
S2_VERSION = int(re.search(r"INSERT INTO schema_version VALUES \((\d+)\);", (FIXTURES / S2_FIXTURE).read_text())[1])
NEW_STEP = S2_VERSION + 1  # the step right above that fixture: the one that adds the reference tables
NEW_TABLES = set(REFERENCE_TABLES)


def columns_of(db: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {row["name"]: row for row in db.execute(f"PRAGMA table_info({table})")}


def unique_columns(db: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    """Every set of columns the table keeps unique, whichever way it was declared."""
    found = set()
    for index in db.execute(f"PRAGMA index_list({table})"):
        if index["unique"]:
            found.add(tuple(column["name"] for column in db.execute(f"PRAGMA index_info({index['name']})")))
    return found


def a_revision(**overrides):
    """A row that satisfies every constraint of `reference_revisions`; a test changes the one thing it is about."""
    row = {
        "doc_id": "ref_0000000000000001",
        "revision": 1,
        "body": "text",
        "author_role": "planner",
        "author_alias_id": None,
        "author_alias_name": None,
        "author_alias_code": None,
        "grant_id": None,
        "request_id": "req-" + request_id(),
        "request_digest": "0" * 64,
        "base_revision": 0,
        "at": "2026-11-03T12:00:00+00:00",
    }
    return {**row, **overrides}


def insert(db: sqlite3.Connection, table: str, row: dict) -> None:
    db.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


# 1. The fixture and the version relationship ---------------------------------------------------------------


def test_the_v17_fixture_has_the_rows_it_is_documented_to_have():
    """GUARD. Two topics with ids, one alias, one conversation in a topic with no `reviewer` in it, one authorized grant
    on the root with its propose and authorize events, no reference table, and no key."""
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    text = (FIXTURES / S2_FIXTURE).read_text()
    raw.executescript(text)
    assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == S2_VERSION
    topics = {row["path"]: row["id"] for row in raw.execute("SELECT path, id FROM topics")}
    assert set(topics) == {"job-hunt", "job-hunt/boards"}
    assert all(one.startswith("topic_") for one in topics.values()) and len(set(topics.values())) == 2
    assert [row["name"] for row in raw.execute("SELECT name FROM aliases")] == ["job-search"]
    assert [(row["title"], row["topic"]) for row in raw.execute("SELECT title, topic FROM conversations")] == [
        ("Which boards to watch", "job-hunt/boards")
    ]
    assert "reviewer" not in {row["role"] for row in raw.execute("SELECT role FROM participants")}
    [grant] = raw.execute("SELECT * FROM topic_grants").fetchall()
    assert grant["topic_id"] == topics["job-hunt"] and grant["inherit"] == 1
    assert grant["revoked_at"] is None and grant["authorized_digest"]
    assert re.fullmatch("[0-9a-f]{64}", grant["proof_digest"])
    assert set(json.loads(grant["capabilities"])) == set(support.ALL)
    assert [row["event"] for row in raw.execute("SELECT event FROM topic_grant_events ORDER BY seq")] == [
        "propose",
        "authorize",
    ]
    assert not NEW_TABLES & tables_of(raw), "the fixture is the schema before the reference tables"
    assert "tgk_" not in text, "a fixture holds no grant key"
    raw.close()


def test_the_version_the_v17_fixture_holds_is_one_the_database_migrates_from():
    """The fixture's own version is one baton climbs from, and the current version is exactly one above the newest of
    those: this change adds one schema step, and no other."""
    assert S2_VERSION in store.MIGRATES_FROM, f"{S2_VERSION} is not a version baton migrates from"
    assert store.SCHEMA_VERSION > S2_VERSION, f"the schema version did not rise above {S2_VERSION}"
    assert store.SCHEMA_VERSION - 1 == max(store.MIGRATES_FROM), "the schema version rises by one"


def test_the_reference_tables_are_declared_as_the_step_above_the_v17_fixture():
    """The step right above the fixture adds the two tables whole: no column to add, no backfill, no procedure, so an
    upgrade writes no row."""
    added = set(store._ADDED_TABLES.get(NEW_STEP, ()))
    assert added == NEW_TABLES, f"step {NEW_STEP} does not add the reference tables"
    assert NEW_STEP in store._STEPS
    assert store._ADDED_COLUMNS.keys() & {NEW_STEP} == set(), "the step adds tables only, no column"
    assert NEW_STEP not in store._BACKFILL and NEW_STEP not in store._PROCEDURES, "no backfill and no procedure"


def test_the_migration_matrix_lists_the_reference_step():
    """The existing matrix spells out the tables each step adds, so a step cannot quietly stand in for them."""
    assert WHOLE_TABLES.get(NEW_STEP) == NEW_TABLES, f"WHOLE_TABLES has no entry for step {NEW_STEP}"
    assert {step: set(names) for step, names in store._ADDED_TABLES.items()} == WHOLE_TABLES


# 2. Every fixture reaches the current schema with both tables, empty -----------------------------------------


@pytest.mark.parametrize("version", FIXTURE_VERSIONS)
def test_every_older_schema_gains_both_reference_tables_empty(tmp_path, version):
    """The upgrade writes no reference row and makes no document for any topic, from every older version."""
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


def test_the_v17_fixture_upgrades_with_the_grant_rows_and_every_other_row_unchanged(tmp_path):
    """Nothing but the two empty tables is new: the topics, their ids, the alias, the conversation, the topic history,
    and the grant with its events are byte for byte as they were, and a second open adds no row."""
    config = topic_config(tmp_path)
    install(config, S2_FIXTURE)
    raw = sqlite3.connect(config.db_path)
    plan = snapshot_plan(raw)
    before = snapshot(raw, plan)
    raw.close()
    assert len(before["topic_grants"]) == 1 and len(before["topic_grant_events"]) == 2 and len(before["topics"]) == 2
    db = connect(config.db_path)
    try:
        assert NEW_TABLES <= tables_of(db), "the reference tables were not added"
        assert snapshot(db, plan) == before
        assert all(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0 for name in NEW_TABLES)
    finally:
        db.close()
    connect(config.db_path).close()
    again = connect(config.db_path)
    try:
        assert snapshot(again, plan) == before
        assert all(again.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0 for name in NEW_TABLES)
    finally:
        again.close()


@pytest.mark.parametrize("version", [S2_VERSION, min(FIXTURE_VERSIONS)])
def test_an_upgraded_database_declares_the_same_reference_tables_as_a_fresh_one(tmp_path, version):
    """From the fixture just below and from the oldest one, the upgrade creates the tables exactly as a fresh database
    does: the same columns, types and uniqueness."""
    fresh = connect(topic_config(tmp_path, data_dir="fresh").db_path)
    config = topic_config(tmp_path, data_dir="old")
    install(config, f"schema_v{version}.sql")
    upgraded = connect(config.db_path)
    try:
        for name in sorted(NEW_TABLES):
            require_reference_tables(fresh)
            require_reference_tables(upgraded)
            assert [tuple(row) for row in upgraded.execute(f"PRAGMA table_info({name})")] == [
                tuple(row) for row in fresh.execute(f"PRAGMA table_info({name})")
            ]
            assert unique_columns(upgraded, name) == unique_columns(fresh, name)
    finally:
        fresh.close()
        upgraded.close()


# 3. The tables a fresh database declares ------------------------------------------------------------------


def test_a_fresh_database_declares_reference_docs_as_the_spec_lists_it(tmp_path):
    """An id, the topic's id, the name and the time, all text; the topic's id and the name are unique together."""
    db = connect(topic_config(tmp_path).db_path)
    try:
        require_reference_tables(db)
        columns = columns_of(db, "reference_docs")
        assert list(columns) == ["id", "topic_id", "name", "created_at"]
        assert columns["id"]["pk"] == 1 and {row["type"] for row in columns.values()} == {"TEXT"}
        assert [name for name, row in columns.items() if not row["pk"] and row["notnull"]] == [
            "topic_id",
            "name",
            "created_at",
        ]
        assert ("topic_id", "name") in unique_columns(db, "reference_docs")
    finally:
        db.close()


def test_a_fresh_database_declares_reference_revisions_as_the_spec_lists_it(tmp_path):
    """Q9. The alias's name and code are columns beside its id, for reading only. The document and revision are the
    composite key; the request id is unique; the alias columns, the grant and nothing else may be null."""
    db = connect(topic_config(tmp_path).db_path)
    try:
        require_reference_tables(db)
        columns = columns_of(db, "reference_revisions")
        assert list(columns) == [
            "doc_id",
            "revision",
            "body",
            "author_role",
            "author_alias_id",
            "author_alias_name",
            "author_alias_code",
            "grant_id",
            "request_id",
            "request_digest",
            "base_revision",
            "at",
        ]
        assert {name: row["type"] for name, row in columns.items() if row["type"] != "TEXT"} == {
            "revision": "INTEGER",
            "base_revision": "INTEGER",
        }
        assert {name for name, row in columns.items() if not row["notnull"]} == {
            "author_alias_id",
            "author_alias_name",
            "author_alias_code",
            "grant_id",
        }
        assert [name for name, row in sorted(columns.items(), key=lambda item: item[1]["pk"]) if row["pk"]] == [
            "doc_id",
            "revision",
        ]
        assert {("doc_id", "revision"), ("request_id",)} <= unique_columns(db, "reference_revisions")
    finally:
        db.close()


def test_the_uniqueness_the_spec_lists_is_enforced(tmp_path):
    """Two documents cannot share a topic and a name, two revisions cannot share a request id, and a document cannot
    have two of one revision; a control row that breaks none of them goes in."""
    db = connect(topic_config(tmp_path).db_path)
    try:
        require_reference_tables(db)
        doc = {"id": "ref_0000000000000001", "topic_id": "topic_a", "name": NAME, "created_at": "2026-11-03T12:00:00Z"}
        insert(db, "reference_docs", doc)
        insert(db, "reference_docs", {**doc, "id": "ref_0000000000000002", "topic_id": "topic_b"})
        insert(db, "reference_docs", {**doc, "id": "ref_0000000000000003", "name": "other"})
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            insert(db, "reference_docs", {**doc, "id": "ref_0000000000000004"})
        first = a_revision()
        insert(db, "reference_revisions", first)
        insert(db, "reference_revisions", a_revision(revision=2, base_revision=1))
        insert(db, "reference_revisions", a_revision(doc_id="ref_0000000000000002"))
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            insert(db, "reference_revisions", a_revision(doc_id="ref_0000000000000009", request_id=first["request_id"]))
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            insert(db, "reference_revisions", a_revision())
    finally:
        db.close()


@pytest.mark.parametrize("revision,base", [(0, -1), (-1, -2), (2, 0), (2, 2), (1, 1), (3, 1)])
def test_both_checks_reject_a_row_that_breaks_them(tmp_path, revision, base):
    """A revision is at least 1 and based on the one before it: revision 0 or below, and a base that is not one
    less, are refused by the database itself, whatever wrote them."""
    db = connect(topic_config(tmp_path).db_path)
    try:
        require_reference_tables(db)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            insert(db, "reference_revisions", a_revision(revision=revision, base_revision=base))
        assert db.execute("SELECT COUNT(*) FROM reference_revisions").fetchone()[0] == 0
        insert(db, "reference_revisions", a_revision(revision=2, base_revision=1))
    finally:
        db.close()


@pytest.mark.parametrize("table", sorted(NEW_TABLES))
def test_the_reference_tables_have_no_foreign_keys_and_no_triggers(tmp_path, table):
    """Like `topic_changes` and the grant tables: creatable before the things they name, and nothing deletes.
    Append-only by convention and by the tests below, not by a trigger (Q14)."""
    db = connect(topic_config(tmp_path).db_path)
    try:
        require_reference_tables(db)
        assert list(db.execute(f"PRAGMA foreign_key_list({table})")) == []
        triggers = db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (table,))
        assert list(triggers) == []
    finally:
        db.close()


# 4. Rows are never changed --------------------------------------------------------------------------------


def test_no_row_of_either_table_is_updated_or_deleted_by_a_stale_edit_a_replay_a_refusal_or_an_undo(
    build_scene, monkeypatch
):
    """R16. Five revisions are written and both tables are snapshotted. Then a stale refusal, a replay, a refused edit
    by a core role, one by a revoked grantee and an undo (revision 3's text as revision 6) each run. After every step
    the earlier rows are byte for byte what they were, and over the whole run no statement updates, deletes or replaces
    a row of either table."""
    # Mutant 11: undo as an UPDATE of the old row, or INSERT OR REPLACE.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        await write_revisions(client, scene, BOARDS, NAME, 4)
        rid = request_id()
        succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(5), 4, request_id=rid))
        staging = Staging(monkeypatch)
        snapshots = [scene.tables()]

        def unchanged():
            """Every row that was there is there as it was; whatever else the tables hold was added after them."""
            now, then = scene.tables(), snapshots[0]
            for table in REFERENCE_TABLES:
                assert now[table][: len(then[table])] == then[table], table
            return now

        assert (await core_edit(client, scene, BOARDS, NAME, "text", 2)).get("error") == "stale_revision"
        assert unchanged() == snapshots[0]
        replay = succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(5), 4, request_id=rid))
        assert replay["created"] is False
        assert unchanged() == snapshots[0]
        refused(await core_edit(client, scene, BOARDS, NAME, "text", 5, role="worker"), "forbidden", refs.f1("worker"))
        assert unchanged() == snapshots[0]
        succeeded(await core_edit(client, scene, LEADS, NAME, "another document", 0))
        undone = succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(3), 5))
        assert undone["revision"] == 6
        scene.revoke(held.grant_id)
        refused(await grantee_edit(client, held, BOARDS, NAME, "text", 6), "forbidden", refs.revoked(held.grant_id))
        final = unchanged()
        assert (len(final["reference_revisions"]), len(final["reference_docs"])) == (7, 2)
        assert staging.writes_to_reference_tables_other_than_inserts() == []

    play(scene.host, scenario)
