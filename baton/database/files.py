"""Open private SQLite files through the same checked filesystem boundary."""

from __future__ import annotations

import os
from pathlib import Path

from baton import datadir
from baton.database import Connection, connect_sqlite


def open_private(path: Path, *, read_only: bool = False) -> Connection:
    """Open a private database and verify that SQLite opened the checked path."""
    directory = datadir.canonical(path.parent)
    path = directory / path.name
    with datadir.opened(directory) as dir_fd:
        flags = os.O_RDONLY if read_only else os.O_RDWR | os.O_CREAT
        fd = datadir.open_file(dir_fd, path.name, flags, directory=directory)
        try:
            checked_file = os.fstat(fd)
        finally:
            os.close(fd)
        checked_dir = os.fstat(dir_fd)
        conn = connect_sqlite(path, read_only=read_only)
        if not (datadir.same_file(directory, checked_dir) and datadir.same_file(path, checked_file)):
            conn.close()
            raise datadir.UnsafeDataDir(path, "changed while it was being opened", "find what is changing it")
    return conn
