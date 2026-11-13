"""The worker's one durable ledger: call reservations, UTC-day budget and every conversation's scheduling record.

Everything lives in one private JSON file replaced atomically under a process-lifetime exclusive lock, so a
reservation and the counters it changes, or an accepted watermark and the posts it meters, can never split. A
second worker finds the lock held and exits
without inference. A call settled as a refusal (the provider did no work) is refunded: its attempt returns,
its launch stays counted in `calls` and is also counted in `refused`, and the daily cap compares the
difference, so only launches that may have used the provider spend the day. A meter counts calls, tokens and
accepted posts per UTC day (the most recent `METER_DAYS`) and per conversation. Source, checkpoint, request,
proposal and compact artifacts stay in their own files and are reconciled by identity after an interruption;
the ledger never contains model output or source prose.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from baton.graph.contract import GraphError
from baton.graph.failure import REFUSALS, Category
from baton.graph.source import load_json
from baton.graph.storage import atomic_json

VERSION = 3
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
    "refusals",
)
RECENT_CALLS = 50
METER_DAYS = 30
COUNTERS = ("calls", "input_tokens", "cached_input_tokens", "output_tokens", "posts")
RETRY_DELAY = 60.0
REFUSAL_CEILING = 3600.0


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


def refusal_delay(refusals: int) -> float:
    """The wait after `refusals` consecutive refused calls: one minute, doubling, then hourly at most."""
    return min(RETRY_DELAY * 2 ** min(refusals - 1, 16), REFUSAL_CEILING)


def call_tokens(usage: Any) -> dict[str, int]:
    """Input (cached included), cached input and output tokens from a transport's usage; 0 where none is reported.

    The event-stream client and the chat-completions API count cached input inside their input total; the
    system-prompt client reports cache reads and writes beside its input count, so they are added to it.
    """
    usage = usage if isinstance(usage, dict) else {}
    if "prompt_tokens" in usage:
        prompt, cached = _count(usage, "prompt_tokens"), _count(usage.get("prompt_tokens_details"), "cached_tokens")
        return {
            "input_tokens": prompt,
            "cached_input_tokens": cached,
            "output_tokens": _count(usage, "completion_tokens"),
        }
    read, written = _count(usage, "cache_read_input_tokens"), _count(usage, "cache_creation_input_tokens")
    return {
        "input_tokens": _count(usage, "input_tokens") + read + written,
        "cached_input_tokens": _count(usage, "cached_input_tokens") + read,
        "output_tokens": _count(usage, "output_tokens"),
    }


def _count(fields: Any, key: str) -> int:
    value = fields.get(key) if isinstance(fields, dict) else None
    return value if type(value) is int and value >= 0 else 0


def meter_report(state: dict[str, Any]) -> dict[str, Any]:
    """The meter as `status` shows it: day rows oldest first and per-conversation totals, with tokens per post."""

    def row(counts: dict[str, int], **label: str) -> dict[str, Any]:
        posts = counts["posts"]
        per_post = {
            f"{key}_per_post": counts[key] / posts if posts else None for key in ("input_tokens", "output_tokens")
        }
        return {**label, **counts, **per_post}

    return {
        "days": [row(counts, day=day) for day, counts in sorted(state.get("meter", {}).items())],
        "conversations": {
            cid: row(record.get("meter") or dict.fromkeys(COUNTERS, 0))
            for cid, record in state["conversations"].items()
        },
    }


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
        "refusals": 0,
        "meter": dict.fromkeys(COUNTERS, 0),
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
            while isinstance(state, dict) and state.get("version") in UPGRADES:  # one version forward per step
                state = UPGRADES[state["version"]](state)
                atomic_json(path, state)
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
        _require_record_fields(fields)
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
            conversation_id,
            phase="pending",
            request_id=None,
            attempts=0,
            not_before=0.0,
            error_category=None,
            refusals=0,
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
        """Atomically take one attempt and one daily slot before any external call; refused means no call.

        A `daily_cap` of 0 is the explicit unlimited setting: the launch is still counted, never refused for the day.
        """
        if self.state["in_flight"] is not None:
            raise Budget("in_flight", "a call is already reserved")
        record = self.record(conversation_id)
        if record["attempts"] >= attempt_cap:
            raise Budget("attempt_cap", f"{phase} already used its {attempt_cap} calls for this request")
        if cycle_calls >= cycle_cap:
            raise Budget("cycle_cap", "this scheduling cycle already used its model calls")
        day = utc_day(now)
        launched, refused = self.state["calls"].get(day, 0), self.state["refused"].get(day, 0)
        if daily_cap and launched - refused >= daily_cap:
            raise Budget("daily_cap", "the daily model call cap is spent until the next UTC day")
        self.state["calls"] = {day: launched + 1}
        self.state["refused"] = {day: refused} if refused else {}
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
        """Record what the reserved call cost and how it ended; model output never enters the ledger.

        A refusal refunds the attempt and counts the launch as refused; any other ending except a cancellation
        clears the conversation's consecutive-refusal count.
        """
        in_flight = self.state["in_flight"]
        if in_flight is None:
            raise GraphError("no call is reserved")
        record = self.records().get(in_flight["conversation_id"])
        if record is not None and outcome["outcome"] in REFUSALS:
            record["attempts"] = in_flight["attempt"] - 1
            record["refusals"] += 1
            day = utc_day(in_flight["reserved_at"])
            self.state["refused"] = {day: self.state["refused"].get(day, 0) + 1}
        elif record is not None and outcome["outcome"] != Category.CANCELLED:
            record["refusals"] = 0
        if outcome["outcome"] not in REFUSALS:
            day = utc_day(in_flight["reserved_at"])
            self._meter(in_flight["conversation_id"], day, calls=1, **call_tokens(outcome.get("usage")))
        entry = {**in_flight, **outcome}
        self.state["recent_calls"] = [*self.state["recent_calls"], entry][-RECENT_CALLS:]
        self.state["in_flight"] = None
        self.save()

    def accept(self, conversation_id: str, watermark: int, now: float, **fields: Any) -> dict[str, Any]:
        """Move `accepted_W` to `watermark` and count the posts that adds on the UTC day of `now`, in one save.

        The count is the step from the saved watermark, so an acceptance repeated after an interruption adds none.
        """
        _require_record_fields(fields)
        added = watermark - self.record(conversation_id)["accepted_W"]
        if added > 0:
            self._meter(conversation_id, utc_day(now), posts=added)
        return self.update(conversation_id, accepted_W=watermark, **fields)

    def _meter(self, conversation_id: str, day: str, **counts: int) -> None:
        """Add to one day's counters and the conversation's totals; days before the newest `METER_DAYS` are dropped."""
        days, record = self.state["meter"], self.records().get(conversation_id)
        targets = [days.setdefault(day, dict.fromkeys(COUNTERS, 0))] + ([record["meter"]] if record else [])
        for counters in targets:
            for key, value in counts.items():
                counters[key] += value
        oldest = (date.fromisoformat(max(days)) - timedelta(days=METER_DAYS - 1)).isoformat()
        self.state["meter"] = {key: days[key] for key in sorted(days) if key >= oldest}

    # Scheduling ---------------------------------------------------------------------------------------------

    def rotate(self, order: list[str]) -> list[str]:
        """Conversations starting after the last served position; the position advances once per cycle."""
        if not order:
            return []
        start = self.state["next_position"] % len(order)
        self.state["next_position"] = (start + 1) % len(order)
        self.save()
        return order[start:] + order[:start]


def _require_record_fields(fields: dict[str, Any]) -> None:
    """Refuse an update naming a field a record doesn't have or a phase the scheduler doesn't know."""
    if set(fields) - set(RECORD_FIELDS) or ("phase" in fields and fields["phase"] not in PHASES):
        raise GraphError("invalid worker record update")


def _initial() -> dict[str, Any]:
    return {
        "version": VERSION,
        "calls": {},
        "refused": {},
        "in_flight": None,
        "next_position": 0,
        "recent_calls": [],
        "conversations": {},
        "meter": {},
    }


def _upgrade_from_1(state: dict[str, Any]) -> dict[str, Any]:
    """Version 2 adds the refusal counters, starting at zero; nothing else changes."""
    for record in state.get("conversations", {}).values():
        record["refusals"] = 0
    return {**state, "version": 2, "refused": {}}


def _upgrade_from_2(state: dict[str, Any]) -> dict[str, Any]:
    """Version 3 adds the meter, empty for every day and conversation; nothing else changes."""
    for record in state.get("conversations", {}).values():
        record["meter"] = dict.fromkeys(COUNTERS, 0)
    return {**state, "version": 3, "meter": {}}


UPGRADES = {1: _upgrade_from_1, 2: _upgrade_from_2}
