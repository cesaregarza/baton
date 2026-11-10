"""A stand-in for the host's gate module, offering only the names the thread bridge declares it needs."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

GATE_DIR = Path(__file__).resolve().parent.parent / "gate"
POLICY_FILE = "policy-v3.json"


def _private_dir(path: Path, *, create: bool = True) -> None:
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError(f"{path.name} is not a private directory")


def _open_private(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError(f"{path.name} is not a private regular file")
    return fd


def _read_private(path: Path):
    try:
        fd = _open_private(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as source:
        return json.loads(source.read())


def _write_private(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".gate-", dir=path.parent)
    with os.fdopen(fd, "wb") as destination:
        os.fchmod(destination.fileno(), 0o600)
        destination.write((json.dumps(value, sort_keys=True) + "\n").encode())
    os.replace(temporary, path)
