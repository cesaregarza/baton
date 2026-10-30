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

from baton import datadir
from baton.datadir import UnsafeDataDir

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
