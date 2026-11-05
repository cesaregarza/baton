"""Offline, host-only attention projection over Baton's retained records.

The authoritative database is opened read-only. This module never reads a mailbox through a cursor API,
never grants an identity authority, and never queues or delivers a wake.

Package map: policy owns validated policy values; sources owns visible source records and
envelopes. Scan, replay, expectation rules, and state transitions remain here. Existing
imports remain aliases to their owners; companion interfaces use the leaf modules directly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from baton import board_read, messages, secretary_store
from baton.config import Config, ConfigError
from baton.database import Connection, Row
from baton.errors import InvalidRequest
from baton.store import connect_read_only

from .policy import BRIEF_BYTES as BRIEF_BYTES
from .policy import DEFAULT_ACKS as DEFAULT_ACKS
from .policy import DEFAULT_ANSWERS as DEFAULT_ANSWERS
from .policy import JUDGE_THRESHOLDS as JUDGE_THRESHOLDS
from .policy import MAX_POLICY_BYTES as MAX_POLICY_BYTES
from .policy import POLICY_VERSION as POLICY_VERSION
from .policy import Policy as Policy
from .policy import _finite as _finite
from .policy import _judge_block as _judge_block
from .policy import _words as _words
from .policy import load_policy as load_policy
from .sources import ARTIFACT as ARTIFACT
from .sources import DEFAULT_BATCH as DEFAULT_BATCH
from .sources import ENVELOPE_VERSION as ENVELOPE_VERSION
from .sources import MAX_CITATIONS as MAX_CITATIONS
from .sources import SUPERSEDES as SUPERSEDES
from .sources import _actor as _actor
from .sources import _artifacts as _artifacts
from .sources import _conversation as _conversation
from .sources import _envelope as _envelope
from .sources import _epoch as _epoch
from .sources import _event_message as _event_message
from .sources import _job as _job
from .sources import _request_artifact as _request_artifact
from .sources import _sha_text as _sha_text
from .sources import _source_rows as _source_rows
from .sources import _supersedes as _supersedes
from .sources import _visible_message as _visible_message

RULES_VERSION = 7
MAX_BATCH = 1000
MAX_OPEN = 500
MAX_EVIDENCE = 64
MAX_BRIEF_BYTES = 16 * 1024
ACTIVE = ("open",)


class Judge(Protocol):
    """What `scan` asks of a judge: secretary_judge.Session in `decide`, secretary_judge.Stored in `replay`."""

    def wants(self, policy: Policy, meta: dict) -> bool:
        """Whether this source goes to the judge rather than to the word lists."""

    def stops_before(self, meta: dict) -> bool:
        """Whether its budget is gone and the scan should stop before this source rather than read it."""

    def outcome(self, state: Connection, baton: Connection, config: Config, policy: Policy,
                meta: dict, text: str) -> tuple[str, str, list[str]]:
        """The source's disposition, reason and answer words, with both verdicts recorded."""


def _tokens(value: str) -> list[str]:
    return re.findall(r"\w+", value.casefold())


def _contains(words: list[str], phrase: str) -> bool:
    needle = _tokens(phrase)
    return any(words[index:index + len(needle)] == needle for index in range(len(words) - len(needle) + 1))


def _signals(text: str, policy: Policy) -> tuple[list[str], list[str]]:
    words = _tokens(text)
    return ([word for word in policy.answer_words if _contains(words, word)],
            [word for word in policy.ack_words if _contains(words, word)])


def _result_kind(text: str) -> str:
    """Conservative built-in kind vocabulary. Ambiguous kinds cannot close typed requests."""
    words = set(_tokens(text))
    kinds = {kind for kind, markers in (("check", {"ci", "check", "test"}),
                                        ("review", {"review"}), ("release", {"release"}))
             if words & markers}
    return next(iter(kinds)) if len(kinds) == 1 else "ambiguous" if kinds else "general"


def _kind_matches(expected: str, observed: str) -> bool:
    return observed == expected or (expected in {"check", "general"} and observed == "general")


def _artifact_match(expected: Row, observed: list[dict]) -> str:
    key, rev = expected["artifact_key"], expected["artifact_rev"]
    if key is None:
        return "none"
    same_key = [item["rev"] for item in observed if item["key"] == key]
    if not same_key:
        return "none"
    matching = [value for value in same_key if _revision_matches(value, rev)]
    if matching and len(same_key) == 1:
        return "match"
    return "uncertain" if matching else "stale"


