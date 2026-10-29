"""Guards the first conversation tests left unchecked, each found by a mutant that survived them."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from test_conversations import group_config, refused, say, start
from test_messages import rid

from baton import conversations, inbox, messages, store
from baton.store import SchemaMismatch, connect


def test_host_notices_do_not_count_toward_the_hourly_limit(tmp_path: Path) -> None:
    config = group_config(tmp_path, max_messages_per_hour=2)
    db = connect(config.db_path)
    conversation_id = start(db, config)
    for _ in range(2):
        conversations.add(db, config, conversation_id, "outsider", actor="orchestrator")
        conversations.remove(db, config, conversation_id, "outsider", actor="orchestrator")
    say(db, config, "reviewer", conversation_id, "one")
    say(db, config, "assistant", conversation_id, "two")
    refused("conversation_rate_limited", say, db, config, "reviewer", conversation_id, "three")


RELAYED = [{"source": "operator", "at": "2026-10-29T07:00:00Z", "text": "approved"}]


def test_relayed_words_are_part_of_a_sends_retry_identity(tmp_path: Path) -> None:
    config = group_config(tmp_path)
    db = connect(config.db_path)
    conversation_id = start(db, config)
    direct, group = rid("direct-relayed"), rid("group-relayed")
    messages.send(db, sender="reviewer", recipient="assistant", text="same", relayed=RELAYED, request_id=direct)
    say(db, config, "reviewer", conversation_id, "same", relayed=RELAYED, request_id=group)
    changed = [{**RELAYED[0], "text": "rejected"}]
    refused("idempotency_conflict", messages.send, db, sender="reviewer", recipient="assistant", text="same",
            relayed=changed, request_id=direct)
    refused("idempotency_conflict", say, db, config, "reviewer", conversation_id, "same", relayed=changed,
            request_id=group)
    refused("idempotency_conflict", say, db, config, "reviewer", conversation_id, "same", request_id=group)


def test_wait_hands_each_recipient_its_own_copy(tmp_path: Path) -> None:
    config = group_config(tmp_path)
    db = connect(config.db_path)
    messages.send(db, sender="outsider", recipient="reviewer", text="so the two mailboxes number differently")
    messages.read(db, "reviewer")
    conversation_id = start(db, config)
    say(db, config, "orchestrator", conversation_id, "to both")
    for role, seq in (("assistant", 1), ("reviewer", 2)):
        [event] = inbox.take(db, role)["events"]
        assert (event["data"]["seq"], event["message"]["seq"], event["message"]["text"]) == (seq, seq, "to both")


def test_a_role_added_back_sees_each_stretch_it_was_in_and_nothing_between(tmp_path: Path) -> None:
    config = group_config(tmp_path)
    db = connect(config.db_path)
    conversation_id = start(db, config)
    say(db, config, "orchestrator", conversation_id, "first stretch")
    conversations.remove(db, config, conversation_id, "assistant", actor="orchestrator")
    say(db, config, "orchestrator", conversation_id, "while it was away")
    conversations.add(db, config, conversation_id, "assistant", actor="orchestrator")
    say(db, config, "reviewer", conversation_id, "second stretch")
    texts = [m["text"] for m in conversations.history(db, "assistant", conversation_id)["messages"]]
    assert texts == ["first stretch", "orchestrator added assistant", "second stretch"]
    assert len(conversations.history(db, "reviewer", conversation_id)["messages"]) == 5
    entry, delivered = (conversations.history(db, "assistant", conversation_id)["messages"][0],
                        messages.read(db, "assistant", after_seq=0)["messages"][0])
    assert set(entry) == set(delivered)  # a history entry has the same fields as a delivered message


def test_a_newer_schema_is_refused_before_anything_changes_even_the_journal_mode(tmp_path: Path) -> None:
    config = group_config(tmp_path)
    config.data_dir.mkdir(mode=0o700)
    raw = sqlite3.connect(config.db_path)
    newer = store.SCHEMA_VERSION + 1  # newer than this baton knows, whatever its version
    raw.executescript("CREATE TABLE schema_version (version INTEGER NOT NULL);"
                      f" INSERT INTO schema_version VALUES ({newer});")
    raw.close()
    os.chmod(config.db_path, 0o600)
    with pytest.raises(SchemaMismatch, match=f"version {newer}"):
        connect(config.db_path)
    raw = sqlite3.connect(config.db_path)
    assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    raw.close()
