"""Quiet wakes: a role woken only by the messages that address it, and the attention that addresses them.

The numbered tests are the ticket's, in its order: 4, 5, 7 and 8. A quiet role still receives every message
in its mailbox; what it does not get is a wake for each one. Synthetic and offline: generic roles, a
temporary data directory, and a recording stand-in for wake delivery.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_conversations import APPROVAL, GROUP, count, job_states, refused, say, start, woken
from test_messages import rid
from test_wakes import Recorder

from baton import config as config_module
from baton import conversations, jobs, messages, wakes
from baton.config import Config, ConfigError
from baton.store import connect

QUIET = "assistant"


def quiet_config(tmp_path: Path, *, hooks: tuple[str, ...] = ("reviewer", "assistant"), quiet: str = QUIET,
                 job_states_table: dict[str, Any] | None = None, **settings: Any) -> Config:
    """Lane A's generic roles. Every role in `hooks` can be woken; `quiet` only when a message addresses it."""
    roles: dict[str, Any] = {"orchestrator": {"can_create_conversations": True, "can_manage_participants": True},
                             "reviewer": {}, "assistant": {}, "outsider": {}}
    for role in hooks:
        roles[role] = {**roles[role], "wake": {"command": ["true"]}}
    roles[quiet] = {**roles[quiet], "wake": {"command": ["true"], "only_when_addressed": True}}
    raw: dict[str, Any] = {"host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")},
                           "roles": roles, "conversations": {"enabled": True, **settings}}
    if job_states_table is not None:
        raw["jobs"] = job_states_table
    return config_module.parse(raw)


def woken_then_reminded(seq: int) -> list[tuple[str, str]]:
    """What `queued` shows once a message wake is delivered: the wake, and the two reminders it sets.

    Delivering a message wake now schedules a reminder for five minutes and one for twenty, in case
    the role never reads. Neither is due here, so both sit scheduled.
    """
    return [(f"messages:{seq}", "delivered"),
            (f"messages:{seq}:r1", "scheduled"),
            (f"messages:{seq}:r2", "scheduled")]


def queued(db: sqlite3.Connection, role: str) -> list[tuple[str, str]]:
    """The wakes owed to `role`: what each is about, and how it ended."""
    return sorted((row["key"], row["status"]) for row in wakes.recent(db) if row["role"] == role)


# 4 -------------------------------------------------------------------------------------------------------


def test_4_a_quiet_role_is_woken_by_the_message_that_names_it_and_by_no_other(tmp_path) -> None:
    config = quiet_config(tmp_path)
    db = connect(config.db_path)
    conversation_id = start(db, config)

    say(db, config, "orchestrator", conversation_id, "chatter the quiet role need not be told about")
    assert [c["role"] for c in woken(config)] == ["reviewer"]  # a role that asked for everything still gets it
    assert queued(db, QUIET) == []

    say(db, config, "orchestrator", conversation_id, "your call", attention=[QUIET])
    assert [(c["role"], c["reason"], c["first_unread_seq"]) for c in woken(config)] == [(QUIET, "message", 1)]
    assert queued(db, QUIET) == woken_then_reminded(1)
    page = messages.read(db, QUIET, after_seq=0)
    assert [(m["attention"], m["needs_decision"]) for m in page["messages"]] == [(None, False), ([QUIET], False)]


def test_4_needs_decision_wakes_a_quiet_role_even_when_it_names_nobody(tmp_path) -> None:
    config = quiet_config(tmp_path, hooks=(QUIET,))
    db = connect(config.db_path)
    conversation_id = start(db, config)
    say(db, config, "orchestrator", conversation_id, "chatter")
    assert woken(config) == []
    assert queued(db, QUIET) == []

    say(db, config, "reviewer", conversation_id, "which way do we go?", needs_decision=True)
    assert [(c["role"], c["first_unread_seq"]) for c in woken(config)] == [(QUIET, 1)]
    page = messages.read(db, QUIET, after_seq=0)
    assert [(m["text"], m["needs_decision"]) for m in page["messages"]] == \
        [("chatter", False), ("which way do we go?", True)]


