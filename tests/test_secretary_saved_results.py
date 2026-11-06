"""Empirical regressions for saved-result recovery and standalone retries."""

from __future__ import annotations

import asyncio
import json

import pytest
from secretary_support import archived_summary_count, conversation, make_world, say
from test_secretary_recovery import _step, _worker

from baton.agents.secretary.attention.decide import SecretaryUnavailable


def _policy(world, **changes: object) -> None:
    value = json.loads(world.policy_path.read_text())
    value.update(changes)
    world.policy_path.write_text(json.dumps(value))


def _windows(world) -> list[tuple[int, int, str, int, int, str]]:
    from baton.agents.secretary.attention.backlog import Backlog
    from baton.agents.secretary.attention.state import AttentionLog

    backlog = Backlog(AttentionLog(world.state_path, world.config.data_dir, "planner"))
    with backlog.log.connection() as conn:
        rows = conn.execute(
            "SELECT since,through,mode,attempts,content_failures,status FROM backlog_windows "
            "WHERE principal='planner' ORDER BY since,through"
        ).fetchall()
    return [tuple(row) for row in rows]


@pytest.mark.parametrize(
    ("forced_wake", "model_plan", "engine_plan", "changed_model_plan", "changed_engine_plan", "kind"),
    [
        (
            True,
            {"steps": [{"exit": 1}]},
            {"decision": "wake", "wake_probability": 0.9},
            {},
            {"decision": "keep", "wake_probability": 0.1},
            "engine_wake",
        ),
        (
            False,
            {"summary": "The original saved summary."},
            {"decision": "keep", "wake_probability": 0.1},
            {"summary": "A changed model summary."},
            {"decision": "wake", "wake_probability": 0.9},
            "summary",
        ),
    ],
    ids=["engine-forced-notice", "ordinary-summary"],
)
def test_saved_result_recovers_after_persist_crash_without_reassessment(
    tmp_path,
    monkeypatch,
    forced_wake,
    model_plan,
    engine_plan,
    changed_model_plan,
    changed_engine_plan,
    kind,
) -> None:
    from baton.agents.secretary.attention import decide as decide_module
    from baton.integrations.secretary.worker import Worker

    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(world, tmp_path, plan=model_plan)
        model.set_engine_plan(engine_plan)
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(worker.instance.secretary.activate())
        sent = say(world, room, "worker", "Keep this assessment available for recovery")

        original_persist = decide_module.persist
        raised = False

        def persist_then_crash(*args, **kwargs):
            nonlocal raised
            result = original_persist(*args, **kwargs)
            if not raised:
                raised = True
                raise RuntimeError("synthetic crash after saved assessment")
            return result

        monkeypatch.setattr(decide_module, "persist", persist_then_crash)
        with pytest.raises(RuntimeError, match="after saved assessment"):
            asyncio.run(_step(worker))
        assert raised
        assert archived_summary_count(world.baton) == 1
        assert len(model.requests) == 1
        assert len(model.engine_requests) == 1
        saved_model_calls = len(model.requests)
        saved_engine_calls = len(model.engine_requests)

        # Keep policy and source identity fixed while the subprocesses would now
        # return different judgments if recovery invoked them again.
        model.set_plan(changed_model_plan)
        model.set_engine_plan(changed_engine_plan)
        monkeypatch.setattr(decide_module, "persist", original_persist)
        restarted = Worker(worker.instance)
        now[0] += 2
        asyncio.run(_step(restarted))

        assert len(model.requests) == saved_model_calls
        assert len(model.engine_requests) == saved_engine_calls
        assert archived_summary_count(world.baton) == 1
        [decision_id] = restarted.instance.receipts.get("pending")
        result = asyncio.run(restarted.instance.secretary.pending(decision_id))
        assert result is not None
        assert result["decision_id"] == decision_id
        assert result["kind"] == kind
        assert f"message:{sent['message_id']}" in result["sources"]
        if forced_wake:
            assert result["engine_verdict"] == {"decision": "wake", "wake_probability": 0.9}
        else:
            assert result["brief"] == "The original saved summary."
        assert _windows(world)[0][-1] == "ready"


