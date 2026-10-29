"""Threads: a conversation message may answer another, and each reader sees only the parents it may see.

The numbered tests are the ticket's, in its order: 1, 2, 3 and 9. Synthetic and offline: generic roles, a
temporary data directory, and a recording stand-in for wake delivery.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_conversations import GROUP, bookmarks, count, group_config, mailbox, refused, say, start, woken
from test_messages import rid

from baton import conversations, inbox, messages
from baton.config import Config
from baton.store import connect

# A parent's words, so a test can assert no reader ever sees them through a reply.
HIDDEN = "the words of a parent a late joiner never saw"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return group_config(tmp_path)


@pytest.fixture
def db(config: Config) -> sqlite3.Connection:
    connection = connect(config.db_path)
    yield connection
    connection.close()


def answers(message: dict[str, Any]) -> Any:
    """What a reader is told about the message this one answers: its id and conversation seq, or None."""
    return message["reply_to"]


def one(page: dict[str, Any], text: str) -> dict[str, Any]:
    [found] = [message for message in page["messages"] if message["text"] == text]
    return found


# 1 -------------------------------------------------------------------------------------------------------


def test_1_a_reply_fans_out_like_any_message_and_reads_back_with_its_parent(db, config) -> None:
    conversation_id = start(db, config)
    parent = say(db, config, "orchestrator", conversation_id, "which release?")
    woken(config)  # the parent's wakes, out of the way

    answer = say(db, config, "reviewer", conversation_id, "the next one", reply_to=parent["message_id"])
    assert answer["seqs"] == {"orchestrator": 1, "assistant": 2}  # one copy each, continuing their mailboxes
    copies = db.execute("SELECT recipient, id, reply_to FROM messages WHERE text = 'the next one'").fetchall()
    assert sorted(row["recipient"] for row in copies) == ["assistant", "orchestrator"]
    assert {row["id"] for row in copies} == {answer["message_id"]}  # one shared message_id
    assert {row["reply_to"] for row in copies} == {parent["message_id"]}
    assert mailbox(db, "reviewer") == []  # the sender gets no copy of its own reply
    for role, seqs in (("orchestrator", [1]), ("assistant", [1, 2])):
        assert [row["seq"] for row in mailbox(db, role)] == seqs

    reference = {"message_id": parent["message_id"], "seq": 1}
    assert answers(one(messages.read(db, "orchestrator"), "the next one")) == reference
    assert answers(one(conversations.history(db, "assistant", conversation_id), "the next one")) == reference
    assert answers(one(conversations.history(db, "assistant", conversation_id), "which release?")) is None
    [event] = [e for e in inbox.take(db, "assistant")["events"] if e["message"]["text"] == "the next one"]
    assert answers(event["message"]) == reference


def test_1_a_reply_is_as_retry_safe_as_any_other_message(db, config) -> None:
    conversation_id = start(db, config)
    parent = say(db, config, "orchestrator", conversation_id, "which release?")
    request_id = rid("one-reply")
    first = say(db, config, "reviewer", conversation_id, "the next one", reply_to=parent["message_id"],
                request_id=request_id)
    again = say(db, config, "reviewer", conversation_id, "the next one", reply_to=parent["message_id"],
                request_id=request_id)
    assert (again["created"], again["message_id"], again["seqs"]) == (False, first["message_id"], first["seqs"])
    assert len(mailbox(db, "assistant")) == 2
    refused("idempotency_conflict", say, db, config, "reviewer", conversation_id, "the next one",
            request_id=request_id)  # the same text, no longer a reply


# 2 -------------------------------------------------------------------------------------------------------


def test_2_a_reply_to_a_message_outside_the_conversation_is_refused_and_stores_nothing(db, config) -> None:
    conversation_id = start(db, config)
    elsewhere = conversations.create(db, config, actor="orchestrator", title="another thread",
                                     participants=["orchestrator", "reviewer"], request_id=rid("elsewhere"))
    other = say(db, config, "orchestrator", elsewhere["conversation_id"], "not in this thread")
    direct = messages.send(db, sender="orchestrator", recipient="reviewer", text="not in any thread")
    say(db, config, "orchestrator", conversation_id, "in this thread")
    woken(config)
    before = (count(db, "messages"), count(db, "events"), count(db, "message_requests"))

    for parent in (other["message_id"], direct["message_id"], "msg_nonexistent"):
        detail = refused("invalid_request", say, db, config, "reviewer", conversation_id, "answering",
                         reply_to=parent)
        assert detail.startswith("reply_to"), detail
    assert (count(db, "messages"), count(db, "events"), count(db, "message_requests")) == before
    assert woken(config) == []


def test_2_a_reply_to_a_message_from_before_the_sender_joined_is_refused(db, config) -> None:
    conversation_id = start(db, config, ["orchestrator", "reviewer"])
    early = say(db, config, "orchestrator", conversation_id, HIDDEN)
    conversations.add(db, config, conversation_id, "assistant", actor="orchestrator")
    woken(config)
    before = (count(db, "messages"), count(db, "events"), count(db, "message_requests"))

    refused("invalid_request", say, db, config, "assistant", conversation_id, "answering",
            reply_to=early["message_id"])
    assert (count(db, "messages"), count(db, "events"), count(db, "message_requests")) == before
    assert woken(config) == []
    assert say(db, config, "reviewer", conversation_id, "the reviewer was there for it",
               reply_to=early["message_id"])["created"] is True


def test_2_a_reply_to_a_parent_retention_has_taken_is_refused(db, config) -> None:
    conversation_id = start(db, config)
    parent = say(db, config, "orchestrator", conversation_id, HIDDEN)
    messages.read(db, "assistant")
    stale = (datetime.now(UTC) - messages.READ_RETENTION - timedelta(days=1)).isoformat()
    db.execute("UPDATE messages SET at = ? WHERE recipient = 'assistant' AND seq = 1", (stale,))
    messages.sweep(db)

    refused("invalid_request", say, db, config, "assistant", conversation_id, "answering",
            reply_to=parent["message_id"])
    assert say(db, config, "reviewer", conversation_id, "still here", reply_to=parent["message_id"])["created"]


def test_2_a_direct_send_has_no_thread_to_reply_into(db, config) -> None:
    conversation_id = start(db, config)
    parent = say(db, config, "orchestrator", conversation_id, "in a conversation")
    detail = refused("invalid_request", messages.send, db, sender="reviewer", recipient="assistant",
                     text="answering", reply_to=parent["message_id"])
    assert "reply_to" in detail and "conversation" in detail
    assert count(db, "messages") == 2  # only the parent's two copies


# 3 -------------------------------------------------------------------------------------------------------


def test_3_history_returns_replies_with_their_parent_in_order_and_moves_no_bookmark(db, config) -> None:
    conversation_id = start(db, config)
    first = say(db, config, "orchestrator", conversation_id, "q1")
    second = say(db, config, "reviewer", conversation_id, "q2")
    say(db, config, "assistant", conversation_id, "a1", reply_to=first["message_id"])
    say(db, config, "reviewer", conversation_id, "a2", reply_to=second["message_id"])
    say(db, config, "orchestrator", conversation_id, "a3", reply_to=first["message_id"])
    messages.read(db, "reviewer")  # one role has read, the others haven't
    before = bookmarks(db)

    for role in GROUP:
        page = conversations.history(db, role, conversation_id)
        assert [(m["seq"], m["text"], None if m["reply_to"] is None else m["reply_to"]["seq"])
                for m in page["messages"]] == [(1, "q1", None), (2, "q2", None), (3, "a1", 1), (4, "a2", 2),
                                               (5, "a3", 1)]
    assert bookmarks(db) == before

    assert [m["text"] for m in messages.read(db, "orchestrator")["messages"]] == ["q2", "a1", "a2"]
    assert [m["text"] for m in messages.read(db, "assistant")["messages"]] == ["q1", "q2", "a2", "a3"]
    for role in GROUP:
        assert messages.read(db, role)["messages"] == []  # once each


def test_3_history_pages_replies_without_losing_their_parent(db, config) -> None:
    conversation_id = start(db, config)
    parent = say(db, config, "orchestrator", conversation_id, "the question")
    for index in range(4):
        say(db, config, "reviewer", conversation_id, f"a{index}", reply_to=parent["message_id"])
    page = conversations.history(db, "assistant", conversation_id, limit=2)
    rest = conversations.history(db, "assistant", conversation_id, after_seq=page["next_seq"], limit=10)
    shown = page["messages"] + rest["messages"]
    assert [m["text"] for m in shown] == ["the question", "a0", "a1", "a2", "a3"]
    assert [None if m["reply_to"] is None else m["reply_to"]["seq"] for m in shown] == [None, 1, 1, 1, 1]


# 9 -------------------------------------------------------------------------------------------------------


def test_9_a_late_joiner_sees_the_reply_and_nothing_of_the_parent(db, config) -> None:
    conversation_id = start(db, config, ["orchestrator", "reviewer"])
    parent = say(db, config, "orchestrator", conversation_id, HIDDEN)
    conversations.add(db, config, conversation_id, "assistant", actor="orchestrator")
    say(db, config, "reviewer", conversation_id, "answering the parent", reply_to=parent["message_id"])

    page = conversations.history(db, "assistant", conversation_id)
    assert [m["text"] for m in page["messages"]] == ["orchestrator added assistant", "answering the parent"]
    reference = answers(one(page, "answering the parent"))
    assert reference == {"message_id": parent["message_id"], "seq": None}  # the parent is unavailable to it
    assert set(reference) == {"message_id", "seq"}  # no text, no author
    delivered = messages.read(db, "assistant", after_seq=0)
    assert answers(one(delivered, "answering the parent")) == reference
    assert HIDDEN not in json.dumps(page) + json.dumps(delivered)

    seen = one(conversations.history(db, "reviewer", conversation_id), "answering the parent")
    assert answers(seen) == {"message_id": parent["message_id"], "seq": 1}  # the reviewer was there for it


def test_9_a_parent_lost_to_retention_reads_the_same_as_one_never_seen(db, config) -> None:
    conversation_id = start(db, config)
    parent = say(db, config, "orchestrator", conversation_id, HIDDEN)
    say(db, config, "reviewer", conversation_id, "answering", reply_to=parent["message_id"])
    assert answers(one(conversations.history(db, "assistant", conversation_id), "answering"))["seq"] == 1

    messages.read(db, "assistant", limit=1)
    stale = (datetime.now(UTC) - messages.READ_RETENTION - timedelta(days=1)).isoformat()
    db.execute("UPDATE messages SET at = ? WHERE recipient = 'assistant' AND seq = 1", (stale,))
    messages.sweep(db)

    page = conversations.history(db, "assistant", conversation_id)
    assert [m["text"] for m in page["messages"]] == ["answering"]
    assert answers(page["messages"][0]) == {"message_id": parent["message_id"], "seq": None}
    assert HIDDEN not in json.dumps(page)
    assert answers(one(conversations.history(db, "reviewer", conversation_id), "answering"))["seq"] == 1


def test_9_a_role_that_was_there_for_the_parent_still_sees_it_after_a_round_trip(db, config) -> None:
    conversation_id = start(db, config)
    parent = say(db, config, "orchestrator", conversation_id, HIDDEN)
    conversations.remove(db, config, conversation_id, "assistant", actor="orchestrator")
    conversations.add(db, config, conversation_id, "assistant", actor="orchestrator")
    say(db, config, "reviewer", conversation_id, "answering", reply_to=parent["message_id"])
    assert answers(one(conversations.history(db, "assistant", conversation_id), "answering"))["seq"] == 1
