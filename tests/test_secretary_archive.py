"""The shared archive keeps only deliverable summaries and pages them by authenticated principal."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from secretary_support import make_world

from baton.agents.agent import Agent
from baton.agents.secretary.attention import archive as archive_module
from baton.agents.secretary.attention.archive import SummaryArchive
from baton.database.store import SCHEMA_VERSION, connect
from baton.errors import Forbidden, InvalidRequest


def _result(principal: str, decision_id: str, brief: str = "A useful summary", *, source_refs=None) -> dict:
    return {
        "principal": principal,
        "decision_id": decision_id,
        "decision": "wake",
        "brief": brief,
        "sources": ["thread-1"] if source_refs else [],
        "source_refs": source_refs or [],
        "kind": "summary",
        "mode": "alert",
        "engine_verdict": {"required": False},
        "policy_digest": "private-policy-metadata",
        "harness": {"model_calls": 1},
    }


def test_archive_is_append_only_paged_and_scoped_to_authenticated_principal(host) -> None:
    archive = SummaryArchive(host.config)
    first = archive.save("secretary-a", _result("planner", "decision-a"))
    second = archive.save("secretary-b", _result("planner", "decision-b"))
    assert first is not None and second is not None
    assert archive.save("secretary-a", _result("planner", "hold", "  ")) is None

    conn = connect(host.config.db_path)
    try:
        planner = Agent(conn, host.config, "planner")
        worker = Agent(conn, host.config, "worker")
        page = archive.page_for(planner, limit=1)
        assert len(page["items"]) == 1
        assert page["has_more"] is True
        assert page["next"] == page["items"][0]["cursor"]
        assert page["items"][0]["secretary_id"] == "secretary-a"
        assert page["items"][0]["summary"] == {
            "decision": "wake",
            "brief": "A useful summary",
            "sources": [],
            "source_refs": [],
            "kind": "summary",
            "mode": "alert",
            "engine_verdict": {"required": False},
        }
        continuation = archive.page_for(planner, after=page["next"], limit=1)
        assert [entry["secretary_id"] for entry in continuation["items"]] == ["secretary-b"]
        assert archive.page_for(worker)["items"] == []

        count = conn.execute("SELECT COUNT(*) FROM secretary_summaries").one()[0]
        original = conn.execute("SELECT cursor,result FROM secretary_summaries WHERE secretary_id='secretary-a'").one()
        archive.save("secretary-a", _result("planner", "decision-a", "a changed retry"))
        assert conn.execute("SELECT COUNT(*) FROM secretary_summaries").one()[0] == count
        unchanged = conn.execute("SELECT cursor,result FROM secretary_summaries WHERE secretary_id='secretary-a'").one()
        assert tuple(unchanged) == tuple(original)
        assert json.loads(unchanged[1])["brief"] == "A useful summary"
        stored = conn.execute("SELECT result FROM secretary_summaries ORDER BY cursor").fetchall()
        assert all("private-policy-metadata" not in row[0] and "harness" not in row[0] for row in stored)
    finally:
        conn.close()


def test_secretary_state_can_be_removed_after_summary_is_archived(tmp_path) -> None:
    from baton.agents.secretary.attention.assessment import Assessment
    from baton.agents.secretary.attention.outcome import Outcome, persist
    from baton.agents.secretary.inbox.window import InboxWindow
    from baton.config.secretary import load_policy

    with make_world(tmp_path) as world:
        policy = load_policy(world.config, world.policy_path)
        from baton.agents.secretary.service import Secretary

        secretary = Secretary(world.config, world.state_path, world.policy_path)
        result = Outcome(
            policy,
            InboxWindow("planner", "epoch-1", 0, 0, [], []),
            floor=0,
            mode="alert",
        ).build(
            Assessment("wake", "needs attention", "The release is ready for review.", []),
            decision_id="persisted-summary",
        )
        persist(secretary, policy, result)
        assert world.state_path.exists()
        world.state_path.unlink()

        conn = connect(world.config.db_path)
        try:
            page = SummaryArchive(world.config).page_for(Agent(conn, world.config, "planner"))
            assert [entry["decision_id"] for entry in page["items"]] == ["persisted-summary"]
            assert page["items"][0]["summary"]["brief"] == "The release is ready for review."
        finally:
            conn.close()


def test_summaries_cli_pages_by_policy_principal_without_private_state(tmp_path) -> None:
    with make_world(tmp_path) as world:
        archive = SummaryArchive(world.config)
        archive.save("planner-secretary", _result("planner", "planner-first", "first"))
        archive.save("planner-secretary", _result("planner", "planner-second", "second"))
        archive.save("worker-secretary", _result("worker", "worker-only", "other principal"))
        assert not world.state_path.exists()

        command = [
            sys.executable,
            "-m",
            "baton.cli",
            "--config",
            str(world.config_path),
            "secretary",
            "--policy",
            str(world.policy_path),
            "summaries",
            "--limit",
            "1",
        ]
        cwd = Path(__file__).resolve().parents[1]
        first = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
        assert first.returncode == 0, first.stderr
        first_page = json.loads(first.stdout)
        assert first_page["principal"] == "planner"
        assert [item["decision_id"] for item in first_page["items"]] == ["planner-first"]
        assert first_page["has_more"] is True
        assert isinstance(first_page["next"], int)

        continuation = subprocess.run(
            [*command, "--after", str(first_page["next"])],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        assert continuation.returncode == 0, continuation.stderr
        next_page = json.loads(continuation.stdout)
        assert [item["decision_id"] for item in next_page["items"]] == ["planner-second"]
        assert next_page["has_more"] is False
        assert not world.state_path.exists()


def test_remote_secretary_summaries_follow_token_role_and_reject_local_policy(
    start_host, capsys, monkeypatch, tmp_path
) -> None:
    from baton.cli.main import main

    host = start_host()
    archive = SummaryArchive(host.config)
    archive.save("planner-secretary", _result("planner", "planner-first", "first"))
    archive.save("planner-secretary", _result("planner", "planner-second", "second"))
    archive.save("worker-secretary", _result("worker", "worker-only", "other principal"))
    assert not (host.config.data_dir / "secretary.db").exists()

    monkeypatch.setenv("BATON_TOKEN", host.tokens["planner"])
    command = [
        "--host",
        host.base,
        "secretary",
        "summaries",
        "--limit",
        "1",
    ]
    assert main(command) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["principal"] == "planner"
    assert [item["decision_id"] for item in first["items"]] == ["planner-first"]
    assert first["has_more"] is True

    wrong_policy = tmp_path / "policy-for-worker.json"
    wrong_policy.write_text('{"principal":"worker"}')
    assert main(["--host", host.base, "secretary", "--policy", str(wrong_policy), "summaries"]) == 2
    rejection = json.loads(capsys.readouterr().out)
    assert rejection["error"] == "config" and "--policy" in rejection["detail"]

    assert main([*command, "--after", str(first["next"])]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["principal"] == "planner"
    assert [item["decision_id"] for item in second["items"]] == ["planner-second"]
    assert second["has_more"] is False

    monkeypatch.setenv("BATON_TOKEN", host.tokens["worker"])
    assert main(["--host", host.base, "secretary", "summaries"]) == 0
    worker_page = json.loads(capsys.readouterr().out)
    assert worker_page["principal"] == "worker"
    assert [item["decision_id"] for item in worker_page["items"]] == ["worker-only"]


def test_archive_pages_past_conversation_revocation_without_skipping_visible_rows(tmp_path) -> None:
    with make_world(tmp_path) as world:
        from baton import conversations

        board = connect(world.config.db_path)
        try:
            room = conversations.create(
                board,
                world.config,
                actor="planner",
                title="Visibility test",
                participants=["planner", "worker", "reviewer"],
            )["conversation_id"]
            sent = conversations.send(
                board, world.config, sender="worker", conversation_id=room, text="source that becomes unavailable"
            )
            message_id = sent["message_id"]
            row = board.execute(
                "SELECT pos FROM events WHERE kind='message' AND json_extract(data,'$.message_id')=? LIMIT 1",
                (message_id,),
            ).one()
            archive = SummaryArchive(world.config)
            archive.save("secretary-a", _result("planner", "before", "visible before"))
            archive.save(
                "secretary-a",
                _result(
                    "planner",
                    "revoked",
                    "no longer visible",
                    source_refs=[
                        {
                            "source_key": f"message:{message_id}",
                            "pos": row[0],
                            "message_id": message_id,
                            "conversation_id": room,
                            "job_id": None,
                        }
                    ],
                ),
            )
            archive.save("secretary-a", _result("planner", "after", "visible after"))

            conversations.remove(board, world.config, room, "planner", actor="planner")

            page = archive.page_for_host("planner", limit=1)
            assert [item["decision_id"] for item in page["items"]] == ["before"]
            assert page["has_more"] is True
            following = archive.page_for_host("planner", after=page["next"], limit=1)
            assert [item["decision_id"] for item in following["items"]] == ["after"]
            assert following["has_more"] is False
        finally:
            board.close()


def test_archive_continues_after_more_than_500_hidden_rows(tmp_path) -> None:
    with make_world(tmp_path) as world:
        from baton import conversations

        board = connect(world.config.db_path)
        try:
            room = conversations.create(
                board,
                world.config,
                actor="planner",
                title="Long visibility test",
                participants=["planner", "worker", "reviewer"],
            )["conversation_id"]
            sent = conversations.send(
                board, world.config, sender="worker", conversation_id=room, text="source later revoked"
            )
            message_id = sent["message_id"]
            event = board.execute(
                "SELECT pos FROM events WHERE kind='message' AND json_extract(data,'$.message_id')=? LIMIT 1",
                (message_id,),
            ).one()
            archive = SummaryArchive(world.config)
            reference = {
                "source_key": f"message:{message_id}",
                "pos": event[0],
                "message_id": message_id,
                "conversation_id": room,
                "job_id": None,
            }
            for index in range(501):
                archive.save("secretary-a", _result("planner", f"hidden-{index:03}", source_refs=[reference]))
            archive.save("secretary-a", _result("planner", "visible-first"))
            archive.save("secretary-a", _result("planner", "visible-second"))
            conversations.remove(board, world.config, room, "planner", actor="planner")

            after = 0
            visible = []
            for _ in range(5):
                page = archive.page_for_host("planner", after=after, limit=1)
                visible.extend(item["decision_id"] for item in page["items"])
                if not page["has_more"]:
                    break
                assert isinstance(page["next"], int) and page["next"] > after
                after = page["next"]
            assert visible == ["visible-first", "visible-second"]
        finally:
            board.close()


def test_archive_checks_only_limit_plus_one_sources_with_indexed_message_lookup(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        from baton import conversations
        from baton.database import connect_read_only, snapshot

        board = connect(world.config.db_path)
        try:
            room = conversations.create(
                board,
                world.config,
                actor="planner",
                title="Lookup plan test",
                participants=["planner", "worker", "reviewer"],
            )["conversation_id"]
            sent = conversations.send(board, world.config, sender="worker", conversation_id=room, text="source")
            event = board.execute(
                "SELECT pos,seq,audience FROM events WHERE kind='message'"
                " AND json_extract(data,'$.message_id')=? LIMIT 1",
                (sent["message_id"],),
            ).one()
            archive = SummaryArchive(world.config)
            reference = {
                "source_key": f"message:{sent['message_id']}",
                "pos": event["pos"],
                "message_id": sent["message_id"],
                "conversation_id": room,
                "job_id": None,
            }
            for index in range(50):
                archive.save("secretary-a", _result("planner", f"visible-{index:02}", source_refs=[reference]))

            calls = 0
            original = archive_module._reference_visible

            def count_reference_checks(conn, config, principal, reference):
                nonlocal calls
                calls += 1
                return original(conn, config, principal, reference)

            monkeypatch.setattr(archive_module, "_reference_visible", count_reference_checks)
            page = archive.page_for_host("planner", limit=1)
            assert [item["decision_id"] for item in page["items"]] == ["visible-00"]
            assert calls == 2

            # Capture the SQL executed by the compatibility fallback itself, then explain that exact
            # statement. This pins the index relied on by `_visible_message`, not a similar hand query.
            from baton.agents.secretary.inbox import sources

            statements = []
            original_visible_message = sources._visible_message

            def trace_visible_message(conn, config, principal, message_id):
                conn.set_trace_callback(statements.append)
                try:
                    return original_visible_message(conn, config, principal, message_id)
                finally:
                    conn.set_trace_callback(None)

            monkeypatch.setattr(sources, "_visible_message", trace_visible_message)
            fallback_summary = _result(
                "planner",
                "reply-context-fallback",
                source_refs=[{**reference, "pos": None}],
            )
            with connect_read_only(world.config.db_path) as readonly, snapshot(readonly):
                assert archive_module._references_visible(readonly, world.config, "planner", fallback_summary)
            actual_query = next(sql for sql in statements if "FROM messages m WHERE m.id=" in sql)
            plan = board.execute("EXPLAIN QUERY PLAN " + actual_query).fetchall()
            plan_text = " ".join(str(row[3]).upper() for row in plan)
            assert "MESSAGES_BY_ID" in plan_text
            assert "SCAN M" not in plan_text
        finally:
            board.close()


def test_archive_keeps_actual_reply_parent_citation_visible_until_revoked(tmp_path) -> None:
    from secretary_support import conversation, say

    from baton.agents.secretary.attention.assessment import Assessment
    from baton.agents.secretary.attention.outcome import Outcome
    from baton.agents.secretary.inbox.window import InboxWindow
    from baton.config.secretary import load_policy

    with make_world(tmp_path) as world:
        room = conversation(world)
        parent = say(world, room, "planner", "Please check the rollout", attention=["worker"])
        _, before_reply = InboxWindow.head(world.config)
        reply = say(world, room, "worker", "The rollout is ready", reply_to=parent["message_id"])
        policy = load_policy(world.config, world.policy_path)
        board = connect(world.config.db_path)
        window = InboxWindow.read(world.config, policy, before_reply)
        assert [item["message"]["id"] for item in window.reply_context] == [parent["message_id"]]

        parent_key = f"message:{parent['message_id']}"
        reply_key = f"message:{reply['message_id']}"
        result = Outcome(policy, window, floor=0, mode="alert").build(
            Assessment("wake", "ready", "The rollout is ready for review.", [reply_key, parent_key]),
            decision_id="reply-parent-archive",
        )
        parent_ref = next(ref for ref in result["source_refs"] if ref["source_key"] == parent_key)
        assert parent_ref["pos"] is None
        archive = SummaryArchive(world.config)
        archive.save(policy.secretary_id, result)

        before_revocation = archive.page_for_host("planner")
        assert [item["decision_id"] for item in before_revocation["items"]] == ["reply-parent-archive"]
        from baton import conversations

        conversations.remove(board, world.config, room, "planner", actor="planner")
        after_revocation = archive.page_for_host("planner")
        assert after_revocation["items"] == []
        board.close()


def test_archive_rechecks_reply_parent_when_view_all_is_revoked(tmp_path, monkeypatch) -> None:
    from dataclasses import replace

    from secretary_support import policy

    from baton import conversations
    from baton.agents.secretary.attention.assessment import Assessment
    from baton.agents.secretary.attention.outcome import Outcome
    from baton.agents.secretary.inbox import sources
    from baton.agents.secretary.inbox.window import InboxWindow
    from baton.config.secretary import load_policy

    with make_world(tmp_path) as world:
        room = conversations.create(
            world.baton,
            world.config,
            actor="planner",
            title="Parent visibility changes",
            participants=["planner", "worker"],
        )["conversation_id"]
        parent = conversations.send(
            world.baton,
            world.config,
            sender="planner",
            conversation_id=room,
            text="Please check the rollout",
        )
        reviewer_role = replace(world.config.roles["reviewer"], can_view_all_conversations=True)
        world.config = replace(
            world.config,
            roles={**world.config.roles, "reviewer": reviewer_role},
        )
        conversations.add(world.baton, world.config, room, "reviewer", actor="planner")
        _, before_reply = InboxWindow.head(world.config)
        reply = conversations.send(
            world.baton,
            world.config,
            sender="worker",
            conversation_id=room,
            text="The rollout is ready",
            reply_to=parent["message_id"],
        )
        policy_path = policy(world, principal="reviewer", secretary_id="reviewer-secretary")
        secretary_policy = load_policy(world.config, policy_path)
        window = InboxWindow.read(world.config, secretary_policy, before_reply)
        parent_key = f"message:{parent['message_id']}"
        reply_key = f"message:{reply['message_id']}"
        assert [item["message"]["id"] for item in window.reply_context] == [parent["message_id"]]
        result = Outcome(secretary_policy, window, floor=0, mode="alert").build(
            Assessment("wake", "ready", "The rollout is ready for review.", [reply_key, parent_key]),
            decision_id="revoked-parent-archive",
        )
        parent_ref = next(ref for ref in result["source_refs"] if ref["source_key"] == parent_key)
        assert parent_ref["pos"] is None
        archive = SummaryArchive(world.config)
        archive.save(secretary_policy.secretary_id, result)
        assert [item["decision_id"] for item in archive.page_for_host("reviewer")["items"]] == [
            "revoked-parent-archive"
        ]

        # The role stays in the conversation and can read its reply, but no longer has a message copy
        # of the parent. Trace the actual fallback query used for that now-hidden parent citation.
        world.config = replace(
            world.config,
            roles={
                **world.config.roles,
                "reviewer": replace(world.config.roles["reviewer"], can_view_all_conversations=False),
            },
        )
        archive = SummaryArchive(world.config)
        statements = []
        original_visible_message = sources._visible_message

        def trace_visible_message(conn, config, principal, message_id):
            conn.set_trace_callback(statements.append)
            try:
                return original_visible_message(conn, config, principal, message_id)
            finally:
                conn.set_trace_callback(None)

        monkeypatch.setattr(sources, "_visible_message", trace_visible_message)
        hidden_page = archive.page_for_host("reviewer")
        assert hidden_page["items"] == []
        actual_query = next(sql for sql in statements if "FROM messages m WHERE m.id=" in sql)
        plan = world.baton.execute("EXPLAIN QUERY PLAN " + actual_query).fetchall()
        plan_text = " ".join(str(row[3]).upper() for row in plan)
        assert "MESSAGES_BY_ID" in plan_text
        assert "SCAN M" not in plan_text


def test_archive_citation_budget_stops_before_advancing_a_partial_row(tmp_path, monkeypatch) -> None:
    from baton import conversations

    with make_world(tmp_path) as world:
        room = conversations.create(
            world.baton,
            world.config,
            actor="planner",
            title="Citation budget",
            participants=["planner", "worker", "reviewer"],
        )["conversation_id"]
        visible = conversations.send(world.baton, world.config, sender="worker", conversation_id=room, text="visible")
        hidden_room = conversations.create(
            world.baton,
            world.config,
            actor="planner",
            title="Citation to revoke",
            participants=["planner", "worker", "reviewer"],
        )["conversation_id"]
        hidden = conversations.send(
            world.baton, world.config, sender="worker", conversation_id=hidden_room, text="hidden"
        )
        visible_pos = world.baton.execute(
            "SELECT pos FROM events WHERE kind='message' AND json_extract(data,'$.message_id')=?",
            (visible["message_id"],),
        ).fetchone()[0]
        hidden_pos = world.baton.execute(
            "SELECT pos FROM events WHERE kind='message' AND json_extract(data,'$.message_id')=?",
            (hidden["message_id"],),
        ).fetchone()[0]

        def ref(message_id: str, pos: int, conversation_id: str) -> dict:
            return {
                "source_key": f"message:{message_id}",
                "pos": pos,
                "message_id": message_id,
                "conversation_id": conversation_id,
                "job_id": None,
            }

        archive = SummaryArchive(world.config)
        archive.save(
            "secretary-a",
            _result("planner", "first-visible", source_refs=[ref(visible["message_id"], visible_pos, room)]),
        )
        archive.save(
            "secretary-a",
            _result(
                "planner",
                "many-citations-then-hidden",
                source_refs=[ref(visible["message_id"], visible_pos, room)] * (archive_module.MAX_CITATION_CHECKS + 272)
                + [ref(hidden["message_id"], hidden_pos, hidden_room)],
            ),
        )
        archive.save("secretary-a", _result("planner", "last-visible"))
        conversations.remove(world.baton, world.config, hidden_room, "planner", actor="planner")

        checks = 0
        original_reference_visible = archive_module._reference_visible

        def count_reference_checks(*args, **kwargs):
            nonlocal checks
            checks += 1
            return original_reference_visible(*args, **kwargs)

        monkeypatch.setattr(archive_module, "_reference_visible", count_reference_checks)
        first = archive.page_for_host("planner", limit=10)
        assert checks == archive_module.MAX_CITATION_CHECKS
        assert [item["decision_id"] for item in first["items"]] == ["first-visible"]
        assert first["next"] == first["items"][0]["cursor"]
        assert first["has_more"] is True

        checks = 0
        second = archive.page_for_host("planner", after=first["next"], limit=10)
        assert checks == archive_module.MAX_CITATION_CHECKS + 273
        assert second["items"] == []
        assert second["next"] > first["next"]
        assert second["has_more"] is True

        third = archive.page_for_host("planner", after=second["next"], limit=10)
        assert [item["decision_id"] for item in third["items"]] == ["last-visible"]
        assert third["has_more"] is False


def test_archive_validates_page_bounds_and_rejects_untrusted_roles(host) -> None:
    archive = SummaryArchive(host.config)
    conn = connect(host.config.db_path)
    try:
        with pytest.raises(Forbidden):
            archive.page_for(Agent(conn, host.config, "guest"))
    finally:
        conn.close()
    with pytest.raises(InvalidRequest):
        archive.page_for_host("planner", after=-1)
    with pytest.raises(InvalidRequest):
        archive.page_for_host("planner", limit=101)


def test_version_20_database_migrates_without_losing_existing_data(tmp_path) -> None:
    path = tmp_path / "baton.db"
    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        raw.execute("INSERT INTO schema_version VALUES (20)")
        raw.execute("CREATE TABLE preserved (value TEXT NOT NULL)")
        raw.execute("INSERT INTO preserved VALUES ('unchanged')")
    os.chmod(path, 0o600)

    conn = connect(path)
    try:
        assert conn.execute("SELECT version FROM schema_version").one()[0] == SCHEMA_VERSION == 22
        assert conn.execute("SELECT value FROM preserved").one()[0] == "unchanged"
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(secretary_summaries)")}
        assert columns == {"cursor", "secretary_id", "principal", "decision_id", "result", "created_at"}
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(secretary_summaries)")}
        assert "secretary_summaries_by_principal" in indexes
    finally:
        conn.close()
