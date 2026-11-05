"""The principal's secretary: model-driven triage, saved assessments, and delivery acknowledgment."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from baton.config import Config, ConfigError
from baton.errors import InvalidRequest

from . import sources
from .policy import Policy, read_policy
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

    async def scan(self, *, limit: int = sources.DEFAULT_BATCH) -> dict:
        """Compatibility catch-up for version 1. Model secretaries read their inbox during decide."""
        async with DatabaseWorker() as database:
            policy = await database.call(read_policy, self.config, self.policy_path)
        if isinstance(policy, Policy):
            raise ConfigError("version-2 secretary needs no catch-up scan; use decide, or read to preview its input")
        from .legacy.tracking import scan_async
        return await scan_async(self.config, self.state_path, self.policy_path, limit=limit)


def _acknowledge(secretary: Secretary, decision_id: str) -> int:
    from .inbox import InboxWindow
    from .state import AttentionLog
    policy = read_policy(secretary.config, secretary.policy_path)
    if not isinstance(policy, Policy):
        raise ConfigError("version-1 hosts pass delivered coverage as since on their next decide call")
    log = AttentionLog(secretary.state_path, secretary.config.data_dir, policy.principal)
    result = log.get(decision_id)
    if result is None:
        raise InvalidRequest("unknown secretary decision")
    current = InboxWindow.read(secretary.config, policy, result["since"], through=result["through_pos"])
    log.bind(current.epoch, result["floor"])
    return log.acknowledge(decision_id)


def scan(config: Config, state_path: Path, policy_path: Path, *, limit: int = sources.DEFAULT_BATCH) -> dict:
    return asyncio.run(Secretary(config, state_path, policy_path).scan(limit=limit))


def show(config: Config, state_path: Path, policy_path: Path, *, source_key: str | None = None) -> dict:
    policy = read_policy(config, policy_path)
    if not isinstance(policy, Policy):
        from .legacy.tracking import show as legacy_show
        return legacy_show(config, state_path, policy_path, source_key=source_key)
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


def replay(config: Config, state_path: Path, policy_path: Path) -> dict:
    if not isinstance(read_policy(config, policy_path), Policy):
        from .legacy.tracking import replay as legacy_replay
        return legacy_replay(config, state_path, policy_path)
    return {**show(config, state_path, policy_path), "mode": "recorded", "model_called": False}


def cancel(config: Config, state_path: Path, policy_path: Path, expectation_id: str, reason: str) -> dict:
    if isinstance(read_policy(config, policy_path), Policy):
        raise ConfigError("version-2 secretary has no inferred request ledger; post the correction in the Baton thread")
    from .legacy.tracking import cancel as legacy_cancel
    return legacy_cancel(config, state_path, policy_path, expectation_id, reason)
