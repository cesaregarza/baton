"""The secretary's judgment step: a host command reads one new message for the principal and makes four calls.

The policy's optional `judge` block names the command, an absolute argv, with its timeout, the sha256 of the question
file the command asks and the two thresholds below, so the policy digest freezes all of them. The package names no
provider and stores no message text: the text goes to the command on stdin and nowhere else. A failed, late or
malformed answer is never a verdict. The word lists decide that message, and its record says
`judge_fallback:<reason>`: error, timeout, invalid_json, wrong_shape or declined, or budget when the decision had no
call or time left for a message it had to cover.

The request, on the command's stdin, is one JSON object:
    {"version": 1, "principal": ROLE, "questions_sha256": HEX, "timeout_seconds": SECONDS,
     "message": {"id", "from", "from_kind", "at", "channel", "conversation_id", "job_id", "attention", "reply_to",
                 "replies_to_principal", "artifacts", "relayed", "notice", "text"},
     "items": [{"id", "direction", "counterpart", "job_id", "artifact", "state", "title", "relayed"}, ...]}
`text` is the authored text only, never relayed text. `items` are at most 4 of the message's candidate items, each
with the first words of its opening message as `title`. The verdict, on stdout, is one JSON object:
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
import os
import selectors
import signal
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import NamedTuple

from baton import messages, secretary, secretary_store
from baton.config import Config

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


@dataclass
class Session:
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

    def wants(self, policy: secretary.Policy, meta: dict) -> bool:
        return _judged_kind(policy, meta) and (self.start_pos is None or meta["event_pos"] >= self.start_pos)

    def stops_before(self, meta: dict) -> bool:
        return self._spent() and self.trigger_pos is not None and meta["event_pos"] > self.trigger_pos

    def _spent(self) -> bool:
        return self.calls_left <= 0 or self.deadline - self.clock() < MIN_CALL_SECONDS

    def outcome(self, state: sqlite3.Connection, baton: sqlite3.Connection, config: Config,
                policy: secretary.Policy, meta: dict, text: str) -> tuple[str, str, list[str]]:
        candidates = secretary._candidates(state, policy.principal, meta)
        if self._spent():
            self.fallbacks += 1
            return _apply(state, baton, config, policy, meta, text, candidates, None, FALLBACK + "budget")
        timeout = min(policy.judge["timeout_seconds"], self.deadline - self.clock())
        request = _request(state, baton, config, policy, meta, text, candidates)
        self.calls_left, self.calls = self.calls_left - 1, self.calls + 1
        verdict, status = ask(policy.judge["command"], {**request, "timeout_seconds": round(timeout, 3)}, timeout,
                              [item["id"] for item in request["items"]])
        self.fallbacks += verdict is None
        return _apply(state, baton, config, policy, meta, text, candidates, verdict, status)


class Stored:
    """Replay's judge: the verdict recorded when each message was first judged. It never runs the command."""

    def __init__(self, rows: dict[str, sqlite3.Row]) -> None:
        self.rows = rows

    def wants(self, policy: secretary.Policy, meta: dict) -> bool:
        return _judged_kind(policy, meta) and meta["source_key"] in self.rows

    def stops_before(self, meta: dict) -> bool:
        return False

    def outcome(self, state: sqlite3.Connection, baton: sqlite3.Connection, config: Config,
                policy: secretary.Policy, meta: dict, text: str) -> tuple[str, str, list[str]]:
        row = self.rows[meta["source_key"]]
        verdict = (Verdict(row["relation"], row["item"], row["opens_owed"], row["needs_now"])
                   if row["status"] == JUDGED else None)
        return _apply(state, baton, config, policy, meta, text, secretary._candidates(state, policy.principal, meta),
                      verdict, row["status"])


def _judged_kind(policy: secretary.Policy, meta: dict) -> bool:
    """A message from someone other than the principal. The principal's own messages never wake it, and the question
    set was validated on messages to the principal."""
    author = secretary._actor(meta)
    return (policy.judge is not None and meta["event_kind"] == "message" and author is not None
            and author != policy.principal)


