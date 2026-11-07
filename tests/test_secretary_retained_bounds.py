"""A retained range written under wider input bounds, recovered after the policy narrows them.

The host runs the secretary through two entry points: a message callback (`Secretary.decide` with the message as
its trigger, then the ready summaries) and the hourly (the queue's scan lane, then its digest lane, else its retry
lane, then the ready summaries). These tests drive only those entry points, on a controllable clock, through the
sequence a host goes through when a range that fit the old bounds no longer fits the new ones: the range refused
before any model call and split at once, its parts assessed, a digest change under a pending summary, a crash before
a split, a duplicate callback, a reply whose parent outweighs the byte bound, an event larger than the byte bound,
newer work going ahead of the old range, callbacks that move a scan backlog, and an hourly whose scheduled lane
alternates a due digest and a due retry when new events reach no callback. Later scenarios pin the edges of those
rules: a catch-up label per call, a split that compares every stored counter, a refusal after inference, a store
written before the passed-over mark, and a retry lane cut off before its attempt records anything.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass

import pytest
from secretary_support import conversation, make_world, policy, say
from test_secretary_recovery import _worker

from baton import conversations, jobs
from baton.agents.secretary.attention.backlog import Backlog
from baton.agents.secretary.attention.decide import DecisionRun, SecretaryUnavailable
from baton.agents.secretary.attention.queue import AttentionQueue
from baton.agents.secretary.attention.runtime import DatabaseWorker
from baton.agents.secretary.attention.state import AttentionLog
from baton.agents.secretary.connections.model import SecretaryModel
from baton.agents.secretary.inbox.window import InboxTooLarge, InboxWindow
from baton.agents.secretary.service import Secretary
from baton.errors import BatonError, InvalidRequest

# The bounds the retained range was scanned under, and the narrower pair under evaluation.
OLD_BOUNDS = {"max_events": 50, "context_bytes": 256 * 1024}
CANDIDATE_BOUNDS = {"max_events": 10, "context_bytes": 24 * 1024}
BACKOFF = {"max_attempts": 3, "retry_initial_seconds": 5, "retry_max_seconds": 300}
BUDGET = 10
RANGE_EVENTS = 41
HOLD = {"decision": "hold", "summary": "", "sources": []}
# Two replies whose parents fall outside the page they end up in. One parent was delivered before the range; the other
# is inside the range until a split puts it in another part. Each then counts against max_events, never toward the
# split, which divides only the visible events.
REPLIES = {25: "delivered", 12: 3}


@dataclass
class Retained:
    world: object
    model: object
    secretary: Secretary
    now: list[float]
    rooms: list[str]
    delivered_parent: dict
    delivered_id: str
    earlier: list[dict]
    earlier_id: str
    events: list[dict]
    old_digest: str


@dataclass
class Hourly:
    """What each of the hourly's two lanes ended with, whether each asked the model, the modes of the scheduled lane's
    model requests, and what is ready after them."""

    scan: Exception | None
    scheduled: Exception | None
    pending: list[dict]
    asked: tuple[bool, bool] = (False, False)
    scheduled_modes: tuple[str, ...] = ()


def _rows(world) -> list[dict]:
    """The stored pages, as a host's status and timing output summarizes them."""
    backlog = Backlog(AttentionLog(world.state_path, world.config.data_dir, "planner"))
    with backlog.log.connection() as conn:
        rows = conn.execute(
            "SELECT since,through,mode,attempts,content_failures,status,next_attempt,decision FROM backlog_windows "
            "WHERE principal='planner' ORDER BY since,through"
        ).fetchall()
    keys = ("since", "through", "mode", "attempts", "content_failures", "status", "next_attempt", "decision")
    return [dict(zip(keys, tuple(row), strict=True)) for row in rows]


def _retrying(world) -> list[dict]:
    return [row for row in _rows(world) if row["status"] == "retry"]


def _delivered(world) -> int:
    return AttentionLog(world.state_path, world.config.data_dir, "planner").positions()[0]


def _scanned(world) -> int:
    return Backlog(AttentionLog(world.state_path, world.config.data_dir, "planner")).scanned()


def _position(world, message_id: str) -> int:
    row = world.baton.execute(
        "SELECT pos FROM events WHERE kind='message' AND audience='planner' "
        "AND json_extract(data, '$.message_id')=? ORDER BY pos LIMIT 1",
        (message_id,),
    ).fetchone()
    assert row is not None
    return row["pos"]


def _trigger(sent: dict) -> str:
    return f"message:{sent['seqs']['planner']}"


async def _callback(secretary: Secretary, trigger: str) -> tuple[dict | None, BatonError | None, list[dict]]:
    """One message wake as the host runs it: decide for the trigger, then read the ready summaries."""
    try:
        result, error = await secretary.decide(budget_seconds=BUDGET, trigger=trigger), None
    except BatonError as raised:
        result, error = None, raised
    async with DatabaseWorker() as database:
        queue = await AttentionQueue.open(secretary, database, budget_seconds=BUDGET)
        return result, error, await queue.pending()


async def _hourly(secretary: Secretary, model=None) -> Hourly:
    """The hourly as the host runs it: the scan lane, then the digest lane if a held page is due, else the retry lane
    if a retry is due, then what is ready. Each lane starts with no error of its own, and a refusal in the scan lane
    does not keep the scheduled lane from running, so each lane's error is its own."""
    calls = (lambda: len(model.requests)) if model is not None else (lambda: 0)
    async with DatabaseWorker() as database:
        queue = await AttentionQueue.open(secretary, database, budget_seconds=BUDGET)
        before = calls()
        scan = await _lane(queue, queue.scan)
        between = calls()
        scheduled = None
        for held, action in ((True, queue.digest), (False, queue.retry)):
            if await database.call(queue.backlog.due, time.time(), held=held):
                scheduled = await _lane(queue, action)
                break
        asked = (between > before, calls() > between)
        modes = tuple(request["context"]["mode"] for request in model.requests[between:]) if model is not None else ()
        return Hourly(scan, scheduled, await queue.pending(), asked, modes)


async def _lane(queue: AttentionQueue, action) -> Exception | None:
    queue.last_error = None
    try:
        await action(BUDGET)
    except BatonError as error:
        return error
    return queue.last_error


def _at_next_retry(world, now: list[float]) -> None:
    """Move the clock to the earliest stored retry, never backwards: a host's next call lands after the backoff."""
    waiting = _retrying(world)
    if waiting:
        now[0] = max(now[0], min(row["next_attempt"] for row in waiting))


def _retained(tmp_path, monkeypatch, *, earlier_count: int = 3) -> Retained:
    """The state a host holds before the edit, built through its own entry points under the old bounds.

    One page was delivered and acknowledged. A later page has a summary that no confirmed queue carried, so it is
    still pending. Then a 41-event range failed twice for a reason that isn't its content, so it waits in retry with
    two attempts and a backoff, exactly as the old bounds scanned it.
    """
    world = make_world(tmp_path)
    worker, model = _worker(world, tmp_path)
    policy(world, **OLD_BOUNDS, **BACKOFF)
    secretary = worker.instance.secretary
    now = [1000.0]
    monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
    asyncio.run(secretary.activate())
    # Four rooms keep every room under its hourly message limit.
    rooms = [conversation(world) for _ in range(4)]

    delivered_parent = say(world, rooms[0], "worker", "A delivered request that a later reply answers")
    result, error, _ = asyncio.run(_callback(secretary, _trigger(delivered_parent)))
    assert error is None and result is not None and result["kind"] == "summary"
    asyncio.run(secretary.acknowledge(result["decision_id"]))
    delivered_id = result["decision_id"]

    earlier = [say(world, rooms[3], "reviewer", f"An earlier update {number}") for number in range(earlier_count)]
    result, error, pending = asyncio.run(_callback(secretary, _trigger(earlier[-1])))
    assert error is None and result is not None and result["kind"] == "summary"
    assert [summary["decision_id"] for summary in pending] == [result["decision_id"]]
    earlier_id = result["decision_id"]

    events: list[dict] = []
    for number in range(1, RANGE_EVENTS + 1):
        parent = REPLIES.get(number)
        if parent == "delivered":
            room, reply_to = rooms[0], delivered_parent["message_id"]
        elif parent is not None:
            room, reply_to = rooms[2], events[parent - 1]["message_id"]
        else:
            room, reply_to = rooms[2] if number in REPLIES.values() else rooms[1 + number % 2], None
        events.append(
            say(world, room, "worker", f"Coordination update {number} in the retained range", reply_to=reply_to)
        )

    model.set_plan({"steps": [{"exit": 1}]})
    for _ in range(2):
        _at_next_retry(world, now)
        result, error, _ = asyncio.run(_callback(secretary, _trigger(events[-1])))
        assert result is None and isinstance(error, SecretaryUnavailable)
    model.set_plan({})
    [stored] = _retrying(world)
    assert (stored["attempts"], stored["content_failures"]) == (2, 0)
    assert stored["next_attempt"] == now[0] + 10  # min(300, 5 * 2 ** (2 - 1)) after the second failure
    assert len(model.requests) == 4  # two assessed pages and the range's two failed attempts
    assert len(model.requests[-1]["context"]["events"]) == RANGE_EVENTS
    assert [entry["source_key"] for entry in model.requests[-1]["context"]["reply_context"]] == [
        f"message:{delivered_parent['message_id']}"
    ]
    old_digest = asyncio.run(secretary.pending(earlier_id))["policy_digest"]
    return Retained(
        world, model, secretary, now, rooms, delivered_parent, delivered_id, earlier, earlier_id, events, old_digest
    )


