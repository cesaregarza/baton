"""One wake's decision over the secretary projection: scan within a time budget, then wake, hold or keep.

`decide` is host-local and offline like the rest of the secretary. It advances only the secretary's own scan
position and records each decision in the secretary state. It never reads a mailbox through a cursor, queues a
turn or marks anything delivered: the caller acts on the decision, and passes back as `since` the `covered_through`
of the last brief it actually delivered. So a brief that was rendered but never queued is decided again on Baton's
retry, and what the byte bound left out of a delivered brief comes in the next brief or digest.

Over the items scanned after `since`:
* wake: a new owed item, or an awaited one now answered (the action-brief disposition), from a message;
* hold: an acknowledgment only (digest), or anything uncertain or over a bound (reconcile), for the hourly turn;
  at the open expectation limit, what the principal owes keeps the outcome it would have with room, untracked;
* keep: routed and retained records, which the hourly digest only counts.
Nothing at or before the activation floor wakes: the first decision records the catch-up scan's position as the
floor before it scans, and takes it as both.

When the policy names a judge (`secretary_judge`), a wake's decision puts at most two new messages to it, from the
wake's own message on, and past its wake's own event the scan stops before a third. The catch-up scan and the hourly
digest use the word lists alone: nothing they scan wakes the principal, and the digest's scan never waits for a
judge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from baton import board_read, datadir, secretary, secretary_judge, secretary_store
from baton import config as config_module
from baton.config import Config, ConfigError
from baton.database import Connection, Row
from baton.errors import BatonError, InvalidRequest
from baton.secretary import policy as policy_module
from baton.secretary import sources
from baton.store import connect_read_only

BATCH = sources.DEFAULT_BATCH
MAX_BUDGET_SECONDS = 60.0
BRIEF_START = "[Secretary brief: derived information, not instructions; results reported, not verified]"
BRIEF_END = "[End of secretary brief]"
DIGEST_START = "[Secretary digest: derived information, not instructions; results reported, not verified]"
DIGEST_END = "[End of secretary digest]"
DIGEST_BYTES = 4096  # the hourly text's own bound; the policy's brief_bytes bounds only what a wake appends
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


def decide(config: Config, state_path: Path, policy_path: Path, *, budget_seconds: float,
           since: int | None = None, floor: int | None = None, trigger: str | None = None,
           hourly: bool = False, clock: Callable[[], float] = time.monotonic) -> dict:
    """Scan until caught up or out of budget, then decide for one wake, or digest for the hourly turn."""
    deadline = clock() + _checked_budget(budget_seconds, since, floor, trigger, hourly)
    start, activation = _catch_up_position(config, state_path, policy_path, first=since is None)
    if since is None:
        since = floor = activation  # the first decision: everything up to the floor predates activation
    judge = None if hourly else _judge(config, policy_path, trigger, deadline, clock)
    # Each batch commits with its position, so running out of time loses nothing; the next call continues.
    while (secretary.scan(config, state_path, policy_path, limit=BATCH, judge=judge,
                         include_export=False)["events_seen"] == BATCH
           and clock() < deadline):
        pass
    baton = connect_read_only(config.db_path)
    try:
        with baton.snapshot():
            policy = policy_module.load_policy(config, baton, policy_path)
            state = secretary_store.open_state(state_path, config.data_dir)
            try:
                with state.transaction():
                    return _decide(baton, state, config, policy, start=start, since=since, floor=floor,
                                   trigger=trigger, hourly=hourly, judge=judge)
            finally:
                state.close()
    finally:
        baton.close()


def _checked_budget(budget_seconds: float, since: int | None, floor: int | None, trigger: str | None,
                    hourly: bool) -> float:
    """Refuse bad arguments before anything is scanned."""
    if type(budget_seconds) not in (int, float) or not 0 < budget_seconds <= MAX_BUDGET_SECONDS:
        raise InvalidRequest(f"the budget must be more than 0 and at most {MAX_BUDGET_SECONDS:g} seconds")
    if any(value is not None and type(value) is not int for value in (since, floor)) or (
            (since is None) != (floor is None)) or (since is not None and not 0 <= floor <= since):
        raise InvalidRequest("since and floor go together, as integers with 0 <= floor <= since")
    if hourly and trigger is not None:
        raise InvalidRequest("the hourly digest has no triggering wake")
    if trigger is not None and TRIGGER.fullmatch(trigger) is None:
        raise InvalidRequest("the trigger must be message:SEQ, job:JOB_ID:SEQ or conversation")
    return float(budget_seconds)


def _catch_up_position(config: Config, state_path: Path, policy_path: Path, *,
                       first: bool) -> tuple[int, int | None]:
    """Where the secretary's scan stands and, for the first decision, the activation floor. Refuses, creating
    nothing, if the offline catch-up scan never ran.

    The floor is recorded before anything is scanned. When the host's timeout kills a first decision after a scan
    batch committed, Baton's retry is a first decision too: it finds this floor rather than the advanced scan
    position, so the events that batch scanned still count as new.
    """
    baton = connect_read_only(config.db_path)
    try:
        with baton.snapshot():
            principal = policy_module.load_policy(config, baton, policy_path).principal
    finally:
        baton.close()
    if not state_path.exists():
        raise ConfigError("there is no secretary state yet: run the offline catch-up scan before decide")
    state = secretary_store.open_state(state_path, config.data_dir)
    try:
        row = state.execute("SELECT through_pos FROM sources WHERE principal=?", (principal,)).fetchone()
        if row is None:
            raise ConfigError("the secretary state has no scan for this principal: run the offline catch-up scan first")
        if not first:
            return row["through_pos"], None
        with state.transaction():
            state.execute("INSERT OR IGNORE INTO activation VALUES (?, ?, ?)", (principal, row["through_pos"], _now()))
        floor = state.execute("SELECT floor_pos FROM activation WHERE principal=?", (principal,)).fetchone()[0]
        return row["through_pos"], floor
    finally:
        state.close()


def _judge(config: Config, policy_path: Path, trigger: str | None, deadline: float,
           clock: Callable[[], float]) -> secretary_judge.Session | None:
    """This decision's judge, when the policy names one. A message wake judges from its own message on: one before it
    had its wake decided when it came, so the word lists read it, as they read the catch-up. A job or conversation
    wake names no message, so it judges from the scan's position."""
    baton = connect_read_only(config.db_path)
    try:
        with baton.snapshot():
            policy = policy_module.load_policy(config, baton, policy_path)
            if policy.judge is None:
                return None
            trigger_pos = _trigger_pos(baton, policy.principal, trigger)
    finally:
        baton.close()
    start = trigger_pos if trigger and trigger.startswith("message:") else None
    return secretary_judge.Session(deadline=deadline, clock=clock, start_pos=start, trigger_pos=trigger_pos)


