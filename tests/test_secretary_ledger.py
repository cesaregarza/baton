"""Persistence invariants for secretary activation, delivery, and scan backlog."""

from __future__ import annotations

import json
import sqlite3

import pytest

from baton.agents.secretary.attention.backlog import Backlog
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


def test_recognized_v1_model_state_migrates_additively(tmp_path) -> None:
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
    assert log.positions() == (3, 3)
    assert log.get("saved")["brief"] == "kept"
    with log.connection() as conn:
        assert conn.execute("SELECT version FROM attention_version").one()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM acknowledgments").one()[0] == 0


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
    backlog.record(7, 8, decision=None, now=100.0)

    restarted = Backlog(log)
    assert restarted.scanned() == 8
    assert restarted.ready() == []
    assert restarted.due(3699.0) is None
    assert restarted.due(3700.0) == (7, 8)
    restarted.record(7, 8, decision="summary", now=4000.0)
    restarted.record(7, 8, decision=None, now=4500.0)
    assert Backlog(log).ready() == ["summary"]
    assert Backlog(log).due(10000.0) is None
    assert restarted.scanned() == 8


def test_backlog_does_not_replay_acknowledged_history_or_invalidate_its_receipts(tmp_path) -> None:
    log = _log(tmp_path)
    log.bind("epoch-a", 0)
    _decision(log, "previous", 0, 4)
    log.acknowledge("previous")
    backlog = Backlog(log)
    assert backlog.scanned() == 4
    backlog.record(0, 4, decision="previous", now=0)
    backlog.invalidate("previous", now=1)
    assert backlog.ready() == []
    assert backlog.due(4000) is None


@pytest.mark.parametrize(("since", "through"), [(-1, 0), (0, -1), (3, 2), (True, 1)])
def test_backlog_rejects_invalid_ranges(tmp_path, since: int, through: int) -> None:
    with pytest.raises(InvalidRequest):
        Backlog(_log(tmp_path)).record(since, through, decision=None, now=0.0)
