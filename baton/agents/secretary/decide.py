"""One alert: read the pending inbox, ask the lightweight model, return its attention decision and summary."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from baton import config as config_module
from baton import datadir
from baton.config import Config, ConfigError
from baton.errors import BatonError, InvalidRequest

from .assessment import Assessment
from .inbox import InboxWindow
from .model import DecisionEngine, ModelUnavailable, SecretaryModel
from .policy import Policy, fingerprint, load_policy
from .prompt import INSTRUCTIONS, RESPONSE_SCHEMA
from .runtime import DatabaseWorker
from .service import Secretary
from .state import AttentionLog

MAX_BUDGET_SECONDS = 60.0
TRIGGER = re.compile(r"message:[1-9][0-9]{0,17}|job:job_[0-9a-f]{16}:[1-9][0-9]{0,17}|conversation")


class SecretaryUnavailable(BatonError):
    code = "secretary_unavailable"
    extra = {"retryable": True}


@dataclass
class DecisionRun:
    secretary: Secretary
    budget_seconds: float
    since: int | None = None
    floor: int | None = None
    trigger: str | None = None
    hourly: bool = False
    clock: Callable[[], float] = time.monotonic

    async def run(self) -> dict:
        self._check()
        async with DatabaseWorker() as database:
            policy = await database.call(load_policy, self.secretary.config, self.secretary.policy_path)
            self.deadline = self.clock() + self.budget_seconds
            try:
                async with asyncio.timeout(self.budget_seconds):
                    return await self._assess(database, policy)
            except TimeoutError as error:
                raise SecretaryUnavailable("secretary budget expired; no delivery position advanced") from error
            except ModelUnavailable as error:
                raise SecretaryUnavailable(f"{error}; no delivery position advanced") from error

    async def _assess(self, database: DatabaseWorker, policy: Policy) -> dict:
        config = self.secretary.config
        log = AttentionLog(self.secretary.state_path, config.data_dir, policy.principal)
        if self.since is None:
            self.since, self.floor = await database.call(log.positions)
        window = await database.call(InboxWindow.read, config, policy, self.since)
        await database.call(log.bind, window.epoch, self.floor)
        context = {"principal": policy.principal, "mode": "digest" if self.hourly else "alert",
                   "trigger": self.trigger, "since": self.since, "floor": self.floor,
                   "through_pos": window.through, "instructions": policy.instructions,
                   "summary_bytes": policy.summary_bytes, "events": window.events,
                   "reply_context": window.reply_context}
        key = fingerprint({"policy": policy.digest, "context": context, "epoch": window.epoch,
                           "prompt": INSTRUCTIONS, "schema": RESPONSE_SCHEMA})
        cached = await database.call(log.get, key)
        if cached is not None:
            return {**cached, "cached": True}
        engine_status = "not_configured"
        context["engine_advice"] = None
        if not window.events:
            assessment = Assessment("keep", "No pending visible events.", "", [])
        else:
            if policy.decision_engine:
                try:
                    context["engine_advice"] = await DecisionEngine(policy.decision_engine).advise(
                        context, min(policy.decision_engine["timeout_seconds"], self._remaining() / 4))
                    engine_status = "available"
                except ModelUnavailable:
                    engine_status = "unavailable"
            answer = await SecretaryModel(policy.model).assess(
                context, min(policy.model["timeout_seconds"], self._remaining()))
            assessment = Assessment.parse(answer, offered=window.source_keys,
                                          summary_bytes=policy.summary_bytes, digest=self.hourly)
        # Recheck the same sources after inference: permission changes or edits invalidate the generated summary.
        current = await database.call(InboxWindow.read, config, policy, self.since, through=window.through)
        if current.digest != window.digest:
            raise SecretaryUnavailable(
                "inbox visibility or content changed during inference; retry with current context")
        result = {"principal": policy.principal, "mode": context["mode"], "trigger": self.trigger,
                  "since": self.since, "floor": self.floor, "through_pos": window.through,
                  "covered_through": window.through if assessment.summary else self.since,
                  "decision": assessment.decision, "reason": assessment.reason,
                  "brief": assessment.summary, "sources": assessment.sources,
                  "source_refs": [{"source_key": e["source_key"], "pos": e["pos"],
                                   "message_id": e["message"]["id"] if e["message"] else None,
                                   "conversation_id": e["message"]["conversation_id"] if e["message"] else None,
                                   "job_id": e["job_id"]} for e in [*window.events, *window.reply_context]
                                  if e["source_key"] in assessment.sources],
                  "decision_id": key, "window_digest": window.digest, "policy_digest": policy.digest,
                  "engine_status": engine_status, "cached": False,
                  "model": {k: policy.model[k] for k in ("provider", "model") if k in policy.model},
                  "scanned_at": _now()}
        return await database.call(log.remember, result)

    def _remaining(self) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    def _check(self) -> None:
        if type(self.budget_seconds) not in (int, float) or not 0 < self.budget_seconds <= MAX_BUDGET_SECONDS:
            raise InvalidRequest("the budget must be more than 0 and at most 60 seconds")
        if self.since is not None or self.floor is not None:
            if type(self.since) is not int or type(self.floor) is not int or not 0 <= self.floor <= self.since:
                raise InvalidRequest("since and floor go together, as integers with 0 <= floor <= since")
        if self.hourly and self.trigger is not None:
            raise InvalidRequest("the hourly digest has no triggering wake")
        if self.trigger is not None and TRIGGER.fullmatch(self.trigger) is None:
            raise InvalidRequest("the trigger must be message:SEQ, job:JOB_ID:SEQ or conversation")


def decide(config: Config, state_path: Path, policy_path: Path, *, budget_seconds: float,
           since: int | None = None, floor: int | None = None, trigger: str | None = None,
           hourly: bool = False, clock: Callable[[], float] = time.monotonic) -> dict:
    """Synchronous CLI boundary. Async hosts await Secretary.decide directly."""
    secretary = Secretary(config, state_path, policy_path)
    return asyncio.run(secretary.decide(budget_seconds=budget_seconds, since=since, floor=floor,
                                       trigger=trigger, hourly=hourly, clock=clock))



def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """decide's own options, shared by `baton secretary decide` and `python -m baton.secretary_decide`."""
    parser.add_argument("--budget-seconds", type=float, required=True,
                        help="maximum time for inbox triage and summary generation")
    parser.add_argument("--since", type=int, help="the covered_through of the last delivered brief (with --floor)")
    parser.add_argument("--floor", type=int, help="the activation position: nothing at or before it wakes")
    parser.add_argument("--trigger", help="the wake decided for: message:SEQ, job:JOB_ID:SEQ or conversation")
    parser.add_argument("--hourly", action="store_true", help="digest everything since --since for the hourly turn")


