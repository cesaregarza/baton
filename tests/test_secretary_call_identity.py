"""Each call gets a result of its own, and a stored page changes only as it was read.

Two rules, each with more than one place to keep it. A saved or cached result is reused for a call only when it carries
that call's identity: its trigger, or none, and its catch-up label. That holds for the cache, for the notices that
concurrent calls converge on, and for the saved result that recovery finds when a call stopped between saving a result
and finishing its page, on an alert page or a digest, and the queue labels a page by the rule the assessment applies.
And a compare-and-set compares the whole row it read, so a caller that stalled after reading a page never undoes what
another caller finished since: not a split, a combine, a failure, a finish or a discard of an empty page.

The host runs the secretary through a message callback (`Secretary.decide` with the message as its trigger) and the
hourly (the queue's scan lane, then `digest` if a held page is due, else `retry`). These tests use only those entry
points, on a controllable clock, with stalls placed where a process can lose the processor or be killed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading

import pytest
from secretary_support import conversation, make_world, policy, say
from test_secretary_recovery import _worker
from test_secretary_retained_bounds import (
    BACKOFF,
    BUDGET,
    CANDIDATE_BOUNDS,
    HOLD,
    _at_next_retry,
    _callback,
    _delivered,
    _retrying,
    _rows,
    _trigger,
)

from baton import conversations
from baton.agents.secretary.attention import decide as decide_module
from baton.agents.secretary.attention.backlog import Backlog
from baton.agents.secretary.attention.queue import AttentionQueue
from baton.agents.secretary.attention.runtime import DatabaseWorker
from baton.agents.secretary.attention.state import AttentionLog
from baton.agents.secretary.inbox.window import InboxWindow
from baton.agents.secretary.service import Secretary
from baton.errors import BatonError

KINDS = ("untriggered", "catch_up", "on_page")


def _labels(result: dict) -> tuple[str | None, bool]:
    return result["trigger"], result.get("catch_up", False)


def _own(world, result: dict, trigger: str | None) -> tuple[str | None, bool]:
    """The labels a result returned to a call with this trigger must carry: the trigger when the result's page holds
    its event, catch-up when the event is elsewhere, neither for an untriggered call."""
    if trigger is None:
        return None, False
    seq = int(trigger.removeprefix("message:"))
    row = world.baton.execute(
        "SELECT pos FROM events WHERE kind='message' AND audience='planner' AND seq=?", (seq,)
    ).fetchone()
    return (trigger, False) if result["since"] < row["pos"] <= result["through_pos"] else (None, True)


async def _lane(secretary: Secretary, name: str) -> dict | None:
    """One of the hourly's lanes, as the host calls it."""
    async with DatabaseWorker() as database:
        queue = await AttentionQueue.open(secretary, database, budget_seconds=BUDGET)
        return await getattr(queue, name)(BUDGET)


async def _pending(secretary: Secretary) -> list[dict]:
    async with DatabaseWorker() as database:
        queue = await AttentionQueue.open(secretary, database, budget_seconds=BUDGET)
        return await queue.pending()


def _saved(world) -> list[dict]:
    return AttentionLog(world.state_path, world.config.data_dir, "planner").recent()


class _Recovery:
    """A page with two kinds of result to save: a summary on a retried page of two messages, or a failure notice on
    a page of one message larger than the byte bound. A later message lies beyond the page."""

    def __init__(self, tmp_path, monkeypatch, kind: str) -> None:
        self.world = make_world(tmp_path)
        self.worker, self.model = _worker(self.world, tmp_path)
        if kind == "summary":
            policy(self.world, **BACKOFF)
        else:
            # One refusal exhausts the page, so the first call that reaches it builds the notice.
            policy(self.world, **CANDIDATE_BOUNDS, **{**BACKOFF, "max_attempts": 1})
        self.secretary = self.worker.instance.secretary
        self.now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: self.now[0])
        asyncio.run(self.secretary.activate())
        room = conversation(self.world)
        if kind == "summary":
            self.events = [say(self.world, room, "worker", f"Retried update {number}") for number in range(2)]
            self.model.set_plan({"steps": [{"exit": 1}]})
            result, error, _ = asyncio.run(_callback(self.secretary, _trigger(self.events[-1])))
            assert result is None and error is not None
            self.model.set_plan({})
            _at_next_retry(self.world, self.now)
        else:
            self.events = [say(self.world, room, "worker", "x" * (CANDIDATE_BOUNDS["context_bytes"] + 1024))]
        self.later = say(self.world, room, "reviewer", "A later update beyond the page")
        self.kind = kind

    def trigger(self, kind: str, *, first: bool) -> str | None:
        """The trigger a call of this kind passes. An on-page call that recovers uses another event of the page when
        the page has one, so its trigger differs from the saver's."""
        if kind == "untriggered":
            return None
        if kind == "catch_up":
            return _trigger(self.later)
        return _trigger(self.events[-1] if first or len(self.events) == 1 else self.events[0])

    def call(self, kind: str, trigger: str | None, *, first: bool) -> dict | None:
        if kind != "untriggered":
            return asyncio.run(self.secretary.decide(budget_seconds=BUDGET, trigger=trigger))
        # The hourly reaches a stored page through its retry lane, and the new oversized page first through its scan.
        return asyncio.run(_lane(self.secretary, "scan" if first and self.kind == "failure_notice" else "retry"))


