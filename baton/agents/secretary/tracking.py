"""Scan and replay the offline secretary projection over retained Baton records.

Baton is opened read-only; only the separate secretary state advances. Rules
interpret sources, Expectations owns their effects, and this module owns the
transaction, cursor and replay lifecycle.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Protocol

from baton import board_read, messages
from baton.config import Config, ConfigError
from baton.database import Connection
from baton.database.store import connect_read_only
from baton.errors import InvalidRequest

from . import judge as secretary_judge
from . import sources
from . import store as secretary_store
from .expectations import Expectations
from .policy import POLICY_VERSION, Policy, load_policy
from .rules import MessageRules, job_disposition
from .runtime import DatabaseWorker

RULES_VERSION = 7
MAX_BATCH = 1000
MAX_BRIEF_BYTES = 16 * 1024


class Judge(Protocol):
    """Synchronous interpretation used by offline scan and RecordedJudgments during replay."""

    def wants(self, policy: Policy, meta: dict) -> bool:
        """Whether this source goes to the judge rather than to the word lists."""

    def stops_before(self, meta: dict) -> bool:
        """Whether its budget is gone and the scan should stop before this source rather than read it."""

    def outcome(self, state: Connection, baton: Connection, config: Config, policy: Policy,
                meta: dict, text: str) -> tuple[str, str, list[str]]:
        """The source's disposition, reason and answer words, with both verdicts recorded."""


def _work_key(meta: dict) -> str:
    if meta["job_id"]:
        return f"job:{meta['job_id']}"
    if meta["artifacts"]:
        return f"artifact:{meta['artifacts'][0]['key']}"
    if meta["conversation_id"]:
        return f"conversation:{meta['conversation_id']}"
    return meta["source_key"]


def _brief(state: Connection, policy: Policy, meta: dict, kind: str,
           reason: str, answers: list[str]) -> tuple[str, int] | None:
    principal, work = policy.principal, _work_key(meta)
    last = state.execute("SELECT version FROM briefs WHERE principal=? AND work_key=?"
                         " ORDER BY version DESC LIMIT 1", (principal, work)).fetchone()
    source = meta["source_key"]
    author = sources._actor(meta)
    if meta["has_notice"] or author is None:
        label = "host notice"
    else:
        label = f"reported by {author}"
    blockers = [answer for answer in answers if answer.casefold() in {"fail", "block", "changes required"}]
    item = {"source_key": source, "source_ref": {"event_pos": meta["event_pos"],
                                                   "message_id": meta["message_id"]},
            "envelope_digest": secretary_store.digest(meta), "label": label, "disposition": kind,
            "reason": reason, "blockers": blockers, "authored_sha256": meta["text_sha256"],
            "artifacts": meta["artifacts"],
            "relayed_sha256": meta["relayed_sha256"], "notice_sha256": meta["notice_sha256"]}
    version = 1 if last is None else last["version"] + 1
    content = {"version": version, "work_key": work,
               "policy": {"id": principal, "version": POLICY_VERSION, "digest": policy.digest},
               "rules_version": RULES_VERSION, "items": [item]}
    if len(secretary_store.canonical(content).encode("utf-8")) > MAX_BRIEF_BYTES:
        return None
    state.execute("INSERT INTO briefs VALUES (?, ?, ?, ?, ?)",
                  (principal, work, version, secretary_store.canonical(content), secretary_store.digest(content)))
    return work, version


def _over_limit(meta: dict, text: str) -> bool:
    return meta["artifact_over_limit"] or len(text.encode("utf-8")) > messages.MAX_MESSAGE_BYTES