def _revision_matches(left: str, right: str) -> bool:
    return min(len(left), len(right)) >= 7 and (left.startswith(right) or right.startswith(left))


def _work_key(meta: dict) -> str:
    if meta["job_id"]:
        return f"job:{meta['job_id']}"
    if meta["artifacts"]:
        return f"artifact:{meta['artifacts'][0]['key']}"
    if meta["conversation_id"]:
        return f"conversation:{meta['conversation_id']}"
    return meta["source_key"]


def _task_ref(meta: dict) -> str | None:
    return (f"job:{meta['job_id']}" if meta["job_id"] else
            f"conversation:{meta['conversation_id']}" if meta["conversation_id"] else None)


def _evidence(state: Connection, principal: str, expected: Row, meta: dict,
              kind: str, rule: str, observed_kind: str | None = None) -> bool:
    count = state.execute("SELECT COUNT(*) FROM expectation_evidence WHERE principal=? AND expectation_id=?",
                          (principal, expected["id"])).fetchone()[0]
    closure = kind in {"satisfied", "cancelled", "superseded"}
    if count >= MAX_EVIDENCE - (0 if closure else 1):
        return False
    artifact = meta["artifacts"][0] if len(meta["artifacts"]) == 1 else None
    secretary_store.add_evidence(state, principal, expected["id"], meta["source_key"], kind, rule,
                                 (artifact["key"], artifact["rev"]) if artifact else (None, None),
                                 observed_kind)
    return True


def _close(state: Connection, principal: str, expected: Row, meta: dict,
           status: str, rule: str) -> bool:
    if not _evidence(state, principal, expected, meta, status, rule):
        return False
    state.execute("UPDATE expectations SET state=?, closed_source=? WHERE principal=? AND id=? AND state='open'",
                  (status, meta["source_key"], principal, expected["id"]))
    return True


def _open(state: Connection, policy: Policy, meta: dict, direction: str, requester: str | None,
          responder: str, artifact: dict | None, opening_message_id: str | None,
          expected_kind: str) -> str | None:
    principal = policy.principal
    count = state.execute("SELECT COUNT(*) FROM expectations WHERE principal=? AND state='open'",
                          (principal,)).fetchone()[0]
    if count >= MAX_OPEN:
        return None
    key, rev = (artifact["key"], artifact["rev"]) if artifact else (None, None)
    identity = {"direction": direction, "source": meta["source_key"], "responder": responder,
                "artifact": artifact, "expected_kind": expected_kind}
    expectation_id = secretary_store.digest(identity)[:24]
    predecessor = meta["supersedes"]
    if predecessor is not None and key == predecessor["key"] and not _revision_matches(rev, predecessor["rev"]):
        candidates = state.execute("SELECT * FROM expectations WHERE principal=? AND state='open' AND direction=?"
                                   " AND requester IS ? AND responder=? AND artifact_key=? AND expected_kind=?",
                                   (principal, direction, requester, responder, key, expected_kind)).fetchall()
        matching = [row for row in candidates if _revision_matches(row["artifact_rev"], predecessor["rev"])]
        if len(matching) == 1:
            _close(state, principal, matching[0], meta, "superseded", "explicit-predecessor")
    state.execute("INSERT OR IGNORE INTO expectations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL)",
                  (principal, expectation_id, direction, requester, responder, _task_ref(meta), key, rev,
                   expected_kind, meta["source_key"], opening_message_id))
    return expectation_id


def _owe(state: Connection, policy: Policy, meta: dict, requester: str | None, artifact: dict | None,
         opening_message_id: str | None, expected_kind: str, outcome: tuple[str, str]) -> tuple[str, str]:
    """Open the item the principal owes, and return `outcome`, the message's outcome with room, either way.

    A full ledger may cost the tracking, never the attention. Holding an owed request because MAX_OPEN items are open
    would let the count decide whether the principal hears of it, so the outcome stands, nothing is inserted, and the
    reason says the item is not tracked. An item the principal awaits still reconciles at the limit: there the hold
    adds attention instead of taking it away.
    """
    if _open(state, policy, meta, "owed", requester, policy.principal, artifact, opening_message_id,
             expected_kind) is not None:
        return outcome
    return outcome[0], f"{outcome[1]}; not tracked: open expectation limit reached"