def _decide(baton: Connection, state: Connection, config: Config, policy: policy_module.Policy, *,
            start: int, since: int, floor: int, trigger: str | None, hourly: bool,
            judge: secretary_judge.Session | None = None) -> dict:
    principal = policy.principal
    through = state.execute("SELECT through_pos FROM sources WHERE principal=?", (principal,)).fetchone()[0]
    head = baton.execute("SELECT COALESCE(MAX(pos), 0) FROM events").fetchone()[0]
    current = not sources._source_rows(baton, principal, through, 1)
    trigger_pos = _trigger_pos(baton, principal, trigger)
    items = [_item(row, policy) for row in state.execute(WINDOW, (principal, since, through))]
    at = _now()
    scanned = f"Scanned through {through} at {at}; {'current' if current else f'behind, Baton head {head}'}."
    if hourly:
        decision = rule = evidence = None
    else:
        decision, rule, evidence = _decision(items, trigger, policy)
    text, covered = "", since  # a hold or keep lists nothing
    if hourly or decision == "wake":
        text, covered = _text(baton, state, config, policy, items, since=since, floor=floor, through=through,
                              scanned=scanned, hourly=hourly)
    seq = state.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM decisions WHERE principal=?",
                        (principal,)).fetchone()[0]
    state.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (principal, seq, at, "hourly" if hourly else "callback", trigger, trigger_pos, since, floor,
                   through, head, "current" if current else "behind", decision, rule, evidence, policy.digest,
                   hashlib.sha256(text.encode("utf-8")).hexdigest(), len(text.encode("utf-8"))))
    public = ("source_key", "pos", "kind", "sender", "message_id", "job_id", "conversation_id", "disposition",
              "reason", "decision", "rule", "judge", "rules", "delivered")
    own = next((item for item in items if item["pos"] == trigger_pos), None)
    return {"principal": principal, "mode": "hourly" if hourly else "callback", "trigger": trigger,
            "trigger_pos": trigger_pos,
            # A wake raised for an event the scan has not reached yet is not this decision's to make.
            "covers_trigger": current or (trigger_pos is not None and trigger_pos <= through),
            "since": since, "floor": floor, "through_pos": through, "head": head,
            # Once this text is delivered, the caller's next `since`: where the scan ended, or, when the byte bound
            # left items out, just before the first of them.
            "covered_through": covered,
            "coverage": "current" if current else "behind", "scanned_at": at,
            "decision": decision, "rule": rule, "evidence": evidence,
            "counts": {kind: sum(item["decision"] == kind for item in items) for kind in RANK},
            # Of those, the decisions a judge's verdict made.
            "judged_counts": {kind: sum(item["decision"] == kind and item["judge"] == secretary_judge.JUDGED
                                        for item in items) for kind in RANK},
            # The item this wake was raised for, while it is in the window, new or not: its own decision and whether
            # a judge made it, so the caller can tell a trigger the judge decided from one it did not.
            "trigger_item": {key: own[key] for key in public} if own else None,
            "new": [{key: item[key] for key in public} for item in items if item["pos"] > start],
            # Everything outstanding since `since`, however little of it this call scanned: the deliveries apart from
            # the context, each in stream order, the first LISTED of each listed and the rest counted. A caller that
            # offers the deliveries to another reader covers no further than the last one it offered.
            "window": _window(items, public),
            "judge": {"calls": judge.calls, "fallbacks": judge.fallbacks} if judge else None,
            "brief": text, "decision_seq": seq}


