"""Conservative message and job rules over the expectation ledger."""

from __future__ import annotations

import re
from dataclasses import dataclass

from baton.config import Config
from baton.database import Connection, Row

from .. import sources
from .expectations import EVIDENCE_LIMIT, OPEN_LIMIT, Expectations, artifact_match, task_ref
from .policy import Policy

NO_ACTION = ("retain", "no new principal action")
PRIORITY = {"retain": 0, "digest": 1, "route": 1, "brief": 2, "reconcile": 3}


def choose_disposition(current: tuple[str, str], proposed: tuple[str, str]) -> tuple[str, str]:
    if PRIORITY[proposed[0]] > PRIORITY[current[0]] or current == NO_ACTION:
        return proposed
    return current


def _tokens(value: str) -> list[str]:
    return re.findall(r"\w+", value.casefold())


@dataclass(frozen=True)
class WordSignals:
    """The answer words, acknowledgment words and result kind found in authored text."""

    answers: list[str]
    acknowledgments: list[str]
    kind: str

    @property
    def conflicting(self) -> bool:
        return len(self.answers) > 1 or bool(self.answers and self.acknowledgments)

    def matches_kind(self, expected: str) -> bool:
        if self.kind == expected:
            return True
        # Untyped answers can satisfy general/check requests, but not review or release requests.
        return self.kind == "general" and expected in {"check", "general"}


def signals(text: str, policy: Policy) -> WordSignals:
    words = _tokens(text)

    def contains(phrase: str) -> bool:
        needle = _tokens(phrase)
        return any(words[index:index + len(needle)] == needle for index in range(len(words) - len(needle) + 1))

    return WordSignals(answers=[word for word in policy.answer_words if contains(word)],
                       acknowledgments=[word for word in policy.ack_words if contains(word)],
                       kind=result_kind(text))


def result_kind(text: str) -> str:
    """Ambiguous kinds cannot close typed requests."""
    words = set(_tokens(text))
    kinds = [kind for kind, markers in (("check", {"ci", "check", "test"}),
                                       ("review", {"review"}), ("release", {"release"})) if words & markers]
    if len(kinds) == 1:
        return kinds[0]
    return "ambiguous" if kinds else "general"


def addressed(baton: Connection, config: Config, principal: str, meta: dict) -> bool:
    """A direct delivery, an attention tag, or a reply to the principal's own message."""
    if meta["conversation_id"] is None and meta["recipient"] == principal:
        return True
    if principal in (meta["attention"] or []):
        return True
    if not meta["reply_to"]:
        return False
    parent = sources._visible_message(baton, config, principal, meta["reply_to"])
    return parent is not None and parent["sender"] == principal


