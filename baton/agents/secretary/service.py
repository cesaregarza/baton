"""The host-facing secretary: attention decisions over one private ledger."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from baton.config import Config

from . import sources, tracking


@dataclass(frozen=True)
class Secretary:
    config: Config
    state_path: Path
    policy_path: Path

    async def scan(self, *, limit: int = sources.DEFAULT_BATCH) -> dict:
        """Catch up the private ledger without asking a judge or delivering anything."""
        return await tracking.scan_async(self.config, self.state_path, self.policy_path, limit=limit)

    async def decide(self, *, budget_seconds: float, since: int | None = None, floor: int | None = None,
                     trigger: str | None = None, hourly: bool = False,
                     clock: Callable[[], float] = time.monotonic) -> dict:
        """Decide one callback, or produce an hourly digest, with cooperative cancellation."""
        from .decide import DecisionRun  # Keep standalone CLI modules executable without eager imports.

        run = DecisionRun(self, budget_seconds=budget_seconds, since=since, floor=floor,
                          trigger=trigger, hourly=hourly, clock=clock)
        return await run.run()