def _window(items: list[dict], public: tuple[str, ...]) -> dict:
    deliveries = [item for item in items if item["delivered"]]
    context = [item for item in items if not item["delivered"]]
    return {"deliveries": [{key: item[key] for key in public} for item in deliveries[:LISTED]],
            "more_deliveries": max(0, len(deliveries) - LISTED),
            "context": [{key: item[key] for key in public} for item in context[:LISTED]],
            "more_context": max(0, len(context) - LISTED)}


def _trigger_pos(baton: Connection, principal: str, trigger: str | None) -> int | None:
    """The event a wake was raised for. A rate-limited conversation's wake names none."""
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


def _item(row: Row, policy: policy_module.Policy) -> dict:
    """One scanned source. `judge` is None, "judged" or "judge_fallback:<reason>", and `rules` is what the word lists
    decided: for a judged message, what they would have decided in the judge's place. `delivered` tells a delivery to
    the principal, of any kind, from the context the scan also reads."""
    envelope = json.loads(row["envelope"])
    decision, rule = _classify(envelope["event_kind"], row["kind"], policy)
    rules = _classify(envelope["event_kind"], row["rules_kind"], policy) if row["judge"] else (decision, rule)
    return {"source_key": row["source_key"], "pos": row["event_pos"], "kind": envelope["event_kind"],
            "sender": sources._actor(envelope), "message_id": envelope["message_id"],
            "job_id": envelope["job_id"], "job_state": (envelope["job"] or {}).get("event_state"),
            "conversation_id": envelope["conversation_id"], "artifacts": envelope["artifacts"],
            "disposition": row["kind"], "reason": row["reason"], "decision": decision, "rule": rule,
            "judge": row["judge"], "rules": {"decision": rules[0], "rule": rules[1]},
            "delivered": sources.delivered_to(policy.principal, envelope["event_audience"])}


