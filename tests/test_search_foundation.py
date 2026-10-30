"""The source of truth for indexing, and keyword search for roles: conversation revisions, a change log, local
read-only `baton index` commands, `get_message`, `read_history(before_seq)` and permission-filtered search.

Synthetic and offline: generic roles, a temporary data directory, nothing outside the process.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from baton import cli, conversations, messages, store
from baton import config as config_module
from baton.config import Config
from baton.errors import BatonError
from baton.store import SCHEMA_VERSION, connect

try:  # written before the modules exist: each test then fails on its own instead of the whole run stopping
    from baton import changes, search
except ImportError:
    changes = search = None  # type: ignore[assignment]

GROUP = ["orchestrator", "reviewer", "assistant"]


def search_config(tmp_path: Path, *, enabled: bool = True) -> Config:
    """Five generic roles. `viewer` may see every conversation; `outsider` is never invited."""
    return config_module.parse({
        "host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")},
        "roles": {"orchestrator": {"can_create_conversations": True, "can_manage_participants": True},
                  "reviewer": {}, "assistant": {}, "outsider": {},
                  "viewer": {"can_view_all_conversations": True}},
        "conversations": {"enabled": True},
        "search": {"enabled": enabled},
    })


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return search_config(tmp_path)


@pytest.fixture
def db(config: Config):
    connection = connect(config.db_path)
    yield connection
    connection.close()


def start(db, config, participants=GROUP, title="Release plan") -> str:
    return conversations.create(db, config, actor="orchestrator", title=title,
                                participants=participants)["conversation_id"]


def say(db, config, sender: str, conversation_id: str, text: str) -> dict[str, Any]:
    return conversations.send(db, config, sender=sender, conversation_id=conversation_id, text=text)


def refused(code: str, operation, *args, **kwargs) -> BatonError:
    with pytest.raises(BatonError) as raised:
        operation(*args, **kwargs)
    assert raised.value.code == code, str(raised.value)
    return raised.value


def revision(db, conversation_id: str) -> int:
    return db.execute("SELECT revision FROM conversations WHERE id = ?", (conversation_id,)).fetchone()[0]


def log(db, conversation_id: str | None = None) -> list[dict[str, Any]]:
    rows = db.execute("SELECT * FROM change_log ORDER BY change_seq").fetchall()
    return [dict(row) for row in rows if conversation_id is None or row["conversation_id"] == conversation_id]


def state(db) -> dict[str, Any]:
    """Everything delivery depends on, so a read can be shown to move none of it."""
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
            for table in ("mailboxes", "cursors", "wakes", "events", "change_log")}


def age(db, message_id: str, days: int = 40) -> None:
    db.execute("UPDATE messages SET at = ? WHERE id = ?",
               ((datetime.now(UTC) - timedelta(days=days)).isoformat(), message_id))


# Revisions and the change log ---------------------------------------------------------------------------


def test_every_source_change_bumps_the_revision_once_in_the_same_transaction(db, config) -> None:
    conversation_id = start(db, config)
    assert revision(db, conversation_id) == 0  # the defined starting revision
    sent = say(db, config, "reviewer", conversation_id, "first")
    assert revision(db, conversation_id) == 1
    conversations.add(db, config, conversation_id, "outsider", actor="orchestrator")
    conversations.remove(db, config, conversation_id, "outsider", actor="orchestrator")
    entries = log(db, conversation_id)
    kinds = [entry["kind"] for entry in entries]
    assert kinds[0] == "message_added" and entries[0]["message_id"] == sent["message_id"]
    assert "participant_added" in kinds and "participant_removed" in kinds
    assert [entry["revision"] for entry in entries] == list(range(1, len(entries) + 1))  # +1 each, no gaps
    assert revision(db, conversation_id) == len(entries)
    joined = next(entry for entry in entries if entry["kind"] == "participant_added")
    assert joined["role"] == "outsider"


def test_a_refused_send_logs_nothing(db, config) -> None:
    conversation_id = start(db, config)
    refused("not_a_participant", say, db, config, "outsider", conversation_id, "let me in")
    assert revision(db, conversation_id) == 0 and log(db, conversation_id) == []


def test_each_expired_copy_is_logged_with_its_role(db, config) -> None:
    conversation_id = start(db, config)
    sent = say(db, config, "orchestrator", conversation_id, "will expire")
    for role in ("reviewer", "assistant"):
        messages.read(db, role)
    age(db, sent["message_id"])
    before = revision(db, conversation_id)
    messages.sweep(db)
    expired = [entry for entry in log(db, conversation_id) if entry["kind"] == "message_expired"]
    assert sorted((entry["message_id"], entry["role"]) for entry in expired) == \
        [(sent["message_id"], "assistant"), (sent["message_id"], "reviewer")]  # one per copy
    assert revision(db, conversation_id) == before + 2


def test_reads_change_no_revision_and_log_nothing(db, config) -> None:
    conversation_id = start(db, config)
    sent = say(db, config, "orchestrator", conversation_id, "steady text")
    snapshot, rev = state(db), revision(db, conversation_id)
    search.search(db, config, "reviewer", "steady")
    search.get_message(db, config, "reviewer", sent["message_id"])
    conversations.history(db, "reviewer", conversation_id, before_seq=2)
    conversations.list_for(db, "reviewer")
    changes.read(db, changes.Scope.ALL)
    assert (state(db), revision(db, conversation_id)) == (snapshot, rev)


def test_the_log_epoch_is_random_per_database_and_stable_across_opens(tmp_path: Path) -> None:
    first, second = search_config(tmp_path / "a"), search_config(tmp_path / "b")
    a, b = connect(first.db_path), connect(second.db_path)
    epoch_a, epoch_b = changes.epoch(a), changes.epoch(b)
    a.close()
    b.close()
    assert len(epoch_a) == 32 and epoch_a != epoch_b
    reopened = connect(first.db_path)
    assert changes.epoch(reopened) == epoch_a
    reopened.close()


# The change feed: cursors that always advance, and expiry ------------------------------------------------


def test_the_cursor_always_advances_past_what_was_scanned(db, config, monkeypatch) -> None:
    conversation_id = start(db, config)
    for n in range(5):
        say(db, config, "reviewer", conversation_id, f"m{n}")
    monkeypatch.setattr(changes, "MAX_SCAN", 2)
    first = changes.read(db, changes.Scope.ALL)
    assert len(first["entries"]) == 2
    second = changes.read(db, changes.Scope.ALL, cursor=first["next_cursor"])
    assert [entry["change_seq"] for entry in second["entries"]] == [3, 4]
    end = changes.read(db, changes.Scope.ALL, cursor=changes.read(db, changes.Scope.ALL,
                                                                 cursor=second["next_cursor"])["next_cursor"])
    assert end["entries"] == [] and end["next_cursor"].endswith(":5")
    assert all(set(entry) == {"change_seq", "conversation_id", "revision", "kind", "message_id", "role", "at"}
               for entry in first["entries"])  # metadata only, never text


def test_a_cursor_from_another_epoch_or_past_retention_is_expired(db, config) -> None:
    conversation_id = start(db, config)
    say(db, config, "reviewer", conversation_id, "one")
    foreign = "0" * 32 + ":1"
    error = refused("cursor_expired", changes.read, db, changes.Scope.ALL, cursor=foreign)
    assert error.extra["resync"] is True and error.extra["reset_cursor"].startswith(changes.epoch(db))

    first = changes.read(db, changes.Scope.ALL)["next_cursor"]
    db.execute("UPDATE change_log SET at = ?", ((datetime.now(UTC) - timedelta(days=40)).isoformat(),))
    say(db, config, "reviewer", conversation_id, "two")  # a new change prunes the aged rows
    error = refused("cursor_expired", changes.read, db, changes.Scope.ALL, cursor=first.split(":")[0] + ":0")
    assert error.extra["reset_cursor"] == f"{changes.epoch(db)}:1"
    db.execute("DELETE FROM change_log")  # an empty log still knows what it pruned
    refused("cursor_expired", changes.read, db, changes.Scope.ALL, cursor=f"{changes.epoch(db)}:0")


def test_no_read_has_an_implicit_scope(db) -> None:
    with pytest.raises(TypeError):
        changes.read(db)  # type: ignore[call-arg]


# Local read-only commands ----------------------------------------------------------------------------------


def config_file(config: Config, tmp_path: Path) -> Path:
    path = tmp_path / "baton.toml"
    path.write_text("\n".join([
        "[host]", 'public_url = "http://127.0.0.1:8765"', f'data_dir = "{config.data_dir}"',
        "[roles.orchestrator]", "can_create_conversations = true", "can_manage_participants = true",
        "[roles.reviewer]", "[roles.assistant]", "[roles.outsider]",
        "[roles.viewer]", "can_view_all_conversations = true",
        "[conversations]", "enabled = true", "[search]", "enabled = true", ""]))
    return path


def run(capsys, *argv: str) -> tuple[int, Any]:
    code = cli.main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def files_digest(directory: Path) -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.iterdir()) if path.name.startswith("baton.db")}


def test_25_the_read_only_index_commands_write_nothing(db, config, tmp_path, capsys) -> None:
    conversation_id = start(db, config)
    say(db, config, "reviewer", conversation_id, "to index")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    path = config_file(config, tmp_path)
    before = files_digest(config.data_dir)
    code, changed = run(capsys, "--config", str(path), "index", "changes")
    assert code == 0 and [entry["kind"] for entry in changed["entries"]] == ["message_added"]
    code, woke = run(capsys, "--config", str(path), "index", "wait", "--after", changed["next_cursor"],
                     "--timeout", "0")
    assert code == 0 and woke == {"timed_out": True}
    assert files_digest(config.data_dir) == before  # not a byte written, the WAL included


def test_the_read_only_connection_refuses_writes(config) -> None:
    connect(config.db_path).close()
    conn = store.connect_read_only(config.db_path)
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE conversations SET title = 'x'")
    conn.close()


def test_the_read_only_connection_is_opened_read_only_not_only_asked_to_be(config) -> None:
    """query_only can be switched off on the connection; the file opened mode=ro still refuses the write."""
    connect(config.db_path).close()
    conn = store.connect_read_only(config.db_path)
    conn.execute("PRAGMA query_only = OFF")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("UPDATE conversations SET title = 'x'")
    conn.close()


def test_the_index_commands_read_through_the_read_only_connection(db, config, tmp_path, capsys,
                                                                  monkeypatch) -> None:
    path = config_file(config, tmp_path)
    seen = []
    original = changes.read

    def spy(conn, scope, **kwargs):
        seen.append(conn.execute("PRAGMA query_only").fetchone()[0])
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE log_meta SET value = value WHERE key = 'log_epoch'")
        return original(conn, scope, **kwargs)

    monkeypatch.setattr(changes, "read", spy)
    code, _ = run(capsys, "--config", str(path), "index", "changes")
    assert code == 0 and seen == [1]


def test_the_read_only_connection_refuses_a_database_that_needs_migrating(tmp_path: Path) -> None:
    config = search_config(tmp_path)
    config.data_dir.mkdir(mode=0o700)
    raw = sqlite3.connect(config.db_path)
    raw.executescript((Path(__file__).parent / "fixtures" / "schema_v6.sql").read_text())
    raw.close()
    os.chmod(config.db_path, 0o600)
    with pytest.raises(store.SchemaMismatch, match="start"):
        store.connect_read_only(config.db_path)


def test_27_wait_returns_only_a_change_seq_once_something_changes(db, config, tmp_path) -> None:
    conversation_id = start(db, config)
    cursor = changes.read(store.connect_read_only(config.db_path), changes.Scope.ALL)["next_cursor"]
    snapshot = state(db)
    result: dict[str, Any] = {}

    def waiter() -> None:
        reader = store.connect_read_only(config.db_path)
        result.update(changes.wait(reader, changes.Scope.ALL, cursor=cursor, timeout_seconds=10, poll_seconds=0.05))
        reader.close()

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.3)
    assert thread.is_alive()  # nothing yet: still waiting
    say(db, config, "reviewer", conversation_id, "wake the indexer")
    thread.join(timeout=10)
    assert result == {"change_seq": 1}  # no content
    after = state(db)  # the send moved mailboxes and events; the wait itself moved no cursor and no wake
    assert after["cursors"] == snapshot["cursors"] and after["wakes"] == snapshot["wakes"]
    timed_out = changes.wait(store.connect_read_only(config.db_path), changes.Scope.ALL,
                             cursor=f"{changes.epoch(db)}:1", timeout_seconds=0.1, poll_seconds=0.02)
    assert timed_out == {"timed_out": True}


def test_the_index_commands_run_without_a_role(db, config, tmp_path, capsys) -> None:
    path = config_file(config, tmp_path)
    code, _ = run(capsys, "--config", str(path), "index", "changes")
    assert code == 0  # no --as, no token: the host user's own local read


# MCP reads for roles: get_message, before_seq, list fields, search -------------------------------------------


def test_get_message_follows_read_history_visibility(db, config) -> None:
    conversation_id = start(db, config)
    sent = say(db, config, "reviewer", conversation_id, "visible to members")
    direct = messages.send(db, sender="reviewer", recipient="assistant", text="just for you")
    assert search.get_message(db, config, "assistant", sent["message_id"])["text"] == "visible to members"
    assert search.get_message(db, config, "reviewer", sent["message_id"])["seq"] == 1  # its own, in conv order
    assert search.get_message(db, config, "viewer", sent["message_id"])["from_role"] == "reviewer"
    missing = refused("unknown_message", search.get_message, db, config, "outsider", sent["message_id"])
    unknown = refused("unknown_message", search.get_message, db, config, "outsider", "msg_0000000000000000")
    assert str(missing).replace(sent["message_id"], "X") == str(unknown).replace("msg_0000000000000000", "X")
    refused("unknown_message", search.get_message, db, config, "viewer", direct["message_id"])  # not a conv
    assert search.get_message(db, config, "assistant", direct["message_id"])["text"] == "just for you"
    conversations.remove(db, config, conversation_id, "assistant", actor="orchestrator")
    refused("unknown_message", search.get_message, db, config, "assistant", sent["message_id"])


def test_read_history_reads_backwards_with_before_seq(db, config) -> None:
    conversation_id = start(db, config)
    for n in range(1, 7):
        say(db, config, "reviewer", conversation_id, f"m{n}")
    page = conversations.history(db, "assistant", conversation_id, before_seq=5, limit=2)
    assert [m["seq"] for m in page["messages"]] == [3, 4]
    assert page["prev_seq"] == 3
    earlier = conversations.history(db, "assistant", conversation_id, before_seq=page["prev_seq"], limit=5)
    assert [m["seq"] for m in earlier["messages"]] == [1, 2]
    refused("invalid_request", conversations.history, db, "assistant", conversation_id, before_seq=0)


def test_list_conversations_carries_revision_last_seq_and_activity(db, config) -> None:
    conversation_id = start(db, config)
    say(db, config, "reviewer", conversation_id, "hello")
    [listed] = conversations.list_for(db, "assistant")
    assert (listed["revision"], listed["last_seq"]) == (1, 1) and listed["last_activity_at"]
    later = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    assert conversations.list_for(db, "assistant", changed_since=later) == []
    earlier = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    assert [c["conversation_id"] for c in conversations.list_for(db, "assistant", changed_since=earlier)] == \
        [conversation_id]


def ids(hits: dict[str, Any]) -> list[str]:
    return [hit["message_id"] for hit in hits["hits"]]


def test_search_authorizes_before_ranking(db, config) -> None:
    private = start(db, config, ["orchestrator", "reviewer"], title="Private")
    shared = start(db, config, ["orchestrator", "assistant"], title="Shared")
    perfect = say(db, config, "orchestrator", private, "zebra zebra zebra zebra")
    weaker = say(db, config, "orchestrator", shared, "a zebra once")
    assert ids(search.search(db, config, "assistant", "zebra", limit=1)) == [weaker["message_id"]]
    assert perfect["message_id"] not in ids(search.search(db, config, "assistant", "zebra", limit=50))
    assert set(ids(search.search(db, config, "viewer", "zebra"))) == {perfect["message_id"], weaker["message_id"]}
    assert ids(search.search(db, config, "outsider", "zebra")) == []


def test_search_returns_each_message_once_and_respects_join_point_and_removal(db, config) -> None:
    conversation_id = start(db, config)
    before_join = say(db, config, "reviewer", conversation_id, "quokka before the join")
    conversations.add(db, config, conversation_id, "outsider", actor="orchestrator")
    after_join = say(db, config, "reviewer", conversation_id, "quokka after the join")
    assert ids(search.search(db, config, "orchestrator", "quokka")).count(before_join["message_id"]) == 1
    assert ids(search.search(db, config, "outsider", "quokka")) == [after_join["message_id"]]
    assert set(ids(search.search(db, config, "reviewer", "quokka"))) == \
        {before_join["message_id"], after_join["message_id"]}  # its own sent messages too
    conversations.remove(db, config, conversation_id, "outsider", actor="orchestrator")
    assert ids(search.search(db, config, "outsider", "quokka")) == []


def test_search_covers_direct_messages_only_for_their_two_ends(db, config) -> None:
    direct = messages.send(db, sender="reviewer", recipient="assistant", text="ocelot sighting")
    assert ids(search.search(db, config, "assistant", "ocelot")) == [direct["message_id"]]
    assert ids(search.search(db, config, "reviewer", "ocelot")) == [direct["message_id"]]
    assert ids(search.search(db, config, "viewer", "ocelot")) == []  # view-all widens to conversations only


def test_search_refuses_artifact_kinds_and_takes_messages(db, config) -> None:
    conversation_id = start(db, config)
    say(db, config, "reviewer", conversation_id, "lemur notes")
    refused("unsupported_kind", search.search, db, config, "assistant", "lemur", kinds=["summary"])
    refused("unsupported_kind", search.search, db, config, "assistant", "lemur", kinds=["message", "decisions"])
    assert len(search.search(db, config, "assistant", "lemur", kinds=["message"])["hits"]) == 1


def test_search_treats_query_syntax_as_plain_words(db, config) -> None:
    conversation_id = start(db, config)
    say(db, config, "reviewer", conversation_id, 'the "deploy" NEAR window')
    for query in ('"deploy', "deploy OR", "NEAR(", "deploy*", "-x"):
        search.search(db, config, "assistant", query)  # never a syntax error
    refused("invalid_request", search.search, db, config, "assistant", "   ")


def test_hits_carry_where_to_read_and_never_more_than_the_reader_may(db, config) -> None:
    conversation_id = start(db, config)
    sent = say(db, config, "reviewer", conversation_id, "the capybara plan is ready")
    [hit] = search.search(db, config, "assistant", "capybara")["hits"]
    assert (hit["message_id"], hit["conversation_id"], hit["seq"], hit["from_role"], hit["derived"]) == \
        (sent["message_id"], conversation_id, 1, "reviewer", False)
    assert "capybara" in hit["snippet"]


def test_the_keyword_fallback_is_reported_and_authorizes_the_same(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store, "fts5_available", lambda conn: False)
    config = search_config(tmp_path)
    db = connect(config.db_path)
    assert search.keyword_mode(db) == "like"
    private = start(db, config, ["orchestrator", "reviewer"], title="Private")
    shared = start(db, config, ["orchestrator", "assistant"], title="Shared")
    hidden = say(db, config, "orchestrator", private, "narwhal in private")
    seen = say(db, config, "orchestrator", shared, "narwhal in public")
    assert ids(search.search(db, config, "assistant", "narwhal")) == [seen["message_id"]]
    assert hidden["message_id"] in ids(search.search(db, config, "viewer", "narwhal"))
    db.close()


def test_the_fts_index_follows_retention(db, config) -> None:
    conversation_id = start(db, config)
    sent = say(db, config, "orchestrator", conversation_id, "platypus goodbye")
    for role in ("reviewer", "assistant"):
        messages.read(db, role)
    age(db, sent["message_id"])
    messages.sweep(db)
    assert ids(search.search(db, config, "viewer", "platypus")) == []
    assert db.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == 0


# Not-consuming, as one end-to-end check (contract test 9) ----------------------------------------------------


def test_9_reads_and_index_commands_consume_nothing(db, config) -> None:
    conversation_id = start(db, config)
    sent = say(db, config, "reviewer", conversation_id, "wombat news")
    unread_before = {role: messages.unread(db, role) for role in GROUP}
    snapshot = state(db)
    for _ in range(3):
        search.search(db, config, "assistant", "wombat")
        search.get_message(db, config, "assistant", sent["message_id"])
        conversations.history(db, "assistant", conversation_id, before_seq=2)
        conversations.list_for(db, "assistant")
        reader = store.connect_read_only(config.db_path)
        changes.read(reader, changes.Scope.ALL)
        changes.wait(reader, changes.Scope.ALL, cursor=f"{changes.epoch(db)}:1", timeout_seconds=0)
        reader.close()
    assert state(db) == snapshot
    assert {role: messages.unread(db, role) for role in GROUP} == unread_before
    delivered = [m["message_id"] for m in messages.read(db, "assistant")["messages"]]
    assert delivered == [sent["message_id"]] and messages.read(db, "assistant")["messages"] == []


# Migration ------------------------------------------------------------------------------------------------


def test_a_version_6_database_gains_revisions_a_fresh_log_and_a_search_index(tmp_path: Path) -> None:
    config = search_config(tmp_path)
    config.data_dir.mkdir(mode=0o700)
    raw = sqlite3.connect(config.db_path)
    raw.executescript((Path(__file__).parent / "fixtures" / "schema_v6.sql").read_text())
    stamp = datetime.now(UTC).isoformat()
    with raw:
        raw.execute("INSERT INTO conversations (id, request_id, request_digest, title, created_by, created_at,"
                    " last_seq) VALUES ('conv_old', 'r', 'd', 'Old', 'orchestrator', ?, 1)", (stamp,))
        raw.executemany("INSERT INTO participants VALUES ('conv_old', ?, ?)", [(r, stamp) for r in GROUP])
        for recipient, seq in (("reviewer", 1), ("assistant", 1)):
            raw.execute("INSERT INTO messages (id, conversation_id, conversation_seq, sender, author_kind,"
                        " recipient, seq, text, at) VALUES ('msg_old', 'conv_old', 1, 'orchestrator', 'agent',"
                        " ?, ?, 'an aardvark from before', ?)", (recipient, seq, stamp))
        raw.executemany("INSERT INTO mailboxes VALUES (?, 1, 0)", [("reviewer",), ("assistant",)])
    raw.close()
    os.chmod(config.db_path, 0o600)

    db = connect(config.db_path)
    assert db.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    assert revision(db, "conv_old") == 0 and log(db) == []
    assert len(changes.epoch(db)) == 32
    [listed] = conversations.list_for(db, "assistant")
    assert listed["last_activity_at"] == stamp
    assert ids(search.search(db, config, "assistant", "aardvark")) == ["msg_old"]
    say(db, config, "reviewer", "conv_old", "after the upgrade")
    assert revision(db, "conv_old") == 1
    db.close()
    connect(config.db_path).close()  # a second open changes nothing


# MCP surface ----------------------------------------------------------------------------------------------


def test_the_search_tools_appear_only_when_search_is_enabled(tmp_path: Path) -> None:
    from baton.app import CONVERSATION_TOOLS, SEARCH_TOOLS

    names = {name for name, _, _ in SEARCH_TOOLS}
    assert names == {"search", "get_message"}
    assert all(read_only for _, _, read_only in SEARCH_TOOLS)
    assert not names & {name for name, _, _ in CONVERSATION_TOOLS}
    assert search_config(tmp_path, enabled=False).search.enabled is False


def test_whoami_reports_the_keyword_mode(config) -> None:
    from baton.app import Tools

    shown = Tools(config).whoami("assistant")
    assert shown["search"] == {"enabled": True, "keyword_index": "fts5"}


def test_no_command_line_index_writer_exists_yet() -> None:
    result = subprocess.run([sys.executable, "-m", "baton.cli", "index", "--help"], capture_output=True, text=True,
                            cwd=Path(__file__).parent.parent)
    assert "changes" in result.stdout and "wait" in result.stdout
    assert "commit" not in result.stdout and "begin" not in result.stdout  # writers arrive in a later slice


@pytest.mark.parametrize("key, value", [("messaging_role", "worker"), ("proposal_recipients", ["worker"])])
def test_the_reserved_indexer_keys_are_refused_for_now(key: str, value: Any) -> None:
    raw = {"host": {"public_url": "http://127.0.0.1:8765"}, "roles": {"worker": {}}, "indexer": {key: value}}
    with pytest.raises(config_module.ConfigError, match=f"indexer.{key} is not supported yet"):
        config_module.parse(raw)


# Review round: over-limit queries, `before` across offsets, one snapshot per change-log read ---------------


@pytest.fixture(params=["fts5", "like"])
def any_mode(request, tmp_path: Path, monkeypatch):
    if request.param == "like":
        monkeypatch.setattr(store, "fts5_available", lambda conn: False)
    config = search_config(tmp_path)
    connection = connect(config.db_path)
    assert search.keyword_mode(connection) == request.param
    yield connection, config
    connection.close()


def test_a_query_over_the_word_limit_is_refused_not_cut(any_mode) -> None:
    db, config = any_mode
    conversation_id = start(db, config)
    say(db, config, "reviewer", conversation_id, "one two three four five six seven eight")
    words = "one two three four five six seven eight"
    assert len(search.search(db, config, "assistant", words)["hits"]) == 1
    error = refused("invalid_request", search.search, db, config, "assistant", words + " absent")
    assert str(search.MAX_TERMS) in str(error)  # a ninth required word is never dropped silently


def test_a_query_over_the_length_limit_is_refused_not_cut(any_mode) -> None:
    db, config = any_mode
    conversation_id = start(db, config)
    say(db, config, "reviewer", conversation_id, "needle")
    padded = "needle " + " " * search.MAX_QUERY + "absent"
    error = refused("invalid_request", search.search, db, config, "assistant", padded)
    assert str(search.MAX_QUERY) in str(error)


def placed(db, message_id: str, at: str) -> None:
    db.execute("UPDATE messages SET at = ? WHERE id = ?", (at, message_id))


def test_before_compares_instants_not_strings(any_mode) -> None:
    db, config = any_mode
    conversation_id = start(db, config)
    sent = say(db, config, "reviewer", conversation_id, "heron")
    placed(db, sent["message_id"], "2026-10-30T10:00:00.500000+00:00")
    found = lambda before: ids(search.search(db, config, "assistant", "heron", before=before))  # noqa: E731
    # the same instant in other offsets: never strictly before itself
    assert found("2026-10-30T10:00:00.500000Z") == []
    assert found("2026-10-30T12:00:00.500000+02:00") == []
    assert found("2026-10-30T05:00:00.500000-05:00") == []
    # a microsecond later, in an offset whose wall clock reads earlier than the stored text
    assert found("2026-10-30T05:00:00.500001-05:00") == [sent["message_id"]]
    # crossing: the wall clock reads later than the stored text, the instant is earlier
    assert found("2026-10-30T11:30:00+02:00") == []


@pytest.mark.parametrize("before", ["2026-10-30T10:00:00", "yesterday", "2026-13-01T00:00:00Z"])
def test_before_without_an_offset_or_unparseable_is_refused(db, config, before: str) -> None:
    refused("invalid_request", search.search, db, config, "assistant", "heron", before=before)


def test_a_prune_between_validating_a_cursor_and_scanning_never_skips_rows(db, config, monkeypatch) -> None:
    conversation_id = start(db, config)
    for word in ("one", "two", "three", "four"):
        say(db, config, "reviewer", conversation_id, word)
    seqs = [entry["change_seq"] for entry in log(db)]
    cursor = f"{changes.epoch(db)}:{seqs[0]}"
    reader = store.connect_read_only(config.db_path)
    validate = changes._position

    def then_prune(conn, cursor):
        position = validate(conn, cursor)
        # another connection prunes past the cursor after it was found valid
        db.execute("DELETE FROM change_log WHERE change_seq <= ?", (seqs[2],))
        db.execute("UPDATE log_meta SET value = ? WHERE key = 'pruned_through'", (str(seqs[2]),))
        return position

    monkeypatch.setattr(changes, "_position", then_prune)
    try:
        page = changes.read(reader, changes.Scope.ALL, cursor=cursor)
    except BatonError as expired:
        assert expired.code == "cursor_expired"
    else:  # a page from one snapshot: everything after the cursor, nothing skipped
        assert [entry["change_seq"] for entry in page["entries"]] == seqs[1:]
    finally:
        reader.close()