def _apply(state: sqlite3.Connection, baton: sqlite3.Connection, config: Config, policy: secretary.Policy,
           meta: dict, text: str, candidates: list[sqlite3.Row], verdict: Verdict | None,
           status: str) -> tuple[str, str, list[str]]:
    """Decide one message from its verdict, or by the word lists without one, and record both verdicts."""
    if verdict is None:
        kind, reason, answers = secretary._message_disposition(state, baton, config, policy, meta, text)
        rules = kind, reason
    else:
        with _rolled_back(state):  # what the word lists would have decided, kept for review and then undone
            rules = secretary._message_disposition(state, baton, config, policy, meta, text)[:2]
        kind, reason = _judged(state, baton, config, policy, meta, text, candidates, verdict)
        answers = secretary._signals(text, policy)[0]  # a brief's blockers stay word-based
    secretary_store.put_judgment(state, policy.principal, meta["source_key"], status,
                                 tuple(verdict) if verdict else (None, None, None, None), rules)
    return kind, reason, answers


def _judged(state: sqlite3.Connection, baton: sqlite3.Connection, config: Config, policy: secretary.Policy,
            meta: dict, text: str, candidates: list[sqlite3.Row], verdict: Verdict) -> tuple[str, str]:
    """The mapping in the module docstring, applied in its order."""
    principal, author, block = policy.principal, secretary._actor(meta), policy.judge
    outcome = "retain", "no new principal action"
    chosen = next((row for row in candidates if row["id"] == verdict.item), None)
    if chosen is not None and verdict.relation != "none":
        outcome = secretary._choose_disposition(outcome, _relation(state, principal, meta, chosen, verdict.relation))
    if secretary._addressed(baton, config, principal, meta):
        if verdict.opens_owed < ASKS:
            owed = "digest", "addressed to the principal; asks nothing of it"
        elif secretary._open(state, policy, meta, "owed", author, principal, secretary._request_artifact(meta),
                             meta["message_id"], secretary._result_kind(text)) is None:
            owed = "reconcile", "open expectation limit reached"
        elif verdict.needs_now < block["owed_digest_below"]:
            owed = "digest", "principal owes a response that can wait"
        else:
            owed = "brief", "principal owes a response"
        outcome = secretary._choose_disposition(outcome, owed)
    if verdict.needs_now >= block["wake_at"] and outcome[0] in ("retain", "digest"):
        outcome = "brief", "needs the principal now"
    return outcome


def _relation(state: sqlite3.Connection, principal: str, meta: dict, chosen: sqlite3.Row,
              relation: str) -> tuple[str, str]:
    limit = "reconcile", "expectation evidence limit reached"

    def recorded(kind: str, rule: str, outcome: tuple[str, str]) -> tuple[str, str]:
        return outcome if secretary._evidence(state, principal, chosen, meta, kind, rule) else limit

    artifact = secretary._artifact_match(chosen, meta["artifacts"])
    if artifact == "stale":
        return recorded("stale", "artifact-revision", ("retain", "stale artifact revision retained"))
    if artifact == "uncertain":
        return recorded("uncertain", "conflicting-result", ("reconcile", "result is uncertain"))
    if relation == "answers":
        closed = secretary._close(state, principal, chosen, meta, "satisfied", "judge:answers")
        return ("brief", "expected result reported") if closed else limit
    if relation == "acknowledges":
        return recorded("ack", "judge:acknowledges", ("digest", "acknowledgment; result still outstanding"))
    # A change or cancellation is evidence only: one reading must never close what the principal still awaits.
    return recorded("changed", "judge:changes-or-cancels", ("digest", "open item changed or cancelled"))


@contextmanager
def _rolled_back(state: sqlite3.Connection) -> Iterator[None]:
    state.execute("SAVEPOINT judge_word_lists")
    try:
        yield
    finally:
        state.execute("ROLLBACK TO judge_word_lists")
        state.execute("RELEASE judge_word_lists")


