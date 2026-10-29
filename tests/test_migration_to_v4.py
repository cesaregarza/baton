"""A schema-version-3 database is migrated to version 4 in place, keeping every message, seq and identity.

Version 4 only adds columns to `messages`, so nothing is rebuilt and nothing is renumbered. What this
pins down is that the upgrade happens at all, that it leaves version 3's rows and retry identities alone,
and that a conversation carried over from version 3 can be replied into straight away.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from test_conversations import group_config
from test_messages import rid

from baton import conversations, events, messages, store
from baton.config import Config
from baton.store import SchemaMismatch, connect

V3_SCHEMA = (Path(__file__).parent / "fixtures" / "schema_v3.sql").read_text()
CONVERSATION = "conv_fromv3"
GROUP = ("orchestrator", "reviewer", "assistant")


def send_digest(sender: str, text: str, **target: str) -> str:
    """The digest version 3 stored for a send, with no relayed entries: what a retry must still match."""
    return events.request_digest("send", {"sender": sender, **target, "text": text, "job_id": None})


def v3_database(config: Config, direct_request: str, group_request: str) -> None:
    """A database as baton a5036b98 left it: one direct message, one conversation message, and their identities."""
    config.data_dir.mkdir(mode=0o700)
    raw = sqlite3.connect(config.db_path)
    raw.executescript(V3_SCHEMA)
    stamp = events.now()
    with raw:
        for recipient, last_seq in (("reviewer", 2), ("assistant", 1)):
            raw.execute("INSERT INTO mailboxes (recipient, last_seq, read_seq) VALUES (?, ?, 0)",
                        (recipient, last_seq))
        raw.execute("INSERT INTO conversations (id, request_id, request_digest, title, created_by, created_at,"
                    " last_seq) VALUES (?, 'conv-request', 'conv-digest', 'From version 3', 'orchestrator', ?, 1)",
                    (CONVERSATION, stamp))
        raw.executemany("INSERT INTO participants (conversation_id, role, added_at) VALUES (?, ?, ?)",
                        [(CONVERSATION, role, stamp) for role in GROUP])
        # A direct message to reviewer, outside any conversation.
        raw.execute("INSERT INTO messages (id, job_id, conversation_id, conversation_seq, sender, recipient, seq,"
                    " text, at) VALUES ('msg_direct', NULL, NULL, NULL, 'assistant', 'reviewer', 1, 'direct', ?)",
                    (stamp,))
        raw.execute("INSERT INTO message_requests (request_id, request_digest, message_id, seq, created_at)"
                    " VALUES (?, ?, 'msg_direct', 1, ?)",
                    (direct_request, send_digest("assistant", "direct", recipient="reviewer"), stamp))
        # One conversation message, fanned out to the two roles that did not send it.
        for recipient, seq in (("reviewer", 2), ("assistant", 1)):
            raw.execute("INSERT INTO messages (id, job_id, conversation_id, conversation_seq, sender, recipient,"
                        " seq, text, at) VALUES ('msg_group', NULL, ?, 1, 'orchestrator', ?, ?, 'in the group', ?)",
                        (CONVERSATION, recipient, seq, stamp))
        raw.execute("INSERT INTO message_requests (request_id, request_digest, message_id, seqs, created_at)"
                    " VALUES (?, ?, 'msg_group', ?, ?)",
                    (group_request, send_digest("orchestrator", "in the group", conversation_id=CONVERSATION),
                     json.dumps({"reviewer": 2, "assistant": 1}), stamp))
        for recipient, seq in (("reviewer", 1), ("reviewer", 2), ("assistant", 1)):
            raw.execute("INSERT INTO events (job_id, seq, at, actor, audience, kind, summary, data)"
                        " VALUES (NULL, ?, ?, 'orchestrator', ?, 'message', 'a message', ?)",
                        (seq, stamp, recipient, json.dumps({"message_id": "msg_group", "seq": seq})))
    raw.close()
    os.chmod(config.db_path, 0o600)


def test_a_version_3_database_gains_the_new_columns_and_keeps_everything_else(tmp_path: Path) -> None:
    config = group_config(tmp_path)
    direct_request, group_request = rid("direct-from-v3"), rid("group-from-v3")
    v3_database(config, direct_request, group_request)

    db = connect(config.db_path)
    assert db.execute("SELECT version FROM schema_version").fetchone()[0] == store.SCHEMA_VERSION >= 4
    columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
    assert {"reply_to", "attention", "needs_decision"} <= columns
    carried = conversations.history(db, "reviewer", CONVERSATION)["messages"]
    assert [(m["seq"], m["from_role"], m["text"], m["reply_to"], m["attention"], m["needs_decision"])
            for m in carried] == [(1, "orchestrator", "in the group", None, None, False)]

    # Version 3's retry identities still replay, so the upgrade admits no stale retry twice.
    replay = messages.send(db, sender="assistant", recipient="reviewer", text="direct",
                           request_id=direct_request)
    assert (replay["created"], replay["message_id"], replay["seq"]) == (False, "msg_direct", 1)
    again = conversations.send(db, config, sender="orchestrator", conversation_id=CONVERSATION,
                               text="in the group", request_id=group_request)
    assert (again["created"], again["seqs"]) == (False, {"reviewer": 2, "assistant": 1})

    # And the conversation can be replied into at once, numbered on from where version 3 left off.
    answer = conversations.send(db, config, sender="reviewer", conversation_id=CONVERSATION, text="answering",
                                reply_to="msg_group", attention=["orchestrator"], needs_decision=True)
    assert answer["seqs"] == {"orchestrator": 1, "assistant": 2}
    [shown] = [m for m in conversations.history(db, "assistant", CONVERSATION)["messages"]
               if m["text"] == "answering"]
    assert shown["seq"] == 2 and shown["reply_to"] == {"message_id": "msg_group", "seq": 1}
    assert (shown["attention"], shown["needs_decision"]) == (["orchestrator"], True)
    db.close()
    connect(config.db_path).close()  # a second open of a migrated database changes nothing


def test_a_version_1_database_is_refused_rather_than_guessed_at(tmp_path: Path) -> None:
    config = group_config(tmp_path)
    config.data_dir.mkdir(mode=0o700)
    raw = sqlite3.connect(config.db_path)
    raw.executescript("CREATE TABLE schema_version (version INTEGER NOT NULL); INSERT INTO schema_version VALUES (1);")
    raw.close()
    os.chmod(config.db_path, 0o600)
    with pytest.raises(SchemaMismatch, match="version 1"):
        connect(config.db_path)
    raw = sqlite3.connect(config.db_path)
    assert [row[0] for row in raw.execute("SELECT name FROM sqlite_master")] == ["schema_version"]
    raw.close()