def _edit_to_candidate(retained: Retained) -> None:
    """The operator's edit, made between host calls: only the input bounds change."""
    policy(retained.world, **CANDIDATE_BOUNDS)


def _range_rows(retained: Retained) -> list[dict]:
    first = _position(retained.world, retained.events[0]["message_id"])
    return [row for row in _rows(retained.world) if row["through"] >= first]


def _event_numbers(retained: Retained, row: dict) -> list[int]:
    positions = [_position(retained.world, event["message_id"]) for event in retained.events]
    return [number for number, pos in enumerate(positions, 1) if row["since"] < pos <= row["through"]]


def _calls(limit: int = 40):
    """Bound every host-call loop, so a recovery that stalls fails the test instead of hanging it."""
    for _ in range(limit):
        yield
    raise AssertionError(f"no recovery within {limit} host calls")


def _drain_range(retained: Retained, *, limit: int = 80) -> list[str]:
    """Hourlies at each next retry until no page waits; what each lane of each call did to the retained work."""
    steps = []
    for _ in range(limit):
        if not _retrying(retained.world):
            return steps
        _at_next_retry(retained.world, retained.now)
        hourly = asyncio.run(_hourly(retained.secretary, retained.model))
        done = [
            "assessed" if asked else "oversized" if isinstance(error, InboxTooLarge) else type(error).__name__
            for error, asked in zip((hourly.scan, hourly.scheduled), hourly.asked, strict=True)
            if asked or error is not None
        ]
        steps.extend(done or ["nothing"])
    raise AssertionError(f"the retained range did not recover within {limit} host calls: {steps}")


def _event_keys(results: list[dict]) -> Counter:
    """The visible events these results cover. Reply parents are cited as context, with no position of their own."""
    return Counter(ref["source_key"] for result in results for ref in result["source_refs"] if ref["pos"] is not None)


def _bytes(entries: list[dict]) -> int:
    """Entries' serialized size, as the policy's context_bytes counts each event and reply parent."""
    return sum(len(json.dumps(entry, ensure_ascii=False).encode()) for entry in entries)


def test_an_oversized_retained_range_splits_on_its_first_refusal_until_every_part_fits(tmp_path, monkeypatch) -> None:
    retained = _retained(tmp_path, monkeypatch)
    with retained.world:
        world, model = retained.world, retained.model
        _edit_to_candidate(retained)
        calls, engine_calls = len(model.requests), len(model.engine_requests)

        # The range's next retry, after the backoff its two transport failures left, is refused before any model or
        # engine call and counted. A range over the bounds stays over them however often it is retried, so it is
        # halved by visible events at once instead of after max_attempts backoffs. Each half starts over, due at once.
        [old] = _range_rows(retained)
        _at_next_retry(world, retained.now)
        assert retained.now[0] == old["next_attempt"]
        result, error, _ = asyncio.run(_callback(retained.secretary, _trigger(retained.events[-1])))
        assert result is None and isinstance(error, InboxTooLarge)
        halves = _range_rows(retained)
        assert [len(_event_numbers(retained, row)) for row in halves] == [21, 20]
        assert all((row["attempts"], row["content_failures"], row["status"]) == (0, 0, "retry") for row in halves)
        assert all(row["next_attempt"] == retained.now[0] for row in halves)
        assert (len(model.requests), len(model.engine_requests)) == (calls, engine_calls)

        steps = _drain_range(retained)
        leaves = _range_rows(retained)
        assert [_event_numbers(retained, row) for row in leaves] == [
            list(range(1, 7)),
            list(range(7, 12)),
            list(range(12, 17)),  # 5 events and the parent of event 12: a full 10-event half was refused
            list(range(17, 22)),
            list(range(22, 27)),  # 5 events and the delivered parent of event 25, refused at 10 + 1
            list(range(27, 32)),
            list(range(32, 42)),
        ]
        assert all(row["status"] == "ready" for row in leaves)
        # One cheap refusal for each further split, one assessment for each part, and in between one more for the
        # pending summary the edit made stale (the digest test follows it). No refusal reached the model or the
        # engine, and none waited: the clock never moved after the range's own retry was due.
        assert steps.count("oversized") == 5 and steps.count("assessed") == 8
        assert set(steps) == {"oversized", "assessed"}
        assert len(model.engine_requests) == engine_calls + 8
        assert retained.now[0] == old["next_attempt"]
        starts = {row["since"]: row for row in leaves}
        leaf_requests = [request for request in model.requests[calls:] if request["context"]["since"] in starts]
        assert len(leaf_requests) == 7
        for request in leaf_requests:
            context = request["context"]
            assert len(context["events"]) + len(context["reply_context"]) <= CANDIDATE_BOUNDS["max_events"]
            assert _bytes([*context["events"], *context["reply_context"]]) <= CANDIDATE_BOUNDS["context_bytes"]
        parents = {
            request["context"]["since"]: [entry["source_key"] for entry in request["context"]["reply_context"]]
            for request in leaf_requests
        }
        assert parents == {
            row["since"]: (
                [f"message:{retained.events[2]['message_id']}"]
                if _event_numbers(retained, row)[0] == 12
                else [f"message:{retained.delivered_parent['message_id']}"]
                if _event_numbers(retained, row)[0] == 22
                else []
            )
            for row in leaves
        }


@pytest.mark.parametrize("earlier_count", [3, 12], ids=["pending-page-fits", "pending-page-over-the-bounds"])
def test_policy_edit_reassesses_a_pending_summary_and_refuses_its_old_acknowledgement(
    tmp_path, monkeypatch, earlier_count
) -> None:
    retained = _retained(tmp_path, monkeypatch, earlier_count=earlier_count)
    with retained.world:
        world, model, secretary = retained.world, retained.model, retained.secretary
        delivered_before = _delivered(world)
        _edit_to_candidate(retained)

        # The saved summary was assessed under the old digest: it is no longer pending, and acknowledging it is stale.
        assert asyncio.run(secretary.pending(retained.earlier_id)) is None
        with pytest.raises(InvalidRequest, match="stale"):
            asyncio.run(secretary.acknowledge(retained.earlier_id))
        # What was acknowledged before the edit stays acknowledged, and the delivered position does not move.
        log = AttentionLog(world.state_path, world.config.data_dir, "planner")
        assert log.is_acknowledged(retained.delivered_id)
        assert _delivered(world) == delivered_before

        asyncio.run(_hourly(secretary))  # the ready summaries are read last, and the stale one is invalidated
        earlier_since = _position(world, retained.earlier[0]["message_id"]) - 1
        [invalidated] = [row for row in _rows(world) if row["since"] <= earlier_since < row["through"]]
        assert invalidated["status"] == "retry" and invalidated["decision"] is None
        assert (invalidated["attempts"], invalidated["content_failures"]) == (0, 0)

        # Its page is assessed again under the new digest. A page the new bounds can hold costs one assessment; one
        # over them is refused and split at once, at no model cost, and costs one assessment for each part.
        expected = Counter(f"message:{sent['message_id']}" for sent in retained.earlier)
        calls, reassessed = len(model.requests), {}
        for _ in _calls():
            if _event_keys(list(reassessed.values())) == expected:
                break
            _at_next_retry(world, retained.now)
            hourly = asyncio.run(_hourly(secretary))
            for summary in hourly.pending:
                if invalidated["since"] <= summary["since"] and summary["through_pos"] <= invalidated["through"]:
                    reassessed[summary["decision_id"]] = summary
        parts = 1 if earlier_count <= CANDIDATE_BOUNDS["max_events"] else 2
        own = [
            request
            for request in model.requests[calls:]
            if invalidated["since"] <= request["context"]["since"] < invalidated["through"]
        ]
        assert len(reassessed) == parts and len(own) == parts
        assert all(summary["kind"] == "summary" for summary in reassessed.values())
        assert all(summary["policy_digest"] != retained.old_digest for summary in reassessed.values())
        assert retained.earlier_id not in reassessed

        for summary in sorted(reassessed.values(), key=lambda summary: summary["since"]):
            assert asyncio.run(secretary.acknowledge(summary["decision_id"])) == summary["covered_through"]
        with pytest.raises(InvalidRequest, match="stale"):
            asyncio.run(secretary.acknowledge(retained.earlier_id))
        assert not log.is_acknowledged(retained.earlier_id)
        assert log.is_acknowledged(retained.delivered_id)


