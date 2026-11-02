"""Topic ids and topic history (CES-1641, slice S0): a stable id beside the path, and `topic_changes`.

These are written before the code, as its specification, and every test of new behaviour first asserts
that the column or the table exists, so that on the old code it fails on that assertion rather than
by raising `OperationalError`. Tests that pin behaviour S0 must not change are marked GUARD and pass on
the old code.

What S0 adds (and nothing else):
* `topics.id`: non-empty, unique across every topic ever created, kept by a rename, never handed to a
  new topic at a path a rename freed. `path` stays the unique key that every filter and response uses.
* `topic_changes`: an append-only row for every successful `create_topic`, `update_topic`,
  `rename_topic` and `set_topic` that changes a conversation's topic, and for nothing else.

Column names and shapes that the design left open, chosen here so the code has one reading:
* columns `topic_id`, `operation`, `actor`, `grant_id`, `before`, `after`, `at`;
* `operation` is the tool name: create_topic, update_topic, rename_topic or set_topic;
* `before` and `after` are JSON objects (NULL where there is no earlier state): create_topic has NULL
  `before`; update_topic holds title, description and members; rename_topic holds `path`; set_topic holds
  `conversation_id`, `topic` (a path or null) and `topic_id` (an id or null);
* `topic_id` is the topic acted on: for set_topic, the topic the conversation moved into, or the one it
  was taken out of when it is cleared;
* `actor` is the calling role, exactly, for a call made as that role. (The alias-bound case is in
  test_topic_audit_and_surface.py, since only the MCP guard knows an alias.)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

import pytest
from test_topic_containers import (
    ALIAS,
    OTHER_ALIAS,
    guest_thread,
    make_topic,
    refused,
    seed_aliases,
    topic_config,
)

from baton import conversations, topics
from baton.config import Config
from baton.store import connect

O = "orchestrator"  # noqa: E741 -- the one role that may manage topics in topic_config
HISTORY_COLUMNS = {"topic_id", "operation", "actor", "grant_id", "before", "after", "at"}


@pytest.fixture
def config(tmp_path) -> Config:
    return topic_config(tmp_path)


@pytest.fixture
def db(config):
    connection = connect(config.db_path)
    seed_aliases(connection, ALIAS, OTHER_ALIAS)
    yield connection
    connection.close()


# Helpers: each asserts the thing exists, so the old code fails on an assertion, not an OperationalError.

def columns_of(db: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {row["name"]: row for row in db.execute(f"PRAGMA table_info({table})")}


def id_of(db: sqlite3.Connection, path: str) -> str:
    assert "id" in columns_of(db, "topics"), "the topics table has no id column"
    row = db.execute("SELECT id FROM topics WHERE path = ?", (path,)).fetchone()
    assert row is not None, f"no topic {path!r}"
    return row["id"]


def all_ids(db: sqlite3.Connection) -> dict[str, str]:
    assert "id" in columns_of(db, "topics"), "the topics table has no id column"
    return {row["path"]: row["id"] for row in db.execute("SELECT path, id FROM topics")}


def history(db: sqlite3.Connection) -> list[sqlite3.Row]:
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'topic_changes'").fetchone()
    assert exists, "there is no topic_changes table"
    return db.execute("SELECT * FROM topic_changes ORDER BY rowid").fetchall()


def decoded(row: sqlite3.Row, side: str) -> Any:
    value = row[side]
    return None if value is None else json.loads(value)


def move_to(db: sqlite3.Connection, config: Any, conversation_id: str, topic: str | None,
            actor: str = O) -> dict[str, Any]:
    return conversations.set_topic(db, config, conversation_id, topic, actor=actor)


def thread(db: sqlite3.Connection, config: Any, topic: str | None = "baton", title: str = "A thread") -> str:
    return conversations.create(db, config, actor=O, title=title, participants=[O, "reviewer"],
                                topic=topic)["conversation_id"]


# 1. The id column ------------------------------------------------------------------------------------

def test_topics_gain_a_not_null_id_beside_path_which_stays_the_key(db):  # FAILS-ON-MAIN
    found = columns_of(db, "topics")
    assert "id" in found, "the topics table has no id column"
    assert found["id"]["notnull"] == 1, "every topic must have an id"
    assert found["path"]["pk"] == 1, "path stays the table's key"


def test_the_database_refuses_two_topics_with_one_id(db, config):  # FAILS-ON-MAIN
    make_topic(db, config, "baton")
    taken = id_of(db, "baton")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO topics (id, path, title, members, created_by, created_at)"
                   " VALUES (?, 'other', 'other', '[]', 'fixture', '2026-01-01T00:00:00+00:00')", (taken,))


def test_every_created_topic_has_its_own_non_empty_id(db, config):  # FAILS-ON-MAIN
    for path in ("baton", "baton/development", "reviews", "reviews/open", "notes"):
        topics_made = topics.create(db, config, actor=O, path=path)
        assert topics_made["path"] == path
    found = all_ids(db)
    assert set(found) == {"baton", "baton/development", "reviews", "reviews/open", "notes"}
    assert all(isinstance(one, str) and one.strip() == one and one for one in found.values())
    assert len(set(found.values())) == len(found), "two topics share an id"
    assert not set(found.values()) & set(found), "an id is not the path"


def test_a_replayed_create_topic_keeps_the_one_topic_and_its_id(db, config):  # FAILS-ON-MAIN
    first = topics.create(db, config, actor=O, path="baton", request_id="same-request")
    before = id_of(db, "baton")
    again = topics.create(db, config, actor=O, path="baton", request_id="same-request")
    assert (first["created"], again["created"]) == (True, False)
    assert db.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 1
    assert id_of(db, "baton") == before


def test_update_topic_keeps_the_id(db, config):  # FAILS-ON-MAIN
    make_topic(db, config, "baton")
    before = id_of(db, "baton")
    topics.update(db, config, actor=O, path="baton", title="Renamed title", description="d", members=["reviewer"])
    assert id_of(db, "baton") == before


def test_a_rename_keeps_the_id_of_the_topic_and_of_every_subtopic(db, config):  # FAILS-ON-MAIN
    make_topic(db, config, "baton")
    make_topic(db, config, "baton/development")
    root, child = id_of(db, "baton"), id_of(db, "baton/development")
    topics.rename(db, config, actor=O, path="baton", new_path="platform")
    assert all_ids(db) == {"platform": root, "platform/development": child}


def test_a_subtopic_moved_to_another_root_keeps_its_id(db, config):  # FAILS-ON-MAIN
    make_topic(db, config, "baton")
    make_topic(db, config, "reviews")
    make_topic(db, config, "baton/development")
    child = id_of(db, "baton/development")
    topics.rename(db, config, actor=O, path="baton/development", new_path="reviews/development")
    assert id_of(db, "reviews/development") == child


def test_a_path_freed_by_a_rename_and_reused_gets_a_new_id(db, config):  # FAILS-ON-MAIN
    make_topic(db, config, "baton")
    original = id_of(db, "baton")
    topics.rename(db, config, actor=O, path="baton", new_path="platform")
    make_topic(db, config, "baton")
    reused = id_of(db, "baton")
    assert reused != original, "a new topic at a freed path took the old topic's id"
    assert id_of(db, "platform") == original
    # And once more, so a counter derived from the path or from the row count would show.
    topics.rename(db, config, actor=O, path="baton", new_path="platform-two")
    make_topic(db, config, "baton")
    ids = [original, reused, id_of(db, "baton")]
    assert len(set(ids)) == 3, f"an id was handed out twice: {ids}"


def test_a_subtopic_path_freed_and_reused_gets_a_new_id(db, config):  # FAILS-ON-MAIN
    make_topic(db, config, "baton")
    make_topic(db, config, "reviews")
    make_topic(db, config, "baton/development")
    original = id_of(db, "baton/development")
    topics.rename(db, config, actor=O, path="baton/development", new_path="reviews/development")
    make_topic(db, config, "baton/development")
    assert id_of(db, "baton/development") != original
    assert id_of(db, "reviews/development") == original


# 2. The history table ---------------------------------------------------------------------------------

def test_topic_changes_has_the_designed_columns_and_a_nullable_grant_id(db):  # FAILS-ON-MAIN
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'topic_changes'").fetchone()
    assert exists, "there is no topic_changes table"
    found = columns_of(db, "topic_changes")
    assert HISTORY_COLUMNS <= set(found), f"missing {sorted(HISTORY_COLUMNS - set(found))}"
    assert found["grant_id"]["notnull"] == 0, "grant_id stays NULL until a later slice sets it"
    for required in ("topic_id", "operation", "actor", "at"):
        assert found[required]["notnull"] == 1, f"{required} cannot be empty"


def test_a_new_database_starts_with_no_history(db):  # FAILS-ON-MAIN
    assert history(db) == []


def test_create_topic_writes_exactly_one_row(db, config):  # FAILS-ON-MAIN
    assert history(db) == []
    topics.create(db, config, actor=O, path="baton", title="Baton", description="The relay", members=[O, "reviewer"])
    [row] = history(db)
    assert row["topic_id"] == id_of(db, "baton")
    assert row["operation"] == "create_topic"
    assert row["actor"] == O
    assert row["grant_id"] is None
    assert row["before"] is None
    after = decoded(row, "after")
    assert (after["path"], after["title"], after["description"], after["members"]) == \
        ("baton", "Baton", "The relay", [O, "reviewer"])
    assert datetime.fromisoformat(row["at"]).tzinfo is not None, "at is an ISO time with its offset"


def test_each_create_writes_its_own_row_in_order(db, config):  # FAILS-ON-MAIN
    make_topic(db, config, "baton")
    make_topic(db, config, "baton/development")
    rows = history(db)
    assert [decoded(row, "after")["path"] for row in rows] == ["baton", "baton/development"]
    assert [row["topic_id"] for row in rows] == [id_of(db, "baton"), id_of(db, "baton/development")]
    assert [row["at"] for row in rows] == sorted(row["at"] for row in rows)


def test_a_replayed_create_writes_no_second_row(db, config):  # FAILS-ON-MAIN
    topics.create(db, config, actor=O, path="baton", request_id="same-request")
    assert len(history(db)) == 1
    topics.create(db, config, actor=O, path="baton", request_id="same-request")
    assert len(history(db)) == 1, "a replay changes nothing, so it is not a change"


def test_update_topic_writes_one_row_showing_what_changed(db, config):  # FAILS-ON-MAIN
    make_topic(db, config, "baton", title="Baton", description="old", members=[O, "reviewer"])
    topics.update(db, config, actor=O, path="baton", title="Baton relay", description="new", members=[O, "assistant"])
    [_, row] = history(db)
    assert row["topic_id"] == id_of(db, "baton")
    assert row["operation"] == "update_topic"
    assert row["actor"] == O
    assert row["grant_id"] is None
    before, after = decoded(row, "before"), decoded(row, "after")
    assert (before["title"], before["description"], before["members"]) == ("Baton", "old", [O, "reviewer"])
    assert (after["title"], after["description"], after["members"]) == ("Baton relay", "new", [O, "assistant"])


def test_an_update_that_leaves_a_field_out_still_records_the_whole_before_and_after(db, config):  # FAILS-ON-MAIN
    make_topic(db, config, "baton", title="Baton", description="kept", members=[O, "reviewer"])
    topics.update(db, config, actor=O, path="baton", title="Baton relay")
    row = history(db)[-1]
    assert decoded(row, "before")["description"] == decoded(row, "after")["description"] == "kept"
    assert decoded(row, "before")["members"] == decoded(row, "after")["members"] == [O, "reviewer"]
    assert decoded(row, "after")["title"] == "Baton relay"


def test_rename_topic_writes_one_row_with_the_old_and_new_path(db, config):  # FAILS-ON-MAIN
    make_topic(db, config, "baton")
    topics.rename(db, config, actor=O, path="baton", new_path="platform")
    [_, row] = history(db)
    assert row["topic_id"] == id_of(db, "platform")
    assert row["operation"] == "rename_topic"
    assert row["actor"] == O
    assert row["grant_id"] is None
    assert decoded(row, "before")["path"] == "baton"
    assert decoded(row, "after")["path"] == "platform"


def test_renaming_a_topic_with_subtopics_is_still_one_row_for_the_topic_named(db, config):  # FAILS-ON-MAIN
    """One call, one row: the subtopics' new paths are a consequence, and their ids are unchanged."""
    make_topic(db, config, "baton")
    make_topic(db, config, "baton/development")
    root = id_of(db, "baton")
    topics.rename(db, config, actor=O, path="baton", new_path="platform")
    rows = history(db)
    assert len(rows) == 3
    assert rows[-1]["topic_id"] == root
    assert rows[-1]["operation"] == "rename_topic"