def _brief(state: Connection, policy: Policy, meta: dict, kind: str,
           reason: str, answers: list[str]) -> tuple[str, int] | None:
    principal, work = policy.principal, _work_key(meta)
    last = state.execute("SELECT version FROM briefs WHERE principal=? AND work_key=?"
                         " ORDER BY version DESC LIMIT 1", (principal, work)).fetchone()
    source = meta["source_key"]
    author = _actor(meta)
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


def _job_event_origin(expected: Row) -> bool:
    return expected["opening_source"].startswith("event:")


def _job_next_actor_changed(config: Config, meta: dict, job: dict) -> bool:
    if meta["event_kind"] != "job.moved":
        return False
    previous = config.jobs.states.get(job["event_from_state"])
    return previous is not None and previous.waiting_on != job["event_waiting_on"]


def _await_job_actor(state: Connection, policy: Policy, meta: dict,
                     job_ref: str, responder: str, changed: bool) -> tuple[str, str]:
    """Follow an event's next actor, preserving evidence for each prior awaited actor."""
    principal = policy.principal
    previous = state.execute("SELECT * FROM expectations WHERE principal=? AND task_ref=?"
                             " AND direction='awaited' AND state='open' ORDER BY id",
                             (principal, job_ref)).fetchall()
    existing = False
    limit = False
    for expected in previous:
        if not _job_event_origin(expected):
            continue
        if expected["responder"] == responder:
            existing = True
            if not _evidence(state, principal, expected, meta, "job-state", "job-moved"):
                limit = True
        elif changed:
            if not _close(state, principal, expected, meta, "superseded", "job-next-actor-changed"):
                limit = True
    if not existing and _open(state, policy, meta, "awaited", principal, responder,
                              None, None, "job") is None:
        return "reconcile", "open expectation limit reached"
    if limit:
        return "reconcile", "expectation evidence limit reached"
    return "route", "job waits on its existing actor"


def _close_returned_awaited(state: Connection, principal: str, meta: dict,
                            job_ref: str, changed: bool) -> bool:
    """Cite a job event when the next actor changes from a prior awaited actor."""
    limited = False
    rows = state.execute("SELECT * FROM expectations WHERE principal=? AND task_ref=?"
                         " AND direction='awaited' AND state='open'", (principal, job_ref)).fetchall()
    for expected in rows:
        if changed and _job_event_origin(expected) and not _close(
                state, principal, expected, meta, "superseded", "job-next-actor-changed"):
            limited = True
    return limited


def _choose_disposition(current: tuple[str, str], proposed: tuple[str, str]) -> tuple[str, str]:
    priority = {"retain": 0, "digest": 1, "route": 1, "brief": 2, "reconcile": 3}
    return proposed if (priority[proposed[0]] > priority[current[0]] or
                        current == ("retain", "no new principal action")) else current


def _uncertain_candidates(state: Connection, principal: str, meta: dict,
                          candidates: list[Row]) -> tuple[str, str]:
    limited = False
    for expected in candidates:
        if not _evidence(state, principal, expected, meta, "uncertain", "ambiguous-correlation"):
            limited = True
    return ("reconcile", "expectation evidence limit reached" if limited else
            "multiple open expectations match this message")


def _candidates(state: Connection, principal: str, meta: dict) -> list[Row]:
    """The open items a message could answer on structure alone: what its author owes in the same job or
    conversation and, for a reply, the item its parent opened."""
    rows = state.execute("SELECT * FROM expectations WHERE principal=? AND state='open' AND responder=? ORDER BY id",
                         (principal, _actor(meta))).fetchall()
    return [row for row in rows if row["task_ref"] == _task_ref(meta) and
            (not meta["reply_to"] or meta["reply_to"] == row["opening_message_id"])]


