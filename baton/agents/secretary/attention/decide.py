"""One alert: read the pending inbox, ask the lightweight model, return its attention decision and summary."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from baton import config as config_module
from baton import datadir
from baton.config import Config, ConfigError
from baton.config.secretary import MAX_BUDGET_SECONDS, Policy, fingerprint, load_policy, validate_decision_budget
from baton.errors import BatonError, InvalidRequest

from ..connections.decisions import AttentionDecision
from ..connections.transport import ModelResponseInvalid, ModelUnavailable
from ..harness.prompt import HARNESS_VERSION, INSTRUCTIONS, RESPONSE_SCHEMA
from ..harness.run import SecretaryHarness
from ..inbox import sources
from ..inbox.window import InboxWindow
from ..service import Secretary
from .assessment import Assessment
from .backlog import Page
from .health import EngineHealth, ModelHealth
from .outcome import Outcome, persist
from .runtime import DatabaseWorker
from .state import AttentionLog

TRIGGER = re.compile(r"message:[1-9][0-9]{0,17}|job:job_[0-9a-f]{16}:[1-9][0-9]{0,17}|conversation")


class SecretaryUnavailable(BatonError):
    code = "secretary_unavailable"
    extra = {"retryable": True}
    category = "unavailable"

    def __init__(self, message: str, *, category: str | None = None):
        super().__init__(message)
        self.category = category or type(self).category


class SecretaryResponseInvalid(SecretaryUnavailable):
    """The page received an unusable model answer, rather than a transport outage."""

    category = "invalid_response"


@dataclass
class DecisionRun:
    secretary: Secretary
    budget_seconds: float
    since: int | None = None
    floor: int | None = None
    trigger: str | None = None
    hourly: bool = False
    clock: Callable[[], float] = time.monotonic
    through: int | None = None
    # The baseline this attempt wrote, so later failures can still match its retained page.
    model_successes: int | None = field(default=None, init=False)
    model_policy: str = field(default="", init=False)
    # Set when the caller's trigger is not on this page: the page is backlog catch-up, never the trigger's assessment.
    catch_up: bool = field(default=False, init=False)

    async def run(self) -> dict:
        self.model_successes = None
        self._check()
        async with DatabaseWorker() as database:
            policy = await database.call(load_policy, self.secretary.config, self.secretary.policy_path)
            validate_decision_budget(policy, self.budget_seconds)
            self.deadline = self.clock() + self.budget_seconds
            try:
                return await self._assess(database, policy)
            except TimeoutError as error:
                raise SecretaryUnavailable(
                    "secretary budget expired; no delivery position advanced", category="timeout"
                ) from error
            except ModelResponseInvalid as error:
                raise SecretaryResponseInvalid(f"{error}; no delivery position advanced") from error
            except ModelUnavailable as error:
                raise SecretaryUnavailable(
                    f"{error}; no delivery position advanced", category=error.category
                ) from error

    async def _assess(self, database: DatabaseWorker, policy: Policy) -> dict:
        config = self.secretary.config
        log = AttentionLog(self.secretary.state_path, config.data_dir, policy.principal)
        if self.since is None:
            epoch, head = await database.call(InboxWindow.head, config)
            self.since, self.floor = await database.call(log.activate, epoch, head)
        assert self.floor is not None  # Explicit positions are validated together; activation supplies both.
        window = await database.call(InboxWindow.read, config, policy, self.since, through=self.through)
        await database.call(log.bind, window.epoch, self.floor)
        if not self._on_page(window):
            # A trigger that is not a visible event is still refused, for every caller; a visible one elsewhere is
            # left unassessed, so this page is catch-up and its result never carries that trigger.
            await database.call(sources.trigger_position, config, policy.principal, self.trigger)
            self.trigger, self.catch_up = None, True
        context = self._context(policy, window)
        harness = SecretaryHarness(policy)
        verdict = None
        if window.events and not self.hourly:
            verdict = await harness.tools.evaluate(context, min(policy.decision_timeout, self._remaining()))
            await database.call(EngineHealth(log).record, harness.tools.health)
        forced = bool(
            verdict and verdict.decision == AttentionDecision.WAKE and verdict.wake_probability >= policy.wake_threshold
        )
        context["engine_verdict"] = harness.tools.verdict
        context["wake_required"] = forced
        key = fingerprint(
            {
                "policy": policy.digest,
                "context": context,
                "epoch": window.epoch,
                "prompt": INSTRUCTIONS,
                "schema": RESPONSE_SCHEMA,
                "harness": HARNESS_VERSION,
            }
        )
        cached = await database.call(log.get, key)
        if cached is not None:
            await database.call(persist, self.secretary, policy, cached)
            return {**cached, "cached": True}  # a catch-up result was stored with its catch_up label
        outcome = Outcome(policy, window, self.floor, context["mode"], self.trigger)
        result = await self._infer(database, harness, context, outcome, key, forced)
        if self.catch_up:
            result["catch_up"] = True
        if not window.events:
            return result
        current = await database.call(InboxWindow.read, config, policy, self.since, through=window.through)
        if current.digest != window.digest:
            raise SecretaryUnavailable(
                "inbox visibility or content changed during inference; retry with current context"
            )
        return await database.call(persist, self.secretary, policy, result)

    def _context(self, policy: Policy, window: InboxWindow) -> dict:
        return {
            "principal": policy.principal,
            "mode": "digest" if self.hourly else "alert",
            "trigger": self.trigger,
            "since": self.since,
            "floor": self.floor,
            "through_pos": window.through,
            "instructions": policy.instructions,
            "knowledge": policy.knowledge,
            "summary_bytes": policy.summary_bytes,
            "events": window.events,
            "reply_context": window.reply_context,
        }

    async def _infer(
        self,
        database: DatabaseWorker,
        harness: SecretaryHarness,
        context: dict,
        outcome: Outcome,
        key: str,
        forced: bool,
    ) -> dict:
        window = outcome.window
        assert isinstance(window, InboxWindow)
        if not window.events:
            assessment = Assessment("keep", "No pending visible events.", "", [])
        else:
            try:
                assessment = await self._model_assessment(database, harness, context, window)
            except (ModelUnavailable, TimeoutError):
                if not forced:
                    raise
                return outcome.notice(
                    "The decision engine requires your attention, but the secretary could not generate a summary.",
                    kind="engine_wake",
                    engine_verdict=harness.tools.verdict,
                )
            if forced and assessment.decision != "wake":
                return outcome.notice(
                    f"The model chose {assessment.decision}, but the independent engine requires your attention.",
                    kind="engine_wake",
                    engine_verdict=harness.tools.verdict,
                )
        return outcome.build(
            assessment,
            decision_id=key,
            engine_status=harness.tools.status,
            engine_verdict=harness.tools.verdict,
            harness=harness.metrics,
        )

    async def _model_assessment(
        self, database: DatabaseWorker, harness: SecretaryHarness, context: dict, window: InboxWindow
    ) -> Assessment:
        policy = harness.policy
        log = AttentionLog(self.secretary.state_path, self.secretary.config.data_dir, policy.principal)
        health = ModelHealth(log, policy.digest)
        try:
            answer = await harness.assess(context, self._remaining())
            assessment = Assessment.parse(
                answer,
                offered=window.source_keys,
                required={event["source_key"] for event in window.events},
                summary_bytes=policy.summary_bytes,
                digest=self.hourly,
            )
        except (ModelUnavailable, TimeoutError) as error:
            category = "timeout" if isinstance(error, TimeoutError) else error.category
            await database.call(health.failed, category)
            raise
        recorded = await database.call(health.succeeded, Page(window.since, window.through, context["mode"]))
        self.model_successes = recorded["successes"]
        self.model_policy = policy.digest
        return assessment

    def _on_page(self, window: InboxWindow) -> bool:
        """Whether the caller's trigger, if it names an event, is one of this bounded page's events.

        A trigger elsewhere used to be refused before its page was queued, so while a backlog existed no callback moved
        it. Its page is now assessed as catch-up instead: the model is never told of the trigger, the result names no
        trigger and is marked catch_up, and the trigger stays unassessed, so the caller decides that wake as any wake
        the secretary has not reached, and a later page covers it.
        """
        if self.trigger is None or self.trigger == "conversation":
            return True
        parts = self.trigger.split(":")
        if parts[0] == "message":
            return any(
                event["kind"] == "message"
                and event["seq"] == int(parts[1])
                and sources.delivered_to(window.principal, event["audience"])
                for event in window.events
            )
        return any(event["job_id"] == parts[1] and event["seq"] == int(parts[2]) for event in window.events)

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
        if self.through is not None and (
            type(self.through) is not int or self.since is None or self.through < self.since
        ):
            raise InvalidRequest("through requires explicit positions and must be at least since")
        if self.trigger is not None and TRIGGER.fullmatch(self.trigger) is None:
            raise InvalidRequest("the trigger must be message:SEQ, job:JOB_ID:SEQ or conversation")


def decide(
    config: Config,
    state_path: Path,
    policy_path: Path,
    *,
    budget_seconds: float,
    since: int | None = None,
    floor: int | None = None,
    trigger: str | None = None,
    hourly: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Synchronous CLI boundary. Async hosts await Secretary.decide directly."""
    secretary = Secretary(config, state_path, policy_path)
    return asyncio.run(
        secretary.decide(
            budget_seconds=budget_seconds, since=since, floor=floor, trigger=trigger, hourly=hourly, clock=clock
        )
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """decide's own options, shared by `baton secretary decide` and `python -m baton.secretary_decide`."""
    parser.add_argument(
        "--budget-seconds", type=float, required=True, help="maximum time for inbox triage and summary generation"
    )
    parser.add_argument("--since", type=int, help="the covered_through of the last delivered brief (with --floor)")
    parser.add_argument("--floor", type=int, help="the activation position: nothing at or before it wakes")
    parser.add_argument("--trigger", help="the wake decided for: message:SEQ, job:JOB_ID:SEQ or conversation")
    parser.add_argument("--hourly", action="store_true", help="digest everything since --since for the hourly turn")


def options(args: argparse.Namespace) -> dict:
    return {
        "budget_seconds": args.budget_seconds,
        "since": args.since,
        "floor": args.floor,
        "trigger": args.trigger,
        "hourly": args.hourly,
    }


def main(argv: list[str] | None = None) -> int:
    """`python -m baton.secretary_decide`: the same options, output and exit codes as `baton secretary decide`.

    The `baton` command imports every interface first: about 3 s on a Raspberry Pi 4, against 0.3 s for this
    module, and a wake callback has only a few seconds for the secretary.
    """
    parser = argparse.ArgumentParser(
        prog="python -m baton.secretary_decide", description="decide wake, hold or keep for one Baton wake"
    )
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
