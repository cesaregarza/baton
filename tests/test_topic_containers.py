"""Topics as containers: a core role makes the topic, any identified agent starts a thread in it.

The numbered tests follow the ratified design's list, in its order. Synthetic and offline: generic
roles, a temporary data directory, and no network. Guest behaviour is exercised by handing
`conversations.create` the alias identity the guard resolves, so the rule under test is the one in
the code rather than the transport; the transport's own rules live in test_topic_guest_surface.py.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from baton import config as config_module
from baton import conversations, messages
from baton.config import Config
from baton.errors import BatonError
from baton.store import SCHEMA_VERSION, connect

try:
    from baton import topics
except ImportError:  # written before the module: each test then fails on its own instead of the whole run stopping
    topics = None  # type: ignore[assignment]

WAKE = {"wake": {"command": ["true"]}}
MEMBERS = ["orchestrator", "reviewer"]
ALIAS = {"alias_id": "al_0000000000000001", "alias": "lane-a", "badge": "guest", "code": "ab12",
         "effective_role": "guest"}
OTHER_ALIAS = {**ALIAS, "alias_id": "al_0000000000000002", "alias": "lane-b", "code": "cd34"}


def topic_config(tmp_path: Path, *, data_dir: str = "data", **settings: Any) -> Config:
    """Generic roles: `orchestrator` makes topics, `reviewer` and `assistant` don't, `outsider` is never invited.

    The guest role reads the board, which is what a guest thread requires, and the thread caps are
    low so a cap test needs few rows.
    """
    aliases = {"max_threads_per_alias_per_hour": 2, "max_threads_per_guest_per_hour": 3,
               "guest_cap_alert_role": "orchestrator", **settings.pop("aliases", {})}
    return config_module.parse({
        "host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / data_dir)},
        "roles": {"orchestrator": {"can_create_conversations": True, "can_manage_participants": True,
                                   "can_create_topics": True, "can_view_all_conversations": True, **WAKE},
                  "reviewer": {"can_create_conversations": True, **WAKE},
                  "assistant": WAKE,
                  "outsider": WAKE,
                  "guest": {"can_view_all_conversations": True, **WAKE}},
        "conversations": {"enabled": True, **settings},
        "aliases": aliases,
    })


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return topic_config(tmp_path)


def seed_aliases(conn: sqlite3.Connection, *identities: dict) -> None:
    """The alias rows a guest thread's attribution refers to.

    `conversations.created_by_alias_id` references `aliases(id)`, the same way a guest post's does, so
    a synthetic identity needs its row: the foreign key is the thing that stops attribution pointing
    at an alias nobody ever claimed.
    """
    with conn:
        conn.executemany("INSERT OR IGNORE INTO aliases (id, name, skeleton, verifier, created_at)"
                         " VALUES (?, ?, ?, 'synthetic-verifier', 0.0)",
                         [(one["alias_id"], one["alias"], one["alias"]) for one in identities])


@pytest.fixture
def db(config: Config) -> sqlite3.Connection:
    connection = connect(config.db_path)
    seed_aliases(connection, ALIAS, OTHER_ALIAS,
                 {"alias_id": "al_0000000000000003", "alias": "lane-c"})
    yield connection
    connection.close()


def refused(code: str, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    with pytest.raises(BatonError) as raised:
        operation(*args, **kwargs)
    assert raised.value.code == code, str(raised.value)
    return str(raised.value)


def make_topic(db: sqlite3.Connection, config: Config, path: str = "baton", *, actor: str = "orchestrator",
               members: list[str] | None = MEMBERS, **kwargs: Any) -> dict[str, Any]:
    return topics.create(db, config, actor=actor, path=path, members=members, **kwargs)


def guest_thread(db: sqlite3.Connection, config: Config, *, topic: str | None = "baton",
                 identity: dict | None = ALIAS, title: str = "A guest thread", **kwargs: Any) -> dict[str, Any]:
    return conversations.create(db, config, actor="guest", title=title, participants=None, topic=topic,
                                identity=identity, **kwargs)


def participants_of(db: sqlite3.Connection, conversation_id: str) -> list[str]:
    return [row[0] for row in db.execute("SELECT role FROM participants WHERE conversation_id = ? ORDER BY role",
                                         (conversation_id,))]


def row_for(db: sqlite3.Connection, conversation_id: str) -> sqlite3.Row:
    return db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()


def alerts(db: sqlite3.Connection, role: str = "orchestrator") -> list[sqlite3.Row]:
    """The cap alerts in a role's mailbox, identified by their notice rather than by being senderless."""
    return db.execute("SELECT * FROM messages WHERE recipient = ? AND sender IS NULL"
                      " AND notice LIKE '%guest_threads.cap_reached%' ORDER BY seq", (role,)).fetchall()