def test_set_topic_writes_one_row_with_the_old_and_new_topic(db, config):  # FAILS-ON-MAIN
    make_topic(db, config, "baton")
    make_topic(db, config, "baton/development")
    conversation = thread(db, config, "baton")
    assert len(history(db)) == 2, "creating a thread in a topic is not a topic change"
    move_to(db, config, conversation, "baton/development")
    [_, _, row] = history(db)
    assert row["topic_id"] == id_of(db, "baton/development")
    assert row["operation"] == "set_topic"
    assert row["actor"] == O
    assert row["grant_id"] is None
    before, after = decoded(row, "before"), decoded(row, "after")
    assert (before["conversation_id"], before["topic"], before["topic_id"]) == \
        (conversation, "baton", id_of(db, "baton"))
    assert (after["conversation_id"], after["topic"], after["topic_id"]) == \
        (conversation, "baton/development", id_of(db, "baton/development"))


def test_filing_a_thread_that_had_no_topic_shows_none_before(db, config):  # FAILS-ON-MAIN
    make_topic(db, config, "baton")
    conversation = thread(db, config, None)
    move_to(db, config, conversation, "baton")
    row = history(db)[-1]
    assert row["operation"] == "set_topic"
    before, after = decoded(row, "before"), decoded(row, "after")
    assert (before["topic"], before["topic_id"]) == (None, None)
    assert (after["topic"], after["topic_id"]) == ("baton", id_of(db, "baton"))
    assert row["topic_id"] == id_of(db, "baton")