def test_newer_pages_go_ahead_of_the_old_range_and_delivery_waits_at_its_gap(tmp_path, monkeypatch) -> None:
    retained = _retained(tmp_path, monkeypatch)
    with retained.world:
        world, model, secretary, now = retained.world, retained.model, retained.secretary, retained.now
        _edit_to_candidate(retained)
        newer = say(world, retained.rooms[1], "reviewer", "A newer message after the retained range")
        trigger = _trigger(newer)

        # The range is due, so the callback spends itself on it: refused and split, and the trigger isn't reached.
        _at_next_retry(world, now)
        result, error, _ = asyncio.run(_callback(secretary, trigger))
        assert result is None and isinstance(error, InboxTooLarge)
        assert _position(world, newer["message_id"]) > _scanned(world)

        # A model outage: every call fails. Each callback works on the earliest due page, splitting an oversized part
        # at no model cost or failing a part that fits, which then backs off, as does the page the edit made stale.
        model.set_plan({"steps": [{"exit": 1}]})
        for _ in _calls():
            if all(row["next_attempt"] > now[0] for row in _retrying(world)):
                break
            result, error, _ = asyncio.run(_callback(secretary, trigger))
            assert result is None and error is not None
        model.set_plan({})

        # With every part backing off, the next callback scans past the range to its own trigger and assesses it.
        newer_result, error, _ = asyncio.run(_callback(secretary, trigger))
        assert error is None and newer_result is not None
        assert newer_result["trigger"] == trigger and newer_result["kind"] == "summary"
        assert _event_keys([newer_result]) == Counter([f"message:{newer['message_id']}"])

        # Delivery advances only through a contiguous run: the newer page waits behind the stale page and the range.
        before = _delivered(world)
        assert asyncio.run(secretary.acknowledge(newer_result["decision_id"])) == before
        acknowledged = [newer_result]
        for _ in _calls():
            if not _retrying(world):
                break
            _at_next_retry(world, now)
            hourly = asyncio.run(_hourly(secretary))
            for summary in hourly.pending:
                assert summary["kind"] == "summary"  # every part was assessed in full; none needed a notice
                previous = _delivered(world)
                delivered = asyncio.run(secretary.acknowledge(summary["decision_id"]))
                acknowledged.append(summary)
                assert delivered >= previous
                covered = sum(_event_keys(acknowledged).values())
                if covered < len(retained.earlier) + RANGE_EVENTS + 1:
                    # A part of the range is still unacknowledged, so delivery cannot reach the newer page.
                    assert delivered < newer_result["since"]
        assert _delivered(world) == newer_result["covered_through"]

        # Every visible event since the edit was assessed in exactly one acknowledged result.
        expected = Counter(f"message:{sent['message_id']}" for sent in [*retained.earlier, *retained.events, newer])
        assert _event_keys(acknowledged) == expected

        later = say(world, retained.rooms[2], "reviewer", "A trigger after the recovered range")
        result, error, _ = asyncio.run(_callback(secretary, _trigger(later)))
        assert error is None and result is not None and result["trigger"] == _trigger(later)


def _crash_before_split(world, now: float, trigger: str) -> None:
    """A callback in its own process, on the test's clock, that dies right after its refusal is recorded."""
    script = """
import asyncio, os, sys, time
from pathlib import Path
from baton.config import load
from baton.agents.secretary import Secretary
from baton.agents.secretary.attention.backlog import Backlog
config_path, state_path, policy_path, now, trigger = sys.argv[1:]
time.time = lambda: float(now)
def crash(*args, **kwargs):
    os._exit(73)
Backlog.replace = crash
secretary = Secretary(load(config_path), Path(state_path), Path(policy_path))
asyncio.run(secretary.decide(budget_seconds=10, trigger=trigger))
"""
    arguments = [str(world.config_path), str(world.state_path), str(world.policy_path), repr(now), trigger]
    process = subprocess.run(
        [sys.executable, "-c", script, *arguments], capture_output=True, text=True, timeout=60, check=False
    )
    assert process.returncode == 73, process.stderr


def test_a_crash_between_the_first_refusal_and_its_split_resumes_from_stored_state(tmp_path, monkeypatch) -> None:
    retained = _retained(tmp_path, monkeypatch)
    with retained.world:
        world, model = retained.world, retained.model
        _edit_to_candidate(retained)
        [before] = _range_rows(retained)
        calls = len(model.requests)

        _at_next_retry(world, retained.now)
        _crash_before_split(world, retained.now[0], _trigger(retained.events[-1]))
        # The refusal was recorded, with its backoff; the split was not.
        [after] = _range_rows(retained)
        assert (after["since"], after["through"]) == (before["since"], before["through"])
        assert (after["attempts"], after["content_failures"]) == (before["attempts"] + 1, 1)
        assert after["next_attempt"] > retained.now[0]

        # A new process, with nothing but the stored state: its next refusal splits the range.
        retained.secretary = Secretary(world.config, world.state_path, world.policy_path)
        _at_next_retry(world, retained.now)
        result, error, _ = asyncio.run(_callback(retained.secretary, _trigger(retained.events[-1])))
        assert result is None and isinstance(error, InboxTooLarge)
        halves = _range_rows(retained)
        assert [len(_event_numbers(retained, row)) for row in halves] == [21, 20]
        assert all((row["attempts"], row["content_failures"]) == (0, 0) for row in halves)
        assert len(model.requests) == calls

        _drain_range(retained)
        leaves = _range_rows(retained)
        assert sorted(number for row in leaves for number in _event_numbers(retained, row)) == list(
            range(1, RANGE_EVENTS + 1)
        )
        assert all(row["status"] == "ready" for row in leaves)
        assert len(model.requests[-1]["context"]["events"]) <= CANDIDATE_BOUNDS["max_events"]


def test_duplicate_concurrent_callbacks_on_a_retried_range_split_it_once(tmp_path, monkeypatch) -> None:
    retained = _retained(tmp_path, monkeypatch)
    with retained.world:
        world, model = retained.world, retained.model
        _edit_to_candidate(retained)
        _at_next_retry(world, retained.now)
        [before] = _range_rows(retained)
        trigger = _trigger(retained.events[-1])

        # Both callbacks read the same due page before either records its failure.
        original = Backlog.failed
        together = threading.Barrier(2, timeout=20)

        def failed_together(self, *args, **kwargs):
            together.wait()
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Backlog, "failed", failed_together)

        async def both() -> list[BaseException | dict]:
            return await asyncio.gather(
                retained.secretary.decide(budget_seconds=BUDGET, trigger=trigger),
                Secretary(world.config, world.state_path, world.policy_path).decide(
                    budget_seconds=BUDGET, trigger=trigger
                ),
                return_exceptions=True,
            )

        outcomes = asyncio.run(both())
        monkeypatch.setattr(Backlog, "failed", original)
        # One records the refusal and splits; the other finds the page already changed and reports it as waiting.
        assert sorted(type(outcome).__name__ for outcome in outcomes) == ["InboxTooLarge", "SecretaryUnavailable"]
        assert len(model.requests) == 4
        halves = _range_rows(retained)
        assert [len(_event_numbers(retained, row)) for row in halves] == [21, 20]
        assert (halves[0]["since"], halves[-1]["through"]) == (before["since"], before["through"])
        assert all((row["attempts"], row["content_failures"], row["status"]) == (0, 0, "retry") for row in halves)

        # The duplicate cost nothing: the halves recover as a single callback's would, each event in one part.
        _drain_range(retained)
        leaves = _range_rows(retained)
        assert sorted(number for row in leaves for number in _event_numbers(retained, row)) == list(
            range(1, RANGE_EVENTS + 1)
        )
        assert len(leaves) == 7 and all(row["status"] == "ready" for row in leaves)


