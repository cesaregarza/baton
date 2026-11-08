"""Locked local snapshots with optimistic proposals and atomic replacement."""

import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from baton.graph.contract import VERSION, require
from baton.graph.source import canonical, load_json, prefix_digest


def distinct_paths(**paths):
    resolved = {key: Path(value).resolve() for key, value in paths.items() if value is not None}
    if "state" in resolved:
        resolved["state lock"] = Path(str(resolved["state"]) + ".lock")
    values = list(resolved.values())
    require(len(set(values)) == len(values), "source/state/request/proposal/lock paths must be distinct")
    for i, path in enumerate(values):
        for other in values[:i]:
            require(
                not (path.exists() and other.exists() and path.samefile(other)), "artifacts cannot alias each other"
            )


def atomic_json(path, value, *, replace=True):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def state_lock(path):
    lock = Path(str(Path(path).resolve()) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def initial_state(source):
    return {
        "version": VERSION,
        "conversation_id": source["conversation_id"],
        "revision": 0,
        "watermark": 0,
        "source_prefix_digest": prefix_digest(source, 0),
        "statements": {},
        "strands": {},
        "decisions": {},
        "edges": [],
        "dispositions": [],
        "receipts": {},
    }


def load_state(path, source):
    state = load_json(path) if Path(path).exists() else initial_state(source)
    require(isinstance(state, dict) and state.get("version") == VERSION, "unsupported graph state")
    require(state.get("conversation_id") == source["conversation_id"], "state conversation mismatch")
    through = state.get("watermark")
    require(type(through) is int and 0 <= through <= len(source["messages"]), "source is shorter than committed prefix")
    require(state.get("source_prefix_digest") == prefix_digest(source, through), "committed source prefix was edited")
    return state
