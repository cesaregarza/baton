"""The principal's secretary: model-driven triage, saved assessments, and delivery acknowledgment."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from baton.config import Config
from baton.config.secretary import Policy, load_policy
from baton.errors import InvalidRequest

from .attention.runtime import DatabaseWorker
from .attention.state import AttentionLog
from .inbox.references import ReferenceWindow
from .inbox.window import InboxTooLarge, InboxWindow


@dataclass(frozen=True)
class Secretary:
    config: Config
    state_path: Path
    policy_path: Path

    async def activate(self) -> tuple[int, int]:
        """Start a new secretary at today's inbox head; preserve an existing activation."""
        async with DatabaseWorker() as database:
            return await database.call(activate, self)

    async def decide(
        self,
        *,
        budget_seconds: float,
        since: int | None = None,
        floor: int | None = None,
        trigger: str | None = None,
        hourly: bool = False,
        clock: Callable[[], float] = time.monotonic,
        through: int | None = None,
    ) -> dict:
        from .attention.decide import DecisionRun

        run = DecisionRun(self, budget_seconds, since, floor, trigger, hourly, clock, through)
        run._check()
        if since is not None:
            return await run.run()
        from .attention.decide import SecretaryUnavailable
        from .attention.queue import AttentionQueue

        async with DatabaseWorker() as database:
            queue = await AttentionQueue.open(self, database)
            result = await queue.digest(budget_seconds) if hourly else await queue.scan(budget_seconds, trigger=trigger)
            if result is not None:
                return result
            if queue.last_error is not None:
                raise queue.last_error
            raise SecretaryUnavailable("no digest is due; held pages remain scheduled")

    async def acknowledge(self, decision_id: str) -> int:
        """Advance coverage after the host has successfully delivered this generated summary."""
        async with DatabaseWorker() as database:
            return await database.call(_acknowledge, self, decision_id)

    async def pending(self, decision_id: str) -> dict | None:
        """Return an undelivered summary only while its policy and source window remain current."""
        async with DatabaseWorker() as database:
            return await database.call(_pending, self, decision_id)


def activate(secretary: Secretary) -> tuple[int, int]:
    """Bind the installation to a database epoch and head without running a model."""
    policy = load_policy(secretary.config, secretary.policy_path)
    epoch, head = InboxWindow.head(secretary.config)
    return AttentionLog(secretary.state_path, secretary.config.data_dir, policy.principal).activate(epoch, head)


def _pending(secretary: Secretary, decision_id: str) -> dict | None:
    policy = load_policy(secretary.config, secretary.policy_path)
    log = AttentionLog(secretary.state_path, secretary.config.data_dir, policy.principal)
    result = log.get(decision_id)
    delivered, _ = log.positions()
    if (
        result is None
        or not result["brief"]
        or result.get("coverage") != "complete"
        or log.is_acknowledged(decision_id)
        or result["covered_through"] <= delivered
        or result["policy_digest"] != policy.digest
    ):
        return None
    return result if _current_window(secretary.config, policy, log, result) else None


def _acknowledge(secretary: Secretary, decision_id: str) -> int:
    policy = load_policy(secretary.config, secretary.policy_path)
    log = AttentionLog(secretary.state_path, secretary.config.data_dir, policy.principal)
    result = log.get(decision_id)
    if result is None:
        raise InvalidRequest("unknown secretary decision")
    if policy.digest != result["policy_digest"] or not _current_window(secretary.config, policy, log, result):
        raise InvalidRequest("secretary summary is stale; receive a current assessment before acknowledging")
    return log.acknowledge(decision_id)


def _current_window(config: Config, policy: Policy, log: AttentionLog, result: dict) -> bool:
    """A saved summary is stale when edited sources no longer fit or match its original context."""
    try:
        if result.get("reference_only"):
            current = ReferenceWindow.read(config, policy, result["since"], result["through_pos"])
        else:
            current = InboxWindow.read(config, policy, result["since"], through=result["through_pos"])
    except InboxTooLarge:
        return False
    log.bind(current.epoch, result["floor"])
    return current.digest == result["window_digest"]


def show(config: Config, state_path: Path, policy_path: Path, *, source_key: str | None = None) -> dict:
    policy = load_policy(config, policy_path)
    log = AttentionLog(state_path, config.data_dir, policy.principal)
    visible, stale = [], 0
    for result in log.recent():
        if not _current_window(config, policy, log, result):
            stale += 1
        elif source_key is None or source_key in result["sources"]:
            visible.append(result)
    since, floor = log.positions()
    return {
        "principal": policy.principal,
        "since": since,
        "floor": floor,
        "assessments": visible,
        "stale_assessments": stale,
    }
