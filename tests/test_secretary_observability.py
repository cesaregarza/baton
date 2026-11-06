"""Worker-level persistence of decision-engine health and independent delivery timeouts."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from dataclasses import replace

import pytest
from secretary_support import conversation, make_world, say
from test_secretary_delivery import _worker

from baton.agents.secretary.attention.health import EngineHealth
from baton.agents.secretary.attention.state import AttentionLog
from baton.integrations.secretary.worker import Worker


async def _run_until_engine_health(worker: Worker) -> dict:
    task = asyncio.create_task(worker.run())
    try:
        async with asyncio.timeout(10):
            while True:
                health = worker.instance.receipts.get("health")
                if isinstance(health, dict) and isinstance(health.get("engine"), dict):
                    return health
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _run_until_delivery(worker: Worker) -> dict:
    task = asyncio.create_task(worker.run())
    try:
        async with asyncio.timeout(10):
            while True:
                queued = worker.instance.receipts.get("queued")
                health = worker.instance.receipts.get("health")
                if queued and health and health.get("delivery", {}).get("status") == "available":
                    return health
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _hook_compatible_policy(world) -> None:
    policy = json.loads(world.policy_path.read_text())
    policy["summary_bytes"] = 4000
    world.policy_path.write_text(json.dumps(policy))


@pytest.mark.parametrize(
    "plan",
    [
        pytest.param({"steps": [{"decision": "hold", "summary": "", "sources": []}]}, id="model-hold"),
        pytest.param({"steps": [{"exit": 1}]}, id="model-failure"),
    ],
)
def test_worker_persists_engine_failure_when_model_holds_or_fails(tmp_path, monkeypatch, plan) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(world, tmp_path, plan=plan)
        model.set_engine_plan({"steps": [{"exit": 1}]})
        _hook_compatible_policy(world)
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(worker.instance.secretary.activate())
        say(world, room, "worker", "A low-priority synthetic update")

        health = asyncio.run(_run_until_engine_health(worker))

        log = AttentionLog(world.state_path, world.config.data_dir, "planner")
        engine_health = EngineHealth(log).read()
        assert health["status"] == "degraded"
        assert health["engine"]["status"] == "unavailable"
        assert health["engine"]["last_failure"] == "transport"
        assert engine_health["status"] == "unavailable"
        assert engine_health["last_failure"] == "transport"
        assert len(model.requests) == 1 and len(model.engine_requests) == 1
        assert worker.instance.receipts.get("pending") == []

        now[0] += 6
        model.set_plan({"decision": "wake", "summary": "The retry succeeded."})
        model.set_engine_plan({"decision": "keep", "wake_probability": 0.2})
        worker.instance.receipts.set("health", None)
        say(world, room, "worker", "A new alert after the engine recovered")
        recovered = asyncio.run(_run_until_engine_health(worker))
        assert recovered["status"] == "running"
        assert recovered["engine"]["status"] == "available"
        assert recovered["engine"]["last_failure"] == "transport"
        assert worker.instance.receipts.get("pending")


def test_engine_timeout_does_not_limit_delayed_codex_delivery(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(world, tmp_path, plan={"decision": "wake"}, product="codex")
        model.set_engine_plan({"steps": [{"sleep": 2}]})
        policy = json.loads(world.policy_path.read_text())
        policy["summary_bytes"] = 4000
        policy["decision_engine"]["timeout_seconds"] = 0.5
        world.policy_path.write_text(json.dumps(policy))

        delivery_log = tmp_path / "codex-delivery.log"
        codex = tmp_path / "fake-codex"
        codex.write_text(
            f"#!{sys.executable}\n"
            "import pathlib,sys,time\n"
            "time.sleep(0.8)\n"
            f"pathlib.Path({str(delivery_log)!r}).write_text('queued')\n"
        )
        codex.chmod(0o700)
        worker = Worker(replace(worker.instance, codex=str(codex)))
        worker.instance.receipts.bind("session_observability")
        asyncio.run(worker.instance.secretary.activate())
        say(world, room, "worker", "A synthetic update to deliver")

        health = asyncio.run(_run_until_delivery(worker))

        assert delivery_log.read_text() == "queued"
        assert health["status"] == "degraded"
        assert health["engine"]["status"] == "unavailable"
        assert health["engine"]["last_failure"] == "timeout"
        assert health["delivery"]["status"] == "available"
        assert health["delivery"]["last_failure"] is None
        assert len(model.requests) == 1
        assert model.engine_requests[0]["version"] == 3
        assert worker.instance.receipts.get("pending")
        queued = worker.instance.receipts.get("queued")
        assert queued["session"] == "session_observability"
        assert queued["decisions"] == worker.instance.receipts.get("pending")
