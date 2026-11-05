"""The principal's secretary: model-driven triage, saved assessments, and delivery acknowledgment."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from baton.config import Config
from baton.errors import InvalidRequest

from .policy import load_policy
from .runtime import DatabaseWorker


@dataclass(frozen=True)
class Secretary:
    config: Config
    state_path: Path
    policy_path: Path

    async def decide(self, *, budget_seconds: float, since: int | None = None, floor: int | None = None,
                     trigger: str | None = None, hourly: bool = False,
                     clock: Callable[[], float] = time.monotonic) -> dict:
        from .decide import DecisionRun
        return await DecisionRun(self, budget_seconds, since, floor, trigger, hourly, clock).run()

    async def acknowledge(self, decision_id: str) -> int:
        """Advance coverage after the host has successfully delivered this generated summary."""
        async with DatabaseWorker() as database:
            return await database.call(_acknowledge, self, decision_id)

    async def pending(self, decision_id: str) -> dict | None:
        """Return an undelivered summary only while its policy and source window remain current."""
        async with DatabaseWorker() as database:
            return await database.call(_pending, self, decision_id)


def _pending(secretary: Secretary, decision_id: str) -> dict | None:
    from .inbox import InboxWindow
    from .state import AttentionLog
    policy = load_policy(secretary.config, secretary.policy_path)
    log = AttentionLog(secretary.state_path, secretary.config.data_dir, policy.principal)
    result = log.get(decision_id)
    delivered, _ = log.positions()
    if (result is None or not result["brief"] or result["covered_through"] <= delivered
            or result["policy_digest"] != policy.digest):
        return None
    current = InboxWindow.read(secretary.config, policy, result["since"], through=result["through_pos"])
    log.bind(current.epoch, result["floor"])
    return result if current.digest == result["window_digest"] else None


def _acknowledge(secretary: Secretary, decision_id: str) -> int:
    from .inbox import InboxWindow
    from .state import AttentionLog
    policy = load_policy(secretary.config, secretary.policy_path)
    log = AttentionLog(secretary.state_path, secretary.config.data_dir, policy.principal)
    result = log.get(decision_id)
    if result is None:
        raise InvalidRequest("unknown secretary decision")
    current = InboxWindow.read(secretary.config, policy, result["since"], through=result["through_pos"])
    log.bind(current.epoch, result["floor"])
    if current.digest != result["window_digest"] or policy.digest != result["policy_digest"]:
        raise InvalidRequest("secretary summary is stale; receive a current assessment before acknowledging")
    return log.acknowledge(decision_id)


def show(config: Config, state_path: Path, policy_path: Path, *, source_key: str | None = None) -> dict:
    policy = load_policy(config, policy_path)
    from .inbox import InboxWindow
    from .state import AttentionLog
    log = AttentionLog(state_path, config.data_dir, policy.principal)
    visible, stale = [], 0
    for result in log.recent():
        current = InboxWindow.read(config, policy, result["since"], through=result["through_pos"])
        log.bind(current.epoch, result["floor"])
        if current.digest != result["window_digest"]:
            stale += 1
        elif source_key is None or source_key in result["sources"]:
            visible.append(result)
    since, floor = log.positions()
    return {"principal": policy.principal, "since": since, "floor": floor,
            "assessments": visible, "stale_assessments": stale}
