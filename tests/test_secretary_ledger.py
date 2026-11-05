"""Persistence invariants for secretary activation, delivery, and scan backlog."""

from __future__ import annotations

import json
import sqlite3

import pytest

from baton.agents.secretary.attention.backlog import Backlog, Page
from baton.agents.secretary.attention.state import AttentionLog
from baton.config import ConfigError
from baton.errors import InvalidRequest


def _log(tmp_path, principal: str = "planner") -> AttentionLog:
    return AttentionLog(tmp_path / "private.sqlite", tmp_path / "data", principal)


def _decision(log: AttentionLog, decision_id: str, since: int, through: int) -> None:
    log.remember(
        {
            "decision_id": decision_id,
            "since": since,
            "covered_through": through,
            "brief": "Delivered summary",
            "coverage": "complete",
        }
    )


def test_activation_sets_one_immutable_floor_and_checks_epoch(tmp_path) -> None:
    log = _log(tmp_path)

    assert log.activate("epoch-a", 12) == (12, 12)
    assert log.activate("epoch-a", 20) == (12, 12)
    assert log.positions() == (12, 12)
    with pytest.raises(ConfigError, match="epoch changed"):
        log.activate("epoch-b", 20)


def test_v1_model_state_requires_a_new_file_and_remains_unchanged(tmp_path) -> None:
    log = _log(tmp_path)
    with sqlite3.connect(log.path) as conn:
        conn.executescript(
            "CREATE TABLE attention_version(version INTEGER NOT NULL);"
            "INSERT INTO attention_version VALUES (1);"
            "CREATE TABLE principals(principal TEXT PRIMARY KEY,epoch TEXT NOT NULL,floor INTEGER NOT NULL,"
            "delivered INTEGER NOT NULL);"
            "INSERT INTO principals VALUES ('planner','epoch-a',3,3);"
            "CREATE TABLE assessments(principal TEXT NOT NULL,id TEXT NOT NULL,result TEXT NOT NULL,"
            "PRIMARY KEY(principal,id));"
        )
        conn.execute(
            "INSERT INTO assessments VALUES (?,?,?)",
            ("planner", "saved", json.dumps({"decision_id": "saved", "brief": "kept"})),
        )

    log.path.chmod(0o600)
    with pytest.raises(ConfigError, match="version 1.*new private state file"):
        log.positions()
    with sqlite3.connect(log.path) as conn:
        assert conn.execute("SELECT version FROM attention_version").fetchone() == (1,)
        assert conn.execute("SELECT * FROM principals").fetchall() == [("planner", "epoch-a", 3, 3)]
        assert conn.execute("SELECT id,result FROM assessments").fetchall() == [
            ("saved", json.dumps({"decision_id": "saved", "brief": "kept"}))
        ]
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall() == [
            ("assessments",),
            ("attention_version",),
            ("principals",),
        ]


def test_legacy_unrecognized_state_is_refused(tmp_path) -> None:
    log = _log(tmp_path)
    with sqlite3.connect(log.path) as conn:
        conn.execute("CREATE TABLE legacy_deterministic_state(value TEXT)")
    log.path.chmod(0o600)

    with pytest.raises(ConfigError, match="obsolete or unrelated schema"):
        log.positions()


def test_out_of_order_acknowledgment_waits_for_gap_then_advances_contiguously(tmp_path) -> None:
    log = _log(tmp_path)
    log.bind("epoch-a", 0)
    _decision(log, "later", 1, 2)
    _decision(log, "first", 0, 1)

    assert log.acknowledge("later") == 0
    assert log.is_acknowledged("later")
    assert log.acknowledge("first") == 2
    assert log.positions() == (2, 0)


def test_empty_coverage_waits_for_gap_then_advances_watermark(tmp_path) -> None:
    log = _log(tmp_path)
    log.bind("epoch-a", 2)
    _decision(log, "visible", 2, 3)

    assert log.acknowledge_empty(3, 5) == 2
    with pytest.raises(InvalidRequest, match="activation floor"):
        log.acknowledge_empty(1, 2)
    assert log.acknowledge("visible") == 5


def test_repeated_acknowledgment_is_idempotent(tmp_path) -> None:
    log = _log(tmp_path)
    log.bind("epoch-a", 0)
    _decision(log, "first", 0, 4)

    assert log.acknowledge("first") == 4
    assert log.acknowledge("first") == 4
    assert log.is_acknowledged("first")


def test_acknowledgment_requires_complete_coverage(tmp_path) -> None:
    log = _log(tmp_path)
    log.bind("epoch-a", 0)
    log.remember(
        {
            "decision_id": "incomplete",
            "since": 0,
            "covered_through": 1,
            "brief": "Partial summary",
            "coverage": "partial",
        }
    )

    with pytest.raises(InvalidRequest, match="complete coverage"):
        log.acknowledge("incomplete")