def fill_guest_cap(db: sqlite3.Connection, config: Config) -> None:
    make_topic(db, config, "baton")
    for identity in (ALIAS, ALIAS, OTHER_ALIAS):
        guest_thread(db, config, identity=identity)


# 1. A core role creates a topic; a guest is refused; an enrolled alias is judged by its mapped role.

def test_a_core_role_creates_a_topic(db, config):
    made = make_topic(db, config, "baton", title="Baton", description="The board")
    assert made["path"] == "baton"
    assert made["title"] == "Baton"
    assert made["description"] == "The board"
    assert made["members"] == MEMBERS
    assert made["created_by"] == "orchestrator"
    assert made["created"] is True


def test_a_topic_title_defaults_to_its_last_segment(db, config):
    make_topic(db, config, "baton")
    assert topics.create(db, config, actor="orchestrator", path="baton/development",
                         members=MEMBERS)["title"] == "development"


def test_a_role_without_the_flag_may_not_create_a_topic(db, config):
    detail = refused("forbidden", make_topic, db, config, "baton", actor="reviewer")
    assert "can_create_topics" in detail


def test_a_guest_may_not_create_a_topic(db, config):
    refused("forbidden", topics.create, db, config, actor="guest", path="baton", members=MEMBERS)


def test_an_enrolled_alias_is_judged_by_its_mapped_role(db, config):
    """The guard resolves an enrolled alias to its mapped role, so the core path applies to it."""
    made = topics.create(db, config, actor="orchestrator", path="baton", members=MEMBERS)
    assert made["created_by"] == "orchestrator"
    refused("forbidden", topics.create, db, config, actor="assistant", path="other", members=MEMBERS)


def test_an_omitted_members_list_means_empty(db, config):
    """An omitted `members` on create is an empty list, not a default membership."""
    assert topics.create(db, config, actor="orchestrator", path="baton")["members"] == []


def test_a_topic_path_follows_the_existing_grammar(db, config):
    for bad in ("Baton", "baton/", "a/b/c", "-baton", "baton//x", "", "x" * 41):
        refused("invalid_request", make_topic, db, config, bad)


def test_a_duplicate_topic_is_refused(db, config):
    make_topic(db, config, "baton")
    refused("invalid_request", make_topic, db, config, "baton")


def test_a_member_must_be_a_known_non_guest_role(db, config):
    refused("invalid_request", make_topic, db, config, "baton", members=["orchestrator", "guest"])
    refused("unknown_role", make_topic, db, config, "baton", members=["orchestrator", "nobody"])


# 2. A subtopic is refused while its parent is missing and accepted once it exists.

def test_a_subtopic_needs_its_parent(db, config):
    detail = refused("invalid_request", make_topic, db, config, "baton/development")
    assert "baton" in detail
    make_topic(db, config, "baton")
    assert make_topic(db, config, "baton/development")["path"] == "baton/development"


# 3. An aliased guest starts a thread: participants copy the topic's members, and it carries the alias.

def test_an_aliased_guest_starts_a_thread_in_a_topic(db, config):
    make_topic(db, config, "baton")
    made = guest_thread(db, config)
    assert made["created"] is True
    assert participants_of(db, made["conversation_id"]) == sorted(MEMBERS)
    row = row_for(db, made["conversation_id"])
    assert row["created_by"] == "guest"
    assert row["created_by_alias_id"] == ALIAS["alias_id"]
    assert row["created_by_alias"] == "lane-a"
    assert row["created_by_alias_code"] == "ab12"
    assert row["topic"] == "baton"