def test_4_a_host_notice_does_not_wake_a_quiet_role(tmp_path) -> None:
    config = quiet_config(tmp_path, hooks=("reviewer", QUIET))
    db = connect(config.db_path)
    conversation_id = start(db, config, ["orchestrator", "reviewer"])
    conversations.add(db, config, conversation_id, QUIET, actor="orchestrator")
    assert [c["role"] for c in woken(config)] == ["reviewer"]  # the loud participant is told
    assert queued(db, QUIET) == []
    assert [m["notice"]["event"] for m in messages.read(db, QUIET, after_seq=0)["messages"]] == \
        ["participant_added"]


def test_4_a_quiet_escalation_role_is_still_told_a_conversation_is_over_its_limit(tmp_path) -> None:
    config = quiet_config(tmp_path, hooks=(QUIET,), max_messages_per_hour=1, escalation_role=QUIET)
    db = connect(config.db_path)
    conversation_id = start(db, config)
    say(db, config, "orchestrator", conversation_id, "only one")
    assert woken(config) == []

    refused("conversation_rate_limited", say, db, config, "reviewer", conversation_id, "two")
    [call] = woken(config)
    assert (call["role"], call["reason"], call["conversation_id"]) == (QUIET, "conversation.rate_limited",
                                                                      conversation_id)
    assert set(call) == {"id", "type", "role", "host", "reason", "conversation_id"}  # content-free


# 5 -------------------------------------------------------------------------------------------------------


def test_5_marking_a_message_needs_decision_changes_no_job(tmp_path) -> None:
    config = quiet_config(tmp_path)
    db = connect(config.db_path)
    first = jobs.create(db, config.jobs, request_id=rid("job-a"), actor="orchestrator", title="a")
    jobs.create(db, config.jobs, request_id=rid("job-b"), actor="orchestrator", title="b")
    jobs.move(db, config.jobs, first["job_id"], "working", actor="reviewer")
    before = job_states(db)
    conversation_id = start(db, config)

    decision = say(db, config, "reviewer", conversation_id, APPROVAL, job_id=first["job_id"],
                   needs_decision=True, attention=["orchestrator", QUIET])
    say(db, config, "reviewer", conversation_id, "and this one answers it", reply_to=decision["message_id"],
        needs_decision=True)
    for role in GROUP:
        messages.read(db, role)
    woken(config)
    assert job_states(db) == before


# 7 -------------------------------------------------------------------------------------------------------


def test_7_unread_quiet_messages_still_leave_exactly_one_wake_for_the_one_that_addresses_it(tmp_path) -> None:
    config = quiet_config(tmp_path)
    db = connect(config.db_path)
    conversation_id = start(db, config)
    for index in range(3):
        say(db, config, "orchestrator", conversation_id, f"chatter {index}")
    assert [c["role"] for c in woken(config)] == ["reviewer"]
    assert queued(db, QUIET) == []

    say(db, config, "reviewer", conversation_id, "your call", attention=[QUIET])
    assert [(c["role"], c["first_unread_seq"]) for c in woken(config)] == [(QUIET, 1)]  # from its first unread
    for index in range(2):
        say(db, config, "orchestrator", conversation_id, f"more chatter {index}")
    assert woken(config) == []
    assert queued(db, QUIET) == woken_then_reminded(1)

    assert len(messages.read(db, QUIET)["messages"]) == 6  # every message was there to read all along
    say(db, config, "orchestrator", conversation_id, "chatter after the read")
    assert woken(config) == []
    # The read answered the batch, so its reminders are retired rather than left to fire later.
    assert queued(db, QUIET) == [("messages:1", "delivered"),
                                 ("messages:1:r1", "superseded"),
                                 ("messages:1:r2", "superseded")]


def test_7_a_failed_wake_is_retried_under_one_id_and_never_duplicated(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 0.0)
    config = quiet_config(tmp_path, hooks=(QUIET,))
    db = connect(config.db_path)
    conversation_id = start(db, config)
    say(db, config, "orchestrator", conversation_id, "chatter")
    say(db, config, "orchestrator", conversation_id, "your decision, please", needs_decision=True)

    recorder = Recorder(False, False)  # two failures, then success
    dispatcher = wakes.Dispatcher(config, recorder)
    for _ in range(3):
        dispatcher.pass_once()
    assert [c["first_unread_seq"] for c in recorder.calls] == [1, 1, 1]
    assert len({c["id"] for c in recorder.calls}) == 1  # one wake, delivered three times over
    assert queued(db, QUIET) == woken_then_reminded(1)

    say(db, config, "orchestrator", conversation_id, "more chatter")
    dispatcher.pass_once()
    assert len(recorder.calls) == 3
    assert queued(db, QUIET) == woken_then_reminded(1)