def test_the_scheduled_lane_alternates_a_due_digest_and_a_due_retry_when_new_events_reach_no_callback(
    tmp_path, monkeypatch
) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path, plan=HOLD)
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF, digest_seconds=60)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        rooms = [conversation(world) for _ in range(3)]

        # Held pages with a failed page between each two, so that each digest takes one run of held pages and stops at
        # a retry: held, retry, held, retry, held. Every held page is due with the first.
        held, failed = [], []
        for number in range(3):
            model.set_plan(HOLD)
            held.append([say(world, rooms[0], "worker", f"Routine update {number}.{n}") for n in range(3)])
            result, error, _ = asyncio.run(_callback(secretary, _trigger(held[-1][-1])))
            assert error is None and result is not None and result["decision"] == "hold"
            if number == 2:
                break
            model.set_plan({"steps": [{"exit": 1}]})
            failed.append([say(world, rooms[1], "reviewer", f"A failed update {number}.{n}") for n in range(2)])
            result, error, _ = asyncio.run(_callback(secretary, _trigger(failed[-1][-1])))
            assert result is None and isinstance(error, SecretaryUnavailable)
        assert [row["status"] for row in _rows(world)] == ["held", "retry", "held", "retry", "held"]

        def hourly(new_events: int, scheduled: dict | None = None) -> Hourly:
            """One hour later, with new events that reached no callback (a forced wake or a job event makes none). The
            scan lane holds them; the scheduled lane's model answer is valid for a digest and for an alert."""
            now[0] += 3600
            for n in range(new_events):
                say(world, rooms[2], "worker", f"An update with no callback {now[0]}.{n}")
            done = len(model.requests)
            model.set_plan({"steps": [{}] * done + ([HOLD] if new_events else []) + [scheduled or {}]})
            return asyncio.run(_hourly(secretary, model))

        def waiting(pages: list[list[dict]]) -> list[bool]:
            starts = [_position(world, page[0]["message_id"]) for page in pages]
            return [any(row["since"] < start <= row["through"] for row in _retrying(world)) for start in starts]

        # Both failed pages are due from the start. The first hourly digests, and its scan lane, with nothing new to
        # scan, never takes the retry: its budget is the smaller, so a page that fits the scheduled lane could time out
        # there, and a timeout counts toward the gate's latch.
        first = hourly(0)
        assert first.asked == (False, True) and first.scheduled_modes == ("digest",)
        assert waiting(failed) == [True, True]

        # From then on the scan lane holds new events every hour, so a digest is due at every scheduled lane, and no
        # callback comes. The digest marked the earliest due retry it passed over, so the next scheduled lane takes
        # that retry first; then the due digest, though the second retry is due too, and it marks that one.
        second = hourly(3)
        assert second.asked == (True, True) and second.scheduled_modes == ("alert",)
        assert waiting(failed) == [False, True]
        third = hourly(3)
        assert third.scheduled_modes == ("digest",) and waiting(failed) == [False, True]
        assert all(lane.scan is None and lane.scheduled is None for lane in (first, second, third))

        # A retry that fails again loses its mark with the attempt, so it never holds the lane: the next one digests.
        fourth = hourly(3, {"exit": 1})
        assert fourth.scheduled_modes == ("alert",) and isinstance(fourth.scheduled, SecretaryUnavailable)
        assert waiting(failed) == [False, True]
        fifth = hourly(3)
        assert fifth.scheduled_modes == ("digest",) and waiting(failed) == [False, True]
        sixth = hourly(3)
        assert sixth.scheduled_modes == ("alert",) and waiting(failed) == [False, False]

        # So neither waited behind more than one of the other. Three digests and both retries are ready, and no event
        # is in two results: each of the first pages in exactly one, and the later held pages wait their turn.
        assert sorted(summary["mode"] for summary in sixth.pending) == ["alert", "alert", "digest", "digest", "digest"]
        covered = _event_keys(sixth.pending)
        assert set(covered.values()) == {1}
        assert all(
            covered[f"message:{sent['message_id']}"] == 1
            for page in [held[0], failed[0], held[1], held[2], failed[1]]
            for sent in page
        )
        assert [row["status"] for row in _rows(world)][-1] == "held"


def test_an_idle_scan_lane_leaves_a_lone_due_retry_to_the_scheduled_lane(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path)
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF, digest_seconds=60)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        room = conversation(world)
        model.set_plan({"steps": [{"exit": 1}]})
        failed = [say(world, room, "reviewer", f"An update whose assessment failed {number}") for number in range(2)]
        result, error, _ = asyncio.run(_callback(secretary, _trigger(failed[-1])))
        assert result is None and isinstance(error, SecretaryUnavailable)
        model.set_plan({})
        _at_next_retry(world, now)
        calls = len(model.requests)

        # No digest is due, so the scheduled lane takes the retry, with a budget never smaller than the scan lane's.
        # The idle scan lane leaves it there, as it leaves every retry: a page that fits the scheduled lane could time
        # out in the scan lane.
        hourly = asyncio.run(_hourly(secretary, model))
        assert hourly.scan is None and hourly.scheduled is None and hourly.asked == (False, True)
        assert len(model.requests) == calls + 1 and not _retrying(world)
        assert _event_keys(hourly.pending) == Counter(f"message:{sent['message_id']}" for sent in failed)


def test_each_callback_behind_a_scan_backlog_assesses_the_earliest_page_as_catch_up_until_its_trigger_is_reached(
    tmp_path, monkeypatch
) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path)
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF)
        secretary = worker.instance.secretary
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: 1000.0)
        asyncio.run(secretary.activate())
        rooms = [conversation(world) for _ in range(2)]
        # Twenty-four events arrived while no callback ran, so the scan cursor is more than two pages behind.
        backlog = [say(world, rooms[number % 2], "worker", f"Backlog update {number}") for number in range(24)]
        log = AttentionLog(world.state_path, world.config.data_dir, "planner")

        # Each arriving message's callback assesses the next bounded page from the cursor. Its own trigger is not on
        # that page, so the page is historical catch-up: the model is not told of the trigger, the result names none
        # and is marked catch_up, stored so too, and the trigger stays beyond the cursor, unassessed, for the host's
        # admission to decide as it decides any wake the secretary has not reached.
        arrivals, results = [], []
        for number in range(2):
            arrivals.append(say(world, rooms[number % 2], "reviewer", f"Arriving message {number}"))
            result, error, _ = asyncio.run(_callback(secretary, _trigger(arrivals[-1])))
            assert error is None and result is not None and result["kind"] == "summary"
            assert result["trigger"] is None and model.requests[-1]["context"]["trigger"] is None
            assert result["catch_up"] is True and log.get(result["decision_id"])["catch_up"] is True
            assert len(model.requests[-1]["context"]["events"]) == CANDIDATE_BOUNDS["max_events"]
            assert _position(world, arrivals[-1]["message_id"]) > _scanned(world)
            results.append(result)

        # Delivering the catch-up summaries never covers the waiting triggers: delivery stops at the cursor.
        for result in results:
            asyncio.run(secretary.acknowledge(result["decision_id"]))
        assert _delivered(world) == results[-1]["covered_through"] < _position(world, arrivals[0]["message_id"])
        waiting = Counter(f"message:{sent['message_id']}" for sent in arrivals)
        assert not waiting & _event_keys(results)

        # The third callback's page reaches its trigger, which is then assessed on its page as before, as its own.
        arrivals.append(say(world, rooms[0], "reviewer", "Arriving message 2"))
        result, error, _ = asyncio.run(_callback(secretary, _trigger(arrivals[-1])))
        assert error is None and result is not None and result["trigger"] == _trigger(arrivals[-1])
        assert "catch_up" not in result and "catch_up" not in log.get(result["decision_id"])
        assert model.requests[-1]["context"]["trigger"] == _trigger(arrivals[-1])
        results.append(result)

        # Every event, the earlier arrivals' included, was assessed exactly once, in order and without a gap.
        assert _event_keys(results) == Counter(f"message:{sent['message_id']}" for sent in [*backlog, *arrivals])
        assert [result["since"] for result in results[1:]] == [result["through_pos"] for result in results[:-1]]
        assert len(model.requests) == 3


def test_a_callback_that_resumes_a_due_retry_for_a_later_trigger_assesses_it_as_catch_up(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path)
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        room = conversation(world)
        failed = [say(world, room, "worker", f"An update whose assessment failed {number}") for number in range(3)]
        model.set_plan({"steps": [{"exit": 1}]})
        result, error, _ = asyncio.run(_callback(secretary, _trigger(failed[-1])))
        assert result is None and isinstance(error, SecretaryUnavailable)
        model.set_plan({})
        later = say(world, room, "reviewer", "A later message whose callback finds that retry due")
        _at_next_retry(world, now)
        log = AttentionLog(world.state_path, world.config.data_dir, "planner")

        # The callback resumes the due page before it scans. Its trigger is not on that page, so the page is catch-up
        # by the same rule as a scanned page without it: the model is not told of the trigger, the result names none
        # and is marked catch_up, stored so too, and the trigger stays beyond the cursor for the host's own fallback.
        result, error, _ = asyncio.run(_callback(secretary, _trigger(later)))
        assert error is None and result is not None and result["kind"] == "summary"
        assert result["trigger"] is None and result["catch_up"] is True
        assert log.get(result["decision_id"])["catch_up"] is True
        assert model.requests[-1]["context"]["trigger"] is None
        assert _event_keys([result]) == Counter(f"message:{sent['message_id']}" for sent in failed)
        assert _position(world, later["message_id"]) > _scanned(world)

        # The next callback reaches its trigger and assesses it as its own.
        own, error, _ = asyncio.run(_callback(secretary, _trigger(later)))
        assert error is None and own is not None and own["trigger"] == _trigger(later) and "catch_up" not in own
        assert _event_keys([own]) == Counter([f"message:{later['message_id']}"])


def test_an_alert_decide_with_nothing_new_to_scan_never_takes_a_due_digest(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path, plan={"decision": "hold", "summary": "", "sources": []})
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF, digest_seconds=60)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        room = conversation(world)
        held = [say(world, room, "worker", f"A routine update {number}") for number in range(3)]
        result, error, _ = asyncio.run(_callback(secretary, _trigger(held[-1])))
        assert error is None and result is not None and result["decision"] == "hold"
        now[0] += 120  # the held page's digest is due
        calls = len(model.requests)

        # An alert decide, untriggered or for a trigger already assessed, finds nothing new and returns the empty
        # assessment it always has. A scan never takes scheduled work: an alert call has already resumed what was due,
        # and never returns a digest.
        for trigger in (None, _trigger(held[-1])):
            result = asyncio.run(secretary.decide(budget_seconds=BUDGET, trigger=trigger))
            assert result["mode"] == "alert" and result["event_count"] == 0
        assert len(model.requests) == calls
        assert [row["status"] for row in _rows(world)] == ["held"]

        # The hourly's scheduled lane takes it, and its idle scan lane asks nothing.
        hourly = asyncio.run(_hourly(secretary, model))
        assert hourly.asked == (False, True)
        assert [request["context"]["mode"] for request in model.requests[calls:]] == ["digest"]