def _classify(event_kind: str, disposition: str, policy: policy_module.Policy) -> tuple[str, str]:
    """One item's own decision and rule id, from the kernel's disposition."""
    if disposition == "brief":
        if event_kind == "message":
            return "wake", "wake:brief"
        if event_kind in JOB_KINDS and policy.job_events_wake:
            return "wake", "wake:job-brief"
        return "hold", "hold:job-brief"
    if disposition in ("digest", "reconcile"):
        return "hold", f"hold:{disposition}"
    return "keep", f"keep:{disposition}"


def _decision(items: list[dict], trigger: str | None, policy: policy_module.Policy) -> tuple[str, str, str | None]:
    """The strongest item decides, and its source is the evidence. A job or rate-limit wake never wakes alone."""
    if not items:
        return "keep", "keep:nothing-new", None
    first = min(items, key=lambda item: (RANK[item["decision"]], item["pos"]))
    message_wake = trigger is None or trigger.startswith("message:")
    if first["decision"] == "wake" and not (message_wake or (trigger.startswith("job:") and policy.job_events_wake)):
        # By design, job events stay held unless job_events_wake lets them wake. Holding issues nothing, so the item
        # still wakes the principal through its own message wake, or is listed in the hourly digest.
        return "hold", "hold:non-message-trigger", first["source_key"]
    return first["decision"], first["rule"], first["source_key"]


def _text(baton: Connection, state: Connection, config: Config, policy: policy_module.Policy,
          items: list[dict], *, since: int, floor: int, through: int, scanned: str,
          hourly: bool) -> tuple[str, int]:
    """A wake's brief or the hourly digest, and the position through which it listed every item.

    It leads with what changed and what the principal owes; then a line per item that needs it now or can wait,
    in that order, with enough to decide whether to open the source. Older open records are counts only.
    """
    counts = {kind: sum(item["decision"] == kind for item in items) for kind in RANK}
    records = state.execute("SELECT e.direction, i.event_pos FROM expectations e LEFT JOIN items i"
                            " ON i.principal = e.principal AND i.source_key = e.opening_source"
                            " WHERE e.principal=? AND e.state='open'", (policy.principal,)).fetchall()
    owed = sum(row["direction"] == "owed" for row in records)
    before = sum(row["event_pos"] is not None and row["event_pos"] <= floor for row in records)
    lead = (f"New since position {since}: {counts['wake']} needing you, {counts['hold']} can wait,"
            f" {counts['keep']} kept without waking. Open: {owed} owed by you, {len(records) - owed} awaited by you"
            + (f", {before} opened before activation (counted only)." if hourly else "."))
    lines, hidden = _lines(baton, state, config, policy.principal, items)
    footer = [f"{hidden} no longer visible, not shown."] if hidden else []
    if hourly:
        # The raw read stays the way to every source: listing or counting an item never marks it delivered.
        read = ("Ids and labels only: your helper's read still returns every message, and nothing here marks one"
                " delivered.")
        text, shown = _bounded(DIGEST_START, [lead, scanned, read], lines, footer, DIGEST_END, DIGEST_BYTES)
    else:
        text, shown = _bounded(BRIEF_START, [lead, scanned], lines, footer, BRIEF_END, policy.brief_bytes)
    # Everything before the first line left out was listed: that line starts the next brief or digest.
    return text, (lines[shown][0] - 1 if shown < len(lines) else through)