def test_untriggered_standalone_retry_waits_then_recovers_old_page_before_new_trigger(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(world, tmp_path, plan={"steps": [{"exit": 1}, {}, {}]})
        _policy(world, max_events=1, retry_initial_seconds=1, retry_max_seconds=1)
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        secretary = worker.instance.secretary
        asyncio.run(secretary.activate())
        first = say(world, room, "worker", "This first page fails and must be retained")
        first_trigger = f"message:{first['seqs']['planner']}"

        with pytest.raises(SecretaryUnavailable) as failed:
            asyncio.run(secretary.decide(budget_seconds=5, trigger=first_trigger))
        assert failed.value.extra["retryable"] is True
        assert len(model.requests) == 1

        with pytest.raises(SecretaryUnavailable) as waiting:
            asyncio.run(secretary.decide(budget_seconds=5))
        assert waiting.value.extra["retryable"] is True
        assert len(model.requests) == 1

        second = say(world, room, "reviewer", "A newer page has its own trigger")
        second_trigger = f"message:{second['seqs']['planner']}"
        now[0] += 2
        recovered_first = asyncio.run(secretary.decide(budget_seconds=5, trigger=second_trigger))
        assert recovered_first["decision"] == "wake"
        assert recovered_first["trigger"] is None
        assert recovered_first["sources"] == [f"message:{first['message_id']}"]
        assert model.requests[-1]["context"]["trigger"] is None
        assert len(model.requests) == 2

        recovered_second = asyncio.run(secretary.decide(budget_seconds=5, trigger=second_trigger))
        assert recovered_second["decision"] == "wake"
        assert recovered_second["trigger"] == second_trigger
        assert recovered_second["sources"] == [f"message:{second['message_id']}"]
        assert model.requests[-1]["context"]["trigger"] == second_trigger
        assert len(model.requests) == 3


def test_untriggered_digest_retry_returns_acknowledgeable_keep_summary(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(
            world,
            tmp_path,
            plan={
                "steps": [
                    {"decision": "hold", "summary": "", "sources": []},
                    {"exit": 1},
                    {
                        "decision": "keep",
                        "summary": "The held event was reviewed in the digest.",
                        "sources": "all",
                    },
                ]
            },
        )
        _policy(world, retry_initial_seconds=1, retry_max_seconds=1, digest_seconds=1)
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        secretary = worker.instance.secretary
        asyncio.run(secretary.activate())
        sent = say(world, room, "worker", "Hold this event for a scheduled digest")

        held = asyncio.run(secretary.decide(budget_seconds=5))
        assert held["decision"] == "hold" and held["brief"] == ""
        assert len(model.requests) == 1

        now[0] += 2
        with pytest.raises(SecretaryUnavailable) as failed_digest:
            asyncio.run(secretary.decide(budget_seconds=5, hourly=True))
        assert failed_digest.value.extra["retryable"] is True
        assert len(model.requests) == 2

        now[0] += 2
        digest = asyncio.run(secretary.decide(budget_seconds=5, hourly=True))
        assert digest["mode"] == "digest"
        assert digest["decision"] == "keep"
        assert digest["brief"] == "The held event was reviewed in the digest."
        assert digest["sources"] == [f"message:{sent['message_id']}"]
        assert len(model.requests) == 3

        # A host delivers every nonempty brief, including keep/digest summaries,
        # and acknowledges only after that delivery succeeds.
        pending = asyncio.run(secretary.pending(digest["decision_id"]))
        assert pending is not None and pending["brief"] == digest["brief"]
        assert asyncio.run(secretary.acknowledge(digest["decision_id"])) == digest["through_pos"]