@pytest.mark.parametrize("recoverer", KINDS)
@pytest.mark.parametrize("saver", KINDS)
@pytest.mark.parametrize("kind", ["summary", "failure_notice"])
def test_a_result_saved_before_its_page_finished_is_returned_only_to_a_call_with_its_labels(
    tmp_path, monkeypatch, kind, saver, recoverer
) -> None:
    recovery = _Recovery(tmp_path, monkeypatch, kind)
    with recovery.world:
        world, model = recovery.world, recovery.model
        before = len(model.requests)

        # The saver's process dies after its result is saved and before its page is finished.
        original, crashed = Backlog.finish, []

        def crash_once(self, *args, **kwargs):
            if not crashed:
                crashed.append(True)
                raise RuntimeError("synthetic crash after the result was saved")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Backlog, "finish", crash_once)
        saver_trigger = recovery.trigger(saver, first=True)
        with pytest.raises(RuntimeError, match="after the result was saved"):
            recovery.call(saver, saver_trigger, first=True)
        monkeypatch.setattr(Backlog, "finish", original)
        [saved] = [result for result in _saved(world) if result["kind"] == kind]
        # Saved with the saver's own labels, so a fresh notice is labelled as a fresh summary is.
        assert _labels(saved) == _own(world, saved, saver_trigger)
        [page] = _retrying(world)
        assert (page["since"], page["through"]) == (saved["since"], saved["through_pos"])
        asked = len(model.requests)
        assert asked == before + (kind == "summary")
        _at_next_retry(world, recovery.now)

        # A later call of either kind finds the saved result. It finishes the page with it, with no model call for
        # that page, but returns it only to a call whose labels it carries: otherwise this call's result is its own,
        # from a later page, or none.
        recoverer_trigger = recovery.trigger(recoverer, first=False)
        result = recovery.call(recoverer, recoverer_trigger, first=False)
        if result is not None:
            assert _labels(result) == _own(world, result, recoverer_trigger)
        if _labels(saved) == _own(world, saved, recoverer_trigger):
            assert result is not None and result["decision_id"] == saved["decision_id"]
        assert all(request["context"]["since"] != saved["since"] for request in model.requests[asked:])
        [row] = [row for row in _rows(world) if row["since"] == saved["since"]]
        assert (row["status"], row["decision"]) == ("ready", saved["decision_id"])

        # What is pending is the saved result, still with the labels of the call that made it.
        pending = asyncio.run(recovery.secretary.pending(saved["decision_id"]))
        assert pending is not None and _labels(pending) == _labels(saved)


