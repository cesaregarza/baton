"""A stalled caller never changes a stored page that has gone through another lifecycle step since it read the page.

A finish and an invalidation put a page back to an immediate retry with every counter at zero: the range, mode, status
and counters a stalled caller read. So a page's answer, like its failure, is taken only by the page exactly as it was
read, its due time included, and a scan stores its page only from the scanned position it read, which only grows. An
answer a page does not take stays saved: a later call under the same policy and labels finishes the page with it and
asks the model nothing, and after a policy edit it is never restored, reported pending or acknowledged.

The host runs the secretary through a message callback (`Secretary.decide` with the message as its trigger, then the
ready summaries) and the hourly's lanes. These tests use only those entry points, on a controllable clock, with one
caller stalled in its own thread where a process can lose the processor.
"""

from __future__ import annotations

import asyncio
import threading
from collections import Counter

import pytest
from secretary_support import conversation, make_world, policy, say
from test_secretary_call_identity import _pending, _saved
from test_secretary_recovery import _worker
from test_secretary_retained_bounds import (
    BACKOFF,
    BUDGET,
    HOLD,
    _at_next_retry,
    _callback,
    _delivered,
    _event_keys,
    _rows,
    _trigger,
)

from baton.agents.secretary.attention import decide as decide_module
from baton.agents.secretary.attention.backlog import Backlog
from baton.agents.secretary.attention.decide import SecretaryUnavailable
from baton.agents.secretary.attention.health import ModelHealth
from baton.agents.secretary.attention.state import AttentionLog
from baton.agents.secretary.service import Secretary
from baton.errors import InvalidRequest


class _Stalled:
    """A callback for this trigger, in its own thread, stalled at its first call of `owner.name` until it resumes."""

    def __init__(self, monkeypatch, world, owner, name: str, trigger: str) -> None:
        original = getattr(owner, name)
        self.entered, self.release, self.outcome = threading.Event(), threading.Event(), []

        def stalled(*args, **kwargs):
            if not self.entered.is_set():
                self.entered.set()
                assert self.release.wait(30)
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, stalled)

        def callback() -> None:
            try:
                other = Secretary(world.config, world.state_path, world.policy_path)
                self.outcome.append(asyncio.run(other.decide(budget_seconds=BUDGET, trigger=trigger)))
            except BaseException as raised:  # the outcome is asserted by the test
                self.outcome.append(raised)

        self.thread = threading.Thread(target=callback)
        self.thread.start()
        assert self.entered.wait(30)

    def resume(self) -> BaseException | dict | None:
        self.release.set()
        self.thread.join(30)
        assert len(self.outcome) == 1
        return self.outcome[0]


def _retried(tmp_path, monkeypatch, steps: list[dict]):
    """A page of two messages that one callback's model failure left as a due retry, with these answers to come."""
    world = make_world(tmp_path)
    worker, model = _worker(world, tmp_path, plan={"steps": [{"exit": 1}, *steps]})
    policy(world, **BACKOFF)
    secretary = worker.instance.secretary
    now = [1000.0]
    monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
    asyncio.run(secretary.activate())
    room = conversation(world)
    sent = [say(world, room, "worker", f"Retried update {number}") for number in range(2)]
    result, error, _ = asyncio.run(_callback(secretary, _trigger(sent[-1])))
    assert result is None and isinstance(error, SecretaryUnavailable)
    _at_next_retry(world, now)
    return world, model, secretary, now, sent


def _finished_then_invalidated(world, secretary, now: list[float], trigger: str) -> tuple[dict, dict]:
    """Another callback summarizes the page; a policy edit then makes the summary stale, so the page is an immediate
    retry again with every counter at zero, at a later due time: the summary, and the page as it is stored now."""
    finished, error, pending = asyncio.run(_callback(secretary, trigger))
    assert error is None and finished is not None and finished["kind"] == "summary"
    assert [summary["decision_id"] for summary in pending] == [finished["decision_id"]]
    now[0] += 1
    policy(world, summary_bytes=4096)
    assert asyncio.run(_pending(secretary)) == []
    [invalidated] = _rows(world)
    assert (invalidated["status"], invalidated["mode"], invalidated["attempts"], invalidated["content_failures"]) == (
        "retry",
        "alert",
        0,
        0,
    )
    assert invalidated["next_attempt"] == now[0] and invalidated["decision"] is None
    return finished, invalidated