def test_clearing_a_threads_topic_is_a_set_topic_row_for_the_topic_it_left(db, config):  # FAILS-ON-MAIN
    make_topic(db, config, "baton")
    conversation = thread(db, config, "baton")
    move_to(db, config, conversation, None)
    row = history(db)[-1]
    assert row["operation"] == "set_topic"
    assert row["topic_id"] == id_of(db, "baton")
    assert decoded(row, "before")["topic"] == "baton"
    after = decoded(row, "after")
    assert (after["conversation_id"], after["topic"], after["topic_id"]) == (conversation, None, None)


def test_a_set_topic_that_changes_nothing_writes_no_row(db, config):  # FAILS-ON-MAIN
    """Reading of 'moving a conversation between topics': a call that moves nothing is not a change."""
    make_topic(db, config, "baton")
    conversation = thread(db, config, "baton")
    assert len(history(db)) == 1
    assert move_to(db, config, conversation, "baton")["changed"] is False
    assert len(history(db)) == 1


def test_a_guest_thread_in_a_topic_writes_no_row(db, config):  # FAILS-ON-MAIN
    make_topic(db, config, "baton")
    before = len(history(db))
    guest_thread(db, config, topic="baton", identity=ALIAS)
    assert len(history(db)) == before


def test_a_rename_leaves_the_threads_rows_alone_in_history(db, config):  # FAILS-ON-MAIN
    """Threads follow a rename by label, which is not a set_topic: only the rename is recorded."""
    make_topic(db, config, "baton")
    thread(db, config, "baton")
    topics.rename(db, config, actor=O, path="baton", new_path="platform")
    assert [row["operation"] for row in history(db)] == ["create_topic", "rename_topic"]


