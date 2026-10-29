"""An optional readable topic on a conversation: a `project` or `project/subtopic` path.

A topic is a label for people, separate from the stable `conversation_id`, so renaming one moves
nothing. Filtering by it is segment-aware and never widens what a role may see. Synthetic and offline:
generic roles and a temporary data directory.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_conversations import GROUP, count, group_config, refused
from test_messages import rid

from baton import conversations, store
from baton.config import Config
from baton.store import SCHEMA_VERSION, connect

FIXTURES = Path(__file__).parent / "fixtures"
# Every older schema there is a real fixture for. Not read off MIGRATES_FROM on purpose: a version missing
# from that list is a failure to catch, not a case to stop generating.
FIXTURE_VERSIONS = tuple(sorted(int(path.stem.removeprefix("schema_v"))
                                for path in FIXTURES.glob("schema_v*.sql")))


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return group_config(tmp_path)


@pytest.fixture
def db(config: Config) -> sqlite3.Connection:
    connection = connect(config.db_path)
    yield connection
    connection.close()


def open_one(db: sqlite3.Connection, config: Config, title: str, **kwargs: Any) -> str:
    made = conversations.create(db, config, actor="orchestrator", title=title, participants=GROUP,
                                request_id=rid(f"open-{title}"), **kwargs)
    return made["conversation_id"]


def titles(listed: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    return sorted((item["title"], item["topic"]) for item in listed)


# The grammar ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("topic", [
    "Project",                      # uppercase
    "project/sub/deeper",           # three segments
    "project//sub",                 # an empty segment in the middle
    "/project",                     # an empty first segment
    "project/",                     # an empty second segment
    "",                             # empty
    "/",                            # nothing but a separator
    "pro ject",                     # a space
    "pro_ject",                     # an underscore
    "-project",                     # a leading hyphen
    "project-",                     # a trailing hyphen
    "project/-sub",                 # a leading hyphen in the second segment
    "projé",                        # not ASCII
    "pro%ject",                     # a LIKE metacharacter, which filtering must never see
    "pro_ject/x",                   # the other LIKE metacharacter
    7,                              # not a string
    ["project"],                    # not a string
])
def test_a_topic_that_is_not_two_lowercase_slugs_is_refused(db, config, topic: Any) -> None:
    detail = refused("invalid_request", conversations.create, db, config, actor="orchestrator", title="t",
                     participants=GROUP, topic=topic, request_id=rid("bad-topic"))
    assert "topic" in detail, detail
    assert count(db, "conversations") == 0


def test_a_segment_is_bounded_in_length(db, config) -> None:
    longest = "a" * conversations.MAX_TOPIC_SEGMENT
    assert open_one(db, config, "at the limit", topic=f"{longest}/{longest}")
    refused("invalid_request", conversations.create, db, config, actor="orchestrator", title="t",
            participants=GROUP, topic=longest + "a", request_id=rid("too-long"))
    assert count(db, "conversations") == 1


@pytest.mark.parametrize("topic", ["project", "project/sub", "p", "p/q", "a-b/c-d", "a1/b2", "0/9"])
def test_a_well_formed_topic_is_kept_as_written(db, config, topic: str) -> None:
    conversation_id = open_one(db, config, "t", topic=topic)
    [listed] = conversations.list_for(db, "reviewer")
    assert (listed["conversation_id"], listed["topic"]) == (conversation_id, topic)


def test_a_conversation_opens_without_a_topic_and_reads_back_as_none(db, config) -> None:
    open_one(db, config, "untagged")
    [listed] = conversations.list_for(db, "reviewer")
    assert listed["topic"] is None


# Setting, renaming and clearing ---------------------------------------------------------------------------


def test_setting_renaming_and_clearing_a_topic_keeps_the_conversation_id(db, config) -> None:
    conversation_id = open_one(db, config, "t")
    first = conversations.set_topic(db, config, conversation_id, "project", actor="orchestrator")
    assert (first["conversation_id"], first["topic"], first["changed"]) == (conversation_id, "project", True)
    again = conversations.set_topic(db, config, conversation_id, "project", actor="orchestrator")
    assert (again["topic"], again["changed"]) == ("project", False)

    renamed = conversations.set_topic(db, config, conversation_id, "project/alpha", actor="orchestrator")
    assert (renamed["conversation_id"], renamed["topic"], renamed["changed"]) == \
        (conversation_id, "project/alpha", True)
    cleared = conversations.set_topic(db, config, conversation_id, None, actor="orchestrator")
    assert (cleared["conversation_id"], cleared["topic"], cleared["changed"]) == (conversation_id, None, True)

    [listed] = conversations.list_for(db, "reviewer")
    assert (listed["conversation_id"], listed["topic"]) == (conversation_id, None)
    assert count(db, "conversations") == 1  # renaming never makes a second conversation


def test_setting_a_topic_needs_the_permission_and_membership(db, config) -> None:
    conversation_id = open_one(db, config, "t", topic="project")
    # A participant without the permission.
    refused("forbidden", conversations.set_topic, db, config, conversation_id, "other", actor="reviewer")
    # A role with the permission that is not in this conversation: the only managing role here is the
    # creator, so it leaves the conversation first.
    conversations.remove(db, config, conversation_id, "orchestrator", actor="orchestrator")
    refused("not_a_participant", conversations.set_topic, db, config, conversation_id, "other",
            actor="orchestrator")
    refused("unknown_conversation", conversations.set_topic, db, config, "conv_nonexistent", "other",
            actor="orchestrator")
    refused("invalid_request", conversations.set_topic, db, config, conversation_id, "Bad",
            actor="orchestrator")
    [listed] = conversations.list_for(db, "reviewer")
    assert (listed["conversation_id"], listed["topic"]) == (conversation_id, "project")  # nothing changed


def test_a_topic_is_part_of_a_creates_retry_identity(db, config) -> None:
    request_id = rid("topic-once")
    first = conversations.create(db, config, actor="orchestrator", title="t", participants=GROUP,
                                 topic="project", request_id=request_id)
    again = conversations.create(db, config, actor="orchestrator", title="t", participants=GROUP,
                                 topic="project", request_id=request_id)
    assert (again["created"], again["conversation_id"], again["topic"]) == \
        (False, first["conversation_id"], "project")
    refused("idempotency_conflict", conversations.create, db, config, actor="orchestrator", title="t",
            participants=GROUP, topic="project/other", request_id=request_id)
    refused("idempotency_conflict", conversations.create, db, config, actor="orchestrator", title="t",
            participants=GROUP, request_id=request_id)
    assert count(db, "conversations") == 1


# Filtering ------------------------------------------------------------------------------------------------


@pytest.fixture
def tagged(db, config) -> sqlite3.Connection:
    """Four conversations: a topic, a subtopic of it, a topic that merely starts the same way, and none."""
    open_one(db, config, "the project", topic="project")
    open_one(db, config, "a subtopic", topic="project/alpha")
    open_one(db, config, "another subtopic", topic="project/beta")
    open_one(db, config, "a different topic", topic="projectile")
    open_one(db, config, "untagged")
    return db


def test_a_prefix_matches_whole_segments_and_never_a_longer_word(tagged, config) -> None:
    assert titles(conversations.list_for(tagged, "reviewer", topic="project")) == \
        [("a subtopic", "project/alpha"), ("another subtopic", "project/beta"), ("the project", "project")]
    assert titles(conversations.list_for(tagged, "reviewer", topic="projectile")) == \
        [("a different topic", "projectile")]
    assert titles(conversations.list_for(tagged, "reviewer", topic="project/alpha")) == \
        [("a subtopic", "project/alpha")]
    assert conversations.list_for(tagged, "reviewer", topic="proj") == []


def test_an_unfiltered_list_keeps_the_untagged_conversations(tagged, config) -> None:
    assert titles(conversations.list_for(tagged, "reviewer")) == \
        [("a different topic", "projectile"), ("a subtopic", "project/alpha"),
         ("another subtopic", "project/beta"), ("the project", "project"), ("untagged", None)]
    assert titles(conversations.list_all(tagged)) == titles(conversations.list_for(tagged, "reviewer"))
    assert titles(conversations.list_all(tagged, topic="project")) == \
        [("a subtopic", "project/alpha"), ("another subtopic", "project/beta"), ("the project", "project")]


def test_a_filter_must_itself_be_a_well_formed_topic(tagged, config) -> None:
    for bad in ("Project", "project/sub/deeper", "pro%ject", "", "project/"):
        refused("invalid_request", conversations.list_for, tagged, "reviewer", topic=bad)
        refused("invalid_request", conversations.list_all, tagged, topic=bad)


def test_a_topic_never_reveals_a_conversation_the_role_is_not_in(db, config) -> None:
    conversation_id = open_one(db, config, "for the participants", topic="project/secret")
    assert conversations.list_for(db, "outsider", topic="project") == []
    assert conversations.list_for(db, "outsider") == []
    refused("not_a_participant", conversations.history, db, "outsider", conversation_id)
    conversations.remove(db, config, conversation_id, "assistant", actor="orchestrator")
    assert conversations.list_for(db, "assistant", topic="project") == []


# Schema 6 -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("version", FIXTURE_VERSIONS)
def test_every_older_database_gains_a_null_topic(tmp_path: Path, version: int) -> None:
    config = group_config(tmp_path)
    config.data_dir.mkdir(mode=0o700)
    raw = sqlite3.connect(config.db_path)
    raw.executescript((FIXTURES / f"schema_v{version}.sql").read_text())
    if version >= 3:  # conversations exist from version 3 on
        with raw:
            raw.execute("INSERT INTO conversations (id, request_id, request_digest, title, created_by,"
                        " created_at, last_seq) VALUES ('conv_old', 'r', 'd', 'Carried over', 'orchestrator',"
                        " '2026-10-01T00:00:00+00:00', 0)")
            raw.execute("INSERT INTO participants (conversation_id, role, added_at) VALUES"
                        " ('conv_old', 'orchestrator', '2026-10-01T00:00:00+00:00')")
            raw.execute("INSERT INTO participants (conversation_id, role, added_at) VALUES"
                        " ('conv_old', 'reviewer', '2026-10-01T00:00:00+00:00')")
    raw.close()
    os.chmod(config.db_path, 0o600)

    db = connect(config.db_path)
    assert db.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION >= 6
    assert "topic" in {row["name"] for row in db.execute("PRAGMA table_info(conversations)")}
    if version >= 3:
        [carried] = conversations.list_for(db, "reviewer")
        assert (carried["conversation_id"], carried["title"], carried["topic"]) == \
            ("conv_old", "Carried over", None)
        # And a carried-over conversation can be given one straight away.
        assert conversations.set_topic(db, config, "conv_old", "project/carried",
                                       actor="orchestrator")["topic"] == "project/carried"
    db.close()


def test_the_topic_step_names_the_table_it_alters() -> None:
    """The steps alter more than one table now, so each has to say which; a step that named none would
    silently alter `messages` and leave `conversations` without its column."""
    assert SCHEMA_VERSION in store._ADDED_COLUMNS, "the current version adds no column"
    assert store._ADDED_COLUMNS[SCHEMA_VERSION] == {"conversations": ("topic TEXT",)}
    assert {table for step in store._ADDED_COLUMNS.values() for table in step} == {"messages", "conversations"}