def test_a_guest_thread_copies_members_rather_than_referring_to_them(db, config):
    """Members are a snapshot: a later member edit must not change an existing thread."""
    make_topic(db, config, "baton")
    made = guest_thread(db, config)
    topics.update(db, config, actor="orchestrator", path="baton", members=["orchestrator", "assistant"])
    assert participants_of(db, made["conversation_id"]) == sorted(MEMBERS)


# 4. An anonymous guest is refused, and so is a guest whose role cannot read the board.

def test_an_anonymous_guest_is_refused(db, config):
    make_topic(db, config, "baton")
    refused("forbidden", guest_thread, db, config, identity=None)


def test_a_guest_that_cannot_read_the_board_is_refused(tmp_path):
    blind = config_module.parse({
        "host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "blind")},
        "roles": {"orchestrator": {"can_create_conversations": True, "can_create_topics": True, **WAKE},
                  "reviewer": {"can_create_conversations": True, **WAKE},
                  "guest": WAKE},
        "conversations": {"enabled": True},
    })
    connection = connect(blind.db_path)
    seed_aliases(connection, ALIAS)
    try:
        topics.create(connection, blind, actor="orchestrator", path="baton", members=MEMBERS)
        detail = refused("forbidden", conversations.create, connection, blind, actor="guest", title="Blind",
                         participants=None, topic="baton", identity=ALIAS)
        assert "can_view_all_conversations" in detail
    finally:
        connection.close()


# 5. A guest create is refused with no topic, a missing topic, too few members, or an unknown member.

def test_a_guest_thread_requires_a_topic(db, config):
    detail = refused("invalid_request", guest_thread, db, config, topic=None)
    assert "topic" in detail


def test_a_guest_thread_requires_the_topic_to_exist(db, config):
    refused("invalid_request", guest_thread, db, config, topic="absent")


def test_a_guest_thread_needs_at_least_two_members(db, config):
    make_topic(db, config, "baton", members=["orchestrator"])
    detail = refused("invalid_request", guest_thread, db, config)
    assert "members" in detail
    topics.update(db, config, actor="orchestrator", path="baton", members=MEMBERS)
    assert guest_thread(db, config)["created"] is True


def test_a_guest_thread_is_refused_when_a_member_role_is_gone(tmp_path):
    """A topic whose membership names a role the config no longer has refuses guest threads, naming it."""
    first = topic_config(tmp_path)
    connection = connect(first.db_path)
    try:
        topics.create(connection, first, actor="orchestrator", path="baton",
                      members=["orchestrator", "assistant"])
    finally:
        connection.close()
    shrunk = config_module.parse({
        "host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")},
        "roles": {"orchestrator": {"can_create_conversations": True, "can_create_topics": True,
                                   "can_view_all_conversations": True, **WAKE},
                  "reviewer": {"can_create_conversations": True, **WAKE},
                  "guest": {"can_view_all_conversations": True, **WAKE}},
        "conversations": {"enabled": True},
    })
    connection = connect(shrunk.db_path)
    seed_aliases(connection, ALIAS)
    try:
        detail = refused("invalid_request", conversations.create, connection, shrunk, actor="guest",
                         title="Gone", participants=None, topic="baton", identity=ALIAS)
        assert "assistant" in detail
    finally:
        connection.close()


# 6. A core role's create with a missing topic is refused; with no topic it is accepted.

def test_a_core_create_refuses_a_missing_topic(db, config):
    detail = refused("invalid_request", conversations.create, db, config, actor="orchestrator",
                     title="Plan", participants=MEMBERS, topic="absent")
    assert "absent" in detail


def test_a_core_create_without_a_topic_is_accepted(db, config):
    made = conversations.create(db, config, actor="orchestrator", title="Plan", participants=MEMBERS)
    assert made["created"] is True
    assert row_for(db, made["conversation_id"])["topic"] is None


def test_a_core_create_in_an_existing_topic_is_accepted(db, config):
    make_topic(db, config, "baton")
    made = conversations.create(db, config, actor="orchestrator", title="Plan", participants=MEMBERS,
                                topic="baton")
    assert row_for(db, made["conversation_id"])["topic"] == "baton"


# 7. A guest naming participants is refused.

def test_a_guest_may_not_name_participants(db, config):
    make_topic(db, config, "baton")
    detail = refused("invalid_request", conversations.create, db, config, actor="guest", title="Mine",
                     participants=["orchestrator", "reviewer"], topic="baton", identity=ALIAS)
    assert "participants" in detail


