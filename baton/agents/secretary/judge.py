"""The secretary's judgment step: a host command reads one new message for the principal and makes four calls.

The policy's optional `judge` block names the command, an absolute argv, with its timeout, the sha256 of the question
file the command asks and the two thresholds below, so the policy digest freezes all of them. The package names no
provider and stores no message text: the text goes to the command on stdin and nowhere else. A failed, late or
malformed answer is never a verdict. The word lists decide that message, and its record says
`judge_fallback:<reason>`: error, timeout, invalid_json, wrong_shape or declined; budget when the decision had no
call or time left for a message it had to cover; or unoffered, below.

The request, on the command's stdin, is one JSON object:
    {"version": 1, "principal": ROLE, "questions_sha256": HEX, "timeout_seconds": SECONDS,
     "message": {"id", "from", "from_kind", "at", "channel", "conversation_id", "job_id", "attention", "reply_to",
                 "replies_to_principal", "artifacts", "relayed", "notice", "text"},
     "items": [{"id", "direction", "counterpart", "job_id", "artifact", "state", "title", "relayed"}, ...]}
`text` is the authored text only, never relayed text. `items` are at most 4 of the message's candidate items, the
most recently opened that the principal can still read, each with the first words of its opening message as
`title`. When the message has a candidate the judge was not offered, a verdict that ties it to none of the offered
items (relation `none`, or no `item`) is not used: the word lists decide the message, as they would without a judge,
and its record says `judge_fallback:unoffered`. A judge that saw only some candidates can't rule the others out, so
its "none" never keeps a message the word lists would hold for reconciliation. The verdict, on stdout, is one JSON
object:
    {"relation": "answers" | "acknowledges" | "changes_or_cancels" | "none", "item": ITEM_ID | null,
     "opens_owed": 0..1, "needs_now": 0..1}
or {"declined": REASON} when the command won't judge the message, for example private text.

How the four calls become a disposition, and so wake, hold or keep (`decide` wakes on brief, holds on digest and
reconcile, and keeps the rest):
    relation answers, on a candidate item        close the item: brief, so wake
    relation acknowledges, on a candidate item   evidence only: digest, so hold
    relation changes_or_cancels, on one          evidence only, never a closure: digest, so hold
    addressed, and opens_owed >= 0.5             open an owed item: brief, so wake; or digest, so hold, when
                                                 needs_now is below the policy's owed_digest_below (0.4)
    addressed, and opens_owed < 0.5              digest, so hold: an addressed message is never only kept
    needs_now at or above the policy's wake_at   brief, so wake, unless the outcome so far is brief or a bound
    (0.8)
    anything else                                retain, so keep
The strongest outcome wins: reconcile, then brief, then digest or route, then retain. A candidate is what the kernel
matches on structure alone: an open item awaited from the sender, in the same job or conversation, and opened by the
message replied to when there is one. So an answer always comes from the item's counterpart. A revision of the item's
artifact other than the one asked for is evidence, exactly as it is for the word lists, whatever the relation.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import NamedTuple

from baton import messages
from baton.config import Config
from baton.database import Connection, Row

from . import policy as policy_module
from . import sources
from . import store as secretary_store
from .command import JudgeCommand
from .expectations import EVIDENCE_LIMIT, Expectations, artifact_match
from .rules import NO_ACTION, MessageRules, addressed, choose_disposition, result_kind, signals

REQUEST_VERSION = 1
JUDGED = "judged"
FALLBACK = "judge_fallback:"
RELATIONS = ("answers", "acknowledges", "changes_or_cancels", "none")
CALLS = ("relation", "item", "opens_owed", "needs_now")
ASKS = 0.5              # opens_owed at or above this opens an owed item
OFFERED = 4             # the question set names item_1 to item_4
TITLE_WORDS = 30
MAX_OUTPUT = 64 * 1024
PER_DECISION = 2         # messages one decision may judge: a wake leaves the secretary only a few seconds
MIN_CALL_SECONDS = 1.0  # no call starts with less of the decision's budget left: a call takes about a second


class Verdict(NamedTuple):
    relation: str
    item: str | None
    opens_owed: float
    needs_now: float

    @property
    def relates_to_item(self) -> bool:
        return self.relation != "none" and self.item is not None

    @classmethod
    def parse(cls, output: bytes, offered: list[str]) -> tuple[Verdict | None, str]:
        try:
            value = json.loads(output)
        except ValueError:  # undecodable bytes too
            return None, FALLBACK + "invalid_json"
        invalid = None, FALLBACK + "wrong_shape"
        if not isinstance(value, dict):
            return invalid
        if set(value) == {"declined"} and isinstance(value["declined"], str):
            return None, FALLBACK + "declined"
        if set(value) != set(CALLS):
            return invalid
        if value["relation"] not in RELATIONS:
            return invalid
        if value["item"] is not None and value["item"] not in offered:
            return invalid
        if not _probability(value["opens_owed"]) or not _probability(value["needs_now"]):
            return invalid
        return cls(value["relation"], value["item"], float(value["opens_owed"]), float(value["needs_now"])), JUDGED


@dataclass
class JudgeSession:
    """One decision's judge: at most `calls_left` messages within the decision's budget, from the wake's own message.

    A message before that one had its own wake decided when it came, so the word lists read it, as they read the
    catch-up. Once the budget is spent, the scan stops before the next message the judge would read and reports
    itself behind, but only past the wake's own event (`trigger_pos`): the decision must still cover its wake, so up
    to that event the word lists read what the judge has no budget for. A conversation wake names no event, so its
    scan never stops for the judge.
    """

    deadline: float
    clock: Callable[[], float]
    start_pos: int | None = None
    trigger_pos: int | None = None
    calls_left: int = PER_DECISION
    calls: int = 0
    fallbacks: int = 0

    def wants(self, policy: policy_module.Policy, meta: dict) -> bool:
        return _judged_kind(policy, meta) and (self.start_pos is None or meta["event_pos"] >= self.start_pos)

    def stops_before(self, meta: dict) -> bool:
        return self._spent() and self.trigger_pos is not None and meta["event_pos"] > self.trigger_pos

    def _spent(self) -> bool:
        return self.calls_left <= 0 or self.deadline - self.clock() < MIN_CALL_SECONDS

    async def ask(self, policy: policy_module.Policy, request: dict,
                  candidate_count: int) -> tuple[Verdict | None, str]:
        """Spend this callback's budget on one question, falling back when it cannot be judged."""
        if self._spent():
            self.fallbacks += 1
            return None, FALLBACK + "budget"
        timeout = min(policy.judge["timeout_seconds"], self.deadline - self.clock())
        self.calls_left -= 1
        self.calls += 1
        offered = [item["id"] for item in request["items"]]
        command = JudgeCommand(policy.judge["command"], max_output=MAX_OUTPUT)
        payload = json.dumps({**request, "timeout_seconds": round(timeout, 3)}).encode("utf-8")
        try:
            code, output = await command.run(payload, timeout)
            verdict, status = Verdict.parse(output, offered) if code == 0 else (None, FALLBACK + "error")
        except TimeoutError:
            verdict, status = None, FALLBACK + "timeout"
        except (OSError, ValueError):
            verdict, status = None, FALLBACK + "error"
        if verdict is not None and len(offered) < candidate_count and not verdict.relates_to_item:
            # Seeing only some candidates cannot rule out the ones the command was not offered.
            verdict, status = None, FALLBACK + "unoffered"
        self.fallbacks += verdict is None
        return verdict, status


