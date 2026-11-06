"""Host callbacks retain their guarantees across overlapping calls and process death."""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
from secretary_support import archived_summary_count, conversation, make_world, policy, say
from test_secretary_recovery import _backlog, _worker

from baton import conversations
from baton.agents.secretary.attention.decide import SecretaryUnavailable
from baton.agents.secretary.connections.model import SecretaryModel
from baton.agents.secretary.connections.transport import ModelUnavailable


def _crash_callback(world, boundary: str) -> None:
    script = """
import asyncio, os, sys
from pathlib import Path
from baton.config import load
from baton.agents.secretary import Secretary
from baton.agents.secretary.attention import decide
from baton.agents.secretary.attention.health import ModelHealth
config_path, state_path, policy_path, boundary = sys.argv[1:]
if boundary == 'answer':
    original = ModelHealth.succeeded
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(73)
    ModelHealth.succeeded = crash
else:
    original = decide.persist
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(73)
    decide.persist = crash
secretary = Secretary(load(config_path), Path(state_path), Path(policy_path))
asyncio.run(secretary.decide(budget_seconds=20))
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(world.config_path), str(world.state_path), str(world.policy_path), boundary],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 73, process.stderr


@pytest.mark.parametrize("change", ["unchanged", "revoke", "acknowledge"])
def test_host_recovers_saved_result_after_process_exit_with_current_visibility(tmp_path, change):
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(world, tmp_path, plan={"summary": "Synthetic private room detail."})
        secretary = worker.instance.secretary
        asyncio.run(secretary.activate())
        say(world, room, "worker", "Synthetic private room detail.")
        _crash_callback(world, "persist")
        assert archived_summary_count(world.baton) == 1
        [saved] = _backlog(world).log.recent()
        assert len(model.requests) == 1
        if change == "revoke":
            conversations.remove(world.baton, world.config, room, "planner", actor="planner")
        elif change == "acknowledge":
            asyncio.run(secretary.acknowledge(saved["decision_id"]))

        recovered = asyncio.run(secretary.decide(budget_seconds=10))
        assert archived_summary_count(world.baton) == 1
        assert len(model.requests) == 1
        if change == "unchanged":
            assert recovered["decision_id"] == saved["decision_id"]
            assert recovered["brief"] == "Synthetic private room detail."
        else:
            assert recovered["decision"] == "keep" and recovered["brief"] == ""
            assert "Synthetic private room detail." not in str(recovered)


def test_hourly_digest_is_not_blocked_by_alert_retry_before_or_after_backoff(tmp_path, monkeypatch):
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(world, tmp_path)
        policy(world, retry_initial_seconds=10, retry_max_seconds=10, digest_seconds=1)
        secretary = worker.instance.secretary
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        for attempt, elapsed in enumerate((2, 20)):
            model.set_plan({"decision": "hold", "summary": "", "sources": []})
            held = say(world, room, "worker", f"Digest item {attempt}")
            result = asyncio.run(secretary.decide(budget_seconds=10, trigger=f"message:{held['seqs']['planner']}"))
            assert result["decision"] == "hold"
            model.set_plan({"steps": [{"exit": 1}]})
            alert = say(world, room, "reviewer", f"Failed alert {attempt}")
            with pytest.raises(SecretaryUnavailable):
                asyncio.run(secretary.decide(budget_seconds=10, trigger=f"message:{alert['seqs']['planner']}"))
            now[0] += elapsed
            model.set_plan({"decision": "keep"})
            digest = asyncio.run(secretary.decide(budget_seconds=10, hourly=True))
            assert digest["mode"] == "digest" and digest["brief"]
            assert digest["sources"] == [f"message:{held['message_id']}"]
            assert len(_backlog(world).due(now[0] + 20)) == 1
            asyncio.run(secretary.acknowledge(digest["decision_id"]))
            now[0] += 20
            model.set_plan({})
            recovered = asyncio.run(secretary.decide(budget_seconds=10))
            asyncio.run(secretary.acknowledge(recovered["decision_id"]))


@pytest.mark.parametrize("acknowledge", [False, True])
def test_overlapping_callbacks_ignore_late_failure_after_success(tmp_path, monkeypatch, acknowledge):
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(world, tmp_path, plan={"steps": [{"exit": 1}]})
        policy(world, max_attempts=1, retry_initial_seconds=1, retry_max_seconds=1)
        secretary = worker.instance.secretary
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        say(world, room, "worker", "Both callbacks attempt this same page")
        with pytest.raises(SecretaryUnavailable):
            asyncio.run(secretary.decide(budget_seconds=10))
        now[0] += 2
        model.set_plan({})
        original_turn = SecretaryModel.turn

        async def overlap():
            entered = [asyncio.Event(), asyncio.Event()]
            release = [asyncio.Event(), asyncio.Event()]
            calls = 0

            async def controlled_turn(self, *args, **kwargs):
                nonlocal calls
                index = calls
                calls += 1
                assert index < 2
                entered[index].set()
                await release[index].wait()
                if index == 1:
                    raise ModelUnavailable("synthetic late failure", category="timeout")
                return await original_turn(self, *args, **kwargs)

            monkeypatch.setattr(SecretaryModel, "turn", controlled_turn)
            async with asyncio.timeout(30):
                async with asyncio.TaskGroup() as group:
                    first = group.create_task(secretary.decide(budget_seconds=20))
                    await entered[0].wait()
                    second = group.create_task(secretary.decide(budget_seconds=20))
                    await entered[1].wait()
                    release[0].set()
                    winner = await first
                    if acknowledge:
                        await secretary.acknowledge(winner["decision_id"])
                    release[1].set()
                    late = await second
            assert late["decision"] == "keep" and late["brief"] == ""
            return winner

        winner = asyncio.run(overlap())
        assert archived_summary_count(world.baton) == 1
        assert _backlog(world).failures() == 0
        if acknowledge:
            assert asyncio.run(secretary.pending(winner["decision_id"])) is None
            later = asyncio.run(secretary.decide(budget_seconds=10))
            assert later["brief"] == ""
        else:
            assert asyncio.run(secretary.pending(winner["decision_id"])) is not None


def test_page_own_answer_before_process_exit_is_not_evidence_for_splitting(tmp_path, monkeypatch):
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(world, tmp_path, plan={"steps": [{"exit": 1}]})
        policy(world, max_attempts=1, retry_initial_seconds=1, retry_max_seconds=1)
        secretary = worker.instance.secretary
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        say(world, room, "worker", "One page must not supply its own health evidence")
        with pytest.raises(SecretaryUnavailable):
            asyncio.run(secretary.decide(budget_seconds=10))
        model.set_plan({})
        _crash_callback(world, "answer")
        assert archived_summary_count(world.baton) == 0
        model.set_plan({"steps": [{"exit": 1}]})
        now[0] += 2
        with pytest.raises(SecretaryUnavailable):
            asyncio.run(secretary.decide(budget_seconds=10))
        [retry] = _backlog(world).due(now[0] + 2)
        assert retry.attempts == 2 and retry.content_failures == 0
        assert archived_summary_count(world.baton) == 0