def options(args: argparse.Namespace) -> dict:
    return {"budget_seconds": args.budget_seconds, "since": args.since, "floor": args.floor,
            "trigger": args.trigger, "hourly": args.hourly}


def main(argv: list[str] | None = None) -> int:
    """`python -m baton.secretary_decide`: the same options, output and exit codes as `baton secretary decide`.

    The `baton` command imports every interface first: about 3 s on a Raspberry Pi 4, against 0.3 s for this
    module, and a wake callback has only a few seconds for the secretary.
    """
    parser = argparse.ArgumentParser(prog="python -m baton.secretary_decide",
                                     description="decide wake, hold or keep for one Baton wake")
    parser.add_argument("--config", help="config file (default: $BATON_CONFIG or ~/.config/baton/baton.toml)")
    parser.add_argument("--state", type=Path, required=True, help="private SQLite state file")
    parser.add_argument("--policy", type=Path, required=True, help="versioned secretary model policy")
    add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        config = config_module.load(args.config)
        os.umask(0o077)
        datadir.check(config.data_dir)
        result, code = decide(config, args.state, args.policy, **options(args)), 0
    except ConfigError as error:
        result, code = {"error": getattr(error, "code", "config"), "detail": str(error)}, 2
    except BatonError as error:
        result, code = {"error": error.code, "detail": str(error), **error.extra}, 1
    print(json.dumps(result), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