def test_history_rows_are_never_rewritten_by_later_changes(db, config):  # FAILS-ON-MAIN
    make_topic(db, config, "baton")
    first = tuple(history(db)[0])
    topics.update(db, config, actor=O, path="baton", title="Again")
    topics.rename(db, config, actor=O, path="baton", new_path="platform")
    make_topic(db, config, "baton")
    rows = history(db)
    assert tuple(rows[0]) == first
    assert [row["operation"] for row in rows] == ["create_topic", "update_topic", "rename_topic", "create_topic"]


def test_a_rename_then_reuse_leaves_two_topics_in_history(db, config):  # FAILS-ON-MAIN
    """The point of the id: the history of the old `baton` and of the new one stay apart."""
    make_topic(db, config, "baton")
    topics.rename(db, config, actor=O, path="baton", new_path="platform")
    make_topic(db, config, "baton")
    first, renamed, second = history(db)
    assert first["topic_id"] == renamed["topic_id"] != second["topic_id"]
    assert decoded(second, "after")["path"] == "baton"


# 3. A refused call writes nothing ---------------------------------------------------------------------

def setup_for_refusals(db, config) -> str:
    make_topic(db, config, "baton")
    make_topic(db, config, "baton/development")
    make_topic(db, config, "reviews")
    return thread(db, config, "baton")