@dataclass
class Projection:
    """Interpret and record sources inside the caller's sidecar transaction."""

    state: Connection
    baton: Connection
    config: Config
    policy: Policy

    def start(self, meta: dict) -> bool:
        if meta["version"] != sources.ENVELOPE_VERSION:
            raise ConfigError("unsupported secretary envelope version")
        return secretary_store.put_item(self.state, self.policy.principal, meta)

    def interpret(self, meta: dict, text: str, judge: Judge | None = None) -> tuple[str, str, list[str]]:
        state, baton, config, policy = self.state, self.baton, self.config, self.policy
        principal = policy.principal
        ledger = Expectations(state, principal)
        kind, reason, answers = "retain", "no new principal action", []
        if meta.get("operator_cancel"):
            expected = state.execute("SELECT * FROM expectations WHERE principal=? AND id=?",
                                     (principal, meta["expectation_id"])).fetchone()
            if expected is None:
                raise InvalidRequest("expectation to cancel does not exist")
            if expected["state"] == "open":
                if not ledger.close(expected, meta, "cancelled", "operator-cancel"):
                    raise InvalidRequest("cancellation evidence limit reached")
                kind, reason = "retain", "operator cancelled expectation"
            else:
                kind, reason = "retain", "expectation already closed or evidence limit reached"
        elif _over_limit(meta, text):
            kind, reason = "reconcile", "source exceeds a bounded input limit"
        elif meta["event_kind"] == "message" and judge is not None and judge.wants(policy, meta):
            kind, reason, answers = judge.outcome(state, baton, config, policy, meta, text)
        elif meta["event_kind"] == "message":
            rules = MessageRules(ledger, baton, config, policy)
            kind, reason, answers = rules.interpret(meta, text)
            if sources._actor(meta) == policy.principal:
                kind, reason = rules.await_recipient(meta, text, (kind, reason))
        elif meta["event_kind"] in {"job.created", "job.moved"}:
            kind, reason = job_disposition(ledger, config, policy, meta)
        return kind, reason, answers

    def record(self, meta: dict, outcome: tuple[str, str, list[str]]) -> None:
        state, policy = self.state, self.policy
        principal = policy.principal
        kind, reason, answers = outcome
        brief = _brief(state, policy, meta, kind, reason, answers)
        if brief is None:
            kind, reason = "reconcile", "brief bound reached; inspect source and reconsider"
        state.execute("INSERT INTO dispositions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (principal, meta["source_key"], policy.digest, kind, reason, RULES_VERSION,
                       sources.ENVELOPE_VERSION, brief[0] if brief else None, brief[1] if brief else None))

    def process(self, meta: dict, text: str, judge: Judge | None = None) -> bool:
        if not self.start(meta):
            return False
        self.record(meta, self.interpret(meta, text, judge))
        return True