def test_a_trigger_that_is_not_a_visible_event_is_still_refused_by_every_caller(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path)
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF)
        secretary = worker.instance.secretary
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: 1000.0)
        _, floor = asyncio.run(secretary.activate())
        room = conversation(world)
        sent = [say(world, room, "worker", f"Backlog update {number}") for number in range(12)]
        missing = f"message:{sent[-1]['seqs']['planner'] + 1000}"

        # Catch-up admits a visible trigger beyond the page, never one that names no visible event: the queue's
        # callers and an explicit-position caller refuse it alike, before any page is queued or any model call.
        for options in ({}, {"since": floor, "floor": floor}):
            with pytest.raises(InvalidRequest, match="not a visible event"):
                asyncio.run(secretary.decide(budget_seconds=BUDGET, trigger=missing, **options))
        assert _scanned(world) == floor and not _rows(world) and not model.requests

        # The same rule for both callers: a visible trigger beyond the explicit page makes that page catch-up.
        beyond = _trigger(sent[-1])
        result = asyncio.run(secretary.decide(budget_seconds=BUDGET, trigger=beyond, since=floor, floor=floor))
        assert result["trigger"] is None and result["catch_up"] is True
        assert model.requests[-1]["context"]["trigger"] is None
        assert len(result["sources"]) == CANDIDATE_BOUNDS["max_events"]
        # The same call again finds the stored assessment, and says so, and still says it is catch-up.
        calls = len(model.requests)
        again = asyncio.run(secretary.decide(budget_seconds=BUDGET, trigger=beyond, since=floor, floor=floor))
        assert again["cached"] is True and again["decision_id"] == result["decision_id"]
        assert again["trigger"] is None and again["catch_up"] is True and len(model.requests) == calls

        # A page with no events validates its trigger first too, so a missing one is refused there as well; a visible
        # trigger behind the page, already scanned, is not on it either, so that page is catch-up and names none.
        head = _position(world, sent[-1]["message_id"])
        with pytest.raises(InvalidRequest, match="not a visible event"):
            asyncio.run(secretary.decide(budget_seconds=BUDGET, trigger=missing, since=head, floor=floor))
        for _ in range(2):  # the second time from the stored result
            empty = asyncio.run(
                secretary.decide(budget_seconds=BUDGET, trigger=_trigger(sent[0]), since=head, floor=floor)
            )
            assert (empty["event_count"], empty["trigger"], empty["catch_up"]) == (0, None, True)
        assert len(model.requests) == calls


def test_a_job_trigger_beyond_its_page_makes_that_page_catch_up_and_stays_pending(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path)
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF)
        secretary = worker.instance.secretary
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: 1000.0)
        asyncio.run(secretary.activate())
        room = conversation(world)
        sent = [say(world, room, "worker", f"Backlog update {number}") for number in range(12)]
        made = jobs.create(world.baton, world.config.jobs, request_id="job-beyond", actor="worker", title="A task")
        created = world.baton.execute(
            "SELECT pos,seq FROM events WHERE job_id=? AND kind='job.created'", (made["job_id"],)
        ).fetchone()
        trigger = f"job:{made['job_id']}:{created['seq']}"
        log = AttentionLog(world.state_path, world.config.data_dir, "planner")

        # A job trigger follows the message trigger's rule: beyond the bounded page, the page is catch-up. The model is
        # not told of the trigger, the result names none and is stored as catch-up, and the job event stays beyond the
        # cursor, unassessed.
        result, error, _ = asyncio.run(_callback(secretary, trigger))
        assert error is None and result is not None and result["kind"] == "summary"
        assert result["trigger"] is None and result["catch_up"] is True
        assert log.get(result["decision_id"])["catch_up"] is True
        assert model.requests[-1]["context"]["trigger"] is None
        assert _event_keys([result]) == Counter(f"message:{item['message_id']}" for item in sent[:10])
        assert created["pos"] > _scanned(world)

        # The next page holds it, and assesses it as its own.
        own, error, _ = asyncio.run(_callback(secretary, trigger))
        assert error is None and own is not None and own["trigger"] == trigger and "catch_up" not in own
        assert model.requests[-1]["context"]["trigger"] == trigger
        assert created["pos"] <= _scanned(world)


@pytest.mark.parametrize(
    ("step", "content_failures"), [({"sleep": 2}, 0), ({"raw": "{}"}, 1)], ids=["timeout", "invalid-response"]
)
def test_a_range_that_fits_is_never_split_early_by_a_timeout_or_an_invalid_response(
    tmp_path, monkeypatch, step, content_failures
) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path, plan={"steps": [step]})
        model.set_timeout(0.5)
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        room = conversation(world)
        sent = [say(world, room, "worker", f"Update {number} on a page that fits") for number in range(6)]
        first, last = (_position(world, sent[index]["message_id"]) for index in (0, -1))

        # The early split is for a deterministic refusal before any model call. A model that timed out or answered
        # unusably may do better on a retry, so the page keeps today's path: its counted failures and backoff, whole,
        # and a split only after max_attempts content failures.
        result, error, _ = asyncio.run(_callback(secretary, _trigger(sent[-1])))
        assert result is None and isinstance(error, SecretaryUnavailable) and not isinstance(error, InboxTooLarge)
        for attempts in (1, 2):
            [page] = _retrying(world)
            assert page["since"] < first and last <= page["through"]  # one page still holds all six
            assert (page["attempts"], page["content_failures"]) == (attempts, content_failures * attempts)
            assert page["next_attempt"] > now[0]
            if attempts == 1:
                _at_next_retry(world, now)
                hourly = asyncio.run(_hourly(secretary))
                assert isinstance(hourly.scan or hourly.scheduled, SecretaryUnavailable)
        assert len(model.requests) == 2


def test_a_short_reply_whose_parent_outweighs_the_byte_bound_gets_a_cited_notice_never_a_summary(
    tmp_path, monkeypatch
) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path)
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        room = conversation(world)
        limit = CANDIDATE_BOUNDS["context_bytes"]
        parent = say(world, room, "worker", "A long review. " + "y" * 16_000)
        result, error, _ = asyncio.run(_callback(secretary, _trigger(parent)))
        assert error is None and result is not None and result["kind"] == "summary"
        asyncio.run(secretary.acknowledge(result["decision_id"]))

        # A reply whose bytes and its parent's fit together is assessed with the parent as its context.
        fitting = say(world, room, "reviewer", "A reply that fits. " + "z" * 6_000, reply_to=parent["message_id"])
        result, error, _ = asyncio.run(_callback(secretary, _trigger(fitting)))
        assert error is None and result is not None and result["kind"] == "summary"
        context = model.requests[-1]["context"]
        assert [entry["source_key"] for entry in context["reply_context"]] == [f"message:{parent['message_id']}"]
        assert _bytes(context["events"]) < _bytes([*context["events"], *context["reply_context"]]) <= limit
        asyncio.run(secretary.acknowledge(result["decision_id"]))
        calls, engine_calls = len(model.requests), len(model.engine_requests)

        # A reply 3,004 bytes longer fits alone, but its parent lies outside its page and counts in full, so together
        # they pass the byte bound. It is one visible event and cannot split: three counted refusals before any model
        # call, then a notice that cites the reply alone, never a summary of it.
        reply = say(world, room, "reviewer", "A reply that does not. " + "z" * 9_000, reply_to=parent["message_id"])
        alone = _bytes(context["events"]) + 3_004
        together = alone + _bytes(context["reply_context"])
        assert alone <= limit < together
        result, error, _ = asyncio.run(_callback(secretary, _trigger(reply)))
        assert result is None and isinstance(error, InboxTooLarge)
        [page] = _retrying(world)
        assert (page["attempts"], page["content_failures"]) == (1, 1)
        for refusal in (2, 3):
            _at_next_retry(world, now)
            hourly = asyncio.run(_hourly(secretary))
            assert isinstance(hourly.scan or hourly.scheduled, InboxTooLarge)
            if refusal == 2:
                [page] = _retrying(world)
                assert (page["attempts"], page["content_failures"]) == (2, 2)
        assert (len(model.requests), len(model.engine_requests)) == (calls, engine_calls)

        [notice] = hourly.pending
        assert notice["kind"] == "failure_notice" and notice["kind"] != "summary"
        assert notice["decision"] == "wake" and notice["reference_only"] is True
        assert "No relevance judgment was made." in notice["brief"]
        assert [ref["source_key"] for ref in notice["source_refs"]] == [f"message:{reply['message_id']}"]
        assert not _retrying(world)
        # Delivery waited at the reply; its notice's acknowledgement passes it.
        assert _delivered(world) < _position(world, reply["message_id"]) <= notice["covered_through"]
        assert asyncio.run(secretary.acknowledge(notice["decision_id"])) >= notice["covered_through"]