def _request(state: sqlite3.Connection, baton: sqlite3.Connection, config: Config, policy: secretary.Policy,
             meta: dict, text: str, candidates: list[sqlite3.Row]) -> dict:
    principal = policy.principal
    parent = secretary._visible_message(baton, config, principal, meta["reply_to"]) if meta["reply_to"] else None
    message = {"id": meta["message_id"], "from": secretary._actor(meta), "from_kind": meta["identity"]["author_kind"],
               "at": meta["at"], "channel": "direct" if meta["conversation_id"] is None else "conversation",
               "conversation_id": meta["conversation_id"], "job_id": meta["job_id"],
               "attention": meta["attention"] or [], "reply_to": meta["reply_to"],
               "replies_to_principal": parent is not None and parent["sender"] == principal,
               "artifacts": [f"{artifact['key']}@{artifact['rev']}" for artifact in meta["artifacts"]],
               "relayed": meta["relayed_count"] > 0, "notice": meta["has_notice"], "text": text}
    return {"version": REQUEST_VERSION, "principal": principal,
            "questions_sha256": policy.judge["questions_sha256"], "message": message,
            "items": _items(state, baton, config, principal, candidates)}


def _items(state: sqlite3.Connection, baton: sqlite3.Connection, config: Config, principal: str,
           candidates: list[sqlite3.Row]) -> list[dict]:
    """The most recently opened candidates, oldest first. Visibility is checked again here: an item whose opening
    message the principal can no longer read is not offered at all, so neither it nor its title reaches the judge."""
    def opened_at(row: sqlite3.Row) -> int:
        found = state.execute("SELECT event_pos FROM items WHERE principal=? AND source_key=?",
                              (principal, row["opening_source"])).fetchone()
        return found["event_pos"] or 0 if found else 0

    offered = []
    for row in sorted(candidates, key=opened_at, reverse=True):
        opening = (secretary._visible_message(baton, config, principal, row["opening_message_id"])
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


def ask(command: list[str], request: dict, timeout: float, offered: list[str]) -> tuple[Verdict | None, str]:
    """Run the judge once. Returns its verdict and "judged", or None and the reason it fell back."""
    try:
        code, output = _run(command, json.dumps(request).encode("utf-8"), timeout)
    except subprocess.TimeoutExpired:
        return None, FALLBACK + "timeout"
    except (OSError, ValueError):
        return None, FALLBACK + "error"
    if code != 0:
        return None, FALLBACK + "error"
    return parse(output, offered)


def parse(output: bytes, offered: list[str]) -> tuple[Verdict | None, str]:
    try:
        value = json.loads(output)
    except ValueError:  # undecodable bytes too
        return None, FALLBACK + "invalid_json"
    if isinstance(value, dict) and set(value) == {"declined"} and isinstance(value["declined"], str):
        return None, FALLBACK + "declined"
    if (not isinstance(value, dict) or set(value) != set(CALLS) or value["relation"] not in RELATIONS
            or not (value["item"] is None or value["item"] in offered)
            or not all(_probability(value[call]) for call in ("opens_owed", "needs_now"))):
        return None, FALLBACK + "wrong_shape"
    return Verdict(value["relation"], value["item"], float(value["opens_owed"]), float(value["needs_now"])), JUDGED


def _probability(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def _run(command: list[str], payload: bytes, timeout: float) -> tuple[int, bytes]:
    """Run the command in its own process group: write the request, read at most MAX_OUTPUT bytes, and kill the
    whole group if it outlives the timeout. Its stderr is discarded, so nothing it prints is kept."""
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    output, pending = bytearray(), memoryview(payload)
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                for key, _ in selector.select(left):
                    if key.fileobj is process.stdin:
                        try:
                            pending = pending[os.write(key.fd, pending[:65536]):]
                        except BrokenPipeError:  # it stopped reading; its exit code says why
                            pending = pending[len(pending):]
                        if not pending:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(process.stdout)
                    elif len(output) + len(chunk) > MAX_OUTPUT:
                        raise ValueError("judge output exceeds its bound")
                    else:
                        output.extend(chunk)
        return process.wait(max(0.0, deadline - time.monotonic())), bytes(output)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        for pipe in (process.stdin, process.stdout):
            if not pipe.closed:
                pipe.close()
