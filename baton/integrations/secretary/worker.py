"""A supervised async worker watches visible Baton activity and announces model summaries."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import shlex
import signal
import sys
import time
from contextlib import contextmanager

from baton import datadir
from baton.agents.secretary.attention.health import EngineHealth
from baton.agents.secretary.attention.queue import AttentionQueue
from baton.agents.secretary.attention.runtime import DatabaseWorker
from baton.config import ConfigError
from baton.config.secretary import load_policy, require_hook_summary

from .instance import Instance

log = logging.getLogger(__name__)


def invocation(instance: Instance, *action: str) -> str:
    return shlex.join([sys.executable, "-m", "baton.integrations.secretary", "--instance", str(instance.path), *action])


@contextmanager
def exclusive(instance: Instance):
    """A kernel lock is released on death, including SIGKILL; no stale PID repair."""
    with datadir.opened(instance.path.parent) as directory:
        fd = datadir.open_file(directory, "worker.lock", os.O_CREAT | os.O_RDWR, directory=instance.path.parent)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigError("a worker already owns this secretary instance") from None
        yield
    finally:
        os.close(fd)


class Worker:
    def __init__(self, instance: Instance):
        self.instance = instance
        self.retry_at = 0.0
        self.failed_pages = 0
        self.digest_first = False
        self.engine_health = {"status": "not_used", "last_failure": None, "checked_at": None}

    async def run(self) -> None:
        with exclusive(self.instance):
            async with DatabaseWorker() as database:
                policy = await database.call(
                    load_policy, self.instance.secretary.config, self.instance.secretary.policy_path
                )
                require_hook_summary(policy)
                retry_seconds = policy.retry_initial_seconds
                while True:
                    try:
                        await self.step(database)
                        await database.call(
                            self.instance.receipts.set,
                            "health",
                            {
                                "status": "degraded" if self.engine_health["status"] == "unavailable" else "running",
                                "failed_pages": self.failed_pages,
                                "engine": self.engine_health,
                                "checked_at": time.time(),
                            },
                        )
                        retry_seconds = policy.retry_initial_seconds
                    except Exception as error:
                        log.warning("secretary pass failed: %s", type(error).__name__)
                        await database.call(
                            self.instance.receipts.set,
                            "health",
                            {
                                "status": "retrying",
                                "error": type(error).__name__,
                                "engine": self.engine_health,
                                "checked_at": time.time(),
                            },
                        )
                        await asyncio.sleep(retry_seconds)
                        retry_seconds = min(policy.retry_max_seconds, retry_seconds * 2)
                    await asyncio.sleep(self.instance.poll_seconds)

    async def step(self, database: DatabaseWorker) -> None:
        queue = await AttentionQueue.open(
            self.instance.secretary, database, budget_seconds=self.instance.budget_seconds
        )
        try:
            pending = await queue.poll(self.instance.budget_seconds, digest_first=self.digest_first)
        finally:
            self.engine_health = await database.call(EngineHealth(queue.backlog.log).read)
        self.digest_first = not self.digest_first
        self.failed_pages = await database.call(queue.backlog.failures)
        decisions = [result["decision_id"] for result in pending]
        receipts = self.instance.receipts
        await database.call(receipts.set, "pending", decisions)
        await database.call(self._signal, decisions)
        session = await database.call(receipts.get, "session")
        if decisions and session and self.instance.product == "codex":
            await self._announce(database, session, decisions, queue.policy)

    async def _announce(self, database: DatabaseWorker, session: str, decisions: list[str], policy) -> None:
        previous = await database.call(self.instance.receipts.get, "queued")
        offered = (
            previous.get("decisions", []) if isinstance(previous, dict) and previous.get("session") == session else []
        )
        if not set(decisions).difference(offered) or time.monotonic() < self.retry_at:
            return
        if await self._queue(session, self.instance.delivery_timeout_seconds):
            await database.call(self.instance.receipts.set, "queued", {"session": session, "decisions": decisions})
            self.retry_at = 0.0
        else:
            self.retry_at = time.monotonic() + policy.retry_initial_seconds

    def _signal(self, decisions: list[str]) -> None:
        # Only a pointer is watched; summaries are read through current authorization checks.
        with datadir.opened(self.instance.path.parent) as directory:
            raw = json.dumps({"decision_ids": decisions}).encode()
            fd = datadir.open_file(
                directory, self.instance.signal_path.name, os.O_CREAT | os.O_RDONLY, directory=self.instance.path.parent
            )
            with os.fdopen(fd, "rb") as source:
                previous = source.read()
            if previous != raw:
                datadir.replace_file(directory, self.instance.signal_path.name, raw)

    async def _queue(self, session: str, timeout: float) -> bool:
        executable = self.instance.codex
        if executable is None:
            raise ValueError("Codex delivery requires a configured executable")
        message = (
            "Baton secretary has an attention summary. Receive it with "
            + invocation(self.instance, "receive", "--text")
            + ". Acknowledge only after reading it. If no summary remains, no action is needed."
        )
        process = await asyncio.create_subprocess_exec(
            executable,
            "queue",
            "--thread",
            session,
            "--message",
            message,
            "--resume",
            cwd=self.instance.project,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            async with asyncio.timeout(timeout):
                return await process.wait() == 0
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