# 7b. `participants` became optional for the guest path, so what each role owes is pinned here.

def test_a_core_role_that_omits_participants_is_refused(db, config):
    """Optional in the schema, still required of a role: the guest path is the only one without it."""
    detail = refused("invalid_request", conversations.create, db, config, actor="orchestrator",
                     title="No participants", participants=None)
    assert "participants" in detail


def test_a_guests_empty_participants_list_reads_as_omitting_it(db, config):
    """An empty list asks for nobody, which is what leaving it out means, so it is not a refusal."""
    make_topic(db, config, "baton")
    made = conversations.create(db, config, actor="guest", title="Empty list", participants=[],
                                topic="baton", identity=ALIAS)
    assert made["created"] is True
    assert participants_of(db, made["conversation_id"]) == sorted(MEMBERS)


def test_a_guests_non_empty_participants_list_is_refused(db, config):
    """Naming even one role is the guest choosing its own audience, which the topic decides."""
    make_topic(db, config, "baton")
    detail = refused("invalid_request", conversations.create, db, config, actor="guest", title="One role",
                     participants=["orchestrator"], topic="baton", identity=ALIAS)
    assert "participants" in detail


# 8. Caps.

def test_the_per_alias_cap_holds(db, config):
    make_topic(db, config, "baton")
    for _ in range(2):
        guest_thread(db, config)
    detail = refused("limit_exceeded", guest_thread, db, config)
    assert "2" in detail


def test_the_guest_wide_cap_holds_across_aliases(db, config):
    """Three threads from two aliases fill the guest-wide cap, so a third alias is refused."""
    fill_guest_cap(db, config)
    third = {**ALIAS, "alias_id": "al_0000000000000003", "code": "ef56"}
    refused("limit_exceeded", guest_thread, db, config, identity=third)


def test_a_refusal_with_both_caps_full_names_both(db, config):
    fill_guest_cap(db, config)
    detail = refused("limit_exceeded", guest_thread, db, config, identity=ALIAS)
    assert "2" in detail and "3" in detail


def test_concurrent_creates_at_the_limit_cannot_both_succeed(config):
    """SQLite's write lock serialises admission, so the cap is not a read-then-write race."""
    setup = connect(config.db_path)
    seed_aliases(setup, ALIAS)
    try:
        topics.create(setup, config, actor="orchestrator", path="baton", members=MEMBERS)
        conversations.create(setup, config, actor="guest", title="one", participants=None, topic="baton",
                             identity=ALIAS)
    finally:
        setup.close()
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        connection = connect(config.db_path)
        try:
            barrier.wait(timeout=10)
            conversations.create(connection, config, actor="guest", title="race", participants=None,
                                 topic="baton", identity=ALIAS)
            outcomes.append("created")
        except BatonError as error:
            outcomes.append(error.code)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert outcomes.count("created") == 1, outcomes
    assert "limit_exceeded" in outcomes, outcomes


# 9. The guest-wide alert.

def test_a_guest_wide_refusal_stores_one_host_message(db, config):
    fill_guest_cap(db, config)
    refused("limit_exceeded", guest_thread, db, config, identity=OTHER_ALIAS)
    stored = alerts(db)
    assert len(stored) == 1
    assert stored[0]["sender"] is None
    assert stored[0]["conversation_id"] is None
    notice = stored[0]["notice"]
    assert "guest_threads.cap_reached" in notice
    assert '"limit": 3' in notice or '"limit":3' in notice, notice
    assert "1h" in notice


def test_the_alert_survives_the_refusal_it_accompanies(db, config):
    """The refusal is raised after the commit, so the alert is not rolled back with it."""
    fill_guest_cap(db, config)
    refused("limit_exceeded", guest_thread, db, config, identity=OTHER_ALIAS)
    fresh = connect(config.db_path)
    try:
        assert len(alerts(fresh)) == 1
    finally:
        fresh.close()


