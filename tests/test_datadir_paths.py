"""Paths the data-directory checks must not be fooled by, and databases from an older schema.

Each case is set up in a temporary directory. The race is simulated by wrapping
os.mkdir in the test, so production code has no test seam.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import make_config

from baton import cli


@pytest.fixture
def umask_077() -> Iterator[None]:
    previous = os.umask(0o077)
    yield
    os.umask(previous)


def run_cli(tmp_path: Path, capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(["--config", str(tmp_path / "baton.toml"), *argv])
    return code, json.loads(capsys.readouterr().out)


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def test_a_dot_dot_after_a_symlink_cannot_redirect_the_database(tmp_path: Path, capsys, umask_077) -> None:
    inside = private_dir(tmp_path / "inside")
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    outside.chmod(0o755)
    (inside / "link").symlink_to(outside / "deep")
    # The kernel resolves inside/link/../data to outside/data, whatever a lexical check makes of it.
    permissive = outside / "data"
    permissive.mkdir()
    permissive.chmod(0o755)
    database = permissive / "baton.db"
    existing = sqlite3.connect(database)
    existing.execute("CREATE TABLE someone_elses (x INTEGER)")
    existing.close()
    database.chmod(0o644)
    before = database.read_bytes()
    make_config(tmp_path, data_dir=inside / "link" / ".." / "data")

    code, out = run_cli(tmp_path, capsys, "read", "--as", "worker")

    assert (code, out.get("error")) == (2, "unsafe_data_dir"), out
    assert ".." in out["detail"]
    assert database.read_bytes() == before
    assert stat.S_IMODE(database.stat().st_mode) == 0o644
    assert sorted(p.name for p in permissive.iterdir()) == ["baton.db"]


def test_a_parent_swapped_for_a_writable_one_mid_check_gets_nothing_created_in_it(tmp_path: Path, capsys,
                                                                                  monkeypatch) -> None:
    parent = private_dir(tmp_path / "parent")
    make_config(tmp_path, data_dir=parent / "data")
    real_mkdir = os.mkdir
    swapped = []

    def mkdir_after_a_swap(path, mode=0o777, *, dir_fd=None):
        # Just before baton creates `data`, replace its checked parent with a world-writable directory.
        if Path(path).name == "data" and not swapped:
            os.rename(parent, tmp_path / "parent-checked")
            real_mkdir(parent, 0o777)
            os.chmod(parent, 0o777)
            swapped.append(True)
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", mkdir_after_a_swap)
    first, out = run_cli(tmp_path, capsys, "read", "--as", "worker")
    monkeypatch.setattr(os, "mkdir", real_mkdir)

    assert swapped
    assert list(parent.iterdir()) == []  # nothing was created in the unchecked, writable directory
    assert (first, out.get("error")) == (2, "unsafe_data_dir"), out
    again, out = run_cli(tmp_path, capsys, "read", "--as", "worker")
    assert (again, out.get("error")) == (2, "unsafe_data_dir"), out


def test_a_database_from_an_older_schema_is_refused_not_used(tmp_path: Path, capsys, umask_077) -> None:
    data = private_dir(tmp_path / "data")
    make_config(tmp_path, data_dir=data)
    old = sqlite3.connect(data / "baton.db")
    old.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (1);
        CREATE TABLE jobs (id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, request_digest TEXT NOT NULL,
            title TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL, waiting_on TEXT,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    """)
    old.close()
    before = (data / "baton.db").read_bytes()

    code, out = run_cli(tmp_path, capsys, "job", "list")

    assert code == 2, out
    assert "schema" in out.get("detail", "") and "1" in out["detail"]
    assert (data / "baton.db").read_bytes() == before


def test_a_database_swapped_just_before_sqlite_opens_it_is_refused(tmp_path: Path, capsys, monkeypatch) -> None:
    data = private_dir(tmp_path / "data")
    make_config(tmp_path, data_dir=data)
    assert run_cli(tmp_path, capsys, "job", "list")[0] == 0
    real_connect = sqlite3.connect
    swapped = []

    def connect_after_a_swap(path, *args, **kwargs):
        # Between baton's check and SQLite's open by path, put a different file at the database's path.
        if not swapped:
            os.rename(data / "baton.db", data / "checked.db")
            real_connect(data / "baton.db").close()
            swapped.append(True)
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect_after_a_swap)
    code, out = run_cli(tmp_path, capsys, "job", "list")
    assert swapped
    refusal = out if isinstance(out, dict) else {}
    assert (code, refusal.get("error")) == (2, "unsafe_data_dir"), out
    assert "changed while it was being opened" in refusal["detail"]


def test_one_walk_refuses_a_path_that_no_longer_names_the_checked_directory(tmp_path: Path, monkeypatch) -> None:
    from baton import datadir
    from baton.config import ConfigError

    parent = private_dir(tmp_path / "parent")
    real_mkdir = os.mkdir

    def mkdir_after_a_swap(path, mode=0o777, *, dir_fd=None):
        if Path(path).name == "data":
            os.rename(parent, tmp_path / "parent-checked")
            real_mkdir(parent, 0o700)
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", mkdir_after_a_swap)
    with pytest.raises(ConfigError, match="unsafe data_dir"):
        datadir.check(parent / "data")
    assert list(parent.iterdir()) == []
