"""Delivery completion must not overwrite the worker pass health receipt."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from dataclasses import replace

from secretary_support import conversation, make_world, say
from test_secretary_delivery import _worker

from baton.integrations.secretary.worker import Worker


async def _wait_for(predicate, *, timeout: float = 15.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_delivery_completion_preserves_retrying_pass_health(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        base_worker, _ = _worker(world, tmp_path, plan={"decision": "wake"}, product="codex")
        policy = json.loads(world.policy_path.read_text())
        policy.update(summary_bytes=4000, retry_initial_seconds=0.4, retry_max_seconds=0.4)
        world.policy_path.write_text(json.dumps(policy))

        marker = tmp_path / "delivery-started"
        release = tmp_path / "delivery-release"
        codex = tmp_path / "fake-codex"
        codex.write_text(
            f"#!{sys.executable}\n"
            "import pathlib,time\n"
            f"marker = pathlib.Path({str(marker)!r})\n"
            f"release = pathlib.Path({str(release)!r})\n"
            "marker.write_text('started')\n"
            "while not release.exists(): time.sleep(0.01)\n"
        )
        codex.chmod(0o700)
        worker = Worker(replace(base_worker.instance, codex=str(codex), delivery_timeout_seconds=20, poll_seconds=0.02))
        worker.instance.receipts.bind("worker_health_race")
        asyncio.run(worker.instance.secretary.activate())
        say(world, room, "worker", "Start a blocked delivery")

        original_step = worker.step
        pass_count = 0
        third_pass_started = asyncio.Event()
        allow_third_pass = asyncio.Event()

        async def fail_one_pass(database) -> None:
            nonlocal pass_count
            pass_count += 1
            if pass_count == 2:
                raise RuntimeError("synthetic pass failure")
            if pass_count == 3:
                third_pass_started.set()
                await allow_third_pass.wait()
            await original_step(database)

        worker.step = fail_one_pass

        async def exercise() -> None:
            task = asyncio.create_task(worker.run())
            try:
                await _wait_for(marker.exists)
                await _wait_for(
                    lambda: (
                        (health := worker.instance.receipts.get("health"))
                        and health.get("status") == "retrying"
                        and health.get("error") == "RuntimeError"
                    )
                )

                release.write_text("release")
                await _wait_for(
                    lambda: (
                        (health := worker.instance.receipts.get("health"))
                        and health.get("delivery", {}).get("status") == "available"
                    )
                )
                during_backoff = worker.instance.receipts.get("health")
                assert during_backoff["status"] == "retrying"
                assert during_backoff["error"] == "RuntimeError"

                async with asyncio.timeout(15):
                    await third_pass_started.wait()
                allow_third_pass.set()
                await _wait_for(
                    lambda: (
                        (health := worker.instance.receipts.get("health"))
                        and health.get("status") == "running"
                        and "error" not in health
                    )
                )
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        asyncio.run(exercise())
