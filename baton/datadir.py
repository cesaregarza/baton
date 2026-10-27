"""The data directory: created private, and refused when it isn't.

Everything that touches `data_dir` goes through here: the CLI and `serve` check
it at startup, the store before opening the database, the OAuth token store and
the audit writer on every access. Nothing found on disk is ever changed. An
unsafe directory or file is reported with its path and what is wrong, and the
operator fixes it.

The rules:

* `data_dir` is one canonical path, used to check, create and open it. A
  '..' component is refused, and so is a symlink anywhere in the path.
* The path is opened one component at a time from `/`, without following
  links, and each directory is checked on the descriptor actually opened:
  every ancestor is owned by root or the current user and is not group- or
  other-writable unless it has the sticky bit (as /tmp does).
* `data_dir` itself is a real directory, not a symlink, owned by the current
  user, with no group or other permission bits. A missing one is created 0700,
  along with any missing parents, each inside its already-checked parent.
* Every entry in `data_dir` is a regular file, not a symlink, owned by the
  current user, with no group or other bits.

Files are opened relative to a descriptor for the checked directory with
`O_NOFOLLOW` and checked again with `fstat`, so a link swapped in after the check
is refused rather than followed. SQLite opens its files by path. For it, the
store checks after opening that the path still names the checked directory and
file (device and inode). What remains: swapping the file and swapping it back
between SQLite's open and that check, which needs write access to the 0700
directory, so the same user or root.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from baton.config import ConfigError

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafeDataDir(ConfigError):
    code = "unsafe_data_dir"

    def __init__(self, path: Path | str, problem: str, fix: str) -> None:
        super().__init__(f"unsafe data_dir: {path} {problem}; {fix}. baton changes nothing it finds on disk.")


def canonical(path: Path | str) -> Path:
    """The one absolute path used to check, create and open the directory. A '..' component is refused.

    A '..' after a symlink means something different to the kernel than to a
    lexical clean-up, so baton never interprets one.
    """
    raw = Path(path)
    if ".." in raw.parts:
        raise UnsafeDataDir(raw, "contains a '..' component", "write data_dir as a plain path without '..'")
    return Path(os.path.normpath(raw if raw.is_absolute() else Path.cwd() / raw))


@contextmanager
def opened(path: Path) -> Iterator[int]:
    """A descriptor for `path` after creating or checking it and its parents, and checking every entry in it."""
    path = canonical(path)
    fd = _walk(path)
    try:
        info = os.fstat(fd)
        _check_private(info, path, want=stat.S_ISDIR, kind="directory", fix=f"chmod 700 {path}")
        # The path must still name the directory that was checked, for anything that opens by path.
        if not same_file(path, info):
            raise UnsafeDataDir(path, "changed while it was being checked", "find what is changing it, then retry")
        with os.scandir(fd) as entries:
            for entry in entries:
                _check_private(entry.stat(follow_symlinks=False), path / entry.name, want=stat.S_ISREG,
                               kind="regular file", fix="remove it or make it a private file (chmod 600)")
        yield fd
    finally:
        os.close(fd)


def same_file(path: Path, info: os.stat_result) -> bool:
    """Whether `path` names the same file (device and inode) as an already-checked descriptor's stat."""
    try:
        current = os.stat(path)
    except FileNotFoundError:
        return False
    return (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino)


def check(path: Path) -> None:
    with opened(path):
        pass


def open_file(dir_fd: int, name: str, flags: int, *, directory: Path) -> int:
    """Open `name` inside a checked directory without following links; new files are 0600."""
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    except OSError as error:
        if error.errno == errno.ELOOP:  # O_NOFOLLOW met a symlink
            raise UnsafeDataDir(directory / name, "is a symlink", "remove it") from None
        raise
    try:
        _check_private(os.fstat(fd), directory / name, want=stat.S_ISREG, kind="regular file",
                       fix=f"chmod 600 {directory / name}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def replace_file(dir_fd: int, name: str, data: bytes) -> os.stat_result:
    """Atomically replace `name` in a checked directory; return the new file's stat.

    The data goes to a unique, exclusively created 0600 temp file in the same
    directory, which is fsynced and renamed over `name`; then the directory is
    fsynced. A fixed temp name is never used.
    """
    temp = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        os.unlink(temp, dir_fd=dir_fd)
        raise
    os.fsync(dir_fd)
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def read_file(dir_fd: int, name: str, *, directory: Path) -> tuple[os.stat_result, bytes] | None:
    """A private file's stat and contents, or None if it doesn't exist."""
    try:
        fd = open_file(dir_fd, name, os.O_RDONLY, directory=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        return os.fstat(handle.fileno()), handle.read()


def _check_private(info: os.stat_result, path: Path, *, want, kind: str, fix: str) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise UnsafeDataDir(path, "is a symlink", "remove it")
    if not want(info.st_mode):
        raise UnsafeDataDir(path, f"is not a {kind}", "remove it")
    if info.st_uid != os.geteuid():
        raise UnsafeDataDir(path, f"is owned by uid {info.st_uid}, not the current user", "run baton as its owner")
    if info.st_mode & 0o077:
        raise UnsafeDataDir(path, f"has group or other permissions ({stat.S_IMODE(info.st_mode):04o})", fix)


def _walk(path: Path) -> int:
    """Open `path` one component at a time from `/`, never following a link, checking each descriptor opened.

    Every ancestor must be owned by root or the current user and not group- or
    other-writable unless sticky. A missing component is created 0700 inside
    the already-checked parent, then opened and checked like any other.
    """
    fd = os.open("/", _DIR_FLAGS)
    current = Path("/")
    try:
        _check_ancestor(os.fstat(fd), current)
        names = path.parts[1:]
        for index, name in enumerate(names):
            current = current / name
            child = _open_or_create(fd, name, current)
            os.close(fd)
            fd = child
            if index < len(names) - 1:
                _check_ancestor(os.fstat(fd), current)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_or_create(dir_fd: int, name: str, path: Path) -> int:
    try:
        return _open_dir(dir_fd, name, path)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o700, dir_fd=dir_fd)
        except FileExistsError:
            pass  # something appeared meanwhile; it is opened and checked like anything else
        return _open_dir(dir_fd, name, path)


def _open_dir(dir_fd: int, name: str, path: Path) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise UnsafeDataDir(path, "is a symlink", "use the real path: data_dir and its parents must not be "
                                "symlinks") from None
        raise UnsafeDataDir(path, "is not a directory", "choose another data_dir") from None


def _check_ancestor(info: os.stat_result, path: Path) -> None:
    if info.st_uid not in (0, os.geteuid()):
        raise UnsafeDataDir(path, f"(a parent) is owned by uid {info.st_uid}",
                            "put data_dir under directories owned by root or you")
    if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
        raise UnsafeDataDir(path, f"(a parent) is group- or other-writable ({stat.S_IMODE(info.st_mode):04o}) "
                            "without the sticky bit", "choose a data_dir whose parents only you or root can write")