class RecordedJudgments:
    """Replay's judge: the verdict recorded when each message was first judged. It never runs the command."""

    def __init__(self, rows: dict[str, Row]) -> None:
        self.rows = rows

    def wants(self, policy: policy_module.Policy, meta: dict) -> bool:
        return _judged_kind(policy, meta) and meta["source_key"] in self.rows

    def stops_before(self, meta: dict) -> bool:
        return False

    def outcome(self, state: Connection, baton: Connection, config: Config,
                policy: policy_module.Policy, meta: dict, text: str) -> tuple[str, str, list[str]]:
        row = self.rows[meta["source_key"]]
        verdict = (Verdict(row["relation"], row["item"], row["opens_owed"], row["needs_now"])
                   if row["status"] == JUDGED else None)
        return Judgment(state, baton, config, policy, meta, text).apply(verdict, row["status"])


def _judged_kind(policy: policy_module.Policy, meta: dict) -> bool:
    """A message from someone other than the principal. The principal's own messages never wake it, and the question
    set was validated on messages to the principal."""
    author = sources._actor(meta)
    return (policy.judge is not None and meta["event_kind"] == "message" and author is not None
            and author != policy.principal)


@dataclass
class Judgment:
    """One visible message: its candidate requests, question and persisted interpretation.

    Methods use the scan's database thread. Only JudgeSession sends the prepared
    question to the asynchronous command, then asks this object to apply it.
    """

    state: Connection
    baton: Connection
    config: Config
    policy: policy_module.Policy
    meta: dict
    text: str

    def __post_init__(self) -> None:
        self.candidates = Expectations(self.state, self.policy.principal).candidates(self.meta)

    def apply(self, verdict: Verdict | None, status: str) -> tuple[str, str, list[str]]:
        """Decide one message from its verdict, or by the word lists without one, and record both verdicts."""
        state, baton, config, policy = self.state, self.baton, self.config, self.policy
        meta, text = self.meta, self.text
        message_rules = MessageRules(Expectations(state, policy.principal), baton, config, policy)
        if verdict is None:
            kind, reason, answers = message_rules.interpret(meta, text)
            rules = kind, reason
        else:
            with _rolled_back(state):  # what the word lists would have decided, kept for review and then undone
                rules = message_rules.interpret(meta, text)[:2]
            kind, reason = self._interpret(verdict)
            answers = signals(text, policy).answers  # a brief's blockers stay word-based
        secretary_store.put_judgment(state, policy.principal, meta["source_key"], status,
                                     tuple(verdict) if verdict else (None, None, None, None), rules)
        return kind, reason, answers

    def _interpret(self, verdict: Verdict) -> tuple[str, str]:
        """The mapping in the module docstring, applied in its order."""
        state, baton, config, policy = self.state, self.baton, self.config, self.policy
        meta, candidates = self.meta, self.candidates
        principal = policy.principal
        ledger = Expectations(state, principal)
        outcome = NO_ACTION
        chosen = next((row for row in candidates if row["id"] == verdict.item), None)
        if chosen is not None and verdict.relates_to_item:
            outcome = choose_disposition(outcome, self._relation(ledger, chosen, verdict.relation))
        if addressed(baton, config, principal, meta):
            request = self._owed_request(ledger, verdict)
            outcome = choose_disposition(outcome, request)
        # Urgency can promote quiet outcomes; it cannot override uncertain or bounded evidence.
        if verdict.needs_now >= policy.judge["wake_at"] and outcome[0] in ("retain", "digest"):
            return "brief", "needs the principal now"
        return outcome

    def _owed_request(self, ledger: Expectations, verdict: Verdict) -> tuple[str, str]:
        policy, meta, text = self.policy, self.meta, self.text
        if verdict.opens_owed < ASKS:
            return "digest", "addressed to the principal; asks nothing of it"
        attention = "brief", "principal owes a response"
        if verdict.needs_now < policy.judge["owed_digest_below"]:
            attention = "digest", "principal owes a response that can wait"
        return ledger.owe(meta, requester=sources._actor(meta), artifact=sources._request_artifact(meta),
                          opening_message_id=meta["message_id"], expected_kind=result_kind(text), outcome=attention)

    def _relation(self, ledger: Expectations, chosen: Row, relation: str) -> tuple[str, str]:
        meta = self.meta
        artifact = artifact_match(chosen, meta["artifacts"])
        if artifact == "stale":
            return ledger.record(chosen, meta, "stale", "artifact-revision",
                                 ("retain", "stale artifact revision retained"))
        if artifact == "uncertain":
            return ledger.record(chosen, meta, "uncertain", "conflicting-result", ("reconcile", "result is uncertain"))
        if relation == "answers":
            if ledger.close(chosen, meta, "satisfied", "judge:answers"):
                return "brief", "expected result reported"
            return EVIDENCE_LIMIT
        if relation == "acknowledges":
            return ledger.record(chosen, meta, "ack", "judge:acknowledges",
                                 ("digest", "acknowledgment; result still outstanding"))
        # A change or cancellation is evidence only: one reading must never close what the principal still awaits.
        return ledger.record(chosen, meta, "changed", "judge:changes-or-cancels",
                             ("digest", "open item changed or cancelled"))

    def request(self) -> dict:
        baton, config, policy, meta, text = self.baton, self.config, self.policy, self.meta, self.text
        principal = policy.principal
        parent = sources._visible_message(baton, config, principal, meta["reply_to"]) if meta["reply_to"] else None
        message = {"id": meta["message_id"], "from": sources._actor(meta), "from_kind": meta["identity"]["author_kind"],
                   "at": meta["at"], "channel": "direct" if meta["conversation_id"] is None else "conversation",
                   "conversation_id": meta["conversation_id"], "job_id": meta["job_id"],
                   "attention": meta["attention"] or [], "reply_to": meta["reply_to"],
                   "replies_to_principal": parent is not None and parent["sender"] == principal,
                   "artifacts": [f"{artifact['key']}@{artifact['rev']}" for artifact in meta["artifacts"]],
                   "relayed": meta["relayed_count"] > 0, "notice": meta["has_notice"], "text": text}
        return {"version": REQUEST_VERSION, "principal": principal,
                "questions_sha256": policy.judge["questions_sha256"], "message": message,
                "items": self._items()}

    def _items(self) -> list[dict]:
        """The most recently opened candidates, oldest first. Visibility is checked again here: an item whose opening
        message the principal can no longer read is omitted along with its title."""
        state, baton, config, principal = self.state, self.baton, self.config, self.policy.principal
        candidates = self.candidates
        def opened_at(row: Row) -> int:
            found = state.execute("SELECT event_pos FROM items WHERE principal=? AND source_key=?",
                                  (principal, row["opening_source"])).fetchone()
            if found is None:
                return 0
            return found["event_pos"] or 0

        offered = []
        for row in sorted(candidates, key=opened_at, reverse=True):
            opening = (sources._visible_message(baton, config, principal, row["opening_message_id"])
                       if row["opening_message_id"] else None)
            if row["opening_message_id"] and opening is None:
                continue
            shaped = messages.as_dict(opening) if opening is not None else None
            acknowledged = state.execute("SELECT 1 FROM expectation_evidence WHERE principal=? AND expectation_id=?"
                                         " AND kind='ack'", (principal, row["id"])).fetchone()
            offered.append({"id": row["id"], "direction": row["direction"],
                            "counterpart": row["responder"] if row["direction"] == "awaited" else row["requester"],
                            "job_id": row["task_ref"][4:] if (row["task_ref"] or "").startswith("job:") else None,
                            "artifact": f"{row['artifact_key']}@{row['artifact_rev']}" if row["artifact_key"] else None,
                            "state": "acknowledged" if acknowledged else "open",
                            "title": " ".join(shaped["text"].split()[:TITLE_WORDS]) if shaped else None,
                            "relayed": bool(shaped and shaped["relayed"])})
            if len(offered) == OFFERED:
                break
        return offered[::-1]


@contextmanager
def _rolled_back(state: Connection) -> Iterator[None]:
    state.execute("SAVEPOINT judge_word_lists")
    try:
        yield
    finally:
        state.execute("ROLLBACK TO judge_word_lists")
        state.execute("RELEASE judge_word_lists")


def _probability(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
