"""Empirical recovery checks for split pages, terminal notices, and worker backoff."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from secretary_support import conversation, make_world, say
from test_secretary_model import Model

from baton.agents.secretary import Secretary
from baton.agents.secretary.attention.backlog import Backlog
from baton.agents.secretary.attention.runtime import DatabaseWorker
from baton.agents.secretary.attention.state import AttentionLog
from baton.agents.secretary.inbox.references import ReferenceWindow
from baton.config.secretary import load_policy
from baton.integrations.secretary.instance import Instance
from baton.integrations.secretary.worker import Worker


def _worker(world, tmp_path: Path, *, plan: dict | None = None) -> tuple[Worker, Model]:
    model = Model(world, tmp_path, plan=plan)
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    private = tmp_path / "instance"
    private.mkdir(mode=0o700, exist_ok=True)
    return (
        Worker(
            Instance(
                path=private / "integration.json",
                product="claude-code",
                project=project,
                secretary=Secretary(world.config, world.state_path, world.policy_path),
                poll_seconds=0.1,
                budget_seconds=5,
                delivery_timeout_seconds=7,
                codex=None,
            )
        ),
        model,
    )


async def _step(worker: Worker) -> None:
    async with DatabaseWorker() as database:
        await worker.step(database)


def _backlog(world) -> Backlog:
    return Backlog(AttentionLog(world.state_path, world.config.data_dir, "planner"))


def test_standalone_decide_advances_scan_past_hold_and_assesses_the_earliest_page_before_a_later_trigger(
    tmp_path,
) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(
            world, tmp_path, plan={"steps": [{"decision": "hold", "summary": "", "sources": []}, {}]}
        )
        policy = json.loads(world.policy_path.read_text())
        policy["max_events"] = 1
        world.policy_path.write_text(json.dumps(policy))
        secretary = worker.instance.secretary
        asyncio.run(secretary.activate())
        first = say(world, room, "worker", "Hold this first message")
        second = say(world, room, "reviewer", "The second page needs attention")
        trigger = f"message:{second['seqs']['planner']}"

        # This call used to refuse a trigger beyond the bounded page before queueing the page, so while a backlog
        # existed no callback moved it. It now assesses the earliest page as catch-up, without the trigger: the model
        # is not told of it, and the result names none and says it is catch-up, so the caller decides that wake as
        # one not yet reached.
        first_result = asyncio.run(secretary.decide(budget_seconds=5, trigger=trigger))
        assert first_result["decision"] == "hold" and first_result["trigger"] is None
        assert first_result["catch_up"] is True
        assert model.requests[0]["context"]["trigger"] is None
        assert [event["message"]["id"] for event in model.requests[0]["context"]["events"]] == [first["message_id"]]

        second_result = asyncio.run(secretary.decide(budget_seconds=5, trigger=trigger))
        assert second_result["decision"] == "wake" and second_result["trigger"] == trigger
        assert "catch_up" not in second_result
        assert second_result["since"] == first_result["through_pos"]
        assert second_result["sources"] == [f"message:{second['message_id']}"]
        assert len(model.requests) == 2
        assert AttentionLog(world.state_path, world.config.data_dir, "planner").positions()[0] == first_result["floor"]


def test_repeated_invalid_multi_event_page_splits_and_finishes_with_source_linked_notices(
    tmp_path, monkeypatch
) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(world, tmp_path, plan={"steps": [{"raw": "{}"}]})
        policy = json.loads(world.policy_path.read_text())
        policy.update(max_attempts=2, max_events=2, retry_initial_seconds=1, retry_max_seconds=1)
        world.policy_path.write_text(json.dumps(policy))
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(worker.instance.secretary.activate())
        first = say(world, room, "worker", "First message for the failing page")
        second = say(world, room, "reviewer", "Second message for the failing page")

        asyncio.run(_step(worker))
        assert len(model.requests) == 1 and worker.failed_pages == 1
        now[0] += 2
        asyncio.run(_step(worker))
        assert len(model.requests) == 2 and worker.failed_pages == 0

        split_pages = _backlog(world).due(now[0], limit=10)
        assert len(split_pages) == 2
        assert all(page.mode == "alert" for page in split_pages)
        policy = load_policy(world.config, world.policy_path)
        split_sources = [
            {ref["source_key"] for ref in ReferenceWindow.read(world.config, policy, page.since, page.through).refs}
            for page in split_pages
        ]
        assert all(len(sources) == 1 for sources in split_sources)
        assert set.union(*split_sources) == {f"message:{first['message_id']}", f"message:{second['message_id']}"}

        for _ in range(4):
            now[0] += 2
            asyncio.run(_step(worker))

        [first_notice, second_notice] = worker.instance.receipts.get("pending")
        summaries = [
            asyncio.run(worker.instance.secretary.pending(first_notice)),
            asyncio.run(worker.instance.secretary.pending(second_notice)),
        ]
        assert all(summary and summary["kind"] == "failure_notice" for summary in summaries)
        source_keys = [{ref["source_key"] for ref in summary["source_refs"]} for summary in summaries]
        assert {f"message:{first['message_id']}"} in source_keys
        assert {f"message:{second['message_id']}"} in source_keys

        calls_after_notices = len(model.requests)
        asyncio.run(_step(worker))
        assert len(model.requests) == calls_after_notices
        assert worker.instance.receipts.get("pending") == [first_notice, second_notice]

        model.set_plan({})
        third = say(world, room, "worker", "The poller can process work after terminal notices")
        asyncio.run(_step(worker))
        decisions = worker.instance.receipts.get("pending")
        assert len(decisions) == 3
        delivered = [asyncio.run(worker.instance.secretary.pending(decision)) for decision in decisions]
        assert any(
            summary and summary["kind"] != "failure_notice" and f"message:{third['message_id']}" in summary["sources"]
            for summary in delivered
        )


def test_successful_pass_with_failed_page_resets_global_retry_backoff(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        worker, _ = _worker(world, tmp_path)
        policy = json.loads(world.policy_path.read_text())
        policy["summary_bytes"] = 4000
        world.policy_path.write_text(json.dumps(policy))
        attempts = 0

        async def step(_database) -> None:
            nonlocal attempts
            attempts += 1
            if attempts in (1, 3):
                raise RuntimeError("whole worker pass failed")
            # A page failed and was scheduled for recovery, but the queue pass itself was healthy.
            worker.failed_pages = 1

        monkeypatch.setattr(worker, "step", step)
        delays: list[float] = []

        async def sleep(delay: float) -> None:
            if delay > 0.2:
                delays.append(delay)
                if len(delays) == 2:
                    raise asyncio.CancelledError

        monkeypatch.setattr("baton.integrations.secretary.worker.asyncio.sleep", sleep)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(worker.run())

        assert delays == [5, 5]