def _direct_request(state: Connection, principal: str, expected: Row) -> bool:
    """An item a principal's direct message opened (`_await_recipient`)."""
    if expected["direction"] != "awaited" or expected["opening_message_id"] is None:
        return False
    opening = state.execute("SELECT envelope FROM items WHERE principal=? AND source_key=?",
                            (principal, expected["opening_source"])).fetchone()
    return opening is not None and json.loads(opening["envelope"])["conversation_id"] is None


def _addressed(baton: Connection, config: Config, principal: str, meta: dict) -> bool:
    """A direct message to the principal, a post naming it in `attention`, or a reply to its own message."""
    parent = _visible_message(baton, config, principal, meta["reply_to"]) if meta["reply_to"] else None
    return ((meta["conversation_id"] is None and meta["recipient"] == principal)
            or principal in (meta["attention"] or []) or (parent is not None and parent["sender"] == principal))


def _await_recipient(state: Connection, policy: Policy, meta: dict, text: str,
                     outcome: tuple[str, str]) -> tuple[str, str]:
    """With a judge, a principal's direct message awaits its recipient, as a post's `attention` does, unless it
    answered one of the recipient's own requests. A direct message carries neither `attention` nor `reply_to`, so
    only the judge can tell what later answers it; the word lists leave such an item out of their candidates."""
    principal = policy.principal
    if (policy.judge is None or meta["conversation_id"] is not None or meta["recipient"] == principal
            or state.execute("SELECT 1 FROM expectation_evidence WHERE principal=? AND source_key=?"
                             " AND kind='satisfied'", (principal, meta["source_key"])).fetchone()):
        return outcome
    if _open(state, policy, meta, "awaited", principal, meta["recipient"], _request_artifact(meta),
             meta["message_id"], _result_kind(text)) is None:
        return _choose_disposition(outcome, ("reconcile", "open expectation limit reached"))
    return outcome


def _message_disposition(state: Connection, baton: Connection, config: Config,
                         policy: Policy, meta: dict, text: str) -> tuple[str, str, list[str]]:
    """The word lists' reading of one message."""
    principal = policy.principal
    kind, reason = "retain", "no new principal action"
    author = _actor(meta)
    answers, acks = _signals(text, policy)
    observed_kind = _result_kind(text)
    current_ref = _task_ref(meta)
    open_rows = state.execute("SELECT * FROM expectations WHERE principal=? AND state='open'"
                              " AND responder=? ORDER BY id", (principal, author)).fetchall()
    direct_pair = (meta["conversation_id"] is None and author == principal and
                   meta["recipient"] != principal)
    if direct_pair:
        pair = [row for row in open_rows if row["direction"] == "owed" and
                row["requester"] == meta["recipient"]]
        if len(pair) > 1:
            kind, reason = _uncertain_candidates(state, principal, meta, pair)
            return kind, reason, answers
        open_rows = [row for row in pair if row["task_ref"] == current_ref]
    else:
        open_rows = [row for row in _candidates(state, principal, meta)
                     if not _direct_request(state, principal, row)]
        if not meta["reply_to"] and not meta["artifacts"]:
            # An ordinary principal post is not an untagged answer to an owed request.
            open_rows = [row for row in open_rows if row["direction"] == "awaited"]
            if len(open_rows) > 1:
                kind, reason = _uncertain_candidates(state, principal, meta, open_rows)
                return kind, reason, answers
    matched = False
    for expected in open_rows:
        relation = _artifact_match(expected, meta["artifacts"])
        if relation == "none" and meta["artifacts"]:
            if direct_pair:
                if _evidence(state, principal, expected, meta, "uncertain", "unrelated-artifact"):
                    outcome = "reconcile", "artifact does not identify the direct request"
                else:
                    outcome = "reconcile", "expectation evidence limit reached"
                kind, reason = _choose_disposition((kind, reason), outcome)
            continue
        matched = True
        if relation == "stale":
            if not _evidence(state, principal, expected, meta, "stale", "artifact-revision"):
                outcome = "reconcile", "expectation evidence limit reached"
            else:
                outcome = "retain", "stale artifact revision retained"
        elif answers and not _kind_matches(expected["expected_kind"], observed_kind):
            if _evidence(state, principal, expected, meta, "kind-mismatch", "expected-result-kind",
                         observed_kind):
                outcome = "reconcile", "result kind does not satisfy expectation"
            else:
                outcome = "reconcile", "expectation evidence limit reached"
        elif relation == "uncertain" or len(answers) > 1 or (answers and acks):
            if not _evidence(state, principal, expected, meta, "uncertain", "conflicting-result"):
                outcome = "reconcile", "expectation evidence limit reached"
            else:
                outcome = "reconcile", "result is uncertain"
        elif answers and (relation == "match" or (expected["artifact_key"] is None and
                                                       (meta["reply_to"] == expected["opening_message_id"] or
                                                        direct_pair))):
            if _close(state, principal, expected, meta, "satisfied", f"answer:{answers[0].casefold()}"):
                outcome = ("brief" if expected["direction"] == "awaited" else "retain",
                           "expected result reported")
            else:
                outcome = "reconcile", "expectation evidence limit reached"
        elif acks:
            if _evidence(state, principal, expected, meta, "ack", f"ack:{acks[0].casefold()}"):
                outcome = ("digest" if expected["direction"] == "awaited" else "retain",
                           "acknowledgment; result still outstanding")
            else:
                outcome = "reconcile", "expectation evidence limit reached"
        else:
            if _evidence(state, principal, expected, meta, "uncertain", "no-result-word"):
                outcome = "reconcile", "result is uncertain"
            else:
                outcome = "reconcile", "expectation evidence limit reached"
        kind, reason = _choose_disposition((kind, reason), outcome)
    direct_incoming = (meta["conversation_id"] is None and meta["recipient"] == principal and
                       author is not None and author != principal)
    if not matched or direct_incoming:
        attention = meta["attention"] or []
        if author == principal and attention:
            responders = [role for role in attention if role != principal]
            if responders:
                for responder in responders:
                    if _open(state, policy, meta, "awaited", principal, responder,
                             _request_artifact(meta), meta["message_id"],
                             _result_kind(text)) is None:
                        kind, reason = "reconcile", "open expectation limit reached"
                        break
                else:
                    kind, reason = "retain", "existing responder owns the next action"
        elif author is not None and author != principal:
            if _addressed(baton, config, principal, meta):
                owed = _owe(state, policy, meta, author, _request_artifact(meta), meta["message_id"],
                            _result_kind(text), ("brief", "principal owes a response"))
                kind, reason = _choose_disposition((kind, reason), owed)
    return kind, reason, answers