def _reassessed_once(world, model, secretary, trigger: str, stale: dict, invalidated: dict) -> None:
    """The next callback assesses the page at once under the new policy. The stale answer stays saved, and is never
    restored, reported pending or acknowledged, and only the new summary delivers the page."""
    delivered = _delivered(world)
    calls = len(model.requests)
    reassessed, error, pending = asyncio.run(_callback(secretary, trigger))
    assert error is None and reassessed is not None and reassessed["kind"] == "summary"
    assert len(model.requests) == calls + 1
    assert reassessed["policy_digest"] != stale["policy_digest"] and reassessed["decision_id"] != stale["decision_id"]
    assert [summary["decision_id"] for summary in pending] == [reassessed["decision_id"]]
    assert stale["decision_id"] in {result["decision_id"] for result in _saved(world)}
    with pytest.raises(InvalidRequest, match="stale"):
        asyncio.run(secretary.acknowledge(stale["decision_id"]))
    assert _delivered(world) == delivered
    asyncio.run(secretary.acknowledge(reassessed["decision_id"]))
    assert _delivered(world) == invalidated["through"]
    assert asyncio.run(_pending(secretary)) == []


def _baseline(world) -> tuple[int, str]:
    """The stored page's model baseline: the successes count and policy its last answer or failure recorded."""
    log = AttentionLog(world.state_path, world.config.data_dir, "planner")
    with log.connection() as conn:
        row = conn.execute("SELECT model_successes,model_policy FROM backlog_windows WHERE principal='planner'").one()
    return row[0], row[1]


@pytest.mark.parametrize("stale", ["hold", "summary"])
def test_a_stalled_answer_never_finishes_a_page_that_was_finished_and_invalidated_since(
    tmp_path, monkeypatch, stale
) -> None:
    world, model, secretary, now, sent = _retried(tmp_path, monkeypatch, [HOLD if stale == "hold" else {}, {}, {}])
    with world:
        trigger = _trigger(sent[-1])

        # Callback A gets its model's answer for the retried page under the policy as it is, and stalls before it
        # saves the answer and finishes the page. Meanwhile callback B, for the page's other message, summarizes the
        # page, and a policy edit invalidates that summary.
        stalled = _Stalled(monkeypatch, world, decide_module, "persist", trigger)
        _, invalidated = _finished_then_invalidated(world, secretary, now, _trigger(sent[0]))

        # A's answer is saved, but the page has been finished and invalidated since A read it, so it takes nothing:
        # a hold would have put the immediate retry off to a digest due later, and a summary would have finished it
        # under the old policy. A's call gets no result from the page, which is waiting to be reassessed.
        outcome = stalled.resume()
        assert _rows(world) == [invalidated]
        assert isinstance(outcome, SecretaryUnavailable) and "waiting for retry backoff" in str(outcome)
        [answer] = [result for result in _saved(world) if result["trigger"] == trigger]
        assert answer["decision"] == ("hold" if stale == "hold" else "wake") and bool(answer["brief"]) == (
            stale == "summary"
        )
        _reassessed_once(world, model, secretary, trigger, answer, invalidated)