class _DigestRecovery:
    """A page of one message held for a digest, and due, with two kinds of result to save on it: a digest, or the
    failure notice for a digest that keeps failing. The hourly's digest lane reaches it first. After one failure there
    it is a digest retry, which the hourly's retry lane resumes, and so does a callback, since a callback resumes any
    due retry before it scans."""

    def __init__(self, tmp_path, monkeypatch, kind: str, saver: str) -> None:
        self.world = make_world(tmp_path)
        self.worker, self.model = _worker(self.world, tmp_path)
        # The notice comes with the invalid answer that exhausts the page: the digest lane's first, or the second.
        policy(self.world, **{**BACKOFF, "max_attempts": 1 if saver == "digest" else 2}, digest_seconds=60)
        self.secretary = self.worker.instance.secretary
        self.now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: self.now[0])
        asyncio.run(self.secretary.activate())
        room = conversation(self.world)
        # The callbacks' trigger is the held message itself, so the digest page holds it.
        self.trigger = _trigger(say(self.world, room, "worker", "Held update"))
        self.model.set_plan({"steps": [HOLD]})
        result, error, _ = asyncio.run(_callback(self.secretary, self.trigger))
        assert error is None and result is not None and result["brief"] == ""
        [held] = _rows(self.world)
        assert (held["status"], held["mode"]) == ("held", "digest")
        self.now[0] = held["next_attempt"]
        failure = {"exit": 1} if kind == "summary" else {"raw": "{}"}
        if saver != "digest":
            self.model.set_plan({"steps": [failure]})
            assert asyncio.run(_lane(self.secretary, "digest")) is None
            [retry] = _retrying(self.world)
            assert (retry["mode"], retry["attempts"]) == ("digest", 1)
            _at_next_retry(self.world, self.now)
        answer = {"decision": "keep", "summary": "The held update.", "sources": "all"} if kind == "summary" else failure
        self.model.set_plan({"steps": [answer]})

    def call(self, caller: str) -> dict | None:
        if caller == "callback":
            return asyncio.run(self.secretary.decide(budget_seconds=BUDGET, trigger=self.trigger))
        return asyncio.run(_lane(self.secretary, caller))

    @staticmethod
    def labels(caller: str) -> tuple[str | None, bool]:
        """A digest never names a trigger, so a callback's result on one is catch-up, though the page holds its
        trigger; the hourly's is neither."""
        return None, caller == "callback"


@pytest.mark.parametrize("recoverer", ["retry", "callback"])
@pytest.mark.parametrize("saver", ["digest", "retry", "callback"])
@pytest.mark.parametrize("kind", ["summary", "failure_notice"])
def test_a_digest_saved_before_its_page_finished_is_returned_only_to_a_call_with_its_labels(
    tmp_path, monkeypatch, kind, saver, recoverer
) -> None:
    recovery = _DigestRecovery(tmp_path, monkeypatch, kind, saver)
    with recovery.world:
        world, model = recovery.world, recovery.model

        # The saver's process dies after its result is saved and before its page is finished, as an hourly killed at
        # its unit's time limit would.
        original, crashed = Backlog.finish, []

        def crash_once(self, *args, **kwargs):
            if not crashed:
                crashed.append(True)
                raise RuntimeError("synthetic crash after the result was saved")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Backlog, "finish", crash_once)
        with pytest.raises(RuntimeError, match="after the result was saved"):
            recovery.call(saver)
        monkeypatch.setattr(Backlog, "finish", original)
        [saved] = [result for result in _saved(world) if result["kind"] == kind and result["mode"] == "digest"]
        # Saved with the saver's own labels: a callback's digest, and the notice a callback builds when it exhausts
        # a digest retry, are catch-up.
        assert _labels(saved) == recovery.labels(saver)
        [page] = _retrying(world)
        assert (page["since"], page["through"], page["mode"]) == (saved["since"], saved["through_pos"], "digest")
        asked = len(model.requests)
        _at_next_retry(world, recovery.now)

        # The other caller finishes the page with the saved result and asks no provider, but gets it as its own only
        # when it carries that caller's labels. Otherwise a callback gets its own result for the next page, and the
        # retry lane none.
        result = recovery.call(recoverer)
        if result is not None:
            assert _labels(result) == recovery.labels(recoverer)
        if _labels(saved) == recovery.labels(recoverer):
            assert result is not None and result["decision_id"] == saved["decision_id"]
        else:
            assert result is None or result["decision_id"] != saved["decision_id"]
        assert len(model.requests) == asked
        [row] = [row for row in _rows(world) if row["since"] == saved["since"]]
        assert (row["status"], row["decision"]) == ("ready", saved["decision_id"])
        pending = asyncio.run(recovery.secretary.pending(saved["decision_id"]))
        assert pending is not None and _labels(pending) == _labels(saved)