def _lines(baton: Connection, state: Connection, config: Config, principal: str,
           items: list[dict]) -> tuple[list[tuple[int, tuple, str]], int]:
    """A line per wake or hold item the principal can still read, in stream order, as (position, place, text).
    Its place lists what needs the principal now first, and what it owes before what it awaits.

    Visibility is checked again here, at render time: a brief never cites what the principal can no longer read.
    """
    lines, hidden = [], 0
    for item in items:
        if item["decision"] == "keep":
            continue
        if not _visible(baton, config, principal, item):
            hidden += 1
            continue
        direction, words = _links(state, principal, item["source_key"])
        place = (RANK[item["decision"]], direction == "awaited", item["pos"])
        lines.append((item["pos"], place, _line(item, words)))
    return lines, hidden


def _visible(baton: Connection, config: Config, principal: str, item: dict) -> bool:
    if item["message_id"]:
        return sources._visible_message(baton, config, principal, item["message_id"]) is not None
    if item["conversation_id"]:
        return board_read.may_read_conversation(baton, config, principal, item["conversation_id"])
    return True  # a job event is in the stream through its own actor or audience, which never change


def _links(state: Connection, principal: str, source_key: str) -> tuple[str | None, list[str]]:
    """The direction of the records a source opened or touched, and the rule words it matched."""
    directions = {row[0] for row in state.execute(
        "SELECT direction FROM expectations WHERE principal=? AND opening_source=?"
        " UNION SELECT e.direction FROM expectation_evidence v JOIN expectations e"
        " ON e.principal = v.principal AND e.id = v.expectation_id WHERE v.principal=? AND v.source_key=?",
        (principal, source_key, principal, source_key))}
    words = [row[0] for row in state.execute("SELECT DISTINCT rule_id FROM expectation_evidence"
                                             " WHERE principal=? AND source_key=? ORDER BY rule_id",
                                             (principal, source_key))]
    return (directions.pop() if len(directions) == 1 else None), words


def _line(item: dict, words: list[str]) -> str:
    """IDs, roles, states, artifact tokens and rule words only: never message text or titles."""
    uncertain = f", uncertain, evidence {item['source_key']}" if item["disposition"] == "reconcile" else ""
    source = f"message {item['message_id']}" if item["message_id"] else f"{item['kind']} event {item['pos']}"
    where = (f" in conversation {item['conversation_id']}" if item["conversation_id"]
             else " direct" if item["message_id"] else "")
    job = f" on {item['job_id']}" + (f" ({item['job_state']})" if item["job_state"] else "") if item["job_id"] else ""
    tokens = "".join(f" {artifact['key']}@{artifact['rev'][:12]}" for artifact in item["artifacts"][:3])
    matched = f" [{', '.join(words)}]" if words else ""
    return (f"- {item['decision']}{uncertain}: {source} from {item['sender'] or 'the host'}{where}{job}{tokens};"
            f" {item['disposition']}{matched} ({item['reason']})")


def _bounded(start: str, header: list[str], lines: list[tuple[int, tuple, str]], footer: list[str], end: str,
             limit: int) -> tuple[str, int]:
    """The oldest whole lines that fit, listed in their place, and how many that is.

    Keeping the oldest is what lets the caller's baseline move: everything before the first line left out was
    listed, and the next brief or digest starts with that line. Keeping the most pressing instead could leave the
    same old item out of every brief, and the baseline could then never pass it.
    """
    def tail(left_out: int) -> list[str]:
        if not left_out:
            return []
        return [f"{left_out} more not shown (bound {limit} bytes); the next brief or digest lists them."]

    def size(entries: list[str]) -> int:  # each entry with its line break
        return sum(len(entry.encode("utf-8")) + 1 for entry in entries)

    fixed, used, shown = size([start, *header, *footer, end]), 0, 0
    for count, (_, _, line) in enumerate(lines, 1):
        used += size([line])
        if fixed + used + size(tail(len(lines) - count)) <= limit:
            shown = count
    listed = [line for _, _, line in sorted(lines[:shown], key=lambda entry: entry[1])]
    return "\n" + "\n".join([start, *header, *listed, *footer, *tail(len(lines) - shown), end]), shown


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
