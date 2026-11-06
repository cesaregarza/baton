"""Open private SQLite files through the same checked filesystem boundary."""

from __future__ import annotations

import os
from pathlib import Path
from threading import Lock

from baton import datadir

from .driver import Connection, connect_sqlite

_OPEN_LOCK = Lock()


def open_private(path: Path, *, read_only: bool = False) -> Connection:
    """Open a private database and verify that SQLite opened the checked path."""
    directory = datadir.canonical(path.parent)
    path = directory / path.name
    # Serialize first creation with other opens in this process. Only a newly created file
    # needs a raw descriptor, and it is closed before any of our SQLite connections opens it.
    with _OPEN_LOCK, datadir.opened(directory) as dir_fd:
        checked_file = _checked_file(dir_fd, path, read_only=read_only)
        checked_dir = os.fstat(dir_fd)
        conn = connect_sqlite(path, read_only=read_only)
        if not (datadir.same_file(directory, checked_dir) and datadir.same_file(path, checked_file)):
            conn.close()
            raise datadir.UnsafeDataDir(path, "changed while it was being opened", "find what is changing it")
    return conn


def _checked_file(dir_fd: int, path: Path, *, read_only: bool) -> os.stat_result:
    try:
        return datadir.stat_file(dir_fd, path.name, directory=path.parent)
    except FileNotFoundError:
        if read_only:
            raise
    try:
        fd = datadir.open_file(dir_fd, path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL, directory=path.parent)
    except FileExistsError:
        # Another process created it; inspect the existing inode without disturbing SQLite locks.
        return datadir.stat_file(dir_fd, path.name, directory=path.parent)
    try:
        return os.fstat(fd)
    finally:
        os.close(fd)