def _job_disposition(state: Connection, config: Config, policy: Policy,
                     meta: dict) -> tuple[str, str]:
    principal = policy.principal
    kind, reason = "retain", "no new principal action"
    job = meta["job"]
    if job is not None:
        job_ref = f"job:{job['id']}"
        entered = config.jobs.states.get(job["event_state"])
        changed = _job_next_actor_changed(config, meta, job)
        if job["event_state"] in policy.terminal_job_states:
            for expected in state.execute("SELECT * FROM expectations WHERE principal=? AND task_ref=?"
                                          " AND state='open'", (principal, job_ref)).fetchall():
                if not _close(state, principal, expected, meta, "cancelled", "terminal-job-state"):
                    kind, reason = "reconcile", "expectation evidence limit reached"
            if kind != "reconcile":
                kind, reason = "retain", "terminal job state recorded"
        elif entered is not None and entered.terminal:
            limited = False
            for expected in state.execute("SELECT * FROM expectations WHERE principal=? AND task_ref=?"
                                          " AND state='open'", (principal, job_ref)).fetchall():
                if changed and _job_event_origin(expected) and not _close(
                        state, principal, expected, meta, "superseded", "job-next-actor-changed"):
                    limited = True
            kind, reason = (("reconcile", "expectation evidence limit reached") if limited else
                            ("retain", "terminal job state outside cancellation policy"))
        elif meta["event_audience"] == principal:
            return_limit = _close_returned_awaited(state, principal, meta, job_ref, changed)
            existing = next((row for row in state.execute(
                "SELECT * FROM expectations WHERE principal=? AND task_ref=?"
                " AND direction='owed' AND state='open' ORDER BY id", (principal, job_ref))
                if _job_event_origin(row)), None)
            if existing:
                if _evidence(state, principal, existing, meta, "job-state", "job-moved"):
                    kind, reason = "retain", "job still awaits principal"
                else:
                    kind, reason = "reconcile", "expectation evidence limit reached"
            else:
                kind, reason = _owe(state, policy, meta, meta["event_actor"], None, None, "job",
                                    ("brief", "job awaits principal"))
            if return_limit:
                kind, reason = "reconcile", "expectation evidence limit reached"
        elif job["event_waiting_on"]:
            handoff_limit = False
            for owed in state.execute("SELECT * FROM expectations WHERE principal=? AND task_ref=?"
                                      " AND direction='owed' AND state='open'",
                                      (principal, job_ref)).fetchall():
                if changed and _job_event_origin(owed) and not _close(
                        state, principal, owed, meta, "superseded", "job-next-actor-changed"):
                    handoff_limit = True
            kind, reason = _await_job_actor(state, policy, meta, job_ref, job["event_waiting_on"], changed)
            if handoff_limit:
                kind, reason = "reconcile", "expectation evidence limit reached"
        else:
            kind, reason = "route", "job waits on its existing actor"
    return kind, reason