def test_backlog_preserves_decision_and_scan_state_across_restart(tmp_path) -> None:
    log = _log(tmp_path)
    log.activate("epoch-a", 7)
    backlog = Backlog(log)
    assert backlog.scanned() == 7
    page = Page(7, 8, mode="digest")
    backlog.enqueue(page, now=100.0)

    restarted = Backlog(log)
    assert restarted.scanned() == 8
    assert restarted.ready() == []
    assert restarted.due(99.0) == []
    assert restarted.due(100.0) == [page]

    _decision(log, "summary", 7, 8)
    retried = restarted.failed(page, now=100.0, error="model unavailable", initial=2.0, maximum=10.0)
    assert retried == Page(7, 8, mode="digest", attempts=1)
    assert restarted.failures() == 1
    assert restarted.due(101.0) == []
    assert restarted.due(102.0) == [retried]
    restarted.finish(
        retried,
        {"brief": "summary", "mode": "digest", "decision_id": "summary"},
        now=102.0,
        digest_seconds=3600.0,
    )
    assert Backlog(log).ready() == ["summary"]
    assert Backlog(log).due(10000.0) == []
    assert restarted.scanned() == 8


def test_backlog_prunes_acknowledged_page_but_keeps_assessment_and_receipt(tmp_path) -> None:
    log = _log(tmp_path)
    log.bind("epoch-a", 0)
    _decision(log, "previous", 0, 4)
    backlog = Backlog(log)
    page = Page(0, 4)
    backlog.enqueue(page, now=0.0)
    backlog.finish(
        page,
        {"brief": "Delivered summary", "mode": "alert", "decision_id": "previous"},
        now=0.0,
        digest_seconds=3600.0,
    )
    log.acknowledge("previous")
    assert backlog.ready() == []
    with log.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM backlog_windows WHERE principal='planner'").one()[0] == 0
    assert log.get("previous") == {
        "decision_id": "previous",
        "since": 0,
        "covered_through": 4,
        "brief": "Delivered summary",
        "coverage": "complete",
    }
    assert log.is_acknowledged("previous")


def test_backlog_held_pages_share_global_digest_deadline(tmp_path) -> None:
    backlog = Backlog(_log(tmp_path))
    pages = [Page(0, 1), Page(1, 2), Page(2, 3)]
    for page in pages:
        backlog.enqueue(page, now=0.0)

    for now, page in zip((10.0, 20.0, 30.0), pages, strict=True):
        backlog.finish(page, {"brief": "", "mode": "digest", "decision_id": "ignored"}, now, 20.0)

    assert backlog.due(29.0, held=True, limit=3) == []
    assert backlog.due(30.0, held=True, limit=3) == [Page(page.since, page.through, "digest") for page in pages]
    assert backlog.due(30.0, limit=3) == []


def test_backlog_refuses_replacement_that_leaves_a_gap(tmp_path) -> None:
    backlog = Backlog(_log(tmp_path))
    originals = [Page(0, 2), Page(2, 4)]
    replacements = [Page(0, 1), Page(2, 4)]
    for page in originals:
        backlog.enqueue(page, now=0.0)

    with pytest.raises(InvalidRequest, match="contiguous"):
        backlog.replace(originals, replacements, now=1.0)
    assert backlog.due(1.0, limit=3) == originals


def test_backlog_due_and_ready_queries_use_their_indexes(tmp_path) -> None:
    log = _log(tmp_path)
    backlog = Backlog(log)
    page = Page(0, 1)
    backlog.enqueue(page, now=0.0)
    backlog.finish(
        page,
        {"brief": "summary", "mode": "alert", "decision_id": "summary"},
        now=0.0,
        digest_seconds=10.0,
    )

    with log.connection() as conn:
        due_plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT since,through,mode,attempts FROM backlog_windows "
            "WHERE principal=? AND status=? AND next_attempt<=? "
            "ORDER BY next_attempt,since LIMIT ?",
            ("planner", "retry", 10.0, 1),
        )
        ready_plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT decision FROM backlog_windows "
            "WHERE principal=? AND status='ready' ORDER BY since,through",
            ("planner",),
        )
        assert any("backlog_due" in row[3] for row in due_plan)
        assert any("backlog_ready" in row[3] for row in ready_plan)


@pytest.mark.parametrize(("since", "through"), [(-1, 0), (0, -1), (3, 2), (True, 1)])
def test_backlog_rejects_invalid_ranges(tmp_path, since: int, through: int) -> None:
    with pytest.raises(InvalidRequest):
        Backlog(_log(tmp_path)).enqueue(Page(since, through), now=0.0)
