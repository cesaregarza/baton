"""A retained range written under wider input bounds, recovered after the policy narrows them.

The host runs the secretary through two entry points: a message callback (`Secretary.decide` with the message as
its trigger, then the ready summaries) and the hourly (the queue's scan, then its digest lane, else its retry lane,
then the ready summaries). These tests drive only those entry points, on a controllable clock, through the sequence
a host goes through when a range that fit the old bounds no longer fits the new ones: the oversize refusals before
any model call, the splits, a digest change under a pending summary, a crash, a duplicate callback, an event larger
than the byte bound, and newer work going ahead of the old range.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import Counter
from dataclasses import dataclass

import pytest
from secretary_support import conversation, make_world, policy, say
from test_secretary_recovery import _worker

from baton import conversations
from baton.agents.secretary.attention.backlog import Backlog
from baton.agents.secretary.attention.decide import SecretaryUnavailable
from baton.agents.secretary.attention.queue import AttentionQueue
from baton.agents.secretary.attention.runtime import DatabaseWorker
from baton.agents.secretary.attention.state import AttentionLog
from baton.agents.secretary.inbox.window import InboxTooLarge
from baton.agents.secretary.service import Secretary
from baton.errors import BatonError, InvalidRequest

# The bounds the retained range was scanned under, and the narrower pair under evaluation.
OLD_BOUNDS = {"max_events": 50, "context_bytes": 256 * 1024}
CANDIDATE_BOUNDS = {"max_events": 10, "context_bytes": 24 * 1024}
BACKOFF = {"max_attempts": 3, "retry_initial_seconds": 5, "retry_max_seconds": 300}
BUDGET = 10
RANGE_EVENTS = 41
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


async def _hourly(secretary: Secretary) -> tuple[Exception | None, list[dict]]:
    """The hourly as the host runs it: the scan, then the digest lane, else the retry lane, then what is ready."""
    async with DatabaseWorker() as database:
        queue = await AttentionQueue.open(secretary, database, budget_seconds=BUDGET)
        pending = await queue.poll(BUDGET, digest_first=True)
        return queue.last_error, pending


def _at_next_retry(world, now: list[float]) -> None:
    """Move the clock to the earliest stored retry, never backwards: a host's next call lands after the backoff."""
    waiting = _retrying(world)
    if waiting:
        now[0] = max(now[0], min(row["next_attempt"] for row in waiting))


def _retained(tmp_path, monkeypatch) -> Retained:
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
    # Three rooms keep every room under its hourly message limit.
    rooms = [conversation(world) for _ in range(3)]

    delivered_parent = say(world, rooms[0], "worker", "A delivered request that a later reply answers")
    result, error, _ = asyncio.run(_callback(secretary, _trigger(delivered_parent)))
    assert error is None and result is not None and result["kind"] == "summary"
    asyncio.run(secretary.acknowledge(result["decision_id"]))
    delivered_id = result["decision_id"]

    earlier = [say(world, rooms[1], "reviewer", f"An earlier update {number}") for number in range(3)]
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
    """Hourlies at each next retry until no page waits; what each call did to the retained work."""
    steps = []
    for _ in range(limit):
        if not _retrying(retained.world):
            return steps
        _at_next_retry(retained.world, retained.now)
        calls = len(retained.model.requests)
        error, _ = asyncio.run(_hourly(retained.secretary))
        if len(retained.model.requests) > calls:
            steps.append("assessed")
        elif isinstance(error, InboxTooLarge):
            steps.append("oversized")
        else:
            steps.append(type(error).__name__ if error else "nothing")
    raise AssertionError(f"the retained range did not recover within {limit} host calls: {steps}")


def _event_keys(results: list[dict]) -> Counter:
    """The visible events these results cover. Reply parents are cited as context, with no position of their own."""
    return Counter(ref["source_key"] for result in results for ref in result["source_refs"] if ref["pos"] is not None)