class ScanBatch:
    """A bounded page, its consistent source snapshot and atomic sidecar progress.

    All methods run on one database thread. A live judgment may await a command
    between prepare and finish; cancellation exits the batch with a rollback.
    """

    def __init__(self, config: Config, state_path: Path, policy_path: Path, limit: int,
                 inject: Callable[[str], None] | None = None) -> None:
        if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
            raise InvalidRequest(f"scan limit must be 1-{MAX_BATCH}")
        self.config, self.state_path, self.policy_path = config, state_path, policy_path
        self.limit, self.inject = limit, inject
        self.stack = ExitStack()
        self.added = self.invisible = 0
        self.seen: list[int] = []
        self.stopped = False

    def __enter__(self) -> ScanBatch:
        try:
            self.baton = self.stack.enter_context(connect_read_only(self.config.db_path))
            self.stack.enter_context(self.baton.snapshot())
            self.policy = load_policy(self.config, self.baton, self.policy_path)
            epoch = sources._epoch(self.baton)
            self.state = self.stack.enter_context(secretary_store.open_state(self.state_path, self.config.data_dir))
            self.stack.enter_context(self.state.transaction())
            self.through = secretary_store.source_position(self.state, self.policy.principal, epoch)
            _check_stored_envelope_version(self.state, self.policy.principal)
            self.rows = sources._source_rows(self.baton, self.policy.principal, self.through, self.limit)
            self.projection = Projection(self.state, self.baton, self.config, self.policy)
            return self
        except BaseException:
            self.stack.__exit__(*sys.exc_info())
            raise

    def __exit__(self, *error: object) -> None:
        self.stack.__exit__(*error)

    def _read(self, event: dict, judge: Judge | secretary_judge.JudgeSession | None) -> tuple[dict, str] | None:
        mapped = sources._envelope(self.baton, self.config, self.policy.principal, event)
        if mapped is None:
            self.invisible += 1
            self._accept(event)
            return None
        meta, text = mapped
        if judge is not None and _waits_for_judge(self.state, self.policy, meta, text, judge):
            self.stopped = True
            return None
        return meta, text

    def _accept(self, event: dict) -> None:
        self.seen.append(event["pos"])
        if self.inject:
            self.inject("mid_batch")

    def step(self, event: dict, judge: Judge | None = None) -> None:
        mapped = self._read(event, judge)
        if mapped is not None:
            self.added += self.projection.process(*mapped, judge)
            self._accept(event)

    def prepare(self, event: dict, judge: secretary_judge.JudgeSession | None) -> secretary_judge.Judgment | None:
        """Apply deterministic sources, or return the one question that needs an external answer."""
        mapped = self._read(event, judge)
        if mapped is None:
            return None
        meta, text = mapped
        if judge is not None and judge.wants(self.policy, meta) and not _over_limit(meta, text):
            if self.projection.start(meta):
                return secretary_judge.Judgment(self.state, self.baton, self.config, self.policy, meta, text)
        else:
            self.added += self.projection.process(meta, text)
        self._accept(event)
        return None

    def finish(self, event: dict, judgment: secretary_judge.Judgment,
               verdict: secretary_judge.Verdict | None, status: str) -> None:
        self.projection.record(judgment.meta, judgment.apply(verdict, status))
        self.added += 1
        self._accept(event)

    def result(self, include_export: bool) -> dict:
        if self.seen:
            secretary_store.set_position(self.state, self.policy.principal, self.seen[-1])
        if self.inject:
            self.inject("before_commit")
        result = {"principal": self.policy.principal, "policy_digest": self.policy.digest,
                  "events_seen": len(self.seen), "items_added": self.added, "invisible": self.invisible,
                  "through_pos": self.seen[-1] if self.seen else self.through}
        if include_export:
            result["export_sha256"] = secretary_store.export_digest(self.state, self.policy.principal)
        return result

    async def run(self, database: DatabaseWorker, judge: secretary_judge.JudgeSession | None,
                  include_export: bool) -> dict:
        """Await judgments while all database operations stay on the same owner thread."""
        try:
            await database.call(self.__enter__)
            for event in self.rows:
                judgment = await database.call(self.prepare, event, judge)
                if self.stopped:
                    break
                if judgment is not None:
                    request = await database.call(judgment.request)
                    verdict, status = await judge.ask(self.policy, request, len(judgment.candidates))
                    await database.call(self.finish, event, judgment, verdict, status)
            result = await database.call(self.result, include_export)
        except BaseException:
            await database.call(self.__exit__, *sys.exc_info())
            raise
        else:
            await database.call(self.__exit__, None, None, None)
            return result


def scan(config: Config, state_path: Path, policy_path: Path, *, limit: int = sources.DEFAULT_BATCH,
         inject: Callable[[str], None] | None = None, judge: Judge | None = None,
         include_export: bool = True) -> dict:
    """Synchronous offline scan. Live decisions use scan_async to await the judge."""
    with ScanBatch(config, state_path, policy_path, limit, inject) as batch:
        for event in batch.rows:
            batch.step(event, judge)
            if batch.stopped:
                break
        return batch.result(include_export)