@dataclass(frozen=True)
class MessageRules:
    ledger: Expectations
    baton: Connection
    config: Config
    policy: Policy

    def interpret(self, meta: dict, text: str) -> tuple[str, str, list[str]]:
        """Match existing requests, record results, then open any newly addressed request."""
        principal, author = self.ledger.principal, sources._actor(meta)
        words = signals(text, self.policy)
        direct_reply = meta["conversation_id"] is None and author == principal and meta["recipient"] != principal
        candidates, ambiguous = self._candidates(meta, direct_reply)
        if ambiguous:
            outcome = self._uncertain(meta, candidates)
            return *outcome, words.answers

        outcome, matched = NO_ACTION, False
        for expected in candidates:
            relation = artifact_match(expected, meta["artifacts"])
            if relation == "none" and meta["artifacts"]:
                if direct_reply:
                    unrelated = self.ledger.record(expected, meta, "uncertain", "unrelated-artifact",
                                                   ("reconcile", "artifact does not identify the direct request"))
                    outcome = choose_disposition(outcome, unrelated)
                continue
            matched = True
            result = self._result(expected, meta, relation, words, direct_reply=direct_reply)
            outcome = choose_disposition(outcome, result)

        incoming = (meta["conversation_id"] is None and meta["recipient"] == principal
                    and author is not None and author != principal)
        if not matched or incoming:
            outcome = self._new_request(meta, words.kind, outcome)
        return *outcome, words.answers

    def _candidates(self, meta: dict, direct_reply: bool) -> tuple[list[Row], bool]:
        if direct_reply:
            # Direct messages have no reply pointer. There must be one possible request from
            # this recipient, even when the pair has open requests in different jobs.
            rows = [row for row in self.ledger.for_responder(self.ledger.principal)
                    if row["direction"] == "owed" and row["requester"] == meta["recipient"]]
            if len(rows) > 1:
                return rows, True
            return [row for row in rows if row["task_ref"] == task_ref(meta)], False
        rows = [row for row in self.ledger.candidates(meta) if not self.ledger.is_direct_request(row)]
        if not meta["reply_to"] and not meta["artifacts"]:
            # An ordinary principal post is not an untagged answer to an owed request.
            rows = [row for row in rows if row["direction"] == "awaited"]
            return rows, len(rows) > 1
        return rows, False

    def _uncertain(self, meta: dict, candidates: list[Row]) -> tuple[str, str]:
        outcome = "reconcile", "multiple open expectations match this message"
        for expected in candidates:
            outcome = self.ledger.record(expected, meta, "uncertain", "ambiguous-correlation", outcome)
        return outcome

    def _result(self, expected: Row, meta: dict, relation: str, words: WordSignals, *,
                direct_reply: bool) -> tuple[str, str]:
        """Apply guards in order: stale revision, wrong kind, conflicting result, answer, acknowledgment."""
        ledger = self.ledger
        if relation == "stale":
            return ledger.record(expected, meta, "stale", "artifact-revision",
                                 ("retain", "stale artifact revision retained"))
        if words.answers and not words.matches_kind(expected["expected_kind"]):
            return ledger.record(expected, meta, "kind-mismatch", "expected-result-kind",
                                 ("reconcile", "result kind does not satisfy expectation"), words.kind)
        if relation == "uncertain" or words.conflicting:
            return ledger.record(expected, meta, "uncertain", "conflicting-result",
                                 ("reconcile", "result is uncertain"))
        if words.answers and _identifies_request(expected, meta, relation, direct_reply=direct_reply):
            if not ledger.close(expected, meta, "satisfied", f"answer:{words.answers[0].casefold()}"):
                return EVIDENCE_LIMIT
            return ("brief" if expected["direction"] == "awaited" else "retain", "expected result reported")
        if words.acknowledgments:
            return ledger.record(expected, meta, "ack", f"ack:{words.acknowledgments[0].casefold()}",
                                 ("digest" if expected["direction"] == "awaited" else "retain",
                                  "acknowledgment; result still outstanding"))
        return ledger.record(expected, meta, "uncertain", "no-result-word", ("reconcile", "result is uncertain"))

    def _new_request(self, meta: dict, expected_kind: str, outcome: tuple[str, str]) -> tuple[str, str]:
        principal, author = self.ledger.principal, sources._actor(meta)
        if author is None:
            return outcome
        if author == principal:
            responders = [role for role in (meta["attention"] or []) if role != principal]
            if not responders:
                return outcome
            for responder in responders:
                opened = self.ledger.open(meta, direction="awaited", requester=principal, responder=responder,
                                          artifact=sources._request_artifact(meta),
                                          opening_message_id=meta["message_id"], expected_kind=expected_kind)
                if opened is None:
                    return OPEN_LIMIT
            return "retain", "existing responder owns the next action"
        if not addressed(self.baton, self.config, principal, meta):
            return outcome
        owed = self.ledger.owe(meta, requester=author, artifact=sources._request_artifact(meta),
                               opening_message_id=meta["message_id"], expected_kind=expected_kind,
                               outcome=("brief", "principal owes a response"))
        return choose_disposition(outcome, owed)

    def await_recipient(self, meta: dict, text: str, outcome: tuple[str, str]) -> tuple[str, str]:
        """With a judge, the principal's direct request can await an untagged reply.

        Word lists alone cannot correlate that reply; a message that already
        answered the recipient's request must not open another expectation.
        """
        if self.policy.judge is None:
            return outcome
        if meta["conversation_id"] is not None or meta["recipient"] == self.ledger.principal:
            return outcome
        if self.ledger.answered(meta):
            return outcome
        opened = self.ledger.open(meta, direction="awaited", requester=self.ledger.principal,
                                  responder=meta["recipient"], artifact=sources._request_artifact(meta),
                                  opening_message_id=meta["message_id"], expected_kind=result_kind(text))
        if opened is None:
            return choose_disposition(outcome, OPEN_LIMIT)
        return outcome


