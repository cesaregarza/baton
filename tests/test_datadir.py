"""The data directory is private, and baton refuses to use one that isn't.

Every case runs in a temporary directory. A refusal must leave everything it
found unchanged: no database, no state file, no write through any link.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest
from conftest import make_config
from test_integration import client, payload

from baton import auth, cli, messages
from baton.app import create_app
from baton.config import ConfigError
from baton.store import connect


@pytest.fixture
def umask_022() -> Iterator[None]:
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def run_cli(host, capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(["--config", str(host.config_path), *argv])
    return code, json.loads(capsys.readouterr().out)


def make(tmp_path: Path, data_dir: Path | None = None):
    host = make_config(tmp_path, data_dir=data_dir)
    host.config_path = tmp_path / "baton.toml"  # type: ignore[attr-defined]
    return host


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def assert_refused(host, capsys, *, what: str) -> None:
    code, out = run_cli(host, capsys, "read", "--as", "worker")
    assert (code, out.get("error")) == (2, "unsafe_data_dir"), out
    assert what in out["detail"]


def test_an_existing_group_or_world_readable_data_dir_is_refused(tmp_path: Path, capsys) -> None:
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o755)
    host = make(tmp_path)
    assert_refused(host, capsys, what=str(data))
    assert list(data.iterdir()) == []
    assert mode(data) == 0o755  # reported, never changed
    with pytest.raises(ConfigError, match="unsafe"):
        create_app(host.config)
    assert list(data.iterdir()) == []


def test_a_symlinked_data_dir_is_refused(tmp_path: Path, capsys) -> None:
    real = private_dir(tmp_path / "real")
    (tmp_path / "data").symlink_to(real)
    host = make(tmp_path)
    assert_refused(host, capsys, what="symlink")
    assert list(real.iterdir()) == []


def test_a_group_writable_parent_is_refused(tmp_path: Path, capsys) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o775)
    host = make(tmp_path, data_dir=shared / "data")
    assert_refused(host, capsys, what=str(shared))
    assert list(shared.iterdir()) == []


@pytest.mark.parametrize("name", ["baton.db", "audit.jsonl"])
def test_a_symlinked_file_in_the_data_dir_is_refused(tmp_path: Path, capsys, name: str) -> None:
    data = private_dir(tmp_path / "data")
    target = tmp_path / "target"
    target.write_bytes(b"keep")
    (data / name).symlink_to(target)
    host = make(tmp_path)
    assert_refused(host, capsys, what=name)
    assert target.read_bytes() == b"keep"
    assert sorted(path.name for path in data.iterdir()) == [name]


def test_a_symlinked_audit_log_is_not_written_through_by_the_server(tmp_path: Path, start_host) -> None:
    host = start_host()
    target = tmp_path / "elsewhere.log"
    target.write_bytes(b"keep")
    host.config.audit_path.unlink(missing_ok=True)
    host.config.audit_path.symlink_to(target)

    async def call() -> bool:
        async with client(host, "worker") as worker:
            return (await worker.call_tool("whoami", {})).is_error

    assert anyio.run(call) is True
    assert target.read_bytes() == b"keep"


@pytest.mark.parametrize("kind", ["stale", "symlink"])
def test_a_leftover_oauth_temp_file_is_refused_and_left_alone(tmp_path: Path, kind: str) -> None:
    data = private_dir(tmp_path / "data")
    target = tmp_path / "target"
    target.write_bytes(b"keep")
    leftover = data / "oauth.tmp"
    if kind == "stale":
        leftover.write_bytes(b"stale")
        leftover.chmod(0o644)
    else:
        leftover.symlink_to(target)
    host = make(tmp_path)
    with pytest.raises(ConfigError, match="oauth.tmp"):
        auth.revoke_all(host.config.oauth_state_path)
    assert not host.config.oauth_state_path.exists()
    assert target.read_bytes() == b"keep"
    if kind == "stale":
        assert (leftover.read_bytes(), mode(leftover)) == (b"stale", 0o644)


def test_every_new_file_is_private_under_a_permissive_umask(tmp_path: Path, start_host, umask_022) -> None:
    host = start_host(oauth=True)
    conn = connect(host.config.db_path)
    messages.send(conn, sender="planner", recipient="worker", text="hello")
    conn.close()
    auth.revoke_all(host.config.oauth_state_path)

    async def call() -> None:
        async with client(host, "worker") as worker:
            payload(await worker.call_tool("read_messages", {}))

    anyio.run(call)
    data = host.config.data_dir
    assert mode(data) == 0o700
    names = {path.name: mode(path) for path in data.iterdir()}
    assert {"baton.db", "audit.jsonl", "oauth.json"} <= names.keys()
    assert names == {name: 0o600 for name in names}


def test_a_fresh_data_dir_works_end_to_end(tmp_path: Path, capsys) -> None:
    host = make(tmp_path, data_dir=tmp_path / "new" / "nested" / "data")
    assert run_cli(host, capsys, "message", "--as", "planner", "--to", "worker", "--text", "hi")[0] == 0
    code, page = run_cli(host, capsys, "read", "--as", "worker")
    assert (code, [m["text"] for m in page["messages"]]) == (0, ["hi"])
    for directory in (tmp_path / "new", tmp_path / "new" / "nested", tmp_path / "new" / "nested" / "data"):
        assert mode(directory) == 0o700


def test_a_sticky_world_writable_parent_is_allowed(tmp_path: Path, capsys) -> None:
    public = tmp_path / "public"
    public.mkdir()
    public.chmod(0o1777)
    host = make(tmp_path, data_dir=public / "data")
    assert run_cli(host, capsys, "message", "--as", "planner", "--to", "worker", "--text", "hi")[0] == 0
    assert mode(public / "data") == 0o700