def test_the_alert_wakes_its_recipient_as_an_ordinary_message(db, config):
    """The alert must arrive as reason=message, which is the only shape the receiver accepts."""
    fill_guest_cap(db, config)
    refused("limit_exceeded", guest_thread, db, config, identity=OTHER_ALIAS)
    stored = alerts(db)[0]
    event = db.execute("SELECT * FROM events WHERE audience = ? AND kind = 'message' ORDER BY seq DESC LIMIT 1",
                       ("orchestrator",)).fetchone()
    assert event is not None, "store_copy must have appended a message event"
    assert event["kind"] == "message"
    assert messages.first_unread_seq(db, "orchestrator") is not None
    assert stored["seq"] >= 1


def test_the_alert_reports_the_configured_limit_not_a_constant(tmp_path):
    """The text reports the configured guest-wide limit, never a constant."""
    config = topic_config(tmp_path, aliases={"max_threads_per_guest_per_hour": 7,
                                             "max_threads_per_alias_per_hour": 7})
    connection = connect(config.db_path)
    seed_aliases(connection, ALIAS, OTHER_ALIAS)
    try:
        topics.create(connection, config, actor="orchestrator", path="baton", members=MEMBERS)
        for index in range(7):
            conversations.create(connection, config, actor="guest", title=f"t{index}", participants=None,
                                 topic="baton", identity=ALIAS)
        refused("limit_exceeded", conversations.create, connection, config, actor="guest", title="over",
                participants=None, topic="baton", identity=ALIAS)
        stored = alerts(connection)
        assert len(stored) == 1
        assert '"limit": 7' in stored[0]["notice"] or '"limit":7' in stored[0]["notice"]
        assert "50" not in stored[0]["text"]
    finally:
        connection.close()


def test_a_second_refusal_within_the_hour_stores_no_alert(db, config):
    fill_guest_cap(db, config)
    refused("limit_exceeded", guest_thread, db, config, identity=OTHER_ALIAS)
    refused("limit_exceeded", guest_thread, db, config, identity=OTHER_ALIAS)
    assert len(alerts(db)) == 1


def test_the_dedup_survives_a_restart(db, config):
    """The timestamp is persisted, so a new process does not alert again inside the hour."""
    fill_guest_cap(db, config)
    refused("limit_exceeded", guest_thread, db, config, identity=OTHER_ALIAS)
    fresh = connect(config.db_path)
    try:
        refused("limit_exceeded", conversations.create, fresh, config, actor="guest", title="again",
                participants=None, topic="baton", identity=OTHER_ALIAS)
        assert len(alerts(fresh)) == 1
    finally:
        fresh.close()


def test_a_full_alert_mailbox_stores_nothing_and_leaves_the_timestamp_unset(db, config):
    """Nothing is stored, and because the timestamp stays unset the next refusal tries again."""
    fill_guest_cap(db, config)
    with db:
        db.execute("INSERT INTO mailboxes (recipient, last_seq, read_seq) VALUES ('orchestrator', ?, 0)"
                   " ON CONFLICT (recipient) DO UPDATE SET last_seq = excluded.last_seq, read_seq = 0",
                   (messages.MAX_UNREAD,))
        for seq in range(1, messages.MAX_UNREAD + 1):
            db.execute("INSERT INTO messages (id, recipient, seq, text, at) VALUES (?, 'orchestrator', ?, 'x', ?)",
                       (f"msg_{seq:016x}", seq, "2026-11-01T00:00:00+00:00"))
    refused("limit_exceeded", guest_thread, db, config, identity=OTHER_ALIAS)
    assert alerts(db) == []
    assert db.execute("SELECT value FROM log_meta WHERE key = 'guest_thread_cap_alerted_at'").fetchone() is None


def test_no_alert_role_means_no_alert(tmp_path):
    config = topic_config(tmp_path, aliases={"guest_cap_alert_role": None})
    connection = connect(config.db_path)
    seed_aliases(connection, ALIAS, OTHER_ALIAS)
    try:
        topics.create(connection, config, actor="orchestrator", path="baton", members=MEMBERS)
        for identity in (ALIAS, ALIAS, OTHER_ALIAS):
            conversations.create(connection, config, actor="guest", title="t", participants=None,
                                 topic="baton", identity=identity)
        refused("limit_exceeded", conversations.create, connection, config, actor="guest", title="over",
                participants=None, topic="baton", identity=OTHER_ALIAS)
        assert alerts(connection) == []
    finally:
        connection.close()