@pytest.mark.parametrize(
    "text",
    [
        "x" * (64 * 1024 - 64),  # ASCII: near the 64 KiB send bound, and 64 KiB of event bytes
        "日" * ((64 * 1024 - 64) // 6),  # non-ASCII: the send bound escapes each as 6 bytes; 3 in the event
    ],
    ids=["ascii-near-64KiB", "non-ascii-near-64KiB"],
)
def test_an_event_larger_than_the_byte_bound_gets_a_cited_failure_notice_never_a_skip(
    tmp_path, monkeypatch, text
) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path)
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        room = conversation(world)
        before = say(world, room, "worker", "An ordinary message before the large one")
        large = say(world, room, "worker", text)
        after = say(world, room, "reviewer", "An ordinary message after the large one")

        # The page before it closes at the event that doesn't fit, so that event is never cut or dropped.
        first, error, _ = asyncio.run(_callback(secretary, _trigger(before)))
        assert error is None and first is not None and first["kind"] == "summary"
        assert _event_keys([first]) == Counter([f"message:{before['message_id']}"])
        asyncio.run(secretary.acknowledge(first["decision_id"]))
        calls, engine_calls = len(model.requests), len(model.engine_requests)

        # One event cannot split, so it keeps the counted path: three cheap refusals, each before any model or engine
        # call, with newer work going ahead during the backoff.
        result, error, _ = asyncio.run(_callback(secretary, _trigger(large)))
        assert result is None and isinstance(error, InboxTooLarge)
        [page] = _retrying(world)
        assert (page["attempts"], page["content_failures"]) == (1, 1)
        now[0] = page["next_attempt"]
        result, error, _ = asyncio.run(_callback(secretary, _trigger(after)))
        assert result is None and isinstance(error, InboxTooLarge)
        newer, error, _ = asyncio.run(_callback(secretary, _trigger(after)))
        assert error is None and newer is not None and newer["kind"] == "summary"
        assert _event_keys([newer]) == Counter([f"message:{after['message_id']}"])
        now[0] = _retrying(world)[0]["next_attempt"]
        hourly = asyncio.run(_hourly(secretary))
        assert isinstance(hourly.scan or hourly.scheduled, InboxTooLarge)
        assert len(model.requests) == calls + 1 and len(model.engine_requests) == engine_calls + 1  # the newer page

        # The notice cites the event, says no judgment was made, and is kept apart from the summaries.
        [notice] = [result for result in hourly.pending if result["kind"] == "failure_notice"]
        assert [result["kind"] for result in hourly.pending if result is not notice] == ["summary"]
        assert notice["decision"] == "wake" and notice["reference_only"] is True
        assert "No relevance judgment was made." in notice["brief"]
        assert [ref["source_key"] for ref in notice["source_refs"]] == [f"message:{large['message_id']}"]
        assert notice["source_refs"][0]["message_id"] == large["message_id"]
        assert notice["source_refs"][0]["conversation_id"] == room
        assert notice["event_count"] == 1 and text not in json.dumps(notice, ensure_ascii=False)
        # The principal reads the source itself, whole, through Baton.
        history = conversations.history(world.baton, "planner", room, config=world.config)
        assert [m["text"] for m in history["messages"] if m["message_id"] == large["message_id"]] == [text]

        # Delivery waits at the noticed event until its notice is acknowledged, then passes the newer page too.
        assert asyncio.run(secretary.acknowledge(newer["decision_id"])) == first["covered_through"]
        assert asyncio.run(secretary.acknowledge(notice["decision_id"])) == newer["covered_through"]
        assert not _retrying(world)


def test_a_digest_range_over_narrowed_bounds_splits_into_digest_parts(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path, plan=HOLD)
        policy(world, **OLD_BOUNDS, **BACKOFF, digest_seconds=60)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        rooms = [conversation(world) for _ in range(3)]
        held = []
        for number in range(3):
            page = [say(world, rooms[number], "worker", f"Routine update {number}.{n}") for n in range(4)]
            held.extend(page)
            result, error, _ = asyncio.run(_callback(secretary, _trigger(page[-1])))
            assert error is None and result is not None and result["decision"] == "hold"

        # The three held pages are combined into one 12-event digest, whose assessment fails: it waits in retry as a
        # digest range, written under the old bounds. Then the bounds narrow.
        now[0] += 120
        model.set_plan({"steps": [{"exit": 1}]})
        hourly = asyncio.run(_hourly(secretary, model))
        assert isinstance(hourly.scheduled, SecretaryUnavailable)
        [digest] = _retrying(world)
        assert digest["mode"] == "digest"
        policy(world, **CANDIDATE_BOUNDS)
        model.set_plan({"decision": "keep"})
        _at_next_retry(world, now)
        calls = len(model.requests)

        # Its first refusal splits it at once into digest parts: the held events are never assessed as alerts, which
        # would ask the decision engine and could wake.
        hourly = asyncio.run(_hourly(secretary, model))
        assert isinstance(hourly.scheduled, InboxTooLarge)
        parts = _retrying(world)
        assert [row["mode"] for row in parts] == ["digest", "digest"]
        assert (parts[0]["since"], parts[-1]["through"]) == (digest["since"], digest["through"])
        assert len(model.requests) == calls

        engine_calls = len(model.engine_requests)
        pending: dict[str, dict] = {}
        for _ in _calls(4):
            if not _retrying(world):
                break
            hourly = asyncio.run(_hourly(secretary, model))
            pending.update({summary["decision_id"]: summary for summary in hourly.pending})
        assert [request["context"]["mode"] for request in model.requests[calls:]] == ["digest", "digest"]
        assert len(model.engine_requests) == engine_calls
        assert {summary["mode"] for summary in pending.values()} == {"digest"}
        assert _event_keys(list(pending.values())) == Counter(f"message:{sent['message_id']}" for sent in held)


def test_a_callback_stalled_inside_its_split_never_puts_back_a_part_another_callback_split(
    tmp_path, monkeypatch
) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path)
        policy(world, **OLD_BOUNDS, **BACKOFF)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        rooms = [conversation(world) for _ in range(4)]
        sent = [say(world, rooms[number % 4], "worker", f"Range update {number}") for number in range(30)]
        trigger = _trigger(sent[-1])
        model.set_plan({"steps": [{"exit": 1}]})
        result, error, _ = asyncio.run(_callback(secretary, trigger))
        assert result is None and isinstance(error, SecretaryUnavailable)
        model.set_plan({})
        policy(world, **CANDIDATE_BOUNDS)
        _at_next_retry(world, now)

        # One callback records its refusal, then stalls before its split, past its own backoff.
        original = InboxWindow.split_position.__func__
        entered, release, first = threading.Event(), threading.Event(), [True]

        def stalled(cls, *args):
            position = original(cls, *args)
            if first[0]:
                first[0] = False
                entered.set()
                assert release.wait(30)
            return position

        monkeypatch.setattr(InboxWindow, "split_position", classmethod(stalled))
        outcome: list[BaseException | dict] = []

        def stalled_callback() -> None:
            try:
                other = Secretary(world.config, world.state_path, world.policy_path)
                outcome.append(asyncio.run(other.decide(budget_seconds=BUDGET, trigger=trigger)))
            except BaseException as raised:  # the outcome is asserted below
                outcome.append(raised)

        thread = threading.Thread(target=stalled_callback)
        thread.start()
        assert entered.wait(30)
        _at_next_retry(world, now)

        # Meanwhile two other callbacks refuse and split: the range into halves, then its first half again.
        for _ in range(2):
            result, error, _ = asyncio.run(_callback(secretary, trigger))
            assert result is None and isinstance(error, InboxTooLarge)
        split = _rows(world)
        release.set()
        thread.join(30)
        assert outcome and isinstance(outcome[0], InboxTooLarge)

        # The stalled split finds the range no longer stored as it read it, and changes nothing: no event is in two
        # stored pages.
        assert _rows(world) == split
        positions = [_position(world, item["message_id"]) for item in sent]
        assert [sum(row["since"] < pos <= row["through"] for row in split) for pos in positions] == [1] * len(sent)


def _marks(world) -> list[tuple[int, int, str, int]]:
    """Each stored page's range, status and passed-over mark, as the private state holds them."""
    backlog = Backlog(AttentionLog(world.state_path, world.config.data_dir, "planner"))
    with backlog.log.connection() as conn:
        rows = conn.execute(
            "SELECT since,through,status,passed_over FROM backlog_windows WHERE principal='planner' ORDER BY since"
        ).fetchall()
    return [tuple(row) for row in rows]


