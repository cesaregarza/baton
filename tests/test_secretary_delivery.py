"""Empirical worker backlog, retry, and delivery checks using a synthetic model command."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from secretary_support import conversation, make_world, say
from test_secretary_model import Model

from baton.agents.secretary import Secretary
from baton.agents.secretary.attention.backlog import Backlog
from baton.agents.secretary.attention.runtime import DatabaseWorker
from baton.agents.secretary.attention.state import AttentionLog
from baton.integrations.secretary.instance import Instance
from baton.integrations.secretary.worker import Worker


def _worker(world, tmp_path: Path, *, plan: dict | None = None, product: str = "claude-code"):
    model = Model(world, tmp_path, plan=plan)
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    private = tmp_path / "instance"
    private.mkdir(mode=0o700, exist_ok=True)
    instance = Instance(
        path=private / "integration.json",
        product=product,
        project=project,
        secretary=Secretary(world.config, world.state_path, world.policy_path),
        poll_seconds=0.1,
        budget_seconds=5,
        codex=None,
    )
    return Worker(instance), model


async def _step(worker: Worker) -> str | None:
    async with DatabaseWorker() as database:
        return await worker.step(database)


def test_wake_survives_a_later_hold_and_worker_restart(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(
            world,
            tmp_path,
            plan={"steps": [{}, {"decision": "hold", "summary": "", "sources": []}]},
        )
        asyncio.run(worker.instance.secretary.activate())
        say(world, room, "worker", "Wake with a summary")

        assert asyncio.run(_step(worker)) is None
        pending = worker.instance.receipts.get("pending")
        assert pending is not None
        say(world, room, "reviewer", "Keep this later message for a digest")
        assert asyncio.run(_step(worker)) is None
        scanned = Backlog(AttentionLog(world.state_path, world.config.data_dir, "planner")).scanned()
        assert scanned > 0 and len(model.requests) == 2
        assert worker.instance.receipts.get("pending") == pending

        restarted = Worker(worker.instance)
        assert asyncio.run(_step(restarted)) is None
        assert len(model.requests) == 2
        assert worker.instance.receipts.get("pending") == pending
        assert Backlog(AttentionLog(world.state_path, world.config.data_dir, "planner")).scanned() == scanned


def test_later_wake_acknowledgment_does_not_jump_over_an_earlier_hold(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(
            world,
            tmp_path,
            plan={"steps": [{"decision": "hold", "summary": "", "sources": []}, {}]},
        )
        asyncio.run(worker.instance.secretary.activate())
        first = say(world, room, "worker", "Earlier held context")
        asyncio.run(_step(worker))
        second = say(world, room, "reviewer", "A later wake")

        assert asyncio.run(_step(worker)) is None
        decision = worker.instance.receipts.get("pending")
        summary = asyncio.run(worker.instance.secretary.pending(decision))
        assert len(model.requests) == 2 and summary is not None
        assert f"message:{second['message_id']}" in summary["sources"]
        assert f"message:{first['message_id']}" not in summary["sources"]

        delivered = asyncio.run(worker.instance.secretary.acknowledge(decision))
        assert delivered == summary["floor"]
        assert AttentionLog(world.state_path, world.config.data_dir, "planner").positions() == (
            summary["floor"],
            summary["floor"],
        )


def test_failed_page_is_recorded_and_does_not_block_later_new_work(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(
            world,
            tmp_path,
            plan={"steps": [{"exit": 1}, {}]},
        )
        asyncio.run(worker.instance.secretary.activate())
        first = say(world, room, "worker", "This page will fail once")
        monkeypatch.setattr("baton.integrations.secretary.worker.time.time", lambda: 100.0)

        failure = asyncio.run(_step(worker))
        failed_through = Backlog(AttentionLog(world.state_path, world.config.data_dir, "planner")).scanned()
        assert failure and failed_through > 0 and worker.instance.receipts.get("pending") is None

        second = say(world, room, "reviewer", "Later work must still be assessed")
        failure = asyncio.run(_step(worker))
        decision = worker.instance.receipts.get("pending")
        summary = asyncio.run(worker.instance.secretary.pending(decision))
        assert failure is not None  # The earlier page's failure remains in health until its retry succeeds.
        assert f"message:{second['message_id']}" in summary["sources"]
        assert f"message:{first['message_id']}" not in summary["sources"]
        assert len(model.requests) == 2


def test_oversized_first_page_is_retained_and_later_page_still_wakes(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(world, tmp_path)
        policy = json.loads(world.policy_path.read_text())
        world.policy_path.write_text(json.dumps({**policy, "context_bytes": 1024}))
        asyncio.run(worker.instance.secretary.activate())
        oversized = say(world, room, "worker", "x" * 3000)

        failure = asyncio.run(_step(worker))
        backlog = Backlog(AttentionLog(world.state_path, world.config.data_dir, "planner"))
        assert failure == "InboxTooLarge"
        assert backlog.scanned() > 0 and worker.instance.receipts.get("pending") is None

        later = say(world, room, "reviewer", "A small later event")
        asyncio.run(_step(worker))
        decision = worker.instance.receipts.get("pending")
        summary = asyncio.run(worker.instance.secretary.pending(decision))
        assert summary is not None and len(model.requests) == 1
        assert f"message:{later['message_id']}" in summary["sources"]
        assert f"message:{oversized['message_id']}" not in summary["sources"]


def test_held_page_is_reassessed_hourly_and_can_later_become_a_summary(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(
            world,
            tmp_path,
            plan={"steps": [{"decision": "hold", "summary": "", "sources": []}, {}]},
        )
        now = [100.0]
        monkeypatch.setattr("baton.integrations.secretary.worker.time.time", lambda: now[0])
        asyncio.run(worker.instance.secretary.activate())
        say(world, room, "worker", "A held request gains urgency")

        assert asyncio.run(_step(worker)) is None
        assert len(model.requests) == 1
        assert worker.instance.receipts.get("pending") is None

        now[0] += 3600
        assert asyncio.run(_step(worker)) is None
        decision = worker.instance.receipts.get("pending")
        summary = asyncio.run(worker.instance.secretary.pending(decision))
        assert len(model.requests) == 2 and summary is not None
        assert model.requests[-1]["context"]["mode"] == "digest"
        assert summary["brief"]
