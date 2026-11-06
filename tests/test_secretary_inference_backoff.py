"""Model answers rejected by the source recheck still back off as failed attempts."""

from __future__ import annotations

import asyncio

import pytest
from secretary_support import archived_summary_count, conversation, make_world, policy, say
from test_secretary_recovery import _backlog, _worker

from baton import conversations
from baton.agents.secretary.attention.decide import SecretaryUnavailable
from baton.agents.secretary.connections.model import SecretaryModel


def _retry_state(world) -> dict:
    with _backlog(world).log.connection() as conn:
        [row] = conn.execute("SELECT * FROM backlog_windows WHERE principal='planner'").fetchall()
        return dict(row)


@pytest.mark.parametrize("previous_failure", [False, True])
def test_visibility_changes_after_model_answers_preserve_exponential_backoff(tmp_path, monkeypatch, previous_failure):
    with make_world(tmp_path) as world:
        rooms = [conversation(world), conversation(world)]
        worker, model = _worker(world, tmp_path)
        policy(world, max_attempts=1, retry_initial_seconds=5, retry_max_seconds=20)
        secretary = worker.instance.secretary
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(secretary.activate())
        for room in rooms:
            say(world, room, "worker", "A room whose access will change during inference")
        if previous_failure:
            model.set_plan({"steps": [{"exit": 1}]})
            with pytest.raises(SecretaryUnavailable):
                asyncio.run(secretary.decide(budget_seconds=10))
            assert _retry_state(world)["error"] == "command_failed"
            now[0] += 5
            model.set_plan({})

        original_turn = SecretaryModel.turn
        answered = 0

        async def answer_then_revoke(self, *args, **kwargs):
            nonlocal answered
            answer = await original_turn(self, *args, **kwargs)
            conversations.remove(world.baton, world.config, rooms[answered], "planner", actor="planner")
            answered += 1
            return answer

        monkeypatch.setattr(SecretaryModel, "turn", answer_then_revoke)
        for turn in range(2):
            with pytest.raises(SecretaryUnavailable, match="visibility or content changed"):
                asyncio.run(secretary.decide(budget_seconds=10))
            attempts = int(previous_failure) + turn + 1
            delay = min(20, 5 * 2 ** (attempts - 1))
            state = _retry_state(world)
            assert state["attempts"] == attempts
            assert state["next_attempt"] == now[0] + delay
            assert state["error"] == "unavailable"
            assert state["model_successes"] == answered == turn + 1
            assert state["content_failures"] == 0 and state["status"] == "retry"
            assert archived_summary_count(world.baton) == 0
            requests = len(model.requests)
            now[0] += delay / 2
            with pytest.raises(SecretaryUnavailable, match="waiting for retry backoff"):
                asyncio.run(secretary.decide(budget_seconds=10))
            assert len(model.requests) == requests
            assert _retry_state(world) == state
            now[0] += delay / 2

        # Once the last room is hidden, the due retry discards the empty page without inference.
        result = asyncio.run(secretary.decide(budget_seconds=10))
        assert result["decision"] == "keep" and not result["brief"]
        assert len(model.requests) == int(previous_failure) + 2
        assert _backlog(world).failures() == 0