def test_oversized_retries_count_as_content_failures_and_halve_until_every_part_fits(tmp_path, monkeypatch) -> None:
    retained = _retained(tmp_path, monkeypatch)
    with retained.world:
        world, model = retained.world, retained.model
        _edit_to_candidate(retained)
        engine_calls = len(model.engine_requests)

        # The old range keeps its stored attempts: each refusal backs off from them, not from zero. The pending
        # summary the edit made stale is reassessed in between; the digest test follows it.
        [old] = _range_rows(retained)
        refusals, others = [], 0
        for _ in _calls():
            if len(_range_rows(retained)) != 1:
                break
            _at_next_retry(world, retained.now)
            calls = len(model.requests)
            error, _ = asyncio.run(_hourly(retained.secretary))
            if len(model.requests) > calls:
                others += 1
                continue
            assert isinstance(error, InboxTooLarge)
            rows = _range_rows(retained)
            if len(rows) == 1:
                [row] = rows
                assert (row["since"], row["through"]) == (old["since"], old["through"])
                refusals.append((row["attempts"], row["content_failures"], row["next_attempt"] - retained.now[0]))
            else:
                refusals.append("split")
        # Backoff min(300, 5 * 2 ** (attempts - 1)) continues from the two attempts made under the old bounds.
        assert refusals == [(3, 1, 20), (4, 2, 40), "split"]
        assert others == 1

        # After max_attempts content failures the range is halved by visible events; each half starts over.
        halves = _range_rows(retained)
        assert [len(_event_numbers(retained, row)) for row in halves] == [21, 20]
        assert all((row["attempts"], row["content_failures"], row["status"]) == (0, 0, "retry") for row in halves)
        assert all(row["next_attempt"] == retained.now[0] for row in halves)
        assert len(model.engine_requests) == engine_calls + 1  # only the reassessed pending page reached the engine

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
        # Every refusal before the leaves fit was a cheap one: no model or engine call.
        assert steps.count("oversized") == 15 and steps.count("assessed") == 7
        assert set(steps) == {"oversized", "assessed"}
        assert len(model.engine_requests) == engine_calls + 1 + 7
        for request in model.requests[-7:]:
            context = request["context"]
            assert len(context["events"]) + len(context["reply_context"]) <= CANDIDATE_BOUNDS["max_events"]
            entries = [*context["events"], *context["reply_context"]]
            size = sum(len(json.dumps(entry, ensure_ascii=False).encode()) for entry in entries)
            assert size <= CANDIDATE_BOUNDS["context_bytes"]
        parents = {
            request["context"]["since"]: [entry["source_key"] for entry in request["context"]["reply_context"]]
            for request in model.requests[-7:]
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


def test_policy_edit_reassesses_a_pending_summary_and_refuses_its_old_acknowledgement(tmp_path, monkeypatch) -> None:
    retained = _retained(tmp_path, monkeypatch)
    with retained.world:
        secretary = retained.secretary
        delivered_before = _delivered(retained.world)
        _edit_to_candidate(retained)

        # The saved summary was assessed under the old digest: it is no longer pending, and acknowledging it is stale.
        assert asyncio.run(secretary.pending(retained.earlier_id)) is None
        with pytest.raises(InvalidRequest, match="stale"):
            asyncio.run(secretary.acknowledge(retained.earlier_id))
        # What was acknowledged before the edit stays acknowledged, and the delivered position does not move.
        log = AttentionLog(retained.world.state_path, retained.world.config.data_dir, "planner")
        assert log.is_acknowledged(retained.delivered_id)
        assert _delivered(retained.world) == delivered_before

        asyncio.run(_hourly(secretary))  # the ready summaries are read last, and the stale one is invalidated
        earlier_since = _position(retained.world, retained.earlier[0]["message_id"]) - 1
        [invalidated] = [row for row in _rows(retained.world) if row["since"] <= earlier_since < row["through"]]
        assert invalidated["status"] == "retry" and invalidated["decision"] is None
        assert (invalidated["attempts"], invalidated["content_failures"]) == (0, 0)

        reassessed = None
        for _ in range(4):
            _at_next_retry(retained.world, retained.now)
            _, pending = asyncio.run(_hourly(secretary))
            reassessed = next((s for s in pending if s["since"] == invalidated["since"]), None)
            if reassessed is not None:
                break
        assert reassessed is not None and reassessed["kind"] == "summary"
        assert reassessed["decision_id"] != retained.earlier_id
        assert reassessed["policy_digest"] != retained.old_digest
        assert _event_keys([reassessed]) == Counter(f"message:{sent['message_id']}" for sent in retained.earlier)

        assert asyncio.run(secretary.acknowledge(reassessed["decision_id"])) == reassessed["covered_through"]
        with pytest.raises(InvalidRequest, match="stale"):
            asyncio.run(secretary.acknowledge(retained.earlier_id))
        assert not log.is_acknowledged(retained.earlier_id)
        assert log.is_acknowledged(retained.delivered_id)


def test_newer_pages_go_ahead_while_the_old_range_backs_off_and_delivery_waits_at_its_gap(
    tmp_path, monkeypatch
) -> None:
    retained = _retained(tmp_path, monkeypatch)
    with retained.world:
        world, model, secretary = retained.world, retained.model, retained.secretary
        _edit_to_candidate(retained)
        acknowledged: list[dict] = []
        newer = say(world, retained.rooms[1], "reviewer", "A newer message after the retained range")
        trigger = _trigger(newer)

        # The range is due, so the callback spends itself on the range: a cheap refusal, and the trigger isn't reached.
        _at_next_retry(retained.world, retained.now)
        result, error, _ = asyncio.run(_callback(secretary, trigger))
        assert result is None and isinstance(error, InboxTooLarge)
        assert _position(world, newer["message_id"]) > _scanned(world)

        # The same callback again: the page the edit made stale is due now and goes first, without the trigger.
        calls = len(model.requests)
        result, error, _ = asyncio.run(_callback(secretary, trigger))
        assert error is None and result is not None and result["trigger"] is None
        assert _event_keys([result]) == Counter(f"message:{sent['message_id']}" for sent in retained.earlier)
        assert len(model.requests) == calls + 1

        # Nothing is due while the range backs off, so the next callback scans past the range to its own trigger.
        assert _retrying(world) and all(row["next_attempt"] > retained.now[0] for row in _retrying(world))
        newer_result, error, _ = asyncio.run(_callback(secretary, trigger))
        assert error is None and newer_result is not None
        assert newer_result["trigger"] == trigger and newer_result["kind"] == "summary"
        assert _event_keys([newer_result]) == Counter([f"message:{newer['message_id']}"])

        # Delivery advances only through a contiguous run: past the earlier page, then it stops at the range.
        range_since = _range_rows(retained)[0]["since"]
        assert asyncio.run(secretary.acknowledge(result["decision_id"])) == range_since
        assert asyncio.run(secretary.acknowledge(newer_result["decision_id"])) == range_since
        acknowledged += [result, newer_result]

        for _ in _calls():
            if not _retrying(world):
                break
            _at_next_retry(retained.world, retained.now)
            _, pending = asyncio.run(_hourly(secretary))
            for summary in pending:
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


def test_crash_between_the_last_oversize_failure_and_its_split_resumes_from_stored_state(tmp_path, monkeypatch) -> None:
    retained = _retained(tmp_path, monkeypatch)
    with retained.world:
        world, model = retained.world, retained.model
        _edit_to_candidate(retained)
        for _ in _calls():
            if _range_rows(retained)[0]["content_failures"] == 2:
                break
            _at_next_retry(retained.world, retained.now)
            asyncio.run(_hourly(retained.secretary))
        [before] = _range_rows(retained)

        original = Backlog.replace

        def crash(self, originals, replacements, now):
            raise RuntimeError("synthetic crash before the split is written")

        monkeypatch.setattr(Backlog, "replace", crash)
        _at_next_retry(retained.world, retained.now)
        with pytest.raises(RuntimeError, match="before the split"):
            asyncio.run(_hourly(retained.secretary))
        # The failure itself was recorded; the split was not.
        [after] = _range_rows(retained)
        assert (after["since"], after["through"]) == (before["since"], before["through"])
        assert (after["attempts"], after["content_failures"]) == (before["attempts"] + 1, 3)

        # A new process, with nothing but the stored state: its next refusal splits the range.
        monkeypatch.setattr(Backlog, "replace", original)
        retained.secretary = Secretary(world.config, world.state_path, world.policy_path)
        _at_next_retry(retained.world, retained.now)
        error, _ = asyncio.run(_hourly(retained.secretary))
        assert isinstance(error, InboxTooLarge)
        halves = _range_rows(retained)
        assert [len(_event_numbers(retained, row)) for row in halves] == [21, 20]
        assert all((row["attempts"], row["content_failures"]) == (0, 0) for row in halves)

        _drain_range(retained)
        leaves = _range_rows(retained)
        assert sorted(number for row in leaves for number in _event_numbers(retained, row)) == list(
            range(1, RANGE_EVENTS + 1)
        )
        assert all(row["status"] == "ready" for row in leaves)
        assert len(model.requests[-1]["context"]["events"]) <= CANDIDATE_BOUNDS["max_events"]


def test_duplicate_concurrent_callbacks_on_a_retried_range_count_one_failure(tmp_path, monkeypatch) -> None:
    retained = _retained(tmp_path, monkeypatch)
    with retained.world:
        world = retained.world
        _edit_to_candidate(retained)
        _at_next_retry(retained.world, retained.now)
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
        # One records the oversize failure; the other finds the page already changed and reports it as waiting.
        assert sorted(type(outcome).__name__ for outcome in outcomes) == ["InboxTooLarge", "SecretaryUnavailable"]
        assert retained.model.requests[-1]["context"]["events"] and len(retained.model.requests) == 4
        [after] = _range_rows(retained)
        assert (after["since"], after["through"], after["status"]) == (before["since"], before["through"], "retry")
        assert (after["attempts"], after["content_failures"]) == (before["attempts"] + 1, 1)

        # The duplicate cost nothing: the range still splits after exactly max_attempts counted refusals.
        refusals = 1
        for _ in _calls():
            if len(_range_rows(retained)) != 1:
                break
            _at_next_retry(retained.world, retained.now)
            calls = len(retained.model.requests)
            error, _ = asyncio.run(_hourly(retained.secretary))
            if len(retained.model.requests) == calls:
                assert isinstance(error, InboxTooLarge)
                refusals += 1
        assert refusals == BACKOFF["max_attempts"]
        assert [len(_event_numbers(retained, row)) for row in _range_rows(retained)] == [21, 20]


def test_each_callback_behind_a_scan_backlog_assesses_the_earliest_page_until_its_trigger_is_reached(
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

        # Each arriving message's callback assesses the next bounded page from the cursor. Its own trigger is not on
        # that page, so the page is assessed without it: the model is not told of it and the result names none, and
        # the trigger stays beyond the cursor for the host's admission to decide.
        arrivals, results = [], []
        for number in range(2):
            arrivals.append(say(world, rooms[number % 2], "reviewer", f"Arriving message {number}"))
            result, error, _ = asyncio.run(_callback(secretary, _trigger(arrivals[-1])))
            assert error is None and result is not None and result["kind"] == "summary"
            assert result["trigger"] is None and model.requests[-1]["context"]["trigger"] is None
            assert len(model.requests[-1]["context"]["events"]) == CANDIDATE_BOUNDS["max_events"]
            assert _position(world, arrivals[-1]["message_id"]) > _scanned(world)
            results.append(result)

        # The third callback's page reaches its trigger, which is then assessed on its page as before.
        arrivals.append(say(world, rooms[0], "reviewer", "Arriving message 2"))
        result, error, _ = asyncio.run(_callback(secretary, _trigger(arrivals[-1])))
        assert error is None and result is not None and result["trigger"] == _trigger(arrivals[-1])
        assert model.requests[-1]["context"]["trigger"] == _trigger(arrivals[-1])
        results.append(result)

        # Every event, the earlier arrivals' included, was assessed exactly once, in order and without a gap.
        assert _event_keys(results) == Counter(f"message:{sent['message_id']}" for sent in [*backlog, *arrivals])
        assert [result["since"] for result in results[1:]] == [result["through_pos"] for result in results[:-1]]
        assert len(model.requests) == 3


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

        # Three cheap refusals, each before any model or engine call; newer work goes ahead during the backoff.
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
        error, pending = asyncio.run(_hourly(secretary))
        assert isinstance(error, InboxTooLarge)
        assert len(model.requests) == calls + 1 and len(model.engine_requests) == engine_calls + 1  # the newer page

        # The notice cites the event, says no judgment was made, and is kept apart from the summaries.
        [notice] = [result for result in pending if result["kind"] == "failure_notice"]
        assert [result["kind"] for result in pending if result is not notice] == ["summary"]
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
