"""Delivery timeouts stay on the notification retry path while polling continues."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from dataclasses import replace

from secretary_support import conversation, make_world, say
from test_secretary_delivery import _worker

from baton.integrations.secretary.worker import Worker


async def _wait_for(predicate, *, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def _exercise_delivery_recovery(worker: Worker, model, world, room, marker, queued) -> dict:
    task = asyncio.create_task(worker.run())
    try:
        await _wait_for(
            lambda: (
                (world_health := worker.instance.receipts.get("health"))
                and world_health.get("delivery", {}).get("status") == "unavailable"
            )
        )
        failed_health = worker.instance.receipts.get("health")
        assert failed_health["delivery"]["last_failure"] == "timeout"
        assert marker.exists() and not queued.exists()
        assert worker.instance.receipts.get("queued") is None

        say(world, room, "reviewer", "Second alert while delivery is retrying")
        await _wait_for(lambda: len(model.requests) >= 2)
        assert worker.instance.receipts.get("health")["delivery"]["status"] == "unavailable"
        assert not queued.exists() and worker.instance.receipts.get("queued") is None

        say(world, room, "worker", "Third alert while delivery is retrying")
        await _wait_for(lambda: len(model.requests) >= 3)
        assert worker.instance.receipts.get("health")["delivery"]["status"] == "unavailable"
        assert not queued.exists() and worker.instance.receipts.get("queued") is None

        await _wait_for(queued.exists, timeout=5.0)
        await _wait_for(
            lambda: (
                (health := worker.instance.receipts.get("health"))
                and health.get("delivery", {}).get("status") == "available"
            )
        )
        recovered = worker.instance.receipts.get("health")
        return recovered
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_hung_delivery_keeps_polling_and_recovers_without_queueing_failed_offer(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(world, tmp_path, plan={"steps": [{}, {}, {}]}, product="codex")
        policy = json.loads(world.policy_path.read_text())
        policy["summary_bytes"] = 4000
        policy["retry_initial_seconds"] = 1.5
        policy["retry_max_seconds"] = 1.5
        world.policy_path.write_text(json.dumps(policy))

        marker = tmp_path / "delivery-started"
        queued = tmp_path / "delivery-completed"
        codex = tmp_path / "fake-codex"
        codex.write_text(
            f"#!{sys.executable}\n"
            "import pathlib,sys,time\n"
            f"marker = pathlib.Path({str(marker)!r})\n"
            f"queued = pathlib.Path({str(queued)!r})\n"
            "if marker.exists():\n"
            "    queued.write_text('queued')\n"
            "else:\n"
            "    marker.write_text('started')\n"
            "    time.sleep(30)\n"
        )
        codex.chmod(0o700)
        worker = Worker(replace(worker.instance, codex=str(codex), delivery_timeout_seconds=0.15, poll_seconds=0.02))
        worker.instance.receipts.bind("delivery_health_test")
        asyncio.run(worker.instance.secretary.activate())
        say(world, room, "worker", "First alert starts a hung delivery")

        health = asyncio.run(_exercise_delivery_recovery(worker, model, world, room, marker, queued))

        assert len(model.requests) >= 3
        assert queued.read_text() == "queued"
        assert health["status"] == "running"
        assert health["delivery"]["status"] == "available"
        assert health["delivery"]["last_failure"] == "timeout"
        assert health["delivery"]["last_failure_at"] < health["delivery"]["checked_at"]
        assert worker.instance.receipts.get("queued")["session"] == "delivery_health_test"


def test_status_heartbeat_budget_includes_slow_successful_delivery(tmp_path) -> None:
    from test_secretary_integration import _cli, _install

    from baton.integrations.secretary.instance import Instance

    with make_world(tmp_path) as world:
        _, instance_path, *_ = _install(world, tmp_path, "codex")
        settings = json.loads(instance_path.read_text())
        settings.update(budget_seconds=20, delivery_timeout_seconds=20)
        instance_path.write_text(json.dumps(settings))
        instance = Instance.load(instance_path)
        instance.receipts.set("health", {"status": "running", "checked_at": time.time() - 45})
        status = _cli(instance_path, "status")
        assert status.returncode == 0, status.stderr
        assert json.loads(status.stdout)["heartbeat_stale"] is False
        instance.receipts.set("health", {"status": "running", "checked_at": time.time() - 80})
        stale = _cli(instance_path, "status")
        assert stale.returncode == 0, stale.stderr
        assert json.loads(stale.stdout)["heartbeat_stale"] is True