# 10. Replay.

def test_a_replay_under_the_same_alias_returns_the_original(db, config):
    make_topic(db, config, "baton")
    first = guest_thread(db, config, request_id="20261101T120000Z-aaaa")
    again = guest_thread(db, config, request_id="20261101T120000Z-aaaa")
    assert again["created"] is False
    assert again["conversation_id"] == first["conversation_id"]


def test_a_replay_uses_no_quota_and_is_not_refused_by_a_full_cap(db, config):
    """The replay comes back after the cap filled, because it creates nothing."""
    make_topic(db, config, "baton")
    first = guest_thread(db, config, request_id="20261101T120000Z-bbbb")
    guest_thread(db, config)
    refused("limit_exceeded", guest_thread, db, config)
    again = guest_thread(db, config, request_id="20261101T120000Z-bbbb")
    assert again["created"] is False
    assert again["conversation_id"] == first["conversation_id"]


def test_a_replay_returns_the_thread_as_it_now_stands(db, config):
    make_topic(db, config, "baton")
    first = guest_thread(db, config, request_id="20261101T120000Z-cccc")
    topics.update(db, config, actor="orchestrator", path="baton", members=["orchestrator", "assistant"])
    topics.rename(db, config, actor="orchestrator", path="baton", new_path="relay")
    # A retry resends the original request, naming the path it used; what comes back is the thread as
    # it now stands, so the rename shows in the result rather than being required in the request.
    again = guest_thread(db, config, request_id="20261101T120000Z-cccc")
    assert again["created"] is False
    assert again["conversation_id"] == first["conversation_id"]
    assert again["topic"] == "relay"


def test_another_alias_reusing_a_request_id_gets_a_generic_conflict(db, config):
    make_topic(db, config, "baton")
    guest_thread(db, config, identity=ALIAS, request_id="20261101T120000Z-dddd")
    detail = refused("idempotency_conflict", guest_thread, db, config, identity=OTHER_ALIAS,
                     request_id="20261101T120000Z-dddd")
    assert "lane-a" not in detail
    assert "conv_" not in detail


def test_a_replay_is_refused_once_the_role_lost_its_flag(tmp_path):
    """Authority is checked before the replay lookup, so a withdrawn flag stops a retry."""
    config = topic_config(tmp_path)
    connection = connect(config.db_path)
    seed_aliases(connection, ALIAS, OTHER_ALIAS)
    try:
        topics.create(connection, config, actor="orchestrator", path="baton", members=MEMBERS,
                      request_id="20261101T120000Z-eeee")
    finally:
        connection.close()
    stripped = config_module.parse({
        "host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")},
        "roles": {"orchestrator": {"can_create_conversations": True, **WAKE},
                  "reviewer": {"can_create_conversations": True, **WAKE},
                  "guest": {"can_view_all_conversations": True, **WAKE}},
        "conversations": {"enabled": True},
    })
    connection = connect(stripped.db_path)
    try:
        refused("forbidden", topics.create, connection, stripped, actor="orchestrator", path="baton",
                members=MEMBERS, request_id="20261101T120000Z-eeee")
    finally:
        connection.close()


# 11. A create_topic replay after a rename returns the renamed topic.

def test_a_create_topic_replay_after_a_rename_returns_the_renamed_topic(db, config):
    make_topic(db, config, "baton", request_id="20261101T120000Z-ffff")
    topics.rename(db, config, actor="orchestrator", path="baton", new_path="relay")
    again = make_topic(db, config, "baton", request_id="20261101T120000Z-ffff")
    assert again["created"] is False
    assert again["path"] == "relay"


# 12. update_topic leaves existing threads' participants unchanged.

def test_update_topic_changes_no_existing_thread(db, config):
    make_topic(db, config, "baton")
    thread = conversations.create(db, config, actor="orchestrator", title="Plan", participants=MEMBERS,
                                  topic="baton")
    topics.update(db, config, actor="orchestrator", path="baton", members=["orchestrator", "assistant"])
    assert participants_of(db, thread["conversation_id"]) == sorted(MEMBERS)


def test_an_omitted_members_on_update_keeps_the_current_list(db, config):
    """Omitted on update means unchanged, unlike omitted on create, which means none."""
    make_topic(db, config, "baton")
    topics.update(db, config, actor="orchestrator", path="baton", title="Renamed")
    assert topics.get(db, config, reader="orchestrator", path="baton")["members"] == MEMBERS


