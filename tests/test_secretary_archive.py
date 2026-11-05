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
        archive.save("secretary-a", _result("planner", "decision-a", "a changed retry"))
        assert conn.execute("SELECT COUNT(*) FROM secretary_summaries").one()[0] == count
        stored = conn.execute("SELECT result FROM secretary_summaries ORDER BY cursor").fetchall()
        assert all("private-policy-metadata" not in row[0] and "harness" not in row[0] for row in stored)
    finally:
        conn.close()


def test_archive_does_not_delete_rows_when_private_state_is_gone(host, tmp_path) -> None:
    archive = SummaryArchive(host.config)
    archive.save("secretary-a", _result("planner", "persisted"))
    private_state = tmp_path / "secretary-private.sqlite"
    private_state.write_text("private working state")
    private_state.unlink()

    conn = connect(host.config.db_path)
    try:
        assert conn.execute("SELECT decision_id FROM secretary_summaries").one()[0] == "persisted"
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
        assert conn.execute("SELECT version FROM schema_version").one()[0] == SCHEMA_VERSION == 21
        assert conn.execute("SELECT value FROM preserved").one()[0] == "unchanged"
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(secretary_summaries)")}
        assert columns == {"cursor", "secretary_id", "principal", "decision_id", "result", "created_at"}
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(secretary_summaries)")}
        assert "secretary_summaries_by_principal" in indexes
    finally:
        conn.close()
