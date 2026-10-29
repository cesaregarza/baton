"""Every upgrade path onto the current schema, from each older version baton says it can climb from.

Both lanes that raised the schema version add columns to `messages`, and one of them backfills
`author_kind`. A database three versions behind has to pick up every step, in order, not just the one
matching its own version. Each fixture is the schema that version really created, generated from the
head that created it, not hand-edited.

From the version that introduced `author_kind` the column already holds values, a person's own message
among them, so these also pin that an upgrade never relabels one as an agent's.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_conversations import group_config
from test_messages import rid

from baton import conversations, events, messages, store
from baton.config import Config
from baton.store import MIGRATES_FROM, SCHEMA_VERSION, SchemaMismatch, connect

FIXTURES = Path(__file__).parent / "fixtures"
# Every older schema there is a real fixture for. Deliberately not read off MIGRATES_FROM: a version
# missing from that list is the failure this is here to catch, not a case to stop generating.
FIXTURE_VERSIONS = tuple(sorted(int(path.stem.removeprefix("schema_v"))
                                for path in FIXTURES.glob("schema_v*.sql")))
CONVERSATION = "conv_carried"
GROUP = ("orchestrator", "reviewer", "assistant")
# The columns each version added, which every older database must end up with.
ADDED = ("reply_to", "attention", "needs_decision", "author_kind")


def old_database(config: Config, version: int, request_id: str) -> None:
    """A database as `version` left it: a direct message with its retry identity, and from 3 on a
    conversation with one fanned-out message and one host notice."""
    config.data_dir.mkdir(mode=0o700)
    raw = sqlite3.connect(config.db_path)
    raw.executescript((FIXTURES / f"schema_v{version}.sql").read_text())
    stamp = events.now()
    digest = events.request_digest("send", {"sender": "reviewer", "recipient": "assistant", "text": "direct",
                                            "job_id": None})
    with raw:
        raw.execute("INSERT INTO mailboxes (recipient, last_seq, read_seq) VALUES ('assistant', 1, 0)")
        raw.execute("INSERT INTO messages (id, job_id, sender, recipient, seq, text, at)"
                    " VALUES ('msg_direct', NULL, 'reviewer', 'assistant', 1, 'direct', ?)", (stamp,))
        raw.execute("INSERT INTO message_requests (request_id, request_digest, message_id, seq, created_at)"
                    " VALUES (?, ?, 'msg_direct', 1, ?)", (request_id, digest, stamp))
        raw.execute("INSERT INTO events (job_id, seq, at, actor, audience, kind, summary, data)"
                    " VALUES (NULL, 1, ?, 'reviewer', 'assistant', 'message', 'a message', ?)",
                    (stamp, json.dumps({"message_id": "msg_direct", "seq": 1})))
        if version >= 3:
            raw.execute("INSERT INTO conversations (id, request_id, request_digest, title, created_by,"
                        " created_at, last_seq) VALUES (?, 'conv-request', 'conv-digest', 'Carried over',"
                        " 'orchestrator', ?, 2)", (CONVERSATION, stamp))
            raw.executemany("INSERT INTO participants (conversation_id, role, added_at) VALUES (?, ?, ?)",
                            [(CONVERSATION, role, stamp) for role in GROUP])
            # One message a role sent, and one notice the host sent, which has no author at all.
            for recipient, seq in (("reviewer", 1), ("assistant", 2)):
                raw.execute("INSERT INTO messages (id, job_id, conversation_id, conversation_seq, sender,"
                            " recipient, seq, text, at) VALUES ('msg_group', NULL, ?, 1, 'orchestrator', ?, ?,"
                            " 'in the group', ?)", (CONVERSATION, recipient, seq, stamp))
            for recipient, seq in (("orchestrator", 1), ("reviewer", 2), ("assistant", 3)):
                raw.execute("INSERT INTO messages (id, job_id, conversation_id, conversation_seq, sender,"
                            " recipient, seq, text, notice, at) VALUES ('msg_notice', NULL, ?, 2, NULL, ?, ?,"
                            " 'orchestrator added assistant', ?, ?)",
                            (CONVERSATION, recipient, seq, json.dumps({"event": "participant_added",
                             "participant": "assistant", "by": "orchestrator"}), stamp))
            raw.execute("UPDATE mailboxes SET last_seq = 3 WHERE recipient = 'assistant'")
            for recipient, last in (("orchestrator", 1), ("reviewer", 2)):
                raw.execute("INSERT INTO mailboxes (recipient, last_seq, read_seq) VALUES (?, ?, 0)",
                            (recipient, last))
        if version >= 5:
            # `author_kind` exists from this version, so a real database of it already holds values, a
            # person's own message among them. The backfill comes last so it covers every row above, and
            # the person's message goes to a mailbox nothing else here uses.
            raw.execute("UPDATE messages SET author_kind = 'agent' WHERE sender IS NOT NULL")
            raw.execute("INSERT INTO mailboxes (recipient, last_seq, read_seq) VALUES ('outsider', 1, 0)")
            raw.execute("INSERT INTO messages (id, job_id, sender, author_kind, recipient, seq, text, at)"
                        " VALUES ('msg_human', NULL, 'orchestrator', 'human', 'outsider', 1,"
                        " 'from the person', ?)", (stamp,))
    raw.close()
    os.chmod(config.db_path, 0o600)


def rows(db: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in db.execute("SELECT id, recipient, seq, text, sender, author_kind FROM messages"
                                             " ORDER BY recipient, seq")]


@pytest.mark.parametrize("version", FIXTURE_VERSIONS)
def test_every_older_schema_there_is_a_fixture_for_reaches_the_current_one_whole(tmp_path: Path,
                                                                                version: int) -> None:
    config = group_config(tmp_path)
    request_id = rid(f"from-v{version}")
    old_database(config, version, request_id)
    before = sqlite3.connect(config.db_path)
    carried = rows(before) if version >= SCHEMA_VERSION else None
    before.close()

    db = connect(config.db_path)
    assert db.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
    assert set(ADDED) <= columns, f"version {version} did not pick up every step: missing {set(ADDED) - columns}"

    # Every row is still there, and every message a role sent now reads as an agent's.
    kinds = {(row["sender"] is None, row["author_kind"]) for row in db.execute("SELECT sender, author_kind"
                                                                              " FROM messages")}
    expected = {(False, "agent")}
    if version >= 3:
        expected.add((True, None))  # a host notice has no author at all
    if version >= 5:
        expected.add((False, "human"))  # and a person's own message is still the person's
    assert kinds == expected, kinds
    if version >= 5:
        assert db.execute("SELECT author_kind FROM messages WHERE id = 'msg_human'").fetchone()[0] == "human", \
            "an upgrade relabelled a person's message as an agent's"
    if carried is not None:
        assert rows(db) == carried

    # The retry identity still replays, so an upgrade admits no stale retry twice.
    replay = messages.send(db, sender="reviewer", recipient="assistant", text="direct", request_id=request_id)
    assert (replay["created"], replay["message_id"], replay["seq"]) == (False, "msg_direct", 1)

    if version >= 3:  # a carried-over conversation takes a reply straight away, numbered on from before
        answer = conversations.send(db, config, sender="reviewer", conversation_id=CONVERSATION,
                                    text="answering", reply_to="msg_group", attention=["orchestrator"],
                                    needs_decision=True)
        [shown] = [m for m in conversations.history(db, "assistant", CONVERSATION)["messages"]
                   if m["text"] == "answering"]
        assert shown["seq"] == 3 and shown["reply_to"] == {"message_id": "msg_group", "seq": 1}
        assert (shown["attention"], shown["needs_decision"], shown["author_kind"]) == \
            (["orchestrator"], True, "agent")
        assert answer["seqs"]["assistant"] == 4
    db.close()
    connect(config.db_path).close()  # a second open changes nothing


@pytest.mark.parametrize("version", [1, SCHEMA_VERSION + 1, SCHEMA_VERSION + 2])
def test_every_other_version_is_still_refused_before_anything_changes(tmp_path: Path, version: int) -> None:
    config = group_config(tmp_path)
    config.data_dir.mkdir(mode=0o700)
    raw = sqlite3.connect(config.db_path)
    raw.executescript("CREATE TABLE schema_version (version INTEGER NOT NULL);"
                      f" INSERT INTO schema_version VALUES ({version});")
    raw.close()
    os.chmod(config.db_path, 0o600)
    with pytest.raises(SchemaMismatch, match=f"version {version}"):
        connect(config.db_path)
    raw = sqlite3.connect(config.db_path)
    assert [row[0] for row in raw.execute("SELECT name FROM sqlite_master")] == ["schema_version"]
    assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    raw.close()


def test_the_upgrade_steps_cover_every_version_there_is_a_fixture_for() -> None:
    """The shape itself, so a later bump cannot leave a version behind without a test saying so.

    A version baton accepts but has no step above it migrates silently and arrives missing columns; a
    version with a fixture but absent from MIGRATES_FROM is refused outright instead of upgraded.
    """
    assert set(MIGRATES_FROM) == set(FIXTURE_VERSIONS), \
        f"baton migrates from {sorted(MIGRATES_FROM)} but there are fixtures for {list(FIXTURE_VERSIONS)}"
    assert set(MIGRATES_FROM) == set(range(min(MIGRATES_FROM), SCHEMA_VERSION)), \
        "MIGRATES_FROM skips a version between the oldest it accepts and the current one"
    assert store._STEPS, "no upgrade steps are declared"
    assert max(store._STEPS) == SCHEMA_VERSION, "the newest step is not the current version"
    assert min(FIXTURE_VERSIONS) < min(store._STEPS), "nothing climbs from the oldest version baton accepts"