@pytest.mark.parametrize("engine_wake", [False, True], ids=["summary", "engine-wake-notice"])
@pytest.mark.parametrize("catch_up_first", [False, True], ids=["untriggered-first", "catch-up-first"])
def test_a_catch_up_call_and_an_untriggered_call_on_one_page_each_keep_their_own_label(
    tmp_path, monkeypatch, catch_up_first, engine_wake
) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path)
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF)
        if engine_wake:
            # The engine requires a wake that the model does not choose, so the page's result is the engine's notice.
            model.set_plan(HOLD)
            model.set_engine_plan({"decision": "wake", "wake_probability": 0.9})
        secretary = worker.instance.secretary
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: 1000.0)
        _, floor = asyncio.run(secretary.activate())
        room = conversation(world)
        sent = [say(world, room, "worker", f"Backlog update {number}") for number in range(12)]
        explicit = {"since": floor, "floor": floor}
        calls = {
            "catch_up": lambda: secretary.decide(budget_seconds=BUDGET, trigger=_trigger(sent[-1]), **explicit),
            "untriggered": lambda: secretary.decide(budget_seconds=BUDGET, **explicit),
        }
        order = ["catch_up", "untriggered"] if catch_up_first else ["untriggered", "catch_up"]

        # Neither page names a trigger, so the two calls give the model the same context. Each still gets a result
        # labelled for itself, whichever came first: catch-up only when its own trigger is not on the page. The label
        # is keyed, so the second call is a separate assessment and never returns the first call's stored label.
        results = {}
        for name in order:
            result = asyncio.run(calls[name]())
            assert result["cached"] is False and result["trigger"] is None
            assert result["kind"] == ("engine_wake" if engine_wake else "summary")
            results[name] = result
        assert results["catch_up"]["catch_up"] is True and "catch_up" not in results["untriggered"]
        assert results["catch_up"]["decision_id"] != results["untriggered"]["decision_id"]
        assert all(request["context"]["trigger"] is None for request in model.requests)
        assert "catch_up" not in json.dumps([request["context"] for request in model.requests])

        # Asked again in either order, each call finds its own stored result, with its own label. A summary is found
        # by its key before any model call; a notice is keyed by its window, so it is found as the model answers.
        calls_before = len(model.requests)
        for name in reversed(order):
            again = asyncio.run(calls[name]())
            assert again["decision_id"] == results[name]["decision_id"]
            assert again.get("catch_up") == results[name].get("catch_up")
        if not engine_wake:
            assert len(model.requests) == calls_before


def test_a_stalled_split_never_deletes_a_page_another_callback_finished_since(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        invalid = {"raw": "{}"}
        worker, model = _worker(world, tmp_path, plan={"steps": [invalid] * BACKOFF["max_attempts"] + [{}]})
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        room = conversation(world)
        sent = [say(world, room, "worker", f"Update {number} on a page that fits") for number in range(6)]
        trigger = _trigger(sent[-1])

        # Two invalid answers leave the page that fits one content failure short of max_attempts.
        result, error, _ = asyncio.run(_callback(secretary, trigger))
        assert result is None and isinstance(error, SecretaryUnavailable)
        _at_next_retry(world, now)
        assert isinstance(asyncio.run(_hourly(secretary)).scheduled, SecretaryUnavailable)
        _at_next_retry(world, now)

        # A third callback's invalid answer exhausts the page, and that callback stalls before its split.
        original = InboxWindow.split_position.__func__
        entered, release = threading.Event(), threading.Event()

        def stalled(cls, *args):
            entered.set()
            assert release.wait(30)
            return original(cls, *args)

        monkeypatch.setattr(InboxWindow, "split_position", classmethod(stalled))
        outcome: list[BaseException | dict] = []

        def stalled_callback() -> None:
            try:
                other = Secretary(world.config, world.state_path, world.policy_path)
                outcome.append(asyncio.run(other.decide(budget_seconds=BUDGET, trigger=trigger)))
            except BaseException as raised:  # the outcome is asserted below
                outcome.append(raised)

        thread = threading.Thread(target=stalled_callback)
        thread.start()
        assert entered.wait(30)
        [exhausted] = _retrying(world)
        assert (exhausted["attempts"], exhausted["content_failures"]) == (3, 3)

        # Past its backoff, another callback finishes the same range with a valid answer: one stored page, ready,
        # with the same range and mode it had, its counters reset.
        _at_next_retry(world, now)
        finished, error, pending = asyncio.run(_callback(secretary, trigger))
        assert error is None and finished is not None and finished["kind"] == "summary"
        ready = _rows(world)
        assert [(row["since"], row["through"], row["status"]) for row in ready] == [
            (exhausted["since"], exhausted["through"], "ready")
        ]
        release.set()
        thread.join(30)
        assert outcome and isinstance(outcome[0], SecretaryUnavailable)

        # The stalled split compares the counters its failure stored, not only the range, so it finds the page
        # changed and leaves it: no part goes back to retry, and no event is assessed twice or in two results.
        assert _rows(world) == ready
        _, _, after = asyncio.run(_callback(secretary, trigger))
        assert [summary["decision_id"] for summary in after] == [finished["decision_id"]]
        assert _event_keys(after) == Counter(f"message:{item['message_id']}" for item in sent)
        assert len(model.requests) == BACKOFF["max_attempts"] + 1


def test_a_store_written_before_the_passed_over_mark_digests_first_after_the_upgrade(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path, plan=HOLD)
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF, digest_seconds=60)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        rooms = [conversation(world) for _ in range(2)]
        held = [say(world, rooms[0], "worker", f"Routine update {number}") for number in range(3)]
        result, error, _ = asyncio.run(_callback(secretary, _trigger(held[-1])))
        assert error is None and result is not None and result["decision"] == "hold"
        model.set_plan({"steps": [{}] * len(model.requests) + [{"exit": 1}]})
        failed = [say(world, rooms[1], "reviewer", f"A failed update {number}") for number in range(2)]
        result, error, _ = asyncio.run(_callback(secretary, _trigger(failed[-1])))
        assert result is None and isinstance(error, SecretaryUnavailable)

        # The previous build's store has no passed_over column: drop it, as that build never wrote one.
        log = AttentionLog(world.state_path, world.config.data_dir, "planner")
        with log.connection() as conn, conn.transaction():
            conn.execute("ALTER TABLE backlog_windows DROP COLUMN passed_over")
        with log.connection() as conn:
            assert "passed_over" not in {row["name"] for row in conn.execute("PRAGMA table_info(backlog_windows)")}
        now[0] += 120  # the held page's digest and the failed page's retry are both due
        model.set_plan({})

        # The first open adds the column unmarked, so a retry stored before the upgrade has not been passed over:
        # the first scheduled lane digests and marks it, and the next one takes it.
        _scanned(world)
        assert [(status, mark) for *_, status, mark in _marks(world)] == [("held", 0), ("retry", 0)]
        first = asyncio.run(_hourly(secretary, model))
        assert first.scheduled is None and first.scheduled_modes == ("digest",)
        assert _event_keys(first.pending) == Counter(f"message:{sent['message_id']}" for sent in held)
        assert [(status, mark) for *_, status, mark in _marks(world)] == [("ready", 0), ("retry", 1)]
        now[0] += 3600
        second = asyncio.run(_hourly(secretary, model))
        assert second.scheduled is None and second.scheduled_modes == ("alert",)
        assert not _retrying(world)


def test_split_parts_start_unmarked_so_a_due_digest_goes_first(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path, plan=HOLD)
        policy(world, **OLD_BOUNDS, **BACKOFF, digest_seconds=60)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        rooms = [conversation(world) for _ in range(2)]
        held = [say(world, rooms[0], "worker", f"Routine update {number}") for number in range(3)]
        result, error, _ = asyncio.run(_callback(secretary, _trigger(held[-1])))
        assert error is None and result is not None and result["decision"] == "hold"
        model.set_plan({"steps": [{}] * len(model.requests) + [{"exit": 1}]})
        ranged = [say(world, rooms[1], "reviewer", f"A range update {number}") for number in range(12)]
        result, error, _ = asyncio.run(_callback(secretary, _trigger(ranged[-1])))
        assert result is None and isinstance(error, SecretaryUnavailable)
        policy(world, **CANDIDATE_BOUNDS)
        model.set_plan({})
        now[0] += 120  # the held page's digest and the range's retry are both due

        # A callback resumes the range, which no longer fits, and splits it before any model call. Its parts are new
        # pages that no digest has passed over, so the hourly's scheduled lane digests first.
        calls = len(model.requests)
        result, error, _ = asyncio.run(_callback(secretary, _trigger(ranged[-1])))
        assert result is None and isinstance(error, InboxTooLarge) and len(model.requests) == calls
        assert [(status, mark) for *_, status, mark in _marks(world)] == [("held", 0), ("retry", 0), ("retry", 0)]
        hourly = asyncio.run(_hourly(secretary, model))
        assert hourly.scheduled is None and hourly.scheduled_modes == ("digest",)
        assert [(status, mark) for *_, status, mark in _marks(world)] == [("ready", 0), ("retry", 1), ("retry", 0)]


def test_a_refusal_after_the_model_answered_keeps_the_counted_path_and_never_splits_early(
    tmp_path, monkeypatch
) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path)
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        room = conversation(world)
        sent = [say(world, room, "worker", f"Update {number} on a page that fits") for number in range(6)]
        first, last = (_position(world, sent[index]["message_id"]) for index in (0, -1))

        # The page fits when it is read, and the model answers. The re-read that checks the sources did not change
        # during inference is refused as over the bounds, as when a visibility change grows the page meanwhile.
        original = InboxWindow.read.__func__
        answered = [False]

        def refused_after_the_answer(cls, *args, **kwargs):
            if answered[0]:
                answered[0] = False
                raise InboxTooLarge("pending inbox and reply context exceed the configured input bounds")
            return original(cls, *args, **kwargs)

        original_turn = SecretaryModel.turn

        async def answer(self, *args, **kwargs):
            reply = await original_turn(self, *args, **kwargs)
            answered[0] = True
            return reply

        monkeypatch.setattr(InboxWindow, "read", classmethod(refused_after_the_answer))
        monkeypatch.setattr(SecretaryModel, "turn", answer)
        result, error, _ = asyncio.run(_callback(secretary, _trigger(sent[-1])))
        assert result is None and isinstance(error, InboxTooLarge)
        assert len(model.requests) == 1 and len(model.engine_requests) == 1

        # The early split is for a refusal before any model call. This one came after the engine and the model were
        # asked, so the page keeps today's path whole: a counted content failure and its backoff, never a split.
        pages = _retrying(world)
        assert len(pages) == 1
        [page] = pages
        assert page["since"] < first and last <= page["through"]
        assert (page["attempts"], page["content_failures"]) == (1, 1)
        assert page["next_attempt"] > now[0]