def test_7_a_read_before_the_message_that_addresses_it_still_leaves_one_wake(tmp_path) -> None:
    config = quiet_config(tmp_path, hooks=(QUIET,))
    db = connect(config.db_path)
    conversation_id = start(db, config)
    say(db, config, "orchestrator", conversation_id, "chatter")
    messages.read(db, QUIET)
    say(db, config, "orchestrator", conversation_id, "your call", attention=[QUIET])
    assert [(c["role"], c["first_unread_seq"]) for c in woken(config)] == [(QUIET, 2)]
    assert queued(db, QUIET) == woken_then_reminded(2)


# 8 -------------------------------------------------------------------------------------------------------


def test_8_attention_naming_a_role_outside_the_conversation_is_refused_and_adds_nobody(tmp_path) -> None:
    config = quiet_config(tmp_path)
    db = connect(config.db_path)
    conversation_id = start(db, config)
    say(db, config, "orchestrator", conversation_id, "chatter")
    woken(config)
    conversations.remove(db, config, conversation_id, "reviewer", actor="orchestrator")
    participants = conversations.list_for(db, "orchestrator")[0]["participants"]
    before = (count(db, "messages"), count(db, "events"), count(db, "message_requests"))

    for code, attention in (("not_a_participant", ["outsider"]), ("not_a_participant", ["reviewer"]),
                            ("not_a_participant", [QUIET, "reviewer"]), ("unknown_role", ["nobody"]),
                            ("invalid_request", QUIET), ("invalid_request", [QUIET, QUIET]),
                            ("invalid_request", [7]), ("invalid_request", [])):
        refused(code, say, db, config, "orchestrator", conversation_id, "x", attention=attention)
    assert (count(db, "messages"), count(db, "events"), count(db, "message_requests")) == before
    assert conversations.list_for(db, "orchestrator")[0]["participants"] == participants
    assert woken(config) == []
    assert queued(db, QUIET) == []


def test_8_a_quiet_role_named_only_in_the_text_is_not_woken(tmp_path) -> None:
    config = quiet_config(tmp_path, hooks=(QUIET,))
    db = connect(config.db_path)
    conversation_id = start(db, config)
    say(db, config, "orchestrator", conversation_id, f"@{QUIET} please look at this, {QUIET}!")
    assert woken(config) == []
    assert queued(db, QUIET) == []

    say(db, config, "orchestrator", conversation_id, "now properly", attention=[QUIET])
    assert [c["role"] for c in woken(config)] == [QUIET]


def test_8_a_sender_may_name_itself_and_is_still_not_woken_by_its_own_message(tmp_path) -> None:
    config = quiet_config(tmp_path, hooks=(QUIET,))
    db = connect(config.db_path)
    conversation_id = start(db, config)
    sent = say(db, config, QUIET, conversation_id, "a note to the room and to me", attention=[QUIET, "reviewer"])
    assert sorted(sent["seqs"]) == ["orchestrator", "reviewer"]
    assert woken(config) == []
    assert queued(db, QUIET) == []


def test_8_attention_and_needs_decision_belong_to_a_conversation(tmp_path) -> None:
    config = quiet_config(tmp_path)
    db = connect(config.db_path)
    for extra in ({"attention": [QUIET]}, {"needs_decision": True}):
        detail = refused("invalid_request", messages.send, db, sender="orchestrator", recipient=QUIET,
                         text="x", **extra)
        assert "conversation" in detail, detail
    assert count(db, "messages") == 0


@pytest.mark.parametrize("extra", [{"attention": []}, {"reply_to": ""}])
def test_8_an_empty_conversation_only_field_on_a_direct_send_is_refused_too(tmp_path, extra: Any) -> None:
    """Supplying one of them at all is a mistake about the API, not an empty value to drop quietly."""
    config = quiet_config(tmp_path, hooks=(QUIET,))
    db = connect(config.db_path)
    [field] = extra
    detail = refused("invalid_request", messages.send, db, sender="orchestrator", recipient=QUIET, text="x",
                     **extra)
    assert detail.startswith(field) and "conversation" in detail, detail
    assert (count(db, "messages"), count(db, "events"), count(db, "message_requests")) == (0, 0, 0)
    assert woken(config) == []
    assert queued(db, QUIET) == []