async def scan_async(config: Config, state_path: Path, policy_path: Path, *, limit: int = sources.DEFAULT_BATCH,
                     inject: Callable[[str], None] | None = None, judge: secretary_judge.JudgeSession | None = None,
                     include_export: bool = True, database: DatabaseWorker | None = None) -> dict:
    """Scan one page, borrowing a decision's database worker or owning one for a standalone scan."""
    batch = ScanBatch(config, state_path, policy_path, limit, inject)
    if database is not None:
        return await batch.run(database, judge, include_export)
    async with DatabaseWorker() as database:
        return await batch.run(database, judge, include_export)


def _waits_for_judge(state: Connection, policy: Policy, meta: dict, text: str, judge: Judge) -> bool:
    """A new message the judge would read once its budget is gone, past the wake's own event: the scan stops before
    it and reports itself behind, rather than leave the message to the word lists for want of time."""
    return (judge.wants(policy, meta) and judge.stops_before(meta) and not _over_limit(meta, text)
            and state.execute("SELECT 1 FROM items WHERE principal=? AND source_key=?",
                              (policy.principal, meta["source_key"])).fetchone() is None)


def _check_stored_envelope_version(state: Connection, principal: str) -> None:
    first = state.execute("SELECT envelope FROM items WHERE principal=? ORDER BY seq LIMIT 1",
                          (principal,)).fetchone()
    if first is not None and json.loads(first["envelope"])["version"] != sources.ENVELOPE_VERSION:
        raise ConfigError("secretary state envelope version differs from this kernel; use a new state file")


def _stored_epoch(state: Connection, principal: str, epoch: str) -> int:
    row = state.execute("SELECT log_epoch, through_pos FROM sources WHERE principal=?", (principal,)).fetchone()
    if row is None:
        raise InvalidRequest("principal has no secretary scan state")
    if row["log_epoch"] != epoch:
        raise ConfigError("secretary state log_epoch differs from Baton")
    _check_stored_envelope_version(state, principal)
    return row["through_pos"]


def _existing_state(config: Config, path: Path) -> Connection:
    if not path.exists():
        raise InvalidRequest("secretary state file does not exist; run scan first")
    return secretary_store.open_state(path, config.data_dir)


def _text_for_replay(baton: Connection, config: Config, principal: str, meta: dict) -> str | None:
    message_id = meta["message_id"]
    if message_id is None:
        return ""
    row = sources._visible_message(baton, config, principal, message_id)
    if row is None:
        return None
    if sources._sha_text(row["text"]) != meta["text_sha256"]:
        raise InvalidRequest("a retained source changed after scanning")
    return row["text"]


def replay(config: Config, state_path: Path, policy_path: Path) -> dict:
    """Recompute from retained references in memory and compare; never rewrite stored records."""
    with connect_read_only(config.db_path) as baton, baton.snapshot():
        policy = load_policy(config, baton, policy_path)
        epoch = sources._epoch(baton)
        with _existing_state(config, state_path) as state:
            through = _stored_epoch(state, policy.principal, epoch)
            rows = state.execute("SELECT envelope FROM items WHERE principal=? ORDER BY seq",
                                 (policy.principal,)).fetchall()
            previous = {row["source_key"]: dict(row) for row in state.execute(
                "SELECT source_key, kind, reason, policy_digest FROM dispositions WHERE principal=?",
                (policy.principal,))}
            # Each judged message reads as it was first judged: replay never runs the judge.
            judged = secretary_judge.RecordedJudgments({row["source_key"]: row for row in state.execute(
                "SELECT * FROM judgments WHERE principal=?", (policy.principal,))})
            inputs = []
            unavailable = []
            for row in rows:
                meta = json.loads(row["envelope"])
                source_text = _text_for_replay(baton, config, policy.principal, meta)
                if source_text is None:
                    unavailable.append({"source_key": meta["source_key"],
                                        "stored": previous.get(meta["source_key"]),
                                        "replayed": None, "reason": "source no longer visible"})
                else:
                    inputs.append((meta, source_text))
            if unavailable:
                return {"principal": policy.principal, "policy_digest": policy.digest,
                        "differences": unavailable,
                        "stored_sha256": secretary_store.export_digest(state, policy.principal),
                        "replayed_sha256": None}
            with secretary_store.memory() as trial:
                with trial.transaction():
                    secretary_store.source_position(trial, policy.principal, epoch)
                    for meta, source_text in inputs:
                        Projection(trial, baton, config, policy).process(meta, source_text, judged)
                    secretary_store.set_position(trial, policy.principal, through)
                now = {row["source_key"]: dict(row) for row in trial.execute(
                    "SELECT source_key, kind, reason, policy_digest FROM dispositions WHERE principal=?",
                    (policy.principal,))}
                changed = [{"source_key": key, "stored": previous.get(key), "replayed": value}
                           for key, value in now.items()
                           if previous.get(key) is None or
                           (previous[key]["kind"], previous[key]["reason"]) != (value["kind"], value["reason"])]
                return {"principal": policy.principal, "policy_digest": policy.digest, "differences": changed,
                        "stored_sha256": secretary_store.export_digest(state, policy.principal),
                        "replayed_sha256": secretary_store.export_digest(trial, policy.principal)}