@pytest.mark.parametrize("kind", ["summary", "failure_notice"])
def test_a_trigger_at_the_end_of_a_page_is_that_pages_and_the_next_page_is_catch_up(
    tmp_path, monkeypatch, kind
) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path)
        # One refusal exhausts an oversized page of one message, so its first call builds the notice.
        policy(world, **CANDIDATE_BOUNDS, **{**BACKOFF, "max_attempts": 1})
        secretary = worker.instance.secretary
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: 1000.0)
        asyncio.run(secretary.activate())
        # With no other participant, the planner's copy of each message is the newest event, so a page ends at it.
        room = conversation(world, ["planner", "worker"])
        trigger = _trigger(say(world, room, "worker", "The first update"))
        first, error, _ = asyncio.run(_callback(secretary, trigger))
        assert error is None and first is not None and first["kind"] == "summary"
        [row] = world.baton.execute(
            "SELECT pos FROM events WHERE kind='message' AND audience='planner' AND seq=?",
            (int(trigger.removeprefix("message:")),),
        ).fetchall()
        # The trigger is the last event of its page, which names it.
        assert first["through_pos"] == row["pos"] and _labels(first) == (trigger, False)

        # The next page starts at the trigger's position. A callback for the same trigger again, as a deferred copy of
        # its wake is, assesses that page as catch-up: its summary, or the notice the queue builds for it, never
        # names the trigger.
        text = "A later update" if kind == "summary" else "x" * (CANDIDATE_BOUNDS["context_bytes"] + 1024)
        say(world, room, "worker", text)
        later, error, _ = asyncio.run(_callback(secretary, trigger))
        assert error is None and later is not None and later["kind"] == kind
        assert later["since"] == row["pos"] and _labels(later) == (None, True)


def test_an_engine_notice_for_one_trigger_is_never_the_result_of_a_call_for_another(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path)
        policy(world, **BACKOFF)
        # The engine requires a wake that the model does not choose, so each call's result is the engine's notice.
        model.set_plan(HOLD)
        model.set_engine_plan({"decision": "wake", "wake_probability": 0.9})
        secretary = worker.instance.secretary
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: 1000.0)
        _, floor = asyncio.run(secretary.activate())
        room = conversation(world)
        sent = [say(world, room, "worker", f"Engine update {number}") for number in range(3)]
        explicit = {"since": floor, "floor": floor}

        # Three calls on the same window: two for different events on it, and one with none. Each notice is the
        # call's own, under its own id, rather than the first call's that the others would converge on.
        results = {
            trigger: asyncio.run(secretary.decide(budget_seconds=BUDGET, trigger=trigger, **explicit))
            for trigger in (_trigger(sent[0]), _trigger(sent[-1]), None)
        }
        for trigger, result in results.items():
            assert result["kind"] == "engine_wake"
            assert _labels(result) == (trigger, False)
        assert len({result["decision_id"] for result in results.values()}) == len(results)


def _held(tmp_path, monkeypatch):
    """One page held for a digest, due now: a callback whose model holds it, then the digest delay."""
    world = make_world(tmp_path)
    worker, model = _worker(world, tmp_path)
    policy(world, **BACKOFF, digest_seconds=60)
    secretary = worker.instance.secretary
    now = [1000.0]
    monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
    asyncio.run(secretary.activate())
    room = conversation(world)
    sent = [say(world, room, "worker", f"Held update {number}") for number in range(2)]
    return world, model, secretary, now, sent


