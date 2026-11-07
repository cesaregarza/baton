"""Delivery timeouts stay on the notification retry path while polling continues."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from secretary_support import conversation, make_world, say
from test_secretary_delivery import _worker

from baton.integrations.secretary.worker import DeliveryOffer, Worker


async def _wait_for(predicate, *, timeout: float = 15.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def _hook_compatible_policy(world) -> None:
    policy = json.loads(world.policy_path.read_text())
    policy["summary_bytes"] = 4000
    world.policy_path.write_text(json.dumps(policy))


async def _exercise_delivery_recovery(worker: Worker, model, world, room, marker, release, queued, starts) -> dict:
    task = asyncio.create_task(worker.run())
    try:
        await _wait_for(marker.exists)
        assert not queued.exists() and worker.instance.receipts.get("queued") is None

        say(world, room, "reviewer", "Second alert while delivery is retrying")
        await _wait_for(lambda: len(model.requests) >= 2)
        assert not queued.exists() and worker.instance.receipts.get("queued") is None

        say(world, room, "worker", "Third alert while delivery is retrying")
        await _wait_for(lambda: len(model.requests) >= 3)
        assert not queued.exists() and worker.instance.receipts.get("queued") is None
        assert len(starts.read_text().splitlines()) == 1
        await _wait_for(lambda: len(worker.instance.receipts.get("pending") or []) >= 3)
        expected = worker.instance.receipts.get("pending")
        assert len(expected) >= 3

        release.write_text("release")
        await _wait_for(
            lambda: (receipt := worker.instance.receipts.get("queued")) and receipt.get("decisions") == expected
        )
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
        release = tmp_path / "delivery-release"
        queued = tmp_path / "delivery-completed"
        starts = tmp_path / "delivery-starts"
        codex = tmp_path / "fake-codex"
        codex.write_text(
            f"#!{sys.executable}\n"
            "import pathlib,sys,time\n"
            f"marker = pathlib.Path({str(marker)!r})\n"
            f"release = pathlib.Path({str(release)!r})\n"
            f"queued = pathlib.Path({str(queued)!r})\n"
            f"starts = pathlib.Path({str(starts)!r})\n"
            "with starts.open('a') as output: output.write('start\\n')\n"
            "marker.write_text('started')\n"
            "while not release.exists(): time.sleep(0.01)\n"
            "queued.write_text('queued')\n"
        )
        codex.chmod(0o700)
        worker = Worker(replace(worker.instance, codex=str(codex), delivery_timeout_seconds=20, poll_seconds=0.02))
        worker.instance.receipts.bind("delivery_health_test")
        asyncio.run(worker.instance.secretary.activate())
        say(world, room, "worker", "First alert starts a hung delivery")

        health = asyncio.run(_exercise_delivery_recovery(worker, model, world, room, marker, release, queued, starts))

        assert len(model.requests) >= 3
        assert queued.read_text() == "queued"
        assert len(starts.read_text().splitlines()) == 2
        assert health["status"] == "running"
        assert health["delivery"]["status"] == "available"
        assert health["delivery"]["last_failure"] is None
        assert worker.instance.receipts.get("queued")["session"] == "delivery_health_test"


def test_delivery_failure_categories_and_backoff_are_observable(tmp_path) -> None:
    with make_world(tmp_path) as world:
        worker, _ = _worker(world, tmp_path, product="codex")

        async def fail_once(executable: str | None, expected: str) -> None:
            worker.instance = replace(worker.instance, codex=executable)
            assert await worker._queue("session", 2.0) is False
            assert worker.delivery_health["status"] == "unavailable"
            assert worker.delivery_health["last_failure"] == expected

        async def check() -> None:
            await fail_once(None, "configuration")
            await fail_once(str(tmp_path / "missing-codex"), "launch")
            codex = tmp_path / "exit-codex"
            codex.write_text(f"#!{sys.executable}\nraise SystemExit(9)\n")
            codex.chmod(0o700)
            await fail_once(str(codex), "nonzero_exit")
            hung = tmp_path / "hung-codex"
            hung.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
            hung.chmod(0o700)
            worker.instance = replace(worker.instance, codex=str(hung))
            assert await worker._queue("session", 0.1) is False
            assert worker.delivery_health["last_failure"] == "timeout"
            recovered = tmp_path / "recovered-codex"
            recovered.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
            recovered.chmod(0o700)
            worker.instance = replace(worker.instance, codex=str(recovered))
            assert await worker._queue("session", 2) is True
            assert worker.delivery_health["status"] == "available"
            assert worker.delivery_health["last_failure"] == "timeout"

        asyncio.run(check())


def test_delivery_retry_backoff_doubles_caps_and_resets_after_success(tmp_path) -> None:
    with make_world(tmp_path) as world:
        worker, _ = _worker(world, tmp_path, product="codex")
        worker.delivery_retry_seconds = 0.1
        offer = DeliveryOffer("session", ("decision",), 0.1, 0.4)

        worker._schedule_delivery_retry(offer, 10.0)
        assert worker.delivery_retry_at == 10.1
        assert worker.delivery_retry_seconds == 0.2
        worker._schedule_delivery_retry(offer, 11.0)
        assert worker.delivery_retry_at == 11.2
        assert worker.delivery_retry_seconds == 0.4
        worker._schedule_delivery_retry(offer, 12.0)
        assert worker.delivery_retry_at == 12.4
        assert worker.delivery_retry_seconds == 0.4

        async def succeed(session: str, timeout: float) -> bool:
            return True

        worker._queue = succeed
        asyncio.run(worker._deliver(offer))
        assert worker.delivery_retry_at == 0
        assert worker.delivery_retry_seconds == 0.1


def test_cancelling_worker_reaps_inflight_delivery(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, _ = _worker(world, tmp_path, plan={"decision": "wake"}, product="codex")
        _hook_compatible_policy(world)
        marker = tmp_path / "delivery-pid"
        codex = tmp_path / "blocking-codex"
        release = tmp_path / "cancel-release"
        codex.write_text(
            f"#!{sys.executable}\n"
            "import os,pathlib,time\n"
            f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
            f"release = pathlib.Path({str(release)!r})\n"
            "while not release.exists(): time.sleep(0.01)\n"
        )
        codex.chmod(0o700)
        worker = Worker(replace(worker.instance, codex=str(codex), delivery_timeout_seconds=20, poll_seconds=0.02))
        worker.instance.receipts.bind("cancel_delivery")
        asyncio.run(worker.instance.secretary.activate())
        say(world, room, "worker", "Start a delivery that must be cancelled")

        async def cancel_worker() -> int:
            task = asyncio.create_task(worker.run())
            try:
                await _wait_for(marker.exists, timeout=15)
                return int(marker.read_text())
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        pid = asyncio.run(cancel_worker())
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("delivery command is still alive after worker cancellation")


def _secretary(capsys, *argv: str) -> dict:
    """`python -m baton.integrations.secretary ARGV`, run in this process, and the JSON it printed.

    The entry point sets the umask and the httpx and httpcore log levels, and may configure the root logger, for its
    own process; all of them are put back so later tests in this process see what they set themselves. The module
    entry still runs as a real process in test_secretary_integration.
    """
    from baton.integrations.secretary.__main__ import main

    loggers = [logging.getLogger(name) for name in ("", "httpx", "httpcore")]
    levels = [logger.level for logger in loggers]
    handlers = list(logging.getLogger().handlers)
    umask = os.umask(0o077)
    try:
        code = main(list(argv))
    finally:
        os.umask(umask)
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)
        logging.getLogger().handlers[:] = handlers
    out = capsys.readouterr()
    assert code == 0, out.err
    return json.loads(out.out)


def test_status_heartbeat_budget_includes_slow_successful_delivery(tmp_path, capsys) -> None:
    from test_secretary_integration import _install_args

    from baton.integrations.secretary.instance import Instance

    with make_world(tmp_path) as world:
        *_, args = _install_args(world, tmp_path, "codex")
        instance_path = Path(_secretary(capsys, *args)["instance"])
        settings = json.loads(instance_path.read_text())
        settings.update(budget_seconds=20, delivery_timeout_seconds=20)
        instance_path.write_text(json.dumps(settings))
        instance = Instance.load(instance_path)
        instance.receipts.set("health", {"status": "running", "checked_at": time.time() - 45})
        assert _secretary(capsys, "--instance", str(instance_path), "status")["heartbeat_stale"] is False
        instance.receipts.set("health", {"status": "running", "checked_at": time.time() - 80})
        assert _secretary(capsys, "--instance", str(instance_path), "status")["heartbeat_stale"] is True