REFUSALS = {
    "create by a role that may not": lambda db, config, cid: refused(
        "forbidden", topics.create, db, config, actor="reviewer", path="elsewhere"),
    "create a duplicate": lambda db, config, cid: refused(
        "invalid_request", topics.create, db, config, actor=O, path="baton"),
    "create below a missing parent": lambda db, config, cid: refused(
        "invalid_request", topics.create, db, config, actor=O, path="nowhere/child"),
    "create with a bad path": lambda db, config, cid: refused(
        "invalid_request", topics.create, db, config, actor=O, path="Not A Topic"),
    "update by a role that may not": lambda db, config, cid: refused(
        "forbidden", topics.update, db, config, actor="reviewer", path="baton", title="x"),
    "update a topic that is not there": lambda db, config, cid: refused(
        "invalid_request", topics.update, db, config, actor=O, path="nowhere", title="x"),
    "update with an unknown member": lambda db, config, cid: refused(
        "unknown_role", topics.update, db, config, actor=O, path="baton", members=["nobody-here"]),
    "rename by a role that may not": lambda db, config, cid: refused(
        "forbidden", topics.rename, db, config, actor="reviewer", path="baton", new_path="platform"),
    "rename a topic that is not there": lambda db, config, cid: refused(
        "invalid_request", topics.rename, db, config, actor=O, path="nowhere", new_path="platform"),
    "rename onto a taken path": lambda db, config, cid: refused(
        "invalid_request", topics.rename, db, config, actor=O, path="baton", new_path="reviews"),
    "rename into itself": lambda db, config, cid: refused(
        "invalid_request", topics.rename, db, config, actor=O, path="baton", new_path="baton/inside"),
    "rename that would make three levels": lambda db, config, cid: refused(
        "invalid_request", topics.rename, db, config, actor=O, path="baton", new_path="reviews/baton"),
    "set_topic by a role that may not": lambda db, config, cid: refused(
        "forbidden", conversations.set_topic, db, config, cid, "reviews", actor="reviewer"),
    "set_topic to a topic that is not there": lambda db, config, cid: refused(
        "invalid_request", conversations.set_topic, db, config, cid, "nowhere", actor=O),
    "set_topic for a conversation that is not there": lambda db, config, cid: refused(
        "unknown_conversation", conversations.set_topic, db, config, "conv_missing", "reviews", actor=O),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_a_refused_call_writes_no_history_and_changes_no_id(db, config, case):  # FAILS-ON-MAIN
    conversation = setup_for_refusals(db, config)
    rows_before, ids_before = [tuple(row) for row in history(db)], all_ids(db)
    REFUSALS[case](db, config, conversation)
    assert [tuple(row) for row in history(db)] == rows_before
    assert all_ids(db) == ids_before


def test_set_topic_by_a_non_participant_writes_no_history(db, config):  # FAILS-ON-MAIN
    make_topic(db, config, "baton")
    make_topic(db, config, "reviews")
    conversation = conversations.create(db, config, actor="reviewer", title="Theirs",
                                        participants=["reviewer", "assistant"], topic="baton")["conversation_id"]
    before = [tuple(row) for row in history(db)]
    refused("not_a_participant", conversations.set_topic, db, config, conversation, "reviews", actor=O)
    assert [tuple(row) for row in history(db)] == before