def test_a_stale_held_snapshot_never_replaces_a_digest_another_lane_finished_since(tmp_path, monkeypatch) -> None:
    world, model, secretary, now, sent = _held(tmp_path, monkeypatch)
    with world:
        model.set_plan({"steps": [HOLD, {"decision": "keep", "summary": "The held updates.", "sources": "all"}]})
        result, error, _ = asyncio.run(_callback(secretary, _trigger(sent[-1])))
        assert error is None and result is not None and result["brief"] == ""
        [held] = _rows(world)
        assert held["status"] == "held"
        now[0] = held["next_attempt"]

        # One scheduled lane reads the held page, then stalls before it combines it. Another lane digests the same
        # page meanwhile: it is ready, with its digest, and every counter is zero again, as on the held page.
        original = Backlog.replace
        entered, release, after = threading.Event(), threading.Event(), []

        def stalled(self, *args, **kwargs):
            if not entered.is_set():
                entered.set()
                assert release.wait(30)
                replaced = original(self, *args, **kwargs)
                after.append((replaced, _rows(world)))
                return replaced
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Backlog, "replace", stalled)
        outcome: list[BaseException | dict | None] = []

        def stalled_lane() -> None:
            try:
                other = Secretary(world.config, world.state_path, world.policy_path)
                outcome.append(asyncio.run(_lane(other, "digest")))
            except BaseException as raised:  # the outcome is asserted below
                outcome.append(raised)

        thread = threading.Thread(target=stalled_lane)
        thread.start()
        assert entered.wait(30)
        digest = asyncio.run(_lane(secretary, "digest"))
        assert digest is not None and digest["mode"] == "digest" and digest["brief"] == "The held updates."
        ready = _rows(world)
        assert [(row["status"], row["decision"]) for row in ready] == [("ready", digest["decision_id"])]
        calls = len(model.requests)
        release.set()
        thread.join(30)

        # The stalled lane compares the whole row it read, held and undecided, so it changes nothing: the ready digest
        # is never put back to retry, and that lane neither asks the model nor reports the digest as its own.
        assert outcome == [None]
        assert after and after[0][0] is None and after[0][1] == ready
        assert _rows(world) == ready and len(model.requests) == calls
        assert [summary["decision_id"] for summary in asyncio.run(_pending(secretary))] == [digest["decision_id"]]


def test_a_stalled_failure_never_holds_back_a_page_that_was_finished_and_invalidated_since(
    tmp_path, monkeypatch
) -> None:
    world, model, secretary, now, sent = _held(tmp_path, monkeypatch)
    with world:
        trigger = _trigger(sent[-1])
        model.set_plan({"steps": [{"exit": 1}]})

        # A callback's model call fails on the new page, and the callback stalls before it records the failure.
        original = Backlog.failed
        entered, release = threading.Event(), threading.Event()

        def stalled(self, *args, **kwargs):
            if not entered.is_set():
                entered.set()
                assert release.wait(30)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Backlog, "failed", stalled)
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

        # Meanwhile another callback assesses the page, and a policy edit then makes its summary stale, so the page is
        # due again at once with no attempts: the same counters the stalled callback read, at a later due time.
        model.set_plan({})
        finished, error, _ = asyncio.run(_callback(secretary, trigger))
        assert error is None and finished is not None and finished["kind"] == "summary"
        now[0] += 1
        policy(world, summary_bytes=4096)
        assert asyncio.run(_pending(secretary)) == []
        [invalidated] = _rows(world)
        assert (invalidated["status"], invalidated["attempts"], invalidated["next_attempt"]) == ("retry", 0, now[0])
        release.set()
        thread.join(30)
        assert outcome and isinstance(outcome[0], BatonError)

        # The stalled failure was for the page as it read it, so it is not recorded: the reassessment is not held in
        # a backoff, and its next callback assesses it at once.
        assert _rows(world) == [invalidated]
        reassessed, error, pending = asyncio.run(_callback(secretary, trigger))
        assert error is None and reassessed is not None and reassessed["kind"] == "summary"
        assert [summary["decision_id"] for summary in pending] == [reassessed["decision_id"]]


