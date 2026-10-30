"""The data directory check and files that vanish while it runs.

`datadir.opened()` lists the directory and checks every entry is a private file, before any caller's own
lock. Other writers create files that exist only briefly: `replace_file` writes a unique temp file and
renames it over its target, and SQLite removes its `-wal` and `-shm` files when the last connection closes.
A file that is listed and gone by the time it is checked can't be unsafe. These tests time the vanishing
exactly, on real files, through a hook on the `os.scandir` the check calls.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from baton import config as config_module
from baton import conversations, datadir
from baton.datadir import UnsafeDataDir
from baton.integrations import decision_push
from baton.store import connect

REAL_SCANDIR = os.scandir


def vanish_after_listing(monkeypatch: pytest.MonkeyPatch, action: Callable[[], None]) -> None:
    """Make the next directory listings run `action` after listing and before any entry is checked."""

    def scandir(fd: Any) -> Any:
        with REAL_SCANDIR(fd) as listing:
            entries = list(listing)
        action()

        class Listing:
            def __enter__(self) -> Any:
                return iter(entries)

            def __exit__(self, *exc: Any) -> bool:
                return False

        return Listing()

    monkeypatch.setattr(datadir.os, "scandir", scandir)


def private_file(path: Path, mode: int = 0o600) -> None:
    path.write_bytes(b"")
    path.chmod(mode)


def test_a_replace_file_temp_renamed_mid_check_is_not_an_error(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "data"
    datadir.check(directory)
    temp = directory / ".state.json.0123456789abcdef.tmp"
    private_file(temp)
    vanish_after_listing(monkeypatch, lambda: os.replace(temp, directory / "state.json"))
    datadir.check(directory)  # must not raise


def test_a_sqlite_side_file_removed_mid_check_is_not_an_error(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "data"
    datadir.check(directory)
    wal = directory / "baton.db-wal"
    private_file(wal)
    vanish_after_listing(monkeypatch, lambda: wal.unlink())
    datadir.check(directory)  # must not raise


def test_a_file_that_stays_is_still_checked_while_another_vanishes(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "data"
    datadir.check(directory)
    temp = directory / ".state.json.fedcba9876543210.tmp"
    private_file(temp)
    private_file(directory / "readable-by-others", mode=0o644)
    vanish_after_listing(monkeypatch, lambda: temp.unlink())
    with pytest.raises(UnsafeDataDir, match="readable-by-others"):
        datadir.check(directory)


def push_setup(tmp_path: Path, transport: Callable[..., int]) -> tuple[Any, Any, str]:
    config = config_module.parse({
        "host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")},
        "roles": {"orchestrator": {"can_create_conversations": True}, "reviewer": {}, "assistant": {}},
        "conversations": {"enabled": True},
        "integrations": {"decision_push": {"enabled": True, "role": "assistant",
                                           "url": "https://push.example.test/d"}},
    })
    push = decision_push.load(config, config.integrations["decision_push"]).with_transport(transport)
    push.prepare()
    db = connect(config.db_path)
    cid = conversations.create(db, config, actor="orchestrator", title="t",
                               participants=["orchestrator", "reviewer", "assistant"])["conversation_id"]
    conversations.send(db, config, sender="orchestrator", conversation_id=cid, text="decide", needs_decision=True)
    db.close()
    return config, push, cid


def test_a_failed_push_is_counted_even_when_another_writer_is_mid_rename(tmp_path, monkeypatch) -> None:
    """The CI flake on 3.12: the push was attempted and failed, but its count was lost to this race."""
    temp_name = ".decision_push.json.feedface00000000.tmp"
    holder: dict[str, Path] = {}

    def refused(url: str, headers: dict[str, str], body: bytes) -> int:
        # Another writer's temp file is in flight while this pass is between its two state writes.
        private_file(holder["dir"] / temp_name)
        vanish_after_listing(monkeypatch, lambda: os.replace(holder["dir"] / temp_name,
                                                             holder["dir"] / "other-state.json"))
        raise ConnectionRefusedError

    config, push, _ = push_setup(tmp_path, refused)
    holder["dir"] = config.data_dir
    push.poll_once()  # must not raise
    monkeypatch.setattr(datadir.os, "scandir", REAL_SCANDIR)
    assert push.counts() == {"sent": 0, "failed": 1, "capped": 0}


def test_reading_the_push_counts_writes_nothing(tmp_path) -> None:
    config, push, _ = push_setup(tmp_path, lambda url, headers, body: 200)
    push.poll_once()
    state = config.data_dir / "decision_push.json"
    before = (state.stat().st_ino, state.stat().st_mtime_ns, state.read_bytes())
    entries_before = sorted(p.name for p in config.data_dir.iterdir())
    assert push.counts() == {"sent": 1, "failed": 0, "capped": 0}
    assert (state.stat().st_ino, state.stat().st_mtime_ns, state.read_bytes()) == before
    assert sorted(p.name for p in config.data_dir.iterdir()) == entries_before