def test_update_topic_needs_the_flag(db, config):
    make_topic(db, config, "baton")
    refused("forbidden", topics.update, db, config, actor="reviewer", path="baton", title="No")


# 13. rename_topic.

def test_rename_carries_subtopics_and_threads_with_ids_unchanged(db, config):
    make_topic(db, config, "baton")
    make_topic(db, config, "baton/development")
    thread = conversations.create(db, config, actor="orchestrator", title="Plan", participants=MEMBERS,
                                  topic="baton/development")
    under = conversations.create(db, config, actor="orchestrator", title="Top", participants=MEMBERS,
                                 topic="baton")
    topics.rename(db, config, actor="orchestrator", path="baton", new_path="relay")
    assert topics.get(db, config, reader="orchestrator", path="relay")["path"] == "relay"
    assert topics.get(db, config, reader="orchestrator", path="relay/development")["path"] == "relay/development"
    assert row_for(db, thread["conversation_id"])["topic"] == "relay/development"
    assert row_for(db, under["conversation_id"])["topic"] == "relay"
    assert participants_of(db, thread["conversation_id"]) == sorted(MEMBERS)


def test_rename_refuses_and_changes_nothing(db, config):
    make_topic(db, config, "baton")
    make_topic(db, config, "baton/development")
    make_topic(db, config, "other")
    for new_path, why in (("baton", "itself"), ("baton/development", "its own subtree"), ("other", "a collision"),
                          ("absent/child", "a missing parent"), ("Baton", "an invalid path")):
        refused("invalid_request", topics.rename, db, config, actor="orchestrator", path="baton",
                new_path=new_path)
        assert topics.get(db, config, reader="orchestrator", path="baton")["path"] == "baton", why
    # A topic that has subtopics may not move under another topic: that would make three levels.
    refused("invalid_request", topics.rename, db, config, actor="orchestrator", path="baton",
            new_path="other/moved")
    assert topics.get(db, config, reader="orchestrator", path="baton/development")["path"] == "baton/development"


def test_rename_needs_the_flag(db, config):
    make_topic(db, config, "baton")
    refused("forbidden", topics.rename, db, config, actor="reviewer", path="baton", new_path="relay")


# 14. set_topic.

def test_set_topic_refuses_a_missing_topic(db, config):
    thread = conversations.create(db, config, actor="orchestrator", title="Plan", participants=MEMBERS)
    refused("invalid_request", conversations.set_topic, db, config, thread["conversation_id"], "absent",
            actor="orchestrator")


def test_set_topic_keeps_its_existing_permission_and_participants(db, config):
    make_topic(db, config, "baton")
    thread = conversations.create(db, config, actor="orchestrator", title="Plan", participants=MEMBERS)
    refused("forbidden", conversations.set_topic, db, config, thread["conversation_id"], "baton",
            actor="reviewer")
    conversations.set_topic(db, config, thread["conversation_id"], "baton", actor="orchestrator")
    assert row_for(db, thread["conversation_id"])["topic"] == "baton"
    assert participants_of(db, thread["conversation_id"]) == sorted(MEMBERS)


def test_set_topic_can_still_clear_a_topic(db, config):
    make_topic(db, config, "baton")
    thread = conversations.create(db, config, actor="orchestrator", title="Plan", participants=MEMBERS,
                                  topic="baton")
    conversations.set_topic(db, config, thread["conversation_id"], None, actor="orchestrator")
    assert row_for(db, thread["conversation_id"])["topic"] is None


# 15. list_topics counts only the threads the caller can read.

def test_list_topics_counts_only_readable_threads(db, config):
    make_topic(db, config, "baton")
    conversations.create(db, config, actor="orchestrator", title="Seen", participants=MEMBERS, topic="baton")
    conversations.create(db, config, actor="reviewer", title="Unseen", participants=["reviewer", "assistant"],
                         topic="baton")
    listed = {entry["path"]: entry for entry in topics.listing(db, config, reader="orchestrator")}
    assert listed["baton"]["threads"] == 2, "orchestrator views all conversations"
    outsiders = {entry["path"]: entry for entry in topics.listing(db, config, reader="assistant")}
    assert outsiders["baton"]["threads"] == 1, "assistant is only in one of them"