def _identifies_request(expected: Row, meta: dict, relation: str, *, direct_reply: bool) -> bool:
    if relation == "match":
        return True
    if expected["artifact_key"] is not None:
        return False  # A reply pointer alone cannot identify the requested revision.
    return direct_reply or meta["reply_to"] == expected["opening_message_id"]


def _job_event_origin(expected: Row) -> bool:
    return expected["opening_source"].startswith("event:")


def _supersede(ledger: Expectations, meta: dict, rows: list[Row], actor_changed: bool) -> bool:
    """Close each previous job actor, preserving requests opened by messages. Return whether all fit."""
    if not actor_changed:
        return True
    complete = True
    for expected in rows:
        if not _job_event_origin(expected):
            continue
        if not ledger.close(expected, meta, "superseded", "job-next-actor-changed"):
            complete = False
    return complete


def job_disposition(ledger: Expectations, config: Config, policy: Policy, meta: dict) -> tuple[str, str]:
    job = meta["job"]
    if job is None:
        return NO_ACTION
    principal, job_ref = ledger.principal, f"job:{job['id']}"
    entered = config.jobs.states.get(job["event_state"])
    previous = config.jobs.states.get(job["event_from_state"]) if meta["event_kind"] == "job.moved" else None
    actor_changed = previous is not None and previous.waiting_on != job["event_waiting_on"]
    if job["event_state"] in policy.terminal_job_states:
        outcome = "retain", "terminal job state recorded"
        for expected in ledger.for_job(job_ref):
            if not ledger.close(expected, meta, "cancelled", "terminal-job-state"):
                outcome = EVIDENCE_LIMIT
        return outcome
    if entered is not None and entered.terminal:
        if not _supersede(ledger, meta, ledger.for_job(job_ref), actor_changed):
            return EVIDENCE_LIMIT
        return "retain", "terminal job state outside cancellation policy"
    if meta["event_audience"] == principal:
        return _job_waits_on_principal(ledger, meta, job_ref, actor_changed)
    if job["event_waiting_on"]:
        complete = _supersede(ledger, meta, ledger.for_job(job_ref, "owed"), actor_changed)
        outcome = _await_job_actor(ledger, meta, job_ref, job["event_waiting_on"], actor_changed)
        if not complete:
            return EVIDENCE_LIMIT
        return outcome
    return "route", "job waits on its existing actor"


def _job_waits_on_principal(ledger: Expectations, meta: dict, job_ref: str,
                            actor_changed: bool) -> tuple[str, str]:
    complete = _supersede(ledger, meta, ledger.for_job(job_ref, "awaited"), actor_changed)
    existing = next((row for row in ledger.for_job(job_ref, "owed") if _job_event_origin(row)), None)
    if existing is None:
        outcome = ledger.owe(meta, requester=meta["event_actor"], artifact=None, opening_message_id=None,
                             expected_kind="job", outcome=("brief", "job awaits principal"))
    else:
        outcome = ledger.record(existing, meta, "job-state", "job-moved", ("retain", "job still awaits principal"))
    # Update this request even if superseding a previous actor ran out of evidence space.
    if not complete:
        return EVIDENCE_LIMIT
    return outcome


def _await_job_actor(ledger: Expectations, meta: dict, job_ref: str, responder: str,
                     actor_changed: bool) -> tuple[str, str]:
    existing, complete = False, True
    for expected in ledger.for_job(job_ref, "awaited"):
        if not _job_event_origin(expected):
            continue
        if expected["responder"] == responder:
            existing = True
            if not ledger.evidence(expected, meta, "job-state", "job-moved"):
                complete = False
            continue
        if actor_changed and not ledger.close(expected, meta, "superseded", "job-next-actor-changed"):
            complete = False
    if not existing:
        opened = ledger.open(meta, direction="awaited", requester=ledger.principal, responder=responder,
                             artifact=None, opening_message_id=None, expected_kind="job")
        if opened is None:
            return OPEN_LIMIT
    if not complete:
        return EVIDENCE_LIMIT
    return "route", "job waits on its existing actor"
