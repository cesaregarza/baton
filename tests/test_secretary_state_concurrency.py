"""State schema initialization remains safe across simultaneous openers."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from baton.agents.secretary.attention.state import AttentionLog
from baton.config import ConfigError


def _private_log(path: Path, data_dir: Path) -> AttentionLog:
    log = AttentionLog(path, data_dir, "planner")
    if path.exists():
        path.chmod(0o600)
    return log


def _simultaneous(count: int, function):
    barrier = Barrier(count)

    def run(index: int):
        barrier.wait(timeout=10)
        return function(index)

    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = [executor.submit(run, index) for index in range(count)]
        return [future.result(timeout=20) for future in futures]


def test_simultaneous_fresh_openers_initialize_once(tmp_path) -> None:
    path = tmp_path / "private.sqlite"
    path.touch(mode=0o600)
    path.chmod(0o600)
    data_dir = tmp_path / "data"

    def open_state(_index: int) -> tuple[int, int]:
        return _private_log(path, data_dir).positions()

    assert _simultaneous(8, open_state) == [(0, 0)] * 8
    path.chmod(0o600)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM attention_version").fetchone() == (2,)
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='acknowledgments_coverage'"
        ).fetchone() == ("acknowledgments_coverage",)


def test_simultaneous_v1_refusals_leave_private_state_unchanged(tmp_path) -> None:
    path = tmp_path / "private.sqlite"
    with sqlite3.connect(path) as conn:
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
    path.chmod(0o600)
    data_dir = tmp_path / "data"

    def open_state(_index: int) -> str:
        with pytest.raises(ConfigError, match="version 1.*new private state file"):
            _private_log(path, data_dir).positions()
        return "refused"

    assert _simultaneous(8, open_state) == ["refused"] * 8
    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as conn:
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