def test_a_stalled_alert_answer_never_finishes_the_digest_its_page_became_since(tmp_path, monkeypatch) -> None:
    world, model, secretary, now, sent = _held(tmp_path, monkeypatch)
    with world:
        trigger = _trigger(sent[-1])
        # The callback's answer wakes; the hourly's retry lane later holds the same page; its digest fails once, then
        # summarizes.
        model.set_plan({"steps": [{}, HOLD, {"exit": 1}, {}]})
        model.set_engine_plan({"decision": "keep", "wake_probability": 0.1})

        # The callback stalls after its model call, before it saves the answer.
        original = decide_module.persist
        entered, release = threading.Event(), threading.Event()

        def stalled(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                assert release.wait(30)
            return original(*args, **kwargs)

        monkeypatch.setattr(decide_module, "persist", stalled)
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

        # Meanwhile the page is held, combined into a digest of exactly its range, and that digest fails once: a
        # retry of the same range, now in digest mode.
        assert asyncio.run(_lane(secretary, "retry")) is not None
        [held] = _rows(world)
        assert (held["status"], held["mode"]) == ("held", "digest")
        now[0] = held["next_attempt"]
        assert asyncio.run(_lane(secretary, "digest")) is None
        [digest] = _rows(world)
        assert (digest["status"], digest["mode"], digest["attempts"]) == ("retry", "digest", 1)
        release.set()
        thread.join(30)
        assert outcome and isinstance(outcome[0], dict) and outcome[0]["mode"] == "alert"

        # The alert answer finishes only an alert page, so the digest retry stays as it was and is what recovers.
        assert _rows(world) == [digest]
        assert asyncio.run(_pending(secretary)) == []
        _at_next_retry(world, now)
        recovered = asyncio.run(_lane(secretary, "retry"))
        assert recovered is not None and recovered["mode"] == "digest"
        assert (recovered["since"], recovered["through_pos"]) == (digest["since"], digest["through"])
        assert [summary["decision_id"] for summary in asyncio.run(_pending(secretary))] == [recovered["decision_id"]]


def test_a_stalled_empty_read_never_discards_a_page_another_callback_finished_since(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path)
        policy(world, **BACKOFF)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        room = conversation(world)
        sent = [say(world, room, "worker", f"Retried update {number}") for number in range(2)]
        trigger = _trigger(sent[-1])
        model.set_plan({"steps": [{"exit": 1}]})
        result, error, _ = asyncio.run(_callback(secretary, trigger))
        assert result is None and error is not None
        model.set_plan({})
        _at_next_retry(world, now)
        delivered = _delivered(world)

        # One callback reads the retried page as it would if its events had stopped being visible, and stalls before
        # it discards the page as empty. Every later read is the page as it is.
        original_read, empty = InboxWindow.read, [True]

        def read(cls, *args, **kwargs):
            window = original_read(*args, **kwargs)
            return dataclasses.replace(window, events=[], reply_context=[]) if empty[0] else window

        monkeypatch.setattr(InboxWindow, "read", classmethod(read))
        original = Backlog.discard_empty
        entered, release = threading.Event(), threading.Event()

        def stalled(self, *args, **kwargs):
            if not entered.is_set():
                empty[0] = False
                entered.set()
                assert release.wait(30)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Backlog, "discard_empty", stalled)
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

        # Meanwhile another callback reads the page as it is and summarizes it.
        finished, error, pending = asyncio.run(_callback(secretary, trigger))
        assert error is None and finished is not None and finished["kind"] == "summary"
        assert [summary["decision_id"] for summary in pending] == [finished["decision_id"]]
        ready = _rows(world)
        release.set()
        thread.join(30)

        # The stalled callback's discard was for the page as it read it, so it changes nothing: the ready page is not
        # deleted, its range is not acknowledged as empty, and its summary is still the one to deliver.
        assert outcome and isinstance(outcome[0], dict) and outcome[0]["event_count"] == 0
        assert _rows(world) == ready and _delivered(world) == delivered
        assert [summary["decision_id"] for summary in asyncio.run(_pending(secretary))] == [finished["decision_id"]]


def test_an_exhausted_page_whose_events_stop_being_visible_is_discarded_as_the_failure_stored_it(
    tmp_path, monkeypatch
) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path)
        # One refusal exhausts an oversized page of one message.
        policy(world, **CANDIDATE_BOUNDS, **{**BACKOFF, "max_attempts": 1})
        secretary = worker.instance.secretary
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: 1000.0)
        asyncio.run(secretary.activate())
        room = conversation(world)
        say(world, room, "worker", "x" * (CANDIDATE_BOUNDS["context_bytes"] + 1024))
        [event] = world.baton.execute("SELECT pos FROM events WHERE kind='message' AND audience='planner'").fetchall()

        # The planner leaves the conversation after the refusal is stored and before its notice reads what to cite,
        # so the notice has nothing to cite and the page has no visible event.
        original = AttentionQueue._failure_notice

        async def leave(self, *args, **kwargs):
            conversations.remove(world.baton, world.config, room, "planner", actor="planner")
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(AttentionQueue, "_failure_notice", leave)
        assert asyncio.run(_lane(secretary, "scan")) is None

        # The page is discarded as the failure left it, and its range is acknowledged as empty.
        assert _rows(world) == [] and _delivered(world) == event["pos"]
        assert model.requests == []