def _over_limit(meta: dict, text: str) -> bool:
    return meta["artifact_over_limit"] or len(text.encode("utf-8")) > messages.MAX_MESSAGE_BYTES


def _process(state: Connection, baton: Connection, config: Config, policy: Policy,
             meta: dict, text: str, judge: Judge | None = None) -> bool:
    """Apply one source inside the caller's sidecar transaction. Returns whether it was new.

    A judge reads only a message inside every bound: the bounds, like the activation floor, the job-event policy and
    visibility, are decided before any judge is asked, and nothing it answers changes them.
    """
    principal = policy.principal
    if meta["version"] != ENVELOPE_VERSION:
        raise ConfigError("unsupported secretary envelope version")
    if not secretary_store.put_item(state, principal, meta):
        return False
    kind, reason, answers = "retain", "no new principal action", []
    if meta.get("operator_cancel"):
        expected = state.execute("SELECT * FROM expectations WHERE principal=? AND id=?",
                                 (principal, meta["expectation_id"])).fetchone()
        if expected is None:
            raise InvalidRequest("expectation to cancel does not exist")
        if expected["state"] == "open":
            if not _close(state, principal, expected, meta, "cancelled", "operator-cancel"):
                raise InvalidRequest("cancellation evidence limit reached")
            kind, reason = "retain", "operator cancelled expectation"
        else:
            kind, reason = "retain", "expectation already closed or evidence limit reached"
    elif _over_limit(meta, text):
        kind, reason = "reconcile", "source exceeds a bounded input limit"
    elif meta["event_kind"] == "message" and judge is not None and judge.wants(policy, meta):
        kind, reason, answers = judge.outcome(state, baton, config, policy, meta, text)
    elif meta["event_kind"] == "message":
        kind, reason, answers = _message_disposition(state, baton, config, policy, meta, text)
        if _actor(meta) == policy.principal:
            kind, reason = _await_recipient(state, policy, meta, text, (kind, reason))
    elif meta["event_kind"] in {"job.created", "job.moved"}:
        kind, reason = _job_disposition(state, config, policy, meta)
    brief = _brief(state, policy, meta, kind, reason, answers)
    if brief is None:
        kind, reason = "reconcile", "brief bound reached; inspect source and reconsider"
    state.execute("INSERT INTO dispositions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (principal, meta["source_key"], policy.digest, kind, reason, RULES_VERSION,
                   ENVELOPE_VERSION, brief[0] if brief else None, brief[1] if brief else None))
    return True


