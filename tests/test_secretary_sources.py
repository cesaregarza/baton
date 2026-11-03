"""Read-only mapping from actual Baton records into bounded attention envelopes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from secretary_support import ALIAS_ID, conversation, make_world, policy, say, table_hashes

from baton import cli, conversations, events, inbox, jobs, messages, secretary, secretary_store, store
from baton.config import Role
from baton.errors import InvalidRequest
from baton.store import transaction


def test_policy_rejects_invalid_relationships_before_state_file_exists(tmp_path) -> None:
    with make_world(tmp_path) as world:
        for principal in ("missing", "guest"):
            policy(world, principal=principal)
            with pytest.raises(ValueError):
                secretary.scan(world.config, world.state_path, world.policy_path)
            assert not world.state_path.exists()
        world.config = replace(world.config, roles={**world.config.roles,
                                                    "planner": replace(world.config.roles["planner"], human=True)})
        policy(world, principal="planner")
        with pytest.raises(ValueError):
            secretary.scan(world.config, world.state_path, world.policy_path)
        assert not world.state_path.exists()
        world.config = replace(world.config, roles={**world.config.roles, "planner": Role("planner")})
        world.config = replace(world.config, roles={**world.config.roles,
                                                    "planner": replace(world.config.roles["planner"],
                                                                       display_only=True)})
        with pytest.raises(ValueError):
            secretary.scan(world.config, world.state_path, world.policy_path)
        assert not world.state_path.exists()
        world.config = replace(world.config, roles={**world.config.roles, "planner": Role("planner")})
        policy(world, secretary_alias_id="unknown")
        with pytest.raises(ValueError):
            secretary.scan(world.config, world.state_path, world.policy_path)
        policy(world, secretary_alias_id="planner")
        with pytest.raises(ValueError):
            secretary.scan(world.config, world.state_path, world.policy_path)
        policy(world, secretary_alias_id=ALIAS_ID)
        world.baton.execute("INSERT INTO alias_roles (role, alias_id) VALUES (?, ?)", ("worker", ALIAS_ID))
        with pytest.raises(ValueError):
            secretary.scan(world.config, world.state_path, world.policy_path)
        assert not world.state_path.exists()


def test_complete_envelopes_and_no_source_text_in_sidecar(tmp_path) -> None:
    with make_world(tmp_path) as world:
        assert world.baton.execute("SELECT version FROM schema_version").fetchone()[0] == store.SCHEMA_VERSION
        room = conversation(world)
        messages.send(world.baton, sender="worker", recipient="planner", text="Direct secret phrase")
        say(world, room, "worker", "Attention post", attention=["planner"])
        say(world, room, "worker", "Decision post", attention=["planner"], needs_decision=True)
        asked = say(world, room, "planner", "Please check repo#7@abcdef0123456789", attention=["worker"])
        say(world, room, "worker", "received", reply_to=asked["message_id"])
        say(world, room, "guest", "Guest contribution", identity={"alias_id": ALIAS_ID,
                                                                     "alias": "lane-a", "code": "ab12"})
        say(world, room, "worker", "Authored words",
            relayed=[{"source": "upstream", "at": "2026-11-02T00:00:00Z", "text": "Relayed secret phrase"}])
        with transaction(world.baton):
            messages.store_copy(world.baton, message_id=messages.new_message_id(), sender=None,
                                recipient="planner", text="Host notice secret phrase", notice={"kind": "limit"})
        made = jobs.create(world.baton, world.config.jobs, request_id="job-source", actor="planner", title="Task")
        jobs.move(world.baton, world.config.jobs, made["job_id"], "working", actor="planner")
        jobs.move(world.baton, world.config.jobs, made["job_id"], "review", actor="worker")
        with transaction(world.baton):
            events.append(world.baton, job_id=None, seq=0, actor="worker", audience="planner",
                          kind="conversation.rate_limited", summary="limited", data={"conversation_id": room})
        result = secretary.scan(world.config, world.state_path, world.policy_path, limit=1000)
        assert result["events_seen"] >= 10
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        envelopes = [item["envelope"] for item in shown["items"]]
        kinds = {item["event_kind"] for item in envelopes}
        assert {"message", "job.created", "job.moved", "conversation.rate_limited"} <= kinds
        assert all(item["version"] == secretary.ENVELOPE_VERSION and item["source_key"] and
                   item["can_view_all_conversations"] is False
                   for item in envelopes)
        assert secretary.ENVELOPE_VERSION == 1
        assert any(item["text_sha256"] == hashlib.sha256(b"Direct secret phrase").hexdigest()
                   for item in envelopes if item["event_kind"] == "message")
        assert any(item["needs_decision"] and item["attention"] == ["planner"] for item in envelopes)
        assert any(item["reply_to"] == asked["message_id"] for item in envelopes)
        assert any(item["identity"]["alias_id"] == ALIAS_ID for item in envelopes)
        assert any(item["relayed_sha256"] and item["relayed_count"] == 1 for item in envelopes)
        assert any(item["has_notice"] and item["identity"]["from_role"] is None for item in envelopes)
        assert any(item["label"] == "host notice" for brief in shown["briefs"]
                   for item in brief["content"]["items"])
        assert all(row["requester"] is not None for row in shown["expectations"] if row["direction"] == "owed")
        assert any(item["job"] and item["job"]["event_state"] == "review" for item in envelopes)
        assert all("text" not in item and "body" not in item for item in envelopes)
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        try:
            stored = " ".join(str(tuple(row)) for row in state.execute("SELECT envelope FROM items"))
            assert "Direct secret phrase" not in stored
            assert "Relayed secret phrase" not in stored
            assert "Host notice secret phrase" not in stored
        finally:
            state.close()
        assert world.state_path.stat().st_mode & 0o777 == 0o600


def test_full_cycle_leaves_every_baton_table_unchanged(tmp_path) -> None:
    with make_world(tmp_path) as world:
        messages.send(world.baton, sender="worker", recipient="planner", text="Please decide")
        before = table_hashes(world.baton)
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        expectation = shown["expectations"][0]
        replayed = secretary.replay(world.config, world.state_path, world.policy_path)
        assert replayed["differences"] == []
        secretary.cancel(world.config, world.state_path, world.policy_path, expectation["id"], "withdrawn")
        assert secretary.replay(world.config, world.state_path, world.policy_path)["differences"] == []
        assert table_hashes(world.baton) == before


def test_read_only_source_refuses_an_unmigrated_database(tmp_path) -> None:
    with make_world(tmp_path) as world:
        legacy_dir = tmp_path / "legacy"
        legacy_dir.mkdir(mode=0o700)
        legacy = legacy_dir / "baton.db"
        raw = sqlite3.connect(legacy)
        try:
            raw.executescript((Path(__file__).parent / "fixtures" / "schema_v16.sql").read_text())
        finally:
            raw.close()
        legacy.chmod(0o600)
        legacy_config = replace(world.config, data_dir=legacy_dir)
        sidecar = world.state_path.with_name("legacy-sidecar.db")
        with pytest.raises(store.SchemaMismatch, match="schema"):
            secretary.scan(legacy_config, sidecar, world.policy_path)
        raw = sqlite3.connect(legacy)
        try:
            assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == 16
        finally:
            raw.close()
        assert not sidecar.exists()


def test_cli_is_host_only_and_requires_explicit_state(tmp_path, capsys) -> None:
    with make_world(tmp_path) as world:
        args = ["--config", str(world.config_path), "secretary", "--state", str(world.state_path),
                "--policy", str(world.policy_path)]
        assert cli.main([*args, "scan"]) == 0
        assert json.loads(capsys.readouterr().out)["events_seen"] == 0
        assert cli.main([*args, "replay"]) == 0
        capsys.readouterr()
        assert cli.main([*args, "show"]) == 0
        assert json.loads(capsys.readouterr().out)["principal"] == "planner"
        assert cli.main(["--config", str(world.config_path), "--host", "http://127.0.0.1:1",
                         "secretary", "--state", str(world.state_path), "--policy", str(world.policy_path),
                         "scan"]) == 2
        assert "host-local" in capsys.readouterr().out
        with pytest.raises(SystemExit) as absent:
            cli.main(["--config", str(world.config_path), "secretary", "--policy", str(world.policy_path), "scan"])
        assert absent.value.code == 2
        messages.send(world.baton, sender="worker", recipient="planner", text="Please decide")
        assert cli.main([*args, "scan"]) == 0
        capsys.readouterr()
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        expected_id = shown["expectations"][0]["id"]
        assert cli.main([*args, "cancel", expected_id, "--reason", "withdrawn"]) == 0
        assert json.loads(capsys.readouterr().out)["created"] is True
        assert cli.main([*args, "show", "--source", "missing"]) == 1
        assert json.loads(capsys.readouterr().out)["error"] == "invalid_request"
        policy(world, version=999)
        assert cli.main([*args, "replay"]) == 2
        assert json.loads(capsys.readouterr().out)["error"] == "config"


def test_unknown_versions_epoch_and_batch_bounds_are_refused(tmp_path) -> None:
    with make_world(tmp_path) as world:
        secretary.scan(world.config, world.state_path, world.policy_path)
        with pytest.raises(InvalidRequest):
            secretary.scan(world.config, world.state_path, world.policy_path, limit=1001)
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        state.execute("UPDATE sources SET log_epoch='another-epoch'")
        state.close()
        with pytest.raises(ValueError, match="log_epoch"):
            secretary.scan(world.config, world.state_path, world.policy_path)
        with pytest.raises(ValueError, match="log_epoch"):
            secretary.show(world.config, world.state_path, world.policy_path)
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        state.execute("UPDATE schema_version SET version=999")
        state.close()
        with pytest.raises(ValueError, match="schema"):
            secretary.scan(world.config, world.state_path, world.policy_path)


def test_unknown_envelope_version_is_refused_on_replay(tmp_path) -> None:
    with make_world(tmp_path) as world:
        messages.send(world.baton, sender="worker", recipient="planner", text="Please decide")
        secretary.scan(world.config, world.state_path, world.policy_path)
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        row = state.execute("SELECT principal, source_key, envelope FROM items LIMIT 1").fetchone()
        envelope = json.loads(row["envelope"])
        envelope["version"] = 999
        state.execute("UPDATE items SET envelope=? WHERE principal=? AND source_key=?",
                      (secretary_store.canonical(envelope), row["principal"], row["source_key"]))
        state.close()
        with pytest.raises(ValueError, match="envelope version"):
            secretary.replay(world.config, world.state_path, world.policy_path)


def test_unsupported_stored_envelope_version_is_refused_before_scan_advances(tmp_path) -> None:
    with make_world(tmp_path) as world:
        messages.send(world.baton, sender="worker", recipient="planner", text="First request")
        first = secretary.scan(world.config, world.state_path, world.policy_path)
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        row = state.execute("SELECT principal, source_key, envelope FROM items LIMIT 1").fetchone()
        envelope = json.loads(row["envelope"])
        envelope["version"] = 999
        state.execute("UPDATE items SET envelope=? WHERE principal=? AND source_key=?",
                      (secretary_store.canonical(envelope), row["principal"], row["source_key"]))
        state.close()
        messages.send(world.baton, sender="worker", recipient="planner", text="Second request")
        with pytest.raises(ValueError, match="envelope version"):
            secretary.scan(world.config, world.state_path, world.policy_path)
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        try:
            assert state.execute("SELECT through_pos FROM sources WHERE principal='planner'").fetchone()[0] == (
                first["through_pos"])
            assert state.execute("SELECT COUNT(*) FROM items WHERE principal='planner'").fetchone()[0] == 1
        finally:
            state.close()


def test_show_hides_sources_after_conversation_visibility_is_removed(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world, ["planner", "worker", "reviewer"])
        sent = say(world, room, "worker", "Private room evidence", attention=["planner"])
        secretary.scan(world.config, world.state_path, world.policy_path)
        key = f"message:{sent['message_id']}"
        assert secretary.show(world.config, world.state_path, world.policy_path, source_key=key)["items"]
        conversations.remove(world.baton, world.config, room, "planner", actor="planner")
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        assert all(item["envelope"]["source_key"] != key for item in shown["items"])
        assert all(row["opening_source"] != key for row in shown["expectations"])
        assert all(all(item["source_key"] != key for item in row["content"]["items"])
                   for row in shown["briefs"])
        with pytest.raises(InvalidRequest, match="not found"):
            secretary.show(world.config, world.state_path, world.policy_path, source_key=key)


def test_table_checksum_detects_naive_delivery_reads(tmp_path) -> None:
    with make_world(tmp_path) as world:
        messages.send(world.baton, sender="worker", recipient="planner", text="Work")
        before = table_hashes(world.baton)
        messages.read(world.baton, "planner", config=world.config)
        after_read = table_hashes(world.baton)
        assert after_read != before and after_read["mailboxes"] != before["mailboxes"]
        messages.send(world.baton, sender="worker", recipient="planner", text="More work")
        before_take = table_hashes(world.baton)
        inbox.take(world.baton, "planner", config=world.config)
        after_take = table_hashes(world.baton)
        assert after_take != before_take and after_take["cursors"] != before_take["cursors"]
