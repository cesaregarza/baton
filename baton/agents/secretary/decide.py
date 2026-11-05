"""One callback's attention decision.

Secretary creates a DecisionRun, which owns activation, a bounded asynchronous
scan and the saved response. AttentionWindow owns the outstanding items and
chooses what needs the principal; the host owns delivering that result.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
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
from baton.database import Connection, Row
from baton.database.store import connect_read_only
from baton.errors import BatonError, InvalidRequest

from . import digest, sources, tracking
from . import judge as secretary_judge
from . import policy as policy_module
from . import store as secretary_store
from .runtime import DatabaseWorker
from .service import Secretary

BATCH = sources.DEFAULT_BATCH
MAX_BUDGET_SECONDS = 60.0
LISTED = 100  # what the window lists of each of its deliveries and its context; the rest are counted
TRIGGER = re.compile(r"message:(?P<seq>[1-9][0-9]{0,17})"
                     r"|job:(?P<job>job_[0-9a-f]{16}):(?P<event_seq>[1-9][0-9]{0,17})|(?P<conversation>conversation)")
JOB_KINDS = ("job.created", "job.moved")
RANK = {"wake": 0, "hold": 1, "keep": 2}
WINDOW = ("SELECT i.source_key, i.event_pos, i.envelope, d.kind, d.reason,"
          " j.status AS judge, j.rules_kind, j.rules_reason FROM items i"
          " JOIN dispositions d ON d.principal = i.principal AND d.source_key = i.source_key"
          " LEFT JOIN judgments j ON j.principal = i.principal AND j.source_key = i.source_key"
          " WHERE i.principal = ? AND i.event_pos > ? AND i.event_pos <= ? ORDER BY i.event_pos")


@dataclass
class AttentionWindow:
    """The outstanding items since delivery: classify, select and present their attention needs."""

    baton: Connection
    state: Connection
    config: Config
    policy: policy_module.Policy
    since: int
    through: int

    def __post_init__(self) -> None:
        rows = self.state.execute(WINDOW, (self.policy.principal, self.since, self.through))
        self.items = [self._item(row) for row in rows]

    def render(self, *, floor: int, scanned: str, hourly: bool) -> tuple[str, int]:
        return digest.render(self.baton, self.state, self.config, self.policy.principal, self.policy.brief_bytes,
                             self.items, since=self.since, floor=floor, through=self.through,
                             scanned=scanned, hourly=hourly)

    def summary(self, *, start: int, trigger_pos: int | None) -> dict:
        public = ("source_key", "pos", "kind", "sender", "message_id", "job_id", "conversation_id", "disposition",
                  "reason", "decision", "rule", "judge", "rules", "delivered")
        own = next((item for item in self.items if item["pos"] == trigger_pos), None)
        return {"counts": {kind: sum(item["decision"] == kind for item in self.items) for kind in RANK},
                "judged_counts": {kind: sum(item["decision"] == kind and item["judge"] == secretary_judge.JUDGED
                                            for item in self.items) for kind in RANK},
                "trigger_item": {key: own[key] for key in public} if own else None,
                "new": [{key: item[key] for key in public} for item in self.items if item["pos"] > start],
                "window": self._listed(public)}

    def _listed(self, public: tuple[str, ...]) -> dict:
        items = self.items
        deliveries = [item for item in items if item["delivered"]]
        context = [item for item in items if not item["delivered"]]
        return {"deliveries": [{key: item[key] for key in public} for item in deliveries[:LISTED]],
                "more_deliveries": max(0, len(deliveries) - LISTED),
                "context": [{key: item[key] for key in public} for item in context[:LISTED]],
                "more_context": max(0, len(context) - LISTED)}

    def _item(self, row: Row) -> dict:
        """Classify a source and its recorded word-rule alternative; keep deliveries distinct from context."""
        policy = self.policy
        envelope = json.loads(row["envelope"])
        decision, rule = self._classify(envelope["event_kind"], row["kind"])
        rules = self._classify(envelope["event_kind"], row["rules_kind"]) if row["judge"] else (decision, rule)
        return {"source_key": row["source_key"], "pos": row["event_pos"], "kind": envelope["event_kind"],
                "sender": sources._actor(envelope), "message_id": envelope["message_id"],
                "job_id": envelope["job_id"], "job_state": (envelope["job"] or {}).get("event_state"),
                "conversation_id": envelope["conversation_id"], "artifacts": envelope["artifacts"],
                "disposition": row["kind"], "reason": row["reason"], "decision": decision, "rule": rule,
                "judge": row["judge"], "rules": {"decision": rules[0], "rule": rules[1]},
                "delivered": sources.delivered_to(policy.principal, envelope["event_audience"])}

    def _classify(self, event_kind: str, disposition: str) -> tuple[str, str]:
        """One item's own decision and rule id, from the kernel's disposition."""
        policy = self.policy
        if disposition == "brief":
            if event_kind == "message":
                return "wake", "wake:brief"
            if event_kind in JOB_KINDS and policy.job_events_wake:
                return "wake", "wake:job-brief"
            return "hold", "hold:job-brief"
        if disposition in ("digest", "reconcile"):
            return "hold", f"hold:{disposition}"
        return "keep", f"keep:{disposition}"

    def choose(self, trigger: str | None) -> tuple[str, str, str | None]:
        """The strongest item decides, and its source is the evidence. A job or rate-limit wake never wakes alone."""
        items, policy = self.items, self.policy
        if not items:
            return "keep", "keep:nothing-new", None
        first = min(items, key=lambda item: (RANK[item["decision"]], item["pos"]))
        decision = first["decision"], first["rule"], first["source_key"]
        if first["decision"] != "wake":
            return decision
        if trigger is None or trigger.startswith("message:"):
            return decision
        if trigger.startswith("job:") and policy.job_events_wake:
            return decision
        # A conversation trigger, or a job trigger without opt-in, holds the item for its own
        # message wake or the hourly digest. Holding does not mark anything delivered.
        return "hold", "hold:non-message-trigger", first["source_key"]


@dataclass
class DecisionRun:
    """One callback's activation, scan budget, judge session and recorded response."""

    secretary: Secretary
    budget_seconds: float
    since: int | None = None
    floor: int | None = None
    trigger: str | None = None
    hourly: bool = False
    clock: Callable[[], float] = time.monotonic

    async def run(self) -> dict:
        deadline = self.clock() + self._checked_budget()
        async with DatabaseWorker() as database:
            self.start, activation = await database.call(self._catch_up_position)
            if self.since is None:
                self.since = self.floor = activation
            judge = None if self.hourly else await database.call(self._judge, deadline)
            await self._scan(database, judge, deadline)
            return await database.call(self._finish, judge)

    async def _scan(self, database: DatabaseWorker, judge: secretary_judge.JudgeSession | None,
                    deadline: float) -> None:
        # Each awaited page commits with its position. A budget prevents starting another page;
        # cancellation rolls back the active page without losing earlier committed progress.
        while True:
            result = await tracking.scan_async(self.secretary.config, self.secretary.state_path,
                                               self.secretary.policy_path, limit=BATCH, judge=judge,
                                               include_export=False, database=database)
            if result["events_seen"] != BATCH or self.clock() >= deadline:
                return

    def _finish(self, judge: secretary_judge.JudgeSession | None) -> dict:
        config = self.secretary.config
        with connect_read_only(config.db_path) as baton, baton.snapshot():
            policy = policy_module.load_policy(config, baton, self.secretary.policy_path)
            with secretary_store.open_state(self.secretary.state_path, config.data_dir) as state, state.transaction():
                through = state.execute("SELECT through_pos FROM sources WHERE principal=?",
                                         (policy.principal,)).fetchone()[0]
                head = baton.execute("SELECT COALESCE(MAX(pos), 0) FROM events").fetchone()[0]
                current = not sources._source_rows(baton, policy.principal, through, 1)
                trigger_pos = self._trigger_pos(baton, policy.principal)
                window = AttentionWindow(baton, state, config, policy, self.since, through)
                at = _now()
                progress = "current" if current else f"behind, Baton head {head}"
                scanned = f"Scanned through {through} at {at}; {progress}."
                decision, rule, evidence = (None, None, None) if self.hourly else window.choose(self.trigger)
                text, covered = "", self.since
                if self.hourly or decision == "wake":
                    text, covered = window.render(floor=self.floor, scanned=scanned, hourly=self.hourly)
                result = {"principal": policy.principal, "mode": "hourly" if self.hourly else "callback",
                          "trigger": self.trigger, "trigger_pos": trigger_pos,
                          "covers_trigger": current or (trigger_pos is not None and trigger_pos <= through),
                          "since": self.since, "floor": self.floor, "through_pos": through, "head": head,
                          "covered_through": covered, "coverage": "current" if current else "behind",
                          "scanned_at": at, "decision": decision, "rule": rule, "evidence": evidence,
                          **window.summary(start=self.start, trigger_pos=trigger_pos),
                          "judge": {"calls": judge.calls, "fallbacks": judge.fallbacks} if judge else None,
                          "brief": text}
                result["decision_seq"] = self._record(state, policy, result)
                return result

    def _record(self, state: Connection, policy: policy_module.Policy, result: dict) -> int:
        seq = state.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM decisions WHERE principal=?",
                            (policy.principal,)).fetchone()[0]
        text = result["brief"].encode("utf-8")
        state.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (policy.principal, seq, result["scanned_at"], result["mode"], self.trigger, result["trigger_pos"],
                       self.since, self.floor, result["through_pos"], result["head"], result["coverage"],
                       result["decision"], result["rule"], result["evidence"], policy.digest,
                       hashlib.sha256(text).hexdigest(), len(text)))
        return seq

    def _checked_budget(self) -> float:
        """Refuse bad arguments before anything is scanned."""
        budget_seconds, since, floor = self.budget_seconds, self.since, self.floor
        trigger, hourly = self.trigger, self.hourly
        if type(budget_seconds) not in (int, float) or not 0 < budget_seconds <= MAX_BUDGET_SECONDS:
            raise InvalidRequest(f"the budget must be more than 0 and at most {MAX_BUDGET_SECONDS:g} seconds")
        if since is not None or floor is not None:
            valid_positions = type(since) is int and type(floor) is int and 0 <= floor <= since
            if not valid_positions:
                raise InvalidRequest("since and floor go together, as integers with 0 <= floor <= since")
        if hourly and trigger is not None:
            raise InvalidRequest("the hourly digest has no triggering wake")
        if trigger is not None and TRIGGER.fullmatch(trigger) is None:
            raise InvalidRequest("the trigger must be message:SEQ, job:JOB_ID:SEQ or conversation")
        return float(budget_seconds)

    def _catch_up_position(self) -> tuple[int, int | None]:
        """Where the secretary's scan stands and, for the first decision, the activation floor. Refuses, creating
        nothing, if the offline catch-up scan never ran.

        The floor is recorded before anything is scanned. When the host's timeout kills a first decision after a scan
        batch committed, Baton's retry is a first decision too: it finds this floor rather than the advanced scan
        position, so the events that batch scanned still count as new.
        """
        config, state_path, policy_path = self.secretary.config, self.secretary.state_path, self.secretary.policy_path
        first = self.since is None
        with connect_read_only(config.db_path) as baton:
            with baton.snapshot():
                principal = policy_module.load_policy(config, baton, policy_path).principal
        if not state_path.exists():
            raise ConfigError("there is no secretary state yet: run the offline catch-up scan before decide")
        with secretary_store.open_state(state_path, config.data_dir) as state:
            row = state.execute("SELECT through_pos FROM sources WHERE principal=?", (principal,)).fetchone()
            if row is None:
                raise ConfigError("the secretary state has no scan for this principal: "
                                  "run the offline catch-up scan first")
            if not first:
                return row["through_pos"], None
            with state.transaction():
                state.execute("INSERT OR IGNORE INTO activation VALUES (?, ?, ?)",
                              (principal, row["through_pos"], _now()))
            floor = state.execute("SELECT floor_pos FROM activation WHERE principal=?", (principal,)).fetchone()[0]
            return row["through_pos"], floor

    def _judge(self, deadline: float) -> secretary_judge.JudgeSession | None:
        """Message triggers start judging at their own event; other callbacks start at the scan position."""
        config, policy_path = self.secretary.config, self.secretary.policy_path
        trigger, clock = self.trigger, self.clock
        with connect_read_only(config.db_path) as baton:
            with baton.snapshot():
                policy = policy_module.load_policy(config, baton, policy_path)
                if policy.judge is None:
                    return None
                trigger_pos = self._trigger_pos(baton, policy.principal)
        start = trigger_pos if trigger and trigger.startswith("message:") else None
        return secretary_judge.JudgeSession(deadline=deadline, clock=clock, start_pos=start, trigger_pos=trigger_pos)

    def _trigger_pos(self, baton: Connection, principal: str) -> int | None:
        """The event a wake was raised for. A rate-limited conversation's wake names none."""
        trigger = self.trigger
        match = TRIGGER.fullmatch(trigger) if trigger else None
        if match is None or match["conversation"]:
            return None
        if match["seq"]:  # a message wake carries the principal's first unread mailbox seq
            row = baton.execute("SELECT pos FROM events WHERE kind='message' AND audience=?"
                                " AND json_extract(data, '$.seq')=?", (principal, int(match["seq"]))).fetchone()
        else:
            row = baton.execute("SELECT pos FROM events WHERE job_id=? AND seq=? AND audience=?",
                                (match["job"], int(match["event_seq"]), principal)).fetchone()
        return row["pos"] if row else None


def decide(config: Config, state_path: Path, policy_path: Path, *, budget_seconds: float,
           since: int | None = None, floor: int | None = None, trigger: str | None = None,
           hourly: bool = False, clock: Callable[[], float] = time.monotonic) -> dict:
    """Blocking CLI/legacy boundary. Async hosts await Secretary.decide directly."""
    secretary = Secretary(config, state_path, policy_path)
    return asyncio.run(secretary.decide(budget_seconds=budget_seconds, since=since, floor=floor,
                                       trigger=trigger, hourly=hourly, clock=clock))



def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """decide's own options, shared by `baton secretary decide` and `python -m baton.secretary_decide`."""
    parser.add_argument("--budget-seconds", type=float, required=True,
                        help="start no new scan batch after this many seconds")
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
    parser.add_argument("--policy", type=Path, required=True, help="versioned JSON relationship and rules")
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
