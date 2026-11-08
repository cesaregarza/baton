"""The worker's one durable ledger: call reservations, UTC-day budget and every conversation's scheduling record.

Everything lives in one private JSON file replaced atomically under a process-lifetime exclusive lock, so a
reservation and the counters it changes can never split. A second worker finds the lock held and exits
without inference. Source, checkpoint, request, proposal and compact artifacts stay in their own files and
are reconciled by identity after an interruption; the ledger never contains model output or source prose.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from baton.graph.contract import GraphError
from baton.graph.source import load_json
from baton.graph.storage import atomic_json

VERSION = 1
PHASES = ("pending", "extracting", "compacting", "publishing", "idle", "blocked")
RECORD_FIELDS = (
    "conversation_id",
    "target_head",
    "accepted_W",
    "published_W",
    "checkpoint_path",
    "phase",
    "request_id",
    "attempts",
    "not_before",
    "error_category",
)
RECENT_CALLS = 50
RETRY_DELAY = 60.0


class WorkerBusy(GraphError):
    """Another process owns this worker's state directory."""


class Budget(GraphError):
    """A reservation was refused; `category` says whether to defer (`daily_cap`, `cycle_cap`) or block."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.category = category


def utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).date().isoformat()


def next_utc_day(now: float) -> float:
    today = datetime.fromtimestamp(now, UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return (today + timedelta(days=1)).timestamp()


def new_record(conversation_id: str, checkpoint_path: str) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "target_head": 0,
        "accepted_W": 0,
        "published_W": 0,
        "checkpoint_path": checkpoint_path,
        "phase": "idle",
        "request_id": None,
        "attempts": 0,
        "not_before": 0.0,
        "error_category": None,
    }


class Ledger:
    """Open with `Ledger.open`; every mutation saves the whole file at once."""

    def __init__(self, path: Path, state: dict[str, Any]) -> None:
        self.path = path
        self.state = state

    @classmethod
    @contextmanager
    def open(cls, directory: Path) -> Generator[Ledger]:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = os.open(directory / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WorkerBusy("another graph worker owns this state directory") from None
            path = directory / "ledger.json"
            state = load_json(path) if path.exists() else _initial()
            if not isinstance(state, dict) or state.get("version") != VERSION:
                raise GraphError("unsupported graph worker ledger")
            yield cls(path, state)
        finally:
            os.close(lock)

    def save(self) -> None:
        atomic_json(self.path, self.state)

    # Conversation records -----------------------------------------------------------------------------------

    def records(self) -> dict[str, dict[str, Any]]:
        return self.state["conversations"]

    def record(self, conversation_id: str, checkpoint_path: str | None = None) -> dict[str, Any]:
        records = self.records()
        if conversation_id not in records:
            if checkpoint_path is None:
                raise GraphError("unknown worker conversation record")
            records[conversation_id] = new_record(conversation_id, checkpoint_path)
            self.save()
        return records[conversation_id]

    def update(self, conversation_id: str, **fields: Any) -> dict[str, Any]:
        record = self.record(conversation_id)
        unknown = set(fields) - set(RECORD_FIELDS)
        if unknown or ("phase" in fields and fields["phase"] not in PHASES):
            raise GraphError("invalid worker record update")
        record.update(fields)
        self.save()
        return record

    def begin(self, conversation_id: str, request_id: str) -> dict[str, Any]:
        """Point the record at a unit of work; a new identity starts its attempt count at zero."""
        record = self.record(conversation_id)
        if record["request_id"] != request_id:
            return self.update(conversation_id, request_id=request_id, attempts=0, error_category=None)
        return record

    def block(self, conversation_id: str, category: str) -> dict[str, Any]:
        """Terminal until an operator resets the record: new posts, restarts and new days change nothing."""
        return self.update(conversation_id, phase="blocked", error_category=category, not_before=0.0)

    def defer(self, conversation_id: str, until: float, category: str | None = None) -> dict[str, Any]:
        return self.update(conversation_id, not_before=until, error_category=category)

    def reset(self, conversation_id: str) -> dict[str, Any]:
        """Explicit host administration: clear a blocked record so the scheduler may try again."""
        return self.update(
            conversation_id, phase="pending", request_id=None, attempts=0, not_before=0.0, error_category=None
        )

    # Call reservations --------------------------------------------------------------------------------------

    def consume_interrupted(self) -> dict[str, Any] | None:
        """A reservation left by a crash was spent: record it as interrupted and clear it, keeping its count."""
        in_flight = self.state["in_flight"]
        if in_flight is None:
            return None
        self.settle({"outcome": "interrupted", "elapsed": None, "input_bytes": None, "output_bytes": None})
        return in_flight

    def reserve(
        self,
        conversation_id: str,
        phase: str,
        request_digest: str,
        *,
        now: float,
        daily_cap: int,
        cycle_calls: int,
        cycle_cap: int,
        attempt_cap: int,
    ) -> dict[str, Any]:
        """Atomically take one attempt and one daily slot before any external call; refused means no call."""
        if self.state["in_flight"] is not None:
            raise Budget("in_flight", "a call is already reserved")
        record = self.record(conversation_id)
        if record["attempts"] >= attempt_cap:
            raise Budget("attempt_cap", f"{phase} already used its {attempt_cap} calls for this request")
        if cycle_calls >= cycle_cap:
            raise Budget("cycle_cap", "this scheduling cycle already used its model calls")
        day = utc_day(now)
        used = self.state["calls"].get(day, 0)
        if used >= daily_cap:
            raise Budget("daily_cap", "the daily model call cap is spent until the next UTC day")
        self.state["calls"] = {day: used + 1}
        record["attempts"] += 1
        self.state["in_flight"] = {
            "conversation_id": conversation_id,
            "phase": phase,
            "request_digest": request_digest,
            "reserved_at": now,
            "attempt": record["attempts"],
        }
        self.save()
        return self.state["in_flight"]

    def settle(self, outcome: dict[str, Any]) -> None:
        """Record what the reserved call cost and how it ended; model output never enters the ledger."""
        in_flight = self.state["in_flight"]
        if in_flight is None:
            raise GraphError("no call is reserved")
        entry = {**in_flight, **outcome}
        self.state["recent_calls"] = [*self.state["recent_calls"], entry][-RECENT_CALLS:]
        self.state["in_flight"] = None
        self.save()

    # Scheduling ---------------------------------------------------------------------------------------------

    def rotate(self, order: list[str]) -> list[str]:
        """Conversations starting after the last served position; the position advances once per cycle."""
        if not order:
            return []
        start = self.state["next_position"] % len(order)
        self.state["next_position"] = (start + 1) % len(order)
        self.save()
        return order[start:] + order[:start]


def _initial() -> dict[str, Any]:
    return {
        "version": VERSION,
        "calls": {},
        "in_flight": None,
        "next_position": 0,
        "recent_calls": [],
        "conversations": {},
    }
