"""A schema-version-2 database is migrated to version 3 in place, keeping every message, seq and retry identity."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from test_conversations import GROUP, conversations, group_config
from test_messages import rid

from baton import events, inbox, messages, store
from baton.config import Config
from baton.store import SchemaMismatch, connect

V2_SCHEMA = (Path(__file__).parent / "fixtures" / "schema_v2.sql").read_text()


def v2_database(config: Config, request_id: str) -> None:
    """A database as baton 226b95f5 left it: one direct message, its retry identity, its event and mailbox row."""
    config.data_dir.mkdir(mode=0o700)
    raw = sqlite3.connect(config.db_path)
    raw.executescript(V2_SCHEMA)
    digest = events.request_digest("send", {"sender": "reviewer", "recipient": "assistant", "text": "from v2",
                                            "job_id": None})
    stamp = events.now()
    with raw:
        raw.execute("INSERT INTO mailboxes VALUES ('assistant', 1, 0)")
        raw.execute("INSERT INTO messages VALUES ('msg_old', NULL, 'reviewer', 'assistant', 1, 'from v2', ?)",
                    (stamp,))
        raw.execute("INSERT INTO message_requests VALUES (?, ?, 'msg_old', 1, ?)", (request_id, digest, stamp))
        raw.execute("INSERT INTO events (job_id, seq, at, actor, audience, kind, summary, data) VALUES"
                    " (NULL, 1, ?, 'reviewer', 'assistant', 'message', 'message 1 from reviewer', ?)",
                    (stamp, json.dumps({"message_id": "msg_old", "seq": 1})))
    raw.close()
    os.chmod(config.db_path, 0o600)


def test_a_version_2_database_is_migrated_and_keeps_its_messages(tmp_path: Path) -> None:
    config = group_config(tmp_path)
    request_id = rid("before-upgrade")
    v2_database(config, request_id)

    db = connect(config.db_path)
    assert db.execute("SELECT version FROM schema_version").fetchone()[0] == store.SCHEMA_VERSION == 3
    replay = messages.send(db, sender="reviewer", recipient="assistant", text="from v2", request_id=request_id)
    assert (replay["created"], replay["message_id"], replay["seq"]) == (False, "msg_old", 1)
    assert messages.send(db, sender="reviewer", recipient="assistant", text="after")["seq"] == 2

    [old, new] = inbox.take(db, "assistant")["events"]
    assert (old["message"]["text"], old["message"]["from_role"], old["message"]["conversation_id"]) == \
        ("from v2", "reviewer", None)
    assert new["message"]["text"] == "after"
    conversation_id = conversations.create(db, config, actor="orchestrator", title="t",
                                           participants=GROUP)["conversation_id"]
    assert conversations.send(db, config, sender="reviewer", conversation_id=conversation_id,
                              text="group")["seqs"] == {"orchestrator": 1, "assistant": 3}
    db.close()
    connect(config.db_path).close()  # a second open of a migrated database changes nothing


def test_a_database_from_a_newer_schema_is_refused_untouched(tmp_path: Path) -> None:
    config = group_config(tmp_path)
    config.data_dir.mkdir(mode=0o700)
    raw = sqlite3.connect(config.db_path)
    raw.executescript("CREATE TABLE schema_version (version INTEGER NOT NULL); INSERT INTO schema_version VALUES (4);")
    raw.close()
    os.chmod(config.db_path, 0o600)
    with pytest.raises(SchemaMismatch, match="version 4"):
        connect(config.db_path)
    raw = sqlite3.connect(config.db_path)
    assert [row[0] for row in raw.execute("SELECT name FROM sqlite_master")] == ["schema_version"]
    raw.close()