def _excerpt(baton: Connection, config: Config, principal: str, meta: dict) -> dict:
    if meta["message_id"] is None:
        return {"authored_excerpt": None, "relayed": [], "notice": None}
    row = sources._visible_message(baton, config, principal, meta["message_id"])
    if row is None:
        return {"authored_excerpt": None, "relayed": [], "notice": None, "visibility": "no longer visible"}
    shaped = messages.as_dict(row)
    return {"authored_excerpt": shaped["text"][:200],
            "relayed": [{"source": entry["source"], "at": entry["at"], "excerpt": entry["text"][:200]}
                        for entry in (shaped["relayed"] or [])[:8]],
            "notice": shaped["notice"]}


def show(config: Config, state_path: Path, policy_path: Path, *, source_key: str | None = None) -> dict:
    with connect_read_only(config.db_path) as baton, baton.snapshot():
        policy = load_policy(config, baton, policy_path)
        with _existing_state(config, state_path) as state:
            through = _stored_epoch(state, policy.principal, sources._epoch(baton))
            @cache
            def visible(key: str) -> bool:
                source = state.execute("SELECT envelope FROM items WHERE principal=? AND source_key=?",
                                       (policy.principal, key)).fetchone()
                if source is None:
                    return False
                envelope = json.loads(source["envelope"])
                if envelope["event_kind"] == "operator.cancel":
                    opening = state.execute("SELECT opening_source FROM expectations WHERE principal=? AND id=?",
                                            (policy.principal, envelope["expectation_id"])).fetchone()
                    return opening is not None and visible(opening["opening_source"])
                if envelope["message_id"]:
                    return sources._visible_message(baton, config, policy.principal,
                                                    envelope["message_id"]) is not None
                visible_directly = (envelope["event_audience"] == policy.principal
                                    or envelope["event_actor"] == policy.principal)
                if envelope["event_kind"] in {"job.created", "job.moved"}:
                    tracked = baton.execute(
                        "SELECT 1 FROM events WHERE job_id=? AND actor=? AND pos<?"
                        " AND kind IN ('job.created','job.moved') LIMIT 1",
                        (envelope["job_id"], policy.principal, envelope["event_pos"])).fetchone()
                    return visible_directly or tracked is not None
                if envelope["conversation_id"]:
                    return board_read.may_read_conversation(baton, config, policy.principal,
                                                            envelope["conversation_id"])
                return visible_directly

            if source_key:
                rows = state.execute("SELECT envelope FROM items WHERE principal=? AND source_key=?",
                                     (policy.principal, source_key)).fetchall()
                if not rows or not visible(source_key):
                    raise InvalidRequest("source not found for principal")
            else:
                rows = state.execute("SELECT envelope FROM items WHERE principal=? ORDER BY seq DESC LIMIT 100",
                                     (policy.principal,)).fetchall()
            items = []
            for row in rows:
                envelope = json.loads(row["envelope"])
                if visible(envelope["source_key"]):
                    items.append({"envelope": envelope,
                                  "excerpt": _excerpt(baton, config, policy.principal, envelope)})
            expectations = [dict(row) for row in state.execute(
                "SELECT * FROM expectations WHERE principal=? ORDER BY id LIMIT 500", (policy.principal,))
                if visible(row["opening_source"])]
            visible_expectations = {row["id"] for row in expectations}
            evidence = [dict(row) for row in state.execute(
                "SELECT * FROM expectation_evidence WHERE principal=?"
                " ORDER BY expectation_id, source_key LIMIT 500",
                (policy.principal,)) if row["expectation_id"] in visible_expectations
                and visible(row["source_key"])]
            dispositions = [dict(row) for row in state.execute(
                "SELECT * FROM dispositions WHERE principal=? ORDER BY source_key LIMIT 100",
                (policy.principal,)) if visible(row["source_key"])]
            briefs = []
            for row in state.execute("SELECT * FROM briefs WHERE principal=?"
                                     " ORDER BY work_key, version DESC LIMIT 100", (policy.principal,)):
                content = json.loads(row["content"])
                count = len(content["items"])
                content["items"] = [item for item in content["items"] if visible(item["source_key"])]
                if content["items"]:
                    briefs.append({"work_key": row["work_key"], "version": row["version"],
                                   "content": content, "stored_digest": row["digest"],
                                   "visibility_limited": len(content["items"]) != count})
            return {"principal": policy.principal, "through_pos": through, "items": items,
                    "expectations": expectations, "evidence": evidence,
                    "dispositions": dispositions, "briefs": briefs,
                    "export_sha256": secretary_store.export_digest(state, policy.principal)}