def test_8_a_flag_left_unset_is_not_a_supplied_field(tmp_path) -> None:
    """needs_decision is False when it is left out, so a direct send that passes nothing still goes."""
    config = quiet_config(tmp_path, hooks=(QUIET,))
    db = connect(config.db_path)
    sent = messages.send(db, sender="orchestrator", recipient=QUIET, text="plain", reply_to=None,
                         attention=None, needs_decision=False)
    assert sent["created"] is True and count(db, "messages") == 1
    assert [(m["reply_to"], m["attention"], m["needs_decision"]) for m in
            messages.read(db, QUIET, after_seq=0)["messages"]] == [(None, None, False)]


def test_8_attention_is_part_of_a_sends_retry_identity(tmp_path) -> None:
    config = quiet_config(tmp_path)
    db = connect(config.db_path)
    conversation_id = start(db, config)
    request_id = rid("attention-once")
    say(db, config, "orchestrator", conversation_id, "same", attention=[QUIET], request_id=request_id)
    refused("idempotency_conflict", say, db, config, "orchestrator", conversation_id, "same",
            attention=["reviewer"], request_id=request_id)
    refused("idempotency_conflict", say, db, config, "orchestrator", conversation_id, "same",
            attention=[QUIET], needs_decision=True, request_id=request_id)
    again = say(db, config, "orchestrator", conversation_id, "same", attention=[QUIET], request_id=request_id)
    assert again["created"] is False


# The quiet setting itself -----------------------------------------------------------------------------------


def test_a_role_is_woken_for_everything_unless_its_config_asks_otherwise(tmp_path) -> None:
    config = quiet_config(tmp_path)
    assert config.roles["reviewer"].wake.only_when_addressed is False
    assert config.roles[QUIET].wake.only_when_addressed is True


@pytest.mark.parametrize("value", ["true", "false", 1, 0.0])
def test_only_when_addressed_is_a_checked_boolean(tmp_path, value: Any) -> None:
    raw = {"host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")},
           "roles": {"reviewer": {"wake": {"command": ["true"], "only_when_addressed": value}}}}
    with pytest.raises(ConfigError, match="only_when_addressed"):
        config_module.parse(raw)


# What a quiet role must keep ------------------------------------------------------------------------------
# These two pass before this change as well as after: being quiet narrows which conversation messages wake a
# role, and must leave everything else that is addressed to it alone.


def test_a_direct_message_addresses_its_recipient_so_it_always_wakes(tmp_path) -> None:
    config = quiet_config(tmp_path, hooks=(QUIET,))
    db = connect(config.db_path)
    messages.send(db, sender="orchestrator", recipient=QUIET, text="just for you")
    assert [(c["role"], c["first_unread_seq"]) for c in woken(config)] == [(QUIET, 1)]


def test_a_message_retention_took_addresses_nobody(tmp_path) -> None:
    """Once the copy is gone there is nothing left to look at, so its event must wake nobody."""
    config = quiet_config(tmp_path, hooks=(QUIET,))
    db = connect(config.db_path)
    conversation_id = start(db, config)
    say(db, config, "orchestrator", conversation_id, "your call", attention=[QUIET])
    assert messages.addressed_to(db, QUIET, 1) is True

    messages.read(db, QUIET)
    stale = (datetime.now(UTC) - messages.READ_RETENTION - timedelta(days=1)).isoformat()
    db.execute("UPDATE messages SET at = ? WHERE recipient = ? AND seq = 1", (stale, QUIET))
    messages.sweep(db)
    assert messages.addressed_to(db, QUIET, 1) is False


def test_a_job_change_still_wakes_a_quiet_role(tmp_path) -> None:
    states = {"initial": "open", "states": {"open": {"waiting_on": QUIET, "next": ["done"]}, "done": {}}}
    config = quiet_config(tmp_path, hooks=(QUIET,), job_states_table=states)
    db = connect(config.db_path)
    job = jobs.create(db, config.jobs, request_id=rid("job-for-a-quiet-role"), actor="orchestrator", title="t")
    calls = woken(config)
    assert [(c["role"], c["reason"], c["job_id"]) for c in calls] == [(QUIET, "job.created", job["job_id"])]