def scan(config: Config, state_path: Path, policy_path: Path, *, limit: int = DEFAULT_BATCH,
         inject: Callable[[str], None] | None = None, judge: Judge | None = None,
         include_export: bool = True) -> dict:
    """Interpret the next page of the principal's events. Only `decide` passes a judge: the catch-up scan, like
    every scan before activation, reads with the word lists alone. The public scan includes a full state
    digest for inspection; the decision loop requests only progress, without exporting historical state."""
    if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
        raise InvalidRequest(f"scan limit must be 1-{MAX_BATCH}")
    baton = connect_read_only(config.db_path)
    try:
        with baton.snapshot():
            policy = load_policy(config, baton, policy_path)  # before the sidecar is opened
            epoch = _epoch(baton)
            state = secretary_store.open_state(state_path, config.data_dir)
            try:
                with state.transaction():
                    through = secretary_store.source_position(state, policy.principal, epoch)
                    _check_stored_envelope_version(state, policy.principal)
                    rows = _source_rows(baton, policy.principal, through, limit)
                    added = invisible = 0
                    seen = []
                    for event in rows:
                        mapped = _envelope(baton, config, policy.principal, event)
                        if mapped is None:
                            invisible += 1
                        else:
                            meta, text = mapped
                            if judge is not None and _waits_for_judge(state, policy, meta, text, judge):
                                break  # the next decision's scan starts at this message, with a fresh budget
                            added += _process(state, baton, config, policy, meta, text, judge)
                        seen.append(event["pos"])
                        if inject:
                            inject("mid_batch")
                    if seen:
                        secretary_store.set_position(state, policy.principal, seen[-1])
                    if inject:
                        inject("before_commit")
                return {"principal": policy.principal, "policy_digest": policy.digest, "events_seen": len(seen),
                        "items_added": added, "invisible": invisible,
                        "through_pos": seen[-1] if seen else through,
                        **({"export_sha256": secretary_store.export_digest(state, policy.principal)}
                           if include_export else {})}
            finally:
                state.close()
    finally:
        baton.close()


def _waits_for_judge(state: Connection, policy: Policy, meta: dict, text: str, judge: Judge) -> bool:
    """A new message the judge would read once its budget is gone, past the wake's own event: the scan stops before
    it and reports itself behind, rather than leave the message to the word lists for want of time."""
    return (judge.wants(policy, meta) and judge.stops_before(meta) and not _over_limit(meta, text)
            and state.execute("SELECT 1 FROM items WHERE principal=? AND source_key=?",
                              (policy.principal, meta["source_key"])).fetchone() is None)


def _check_stored_envelope_version(state: Connection, principal: str) -> None:
    first = state.execute("SELECT envelope FROM items WHERE principal=? ORDER BY seq LIMIT 1",
                          (principal,)).fetchone()
    if first is not None and json.loads(first["envelope"])["version"] != ENVELOPE_VERSION:
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
    row = _visible_message(baton, config, principal, message_id)
    if row is None:
        return None
    if _sha_text(row["text"]) != meta["text_sha256"]:
        raise InvalidRequest("a retained source changed after scanning")
    return row["text"]