def cancel(config: Config, state_path: Path, policy_path: Path, expectation_id: str, reason: str) -> dict:
    if not reason.strip() or len(reason) > 200:
        raise InvalidRequest("cancellation reason must be 1-200 characters")
    with connect_read_only(config.db_path) as baton, baton.snapshot():
        policy = load_policy(config, baton, policy_path)
        epoch = sources._epoch(baton)
        with _existing_state(config, state_path) as state:
            with state.transaction():
                _stored_epoch(state, policy.principal, epoch)
                expected = state.execute("SELECT * FROM expectations WHERE principal=? AND id=?",
                                         (policy.principal, expectation_id)).fetchone()
                if expected is None:
                    raise InvalidRequest("expectation not found for principal")
                source_key = "operator:cancel:" + secretary_store.digest(
                    {"principal": policy.principal, "expectation_id": expectation_id, "reason": reason})[:24]
                meta = {"version": sources.ENVELOPE_VERSION, "source_key": source_key, "event_pos": None,
                        "event_kind": "operator.cancel", "event_actor": "operator",
                        "event_audience": policy.principal,
                        "at": None, "identity": {"from_role": "operator", "author_kind": None,
                                                  "alias_id": None, "alias": None, "code": None},
                        "conversation_id": None, "job_id": expected["task_ref"][4:] if expected["task_ref"]
                        and expected["task_ref"].startswith("job:") else None,
                        "job": None, "conversation": None, "can_view_all_conversations": False,
                        "artifacts": [], "artifact_over_limit": False, "message_id": None, "seq": None,
                        "recipient": policy.principal, "conversation_seq": None, "reply_to": None,
                        "attention": None, "needs_decision": False, "text_sha256": None,
                        "relayed_sha256": None, "notice_sha256": None, "relayed_count": 0, "has_notice": False,
                        "operator_cancel": True, "expectation_id": expectation_id, "reason": reason}
                created = Projection(state, baton, config, policy).process(meta, "")
            return {"principal": policy.principal, "expectation_id": expectation_id,
                    "source_key": source_key, "created": created,
                    "export_sha256": secretary_store.export_digest(state, policy.principal)}