def test_a_stalled_restore_never_finishes_a_page_that_was_finished_and_invalidated_since(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path, plan={"steps": [{"exit": 1}]})
        policy(world, **BACKOFF)
        secretary = worker.instance.secretary
        now = [1000.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        room = conversation(world)
        sent = [say(world, room, "worker", f"Saved update {number}") for number in range(2)]
        trigger = _trigger(sent[-1])

        # The engine requires attention and the model is down, so a callback's process saves the engine's notice, with
        # no model answer and so no model baseline, and is killed before it finishes the page: a fresh retry with no
        # attempt, due at once, every counter and baseline zero, beside a saved result.
        model.set_engine_plan({"decision": "wake", "wake_probability": 0.95})
        original = Backlog.finish

        def killed(self, *args, **kwargs):
            raise RuntimeError("synthetic kill between saving a result and finishing its page")

        monkeypatch.setattr(Backlog, "finish", killed)
        with pytest.raises(RuntimeError, match="synthetic kill"):
            asyncio.run(secretary.decide(budget_seconds=BUDGET, trigger=trigger))
        monkeypatch.setattr(Backlog, "finish", original)
        model.set_plan({})
        [read] = _rows(world)
        assert (read["status"], read["attempts"], read["next_attempt"]) == ("retry", 0, now[0])
        assert _baseline(world) == (0, "")
        [saved] = _saved(world)
        assert saved["kind"] == "engine_wake" and saved["trigger"] == trigger

        # Callback A, for the same message, finds that result and stalls before it finishes the page with it.
        # Meanwhile callback B, for the other message, finishes the page with it, and a policy edit invalidates it.
        # The page is back to what A read in every column but its due time.
        stalled = _Stalled(monkeypatch, world, Backlog, "finish", trigger)
        restored, error, pending = asyncio.run(_callback(secretary, _trigger(sent[0])))
        assert error is None and restored is not None and restored["event_count"] == 0
        assert [summary["decision_id"] for summary in pending] == [saved["decision_id"]]
        now[0] += 1
        policy(world, summary_bytes=4096)
        assert asyncio.run(_pending(secretary)) == []
        [invalidated] = _rows(world)
        assert {key: value for key, value in invalidated.items() if key != "next_attempt"} == {
            key: value for key, value in read.items() if key != "next_attempt"
        }
        assert invalidated["next_attempt"] == now[0] > read["next_attempt"] and _baseline(world) == (0, "")

        # The due time tells them apart, so A's restore finishes nothing, and A gets nothing from the page.
        outcome = stalled.resume()
        assert _rows(world) == [invalidated]
        assert isinstance(outcome, SecretaryUnavailable) and "waiting for retry backoff" in str(outcome)
        assert len(model.requests) == 1
        _reassessed_once(world, model, secretary, trigger, saved, invalidated)


@pytest.mark.parametrize("edit", [False, True], ids=["same_policy", "policy_edit"])
def test_a_valid_answer_that_a_failure_recorded_since_refuses_is_restored_only_under_its_own_policy(
    tmp_path, monkeypatch, edit
) -> None:
    world, model, secretary, now, sent = _retried(tmp_path, monkeypatch, [{}, {"exit": 1}, {}])
    with world:
        trigger = _trigger(sent[-1])

        # Callback A gets a valid answer and stalls before saving it. Meanwhile a duplicate callback for the same
        # message fails on the same page, which records a second attempt and backs off.
        stalled = _Stalled(monkeypatch, world, decide_module, "persist", trigger)
        result, error, _ = asyncio.run(_callback(secretary, trigger))
        assert result is None and isinstance(error, SecretaryUnavailable)
        [failed] = _rows(world)
        assert (failed["status"], failed["attempts"]) == ("retry", 2)

        # The page changed since A read it, so it does not take A's answer: A's call gets no result from it, and the
        # answer stays saved.
        outcome = stalled.resume()
        assert _rows(world) == [failed]
        assert isinstance(outcome, SecretaryUnavailable) and "waiting for retry backoff" in str(outcome)
        [answer] = _saved(world)
        assert answer["kind"] == "summary" and answer["trigger"] == trigger
        _at_next_retry(world, now)
        calls = len(model.requests)
        if not edit:
            # Under the same policy, the next call with the same labels finishes the page with the saved answer and
            # gets it as its own, with no model call; it is the summary that is pending.
            restored, error, pending = asyncio.run(_callback(secretary, trigger))
            assert error is None and restored is not None and restored["decision_id"] == answer["decision_id"]
            assert len(model.requests) == calls
            assert [summary["decision_id"] for summary in pending] == [answer["decision_id"]]
            assert _event_keys(pending) == Counter(f"message:{item['message_id']}" for item in sent)
            return
        # After a policy edit the saved answer is never restored or delivered: the page is assessed again.
        policy(world, summary_bytes=4096)
        _reassessed_once(world, model, secretary, trigger, answer, failed)


def test_a_stalled_answers_baseline_on_a_reassessed_page_never_delays_its_reassessment(tmp_path, monkeypatch) -> None:
    world, model, secretary, now, sent = _retried(tmp_path, monkeypatch, [{}, {}, {"exit": 1}, {}])
    with world:
        trigger = _trigger(sent[-1])

        # Callback A's model answers, and A stalls before it records the answer as the page's baseline. Meanwhile
        # callback B summarizes the page and a policy edit invalidates the summary.
        stalled = _Stalled(monkeypatch, world, ModelHealth, "succeeded", trigger)
        finished, invalidated = _finished_then_invalidated(world, secretary, now, _trigger(sent[0]))

        # A's baseline lands on the reassessment's page: it records that range's own answer, under the old policy.
        # It needs no snapshot, and changes nothing else: A's finish still refuses the page, which stays due at once
        # with no attempts.
        outcome = stalled.resume()
        assert _rows(world) == [invalidated]
        assert isinstance(outcome, SecretaryUnavailable)
        successes, recorded = _baseline(world)
        assert successes > 0 and recorded == finished["policy_digest"]

        # The next callback reassesses the page at once. Its failure records its own baseline under the new policy
        # over A's, and the next attempt after the backoff summarizes the page.
        result, error, _ = asyncio.run(_callback(secretary, trigger))
        assert result is None and isinstance(error, SecretaryUnavailable)
        [retried] = _rows(world)
        assert (retried["attempts"], retried["content_failures"]) == (1, 0)
        assert _baseline(world)[1] not in {"", finished["policy_digest"]}
        _at_next_retry(world, now)
        reassessed, error, pending = asyncio.run(_callback(secretary, trigger))
        assert error is None and reassessed is not None and reassessed["kind"] == "summary"
        assert [summary["decision_id"] for summary in pending] == [reassessed["decision_id"]]


def test_a_stalled_scan_never_stores_a_page_beside_the_one_another_scan_stored_since(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        worker, model = _worker(world, tmp_path)
        policy(world, **BACKOFF)
        secretary = worker.instance.secretary
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: 1000.0)
        asyncio.run(secretary.activate())
        room = conversation(world)
        sent = [say(world, room, "worker", f"Scanned update {number}") for number in range(2)]

        # Callback A reads the page of two messages and stalls before it stores it. Meanwhile a third message
        # arrives, and its callback scans from the same position: one page of all three, summarized.
        stalled = _Stalled(monkeypatch, world, Backlog, "enqueue", _trigger(sent[-1]))
        sent.append(say(world, room, "worker", "A later update"))
        finished, error, pending = asyncio.run(_callback(secretary, _trigger(sent[-1])))
        assert error is None and finished is not None and finished["kind"] == "summary"
        assert [summary["decision_id"] for summary in pending] == [finished["decision_id"]]
        ready = _rows(world)
        calls = len(model.requests)

        # The scanned position has moved past where A started, so A stores nothing and asks the model nothing: no
        # event is in two stored pages, or in two summaries.
        outcome = stalled.resume()
        assert _rows(world) == ready
        assert len(model.requests) == calls
        assert isinstance(outcome, SecretaryUnavailable)
        _, _, after = asyncio.run(_callback(secretary, _trigger(sent[-1])))
        assert [summary["decision_id"] for summary in after] == [finished["decision_id"]]
        assert _event_keys(after) == Counter(f"message:{item['message_id']}" for item in sent)