def test_a_callback_that_resumes_a_due_digest_retry_labels_it_catch_up(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path, plan=HOLD)
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF, digest_seconds=60)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        room = conversation(world)
        held = [say(world, room, "worker", f"Routine update {number}") for number in range(3)]
        result, error, _ = asyncio.run(_callback(secretary, _trigger(held[-1])))
        assert error is None and result is not None and result["decision"] == "hold"
        now[0] += 120
        model.set_plan({"steps": [{}] * len(model.requests) + [{"exit": 1}]})
        assert isinstance(asyncio.run(_hourly(secretary, model)).scheduled, SecretaryUnavailable)
        [digest] = _retrying(world)
        assert digest["mode"] == "digest"
        model.set_plan({})
        later = say(world, room, "reviewer", "A later message whose callback finds the digest retry due")
        _at_next_retry(world, now)
        log = AttentionLog(world.state_path, world.config.data_dir, "planner")

        # The callback resumes the due digest before it scans. A digest never carries a trigger, so the page is
        # historical catch-up for this callback, labelled so and stored so, and its trigger stays beyond the cursor.
        result, error, _ = asyncio.run(_callback(secretary, _trigger(later)))
        assert error is None and result is not None and result["mode"] == "digest"
        assert result["trigger"] is None and result["catch_up"] is True
        assert log.get(result["decision_id"])["catch_up"] is True
        assert model.requests[-1]["context"]["trigger"] is None and model.requests[-1]["context"]["mode"] == "digest"
        assert _event_keys([result]) == Counter(f"message:{sent['message_id']}" for sent in held)
        assert _position(world, later["message_id"]) > _scanned(world)


def test_a_retry_lane_cut_off_before_its_attempt_records_anything_never_holds_later_lanes(
    tmp_path, monkeypatch
) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path, plan=HOLD)
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF, digest_seconds=60)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        rooms = [conversation(world) for _ in range(3)]
        held = [say(world, rooms[0], "worker", f"Routine update {number}") for number in range(3)]
        result, error, _ = asyncio.run(_callback(secretary, _trigger(held[-1])))
        assert error is None and result is not None and result["decision"] == "hold"
        model.set_plan({"steps": [{}] * len(model.requests) + [{"exit": 1}]})
        failed = [say(world, rooms[1], "reviewer", f"A failed update {number}") for number in range(2)]
        result, error, _ = asyncio.run(_callback(secretary, _trigger(failed[-1])))
        assert result is None and isinstance(error, SecretaryUnavailable)

        def hourly() -> Hourly:
            """An hour later, with new events that reached no callback; the scan lane holds them."""
            now[0] += 3600
            for number in range(3):
                say(world, rooms[2], "worker", f"An update with no callback {now[0]}.{number}")
            model.set_plan({"steps": [{}] * len(model.requests) + [HOLD, {}]})
            return asyncio.run(_hourly(secretary, model))

        first = hourly()
        assert first.scheduled_modes == ("digest",) and [mark for *_, mark in _marks(world)][1] == 1
        [retry] = _retrying(world)

        # The next lane takes the marked retry, and the host's lane timeout cuts its attempt off before it records
        # any outcome: no failure, no backoff, the page still due.
        original_run = DecisionRun.run
        cut = [True]

        async def cut_off(self):
            if cut[0] and self.since == retry["since"]:
                cut[0] = False
                raise TimeoutError("the host's lane timeout")
            return await original_run(self)

        monkeypatch.setattr(DecisionRun, "run", cut_off)
        with pytest.raises(TimeoutError):
            hourly()
        monkeypatch.setattr(DecisionRun, "run", original_run)
        [stalled] = _retrying(world)
        assert stalled["attempts"] == 1 and stalled["next_attempt"] <= now[0]

        # The lane cleared the mark as it took the retry, so the cut-off attempt cannot hold the lane: the next one
        # digests, and marks the retry again, and the one after takes it.
        assert all(mark == 0 for *_, mark in _marks(world))
        third = hourly()
        assert third.scheduled_modes == ("digest",)
        fourth = hourly()
        assert fourth.scheduled_modes == ("alert",) and not _retrying(world)


def test_a_reassessed_page_starts_unmarked_after_a_digest_change(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path, plan=HOLD)
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF, digest_seconds=60)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        rooms = [conversation(world) for _ in range(3)]

        # Held, failed, held: the first digest stops at the failed page and marks it.
        first_held = [say(world, rooms[0], "worker", f"Routine update {number}") for number in range(3)]
        assert asyncio.run(_callback(secretary, _trigger(first_held[-1])))[0]["decision"] == "hold"
        model.set_plan({"steps": [{}] * len(model.requests) + [{"exit": 1}]})
        failed = [say(world, rooms[1], "reviewer", f"A failed update {number}") for number in range(2)]
        assert isinstance(asyncio.run(_callback(secretary, _trigger(failed[-1])))[1], SecretaryUnavailable)
        model.set_plan(HOLD)
        second_held = [say(world, rooms[0], "worker", f"Another routine update {number}") for number in range(3)]
        assert asyncio.run(_callback(secretary, _trigger(second_held[-1])))[0]["decision"] == "hold"
        now[0] += 120
        model.set_plan({})
        first = asyncio.run(_hourly(secretary, model))
        assert first.scheduled_modes == ("digest",)
        assert [(status, mark) for *_, status, mark in _marks(world)] == [("ready", 0), ("retry", 1), ("held", 0)]

        # A callback finishes the marked retry, and a digest change then turns both ready pages back into retries.
        # A reassessed page has not been passed over, so the next scheduled lane digests the held page that is due.
        later = say(world, rooms[2], "reviewer", "A later message whose callback finds the retry due")
        result, error, _ = asyncio.run(_callback(secretary, _trigger(later)))
        assert error is None and result is not None and result["catch_up"] is True
        policy(world, summary_bytes=2048)
        asyncio.run(_callback(secretary, _trigger(later)))
        assert [(status, mark) for *_, status, mark in _marks(world)][:3] == [
            ("retry", 0),
            ("retry", 0),
            ("held", 0),
        ]
        calls = len(model.requests)
        asyncio.run(_hourly(secretary, model))
        [scheduled] = [request for request in model.requests[calls:] if request["context"]["mode"] == "digest"]
        assert scheduled["context"]["since"] == _marks(world)[2][0]


def test_a_callback_whose_retry_fails_again_clears_the_mark_so_the_next_lane_digests_first(
    tmp_path, monkeypatch
) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path, plan=HOLD)
        policy(world, **CANDIDATE_BOUNDS, **BACKOFF, digest_seconds=60)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        rooms = [conversation(world) for _ in range(3)]
        held = [say(world, rooms[0], "worker", f"Routine update {number}") for number in range(3)]
        result, error, _ = asyncio.run(_callback(secretary, _trigger(held[-1])))
        assert error is None and result is not None and result["decision"] == "hold"
        model.set_plan({"steps": [{}] * len(model.requests) + [{"exit": 1}]})
        failed = [say(world, rooms[1], "reviewer", f"A failed update {number}") for number in range(2)]
        result, error, _ = asyncio.run(_callback(secretary, _trigger(failed[-1])))
        assert result is None and isinstance(error, SecretaryUnavailable)

        def hourly() -> Hourly:
            """An hour later, with new events that reached no callback; the scan lane holds them."""
            now[0] += 3600
            for number in range(3):
                say(world, rooms[2], "worker", f"An update with no callback {now[0]}.{number}")
            model.set_plan({"steps": [{}] * len(model.requests) + [HOLD, {}]})
            return asyncio.run(_hourly(secretary, model))

        first = hourly()
        assert first.scheduled_modes == ("digest",) and [mark for *_, mark in _marks(world)][1] == 1

        # A callback inside the marked retry takes it and fails again. That attempt was the retry's turn, so the mark
        # goes with it and the next lane digests first.
        model.set_plan({"steps": [{}] * len(model.requests) + [{"exit": 1}]})
        result, error, _ = asyncio.run(_callback(secretary, _trigger(failed[-1])))
        assert result is None and isinstance(error, SecretaryUnavailable)
        [retry] = _retrying(world)
        assert retry["attempts"] == 2 and all(mark == 0 for *_, mark in _marks(world))
        second = hourly()
        assert second.scheduled_modes == ("digest",)
        third = hourly()
        assert third.scheduled_modes == ("alert",) and not _retrying(world)