def replay(config: Config, state_path: Path, policy_path: Path) -> dict:
    """Recompute from retained references in memory and compare; never rewrite stored records."""
    baton = connect_read_only(config.db_path)
    try:
        with baton.snapshot():
            policy = load_policy(config, baton, policy_path)
            epoch = _epoch(baton)
            state = _existing_state(config, state_path)
            try:
                through = _stored_epoch(state, policy.principal, epoch)
                rows = state.execute("SELECT envelope FROM items WHERE principal=? ORDER BY seq",
                                     (policy.principal,)).fetchall()
                previous = {row["source_key"]: dict(row) for row in state.execute(
                    "SELECT source_key, kind, reason, policy_digest FROM dispositions WHERE principal=?",
                    (policy.principal,))}
                from baton import secretary_judge  # it builds on this module, so it is imported here, late

                # Each judged message reads as it was first judged: replay never runs the judge.
                judged = secretary_judge.Stored({row["source_key"]: row for row in state.execute(
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
                trial = secretary_store.memory()
                try:
                    with trial.transaction():
                        secretary_store.source_position(trial, policy.principal, epoch)
                        for meta, source_text in inputs:
                            _process(trial, baton, config, policy, meta, source_text, judged)
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
                finally:
                    trial.close()
            finally:
                state.close()
    finally:
        baton.close()


def _excerpt(baton: Connection, config: Config, principal: str, meta: dict) -> dict:
    if meta["message_id"] is None:
        return {"authored_excerpt": None, "relayed": [], "notice": None}
    row = _visible_message(baton, config, principal, meta["message_id"])
    if row is None:
        return {"authored_excerpt": None, "relayed": [], "notice": None, "visibility": "no longer visible"}
    shaped = messages.as_dict(row)
    return {"authored_excerpt": shaped["text"][:200],
            "relayed": [{"source": entry["source"], "at": entry["at"], "excerpt": entry["text"][:200]}
                        for entry in (shaped["relayed"] or [])[:8]],
            "notice": shaped["notice"]}


def show(config: Config, state_path: Path, policy_path: Path, *, source_key: str | None = None) -> dict:
    baton = connect_read_only(config.db_path)
    try:
        with baton.snapshot():
            policy = load_policy(config, baton, policy_path)
            state = _existing_state(config, state_path)
            try:
                through = _stored_epoch(state, policy.principal, _epoch(baton))
                visibility: dict[str, bool] = {}

                def visible(key: str) -> bool:
                    if key not in visibility:
                        source = state.execute("SELECT envelope FROM items WHERE principal=? AND source_key=?",
                                               (policy.principal, key)).fetchone()
                        if source is None:
                            visibility[key] = False
                        else:
                            envelope = json.loads(source["envelope"])
                            if envelope["event_kind"] == "operator.cancel":
                                opening = state.execute("SELECT opening_source FROM expectations WHERE principal=?"
                                                        " AND id=?", (policy.principal,
                                                                      envelope["expectation_id"])).fetchone()
                                visibility[key] = opening is not None and visible(opening["opening_source"])
                            elif envelope["message_id"]:
                                visibility[key] = _visible_message(baton, config, policy.principal,
                                                                   envelope["message_id"]) is not None
                            elif envelope["event_kind"] in {"job.created", "job.moved"}:
                                visible_directly = (envelope["event_audience"] == policy.principal
                                                    or envelope["event_actor"] == policy.principal)
                                tracked = baton.execute(
                                    "SELECT 1 FROM events WHERE job_id=? AND actor=? AND pos<?"
                                    " AND kind IN ('job.created','job.moved') LIMIT 1",
                                    (envelope["job_id"], policy.principal, envelope["event_pos"])).fetchone()
                                visibility[key] = visible_directly or tracked is not None
                            elif envelope["conversation_id"]:
                                visibility[key] = board_read.may_read_conversation(
                                    baton, config, policy.principal, envelope["conversation_id"])
                            else:
                                visibility[key] = (envelope["event_audience"] == policy.principal
                                                   or envelope["event_actor"] == policy.principal)
                    return visibility[key]

                if source_key:
                    rows = state.execute("SELECT envelope FROM items WHERE principal=? AND source_key=?",
                                         (policy.principal, source_key)).fetchall()
                    if not rows or not visible(source_key):
                        raise InvalidRequest("source not found for principal")
                else:
                    rows = state.execute("SELECT envelope FROM items WHERE principal=? ORDER BY seq DESC LIMIT 100",
                                         (policy.principal,)).fetchall()
                items = [{"envelope": json.loads(row["envelope"]),
                          "excerpt": _excerpt(baton, config, policy.principal, json.loads(row["envelope"]))}
                         for row in rows if visible(json.loads(row["envelope"])["source_key"])]
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
            finally:
                state.close()
    finally:
        baton.close()


def cancel(config: Config, state_path: Path, policy_path: Path, expectation_id: str, reason: str) -> dict:
    if not reason.strip() or len(reason) > 200:
        raise InvalidRequest("cancellation reason must be 1-200 characters")
    baton = connect_read_only(config.db_path)
    try:
        with baton.snapshot():
            policy = load_policy(config, baton, policy_path)
            epoch = _epoch(baton)
            state = _existing_state(config, state_path)
            try:
                with state.transaction():
                    _stored_epoch(state, policy.principal, epoch)
                    expected = state.execute("SELECT * FROM expectations WHERE principal=? AND id=?",
                                             (policy.principal, expectation_id)).fetchone()
                    if expected is None:
                        raise InvalidRequest("expectation not found for principal")
                    source_key = "operator:cancel:" + secretary_store.digest(
                        {"principal": policy.principal, "expectation_id": expectation_id, "reason": reason})[:24]
                    meta = {"version": ENVELOPE_VERSION, "source_key": source_key, "event_pos": None,
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
                    created = _process(state, baton, config, policy, meta, "")
                return {"principal": policy.principal, "expectation_id": expectation_id,
                        "source_key": source_key, "created": created,
                        "export_sha256": secretary_store.export_digest(state, policy.principal)}
            finally:
                state.close()
    finally:
        baton.close()