def test_list_topics_filters_by_parent(db, config):
    make_topic(db, config, "baton")
    make_topic(db, config, "baton/development")
    make_topic(db, config, "other")
    assert [e["path"] for e in topics.listing(db, config, reader="orchestrator", parent="baton")] == \
        ["baton/development"]


def test_get_topic_is_open_to_a_guest(db, config):
    make_topic(db, config, "baton", description="The board")
    seen = topics.get(db, config, reader="guest", path="baton")
    assert seen["path"] == "baton"
    assert seen["description"] == "The board"


def test_get_an_absent_topic_is_not_found(db, config):
    refused("unknown_topic", topics.get, db, config, reader="orchestrator", path="absent")


# The schema assertion is against the constant, never a literal.

def test_the_topics_table_is_part_of_the_current_schema(db, config):
    assert db.execute("SELECT version FROM schema_version").fetchone()["version"] == SCHEMA_VERSION
    columns = {row["name"] for row in db.execute("PRAGMA table_info(topics)")}
    assert {"path", "title", "description", "members", "created_by", "created_at"} <= columns
    conversation_columns = {row["name"] for row in db.execute("PRAGMA table_info(conversations)")}
    assert {"created_by_alias_id", "created_by_alias", "created_by_alias_code"} <= conversation_columns

# A count and a timestamp must be taken over the same threads: the reader's own.

def dated(db: sqlite3.Connection, conversation_id: str, when: str) -> None:
    """Set a thread's activity time directly. What is under test is which rows are aggregated, not how
    the column comes to be written, and a fixed value keeps the assertion off the clock."""
    with db:
        db.execute("UPDATE conversations SET last_activity_at = ? WHERE id = ?", (when, conversation_id))


def test_the_listed_activity_time_covers_only_the_readable_threads(db, config):
    """A newer thread the reader cannot see must not lend the topic its timestamp.

    The thread count was already the reader's own, so a time taken over every thread disclosed that
    something had happened in a conversation the reader is not in, and when.
    """
    make_topic(db, config, "baton")
    mine = conversations.create(db, config, actor="orchestrator", title="Mine",
                                participants=["orchestrator", "reviewer"], topic="baton")
    theirs = conversations.create(db, config, actor="orchestrator", title="Not mine",
                                  participants=["orchestrator", "assistant"], topic="baton")
    dated(db, mine["conversation_id"], "2026-11-01T10:00:00+00:00")
    dated(db, theirs["conversation_id"], "2026-11-02T10:00:00+00:00")   # newer, and unreadable

    [entry] = topics.listing(db, config, reader="reviewer")
    assert entry["threads"] == 1, "the count was already the reader's own"
    assert entry["last_activity_at"] == "2026-11-01T10:00:00+00:00", \
        "the time must come from the same threads the count did"


def test_a_board_reader_still_sees_the_newest_of_everything(db, config):
    make_topic(db, config, "baton")
    mine = conversations.create(db, config, actor="orchestrator", title="Mine",
                                participants=["orchestrator", "reviewer"], topic="baton")
    theirs = conversations.create(db, config, actor="orchestrator", title="Theirs",
                                  participants=["orchestrator", "assistant"], topic="baton")
    dated(db, mine["conversation_id"], "2026-11-01T10:00:00+00:00")
    dated(db, theirs["conversation_id"], "2026-11-02T10:00:00+00:00")
    [entry] = topics.listing(db, config, reader="orchestrator")
    assert entry["threads"] == 2
    assert entry["last_activity_at"] == "2026-11-02T10:00:00+00:00"


def test_a_topic_the_reader_sees_nothing_in_reports_no_activity(db, config):
    make_topic(db, config, "baton")
    theirs = conversations.create(db, config, actor="orchestrator", title="Not mine",
                                  participants=["orchestrator", "assistant"], topic="baton")
    dated(db, theirs["conversation_id"], "2026-11-02T10:00:00+00:00")
    [entry] = topics.listing(db, config, reader="reviewer")
    assert entry["threads"] == 0
    assert entry["last_activity_at"] is None, "no readable thread means nothing to report"

