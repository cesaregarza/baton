"""Schema 15: a schema-14 database gains the topics table, with a topic discovered for every stored label.

The migration is data-driven: it reads the labels that are actually there rather than a hard-coded
list, adds any missing parent, and takes each topic's members from the threads filed directly under
it. Nothing is cut to fit: a membership that cannot be represented is left empty and reported, and a
stored label that fails today's grammar stops the upgrade instead of being silently dropped.
Synthetic and offline: a fixture dump of schema 14, never a live database.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from baton import store
from baton.config import Config
from baton.errors import BatonError
from baton.store import connect

from test_topic_containers import topic_config

V14_SCHEMA = (Path(__file__).parent / "fixtures" / "schema_v14.sql").read_text()
V14_VERSION = 14  # the version the fixture dump holds, not an assertion about the current one


def v14_database(config: Config, threads: list[tuple[str, str | None, list[str]]]) -> None:
    """A schema-14 database holding `threads` as (conversation_id, topic, participants)."""
    config.data_dir.mkdir(mode=0o700, parents=True)
    raw = sqlite3.connect(config.db_path)
    raw.executescript(V14_SCHEMA)
    with raw:
        for index, (conversation_id, topic, participants) in enumerate(threads, 1):
            raw.execute("INSERT INTO conversations (id, request_id, request_digest, title, created_by,"
                        " created_at, topic) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (conversation_id, f"req-{index}", f"digest-{index}", f"Thread {index}",
                         participants[0], "2026-10-01T00:00:00+00:00", topic))
            raw.executemany("INSERT INTO participants (conversation_id, role, added_at) VALUES (?, ?, ?)",
                            [(conversation_id, role, "2026-10-01T00:00:00+00:00") for role in participants])
    raw.close()
    os.chmod(config.db_path, 0o600)


def topics_in(db: sqlite3.Connection) -> dict[str, dict]:
    return {row["path"]: {"title": row["title"], "created_by": row["created_by"],
                          "members": json.loads(row["members"])}
            for row in db.execute("SELECT * FROM topics")}


def test_the_upgrade_discovers_every_stored_label_and_its_missing_parents(tmp_path: Path) -> None:
    config = topic_config(tmp_path)
    v14_database(config, [
        ("conv_1", "baton", ["orchestrator", "reviewer"]),
        ("conv_2", "baton/development", ["orchestrator", "reviewer"]),
        ("conv_3", "knowledge-exchange/help", ["orchestrator", "assistant"]),  # parent never stored
        ("conv_4", None, ["orchestrator", "reviewer"]),                        # untopiced, left alone
    ])
    db = connect(config.db_path)
    try:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == store.SCHEMA_VERSION
        found = topics_in(db)
        assert set(found) == {"baton", "baton/development", "knowledge-exchange", "knowledge-exchange/help"}
        assert all(entry["created_by"] == "migration" for entry in found.values())
        assert found["baton/development"]["title"] == "development"
        assert db.execute("SELECT topic FROM conversations WHERE id = 'conv_4'").fetchone()[0] is None
    finally:
        db.close()


def test_members_come_from_direct_threads_only(tmp_path: Path) -> None:
    """A parent does not inherit its subtopic's members, and an implicit parent starts empty."""
    config = topic_config(tmp_path)
    v14_database(config, [
        ("conv_1", "baton", ["orchestrator", "reviewer"]),
        ("conv_2", "baton/development", ["orchestrator", "assistant"]),
        ("conv_3", "knowledge-exchange/help", ["orchestrator", "assistant"]),
    ])
    db = connect(config.db_path)
    try:
        found = topics_in(db)
        assert found["baton"]["members"] == ["orchestrator", "reviewer"]
        assert found["baton/development"]["members"] == ["assistant", "orchestrator"]
        assert found["knowledge-exchange"]["members"] == [], "an implicit parent has no thread of its own"
    finally:
        db.close()


def test_a_member_the_config_no_longer_has_is_dropped(tmp_path: Path) -> None:
    config = topic_config(tmp_path)
    v14_database(config, [("conv_1", "baton", ["orchestrator", "reviewer", "departed"])])
    db = connect(config.db_path)
    try:
        assert topics_in(db)["baton"]["members"] == ["orchestrator", "reviewer"]
    finally:
        db.close()


def test_a_guest_participant_is_never_backfilled_as_a_member(tmp_path: Path) -> None:
    config = topic_config(tmp_path)
    v14_database(config, [("conv_1", "baton", ["orchestrator", "reviewer", "guest"])])
    db = connect(config.db_path)
    try:
        assert "guest" not in topics_in(db)["baton"]["members"]
    finally:
        db.close()


def test_a_membership_below_two_is_left_empty(tmp_path: Path) -> None:
    """Nothing is invented to reach the guest-thread minimum; a core role fixes it with update_topic."""
    config = topic_config(tmp_path)
    v14_database(config, [("conv_1", "baton", ["orchestrator"])])
    db = connect(config.db_path)
    try:
        assert topics_in(db)["baton"]["members"] == []
    finally:
        db.close()


def test_a_membership_above_the_maximum_is_left_empty(tmp_path: Path) -> None:
    config = topic_config(tmp_path, max_participants=2)
    v14_database(config, [("conv_1", "baton", ["orchestrator", "reviewer", "assistant"])])
    db = connect(config.db_path)
    try:
        assert topics_in(db)["baton"]["members"] == [], "three members cannot fit a maximum of two"
    finally:
        db.close()


def test_a_stored_label_that_fails_the_grammar_stops_the_upgrade(tmp_path: Path) -> None:
    """The label is named, and nothing is half-migrated: the version stays at 14."""
    config = topic_config(tmp_path)
    v14_database(config, [("conv_1", "Not A Topic", ["orchestrator", "reviewer"])])
    with pytest.raises(BatonError) as raised:
        connect(config.db_path)
    assert "Not A Topic" in str(raised.value)
    raw = sqlite3.connect(config.db_path)
    try:
        assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == V14_VERSION
        assert raw.execute("SELECT 1 FROM sqlite_master WHERE name = 'topics'").fetchone() is None
    finally:
        raw.close()


def test_an_already_upgraded_database_is_left_alone(tmp_path: Path) -> None:
    config = topic_config(tmp_path)
    v14_database(config, [("conv_1", "baton", ["orchestrator", "reviewer"])])
    first = connect(config.db_path)
    try:
        first.execute("UPDATE topics SET title = 'Edited' WHERE path = 'baton'")
        first.commit()
    finally:
        first.close()
    again = connect(config.db_path)
    try:
        assert topics_in(again)["baton"]["title"] == "Edited", "a second open must not re-run the backfill"
    finally:
        again.close()


def test_the_new_conversation_columns_arrive_empty(tmp_path: Path) -> None:
    config = topic_config(tmp_path)
    v14_database(config, [("conv_1", "baton", ["orchestrator", "reviewer"])])
    db = connect(config.db_path)
    try:
        row = db.execute("SELECT * FROM conversations WHERE id = 'conv_1'").fetchone()
        assert row["created_by_alias_id"] is None
        assert row["created_by_alias"] is None
        assert row["created_by_alias_code"] is None
    finally:
        db.close()


def test_the_version_this_fixture_holds_is_upgradable() -> None:
    """Pinned to the fixture's version, never to the new number: a literal would break a stacked lane."""
    assert V14_VERSION in store.MIGRATES_FROM
    assert store.SCHEMA_VERSION > V14_VERSION
