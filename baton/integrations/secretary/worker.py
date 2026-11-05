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
from baton.agents.secretary.attention.runtime import DatabaseWorker
from baton.agents.secretary.inbox.read import read
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
        self.seen: int | None = None
        self.retry_at = 0.0

    async def run(self) -> None:
        with exclusive(self.instance):
            async with DatabaseWorker() as database:
                policy = await database.call(
                    load_policy, self.instance.secretary.config, self.instance.secretary.policy_path
                )
                require_hook_summary(policy)
                retry_seconds = 5
                while True:
                    try:
                        await self.step(database)
                        await database.call(
                            self.instance.receipts.set, "health", {"status": "running", "checked_at": time.time()}
                        )
                        retry_seconds = 5
                    except Exception as error:
                        # No inbound text, model output, URLs or credentials in service logs.
                        log.warning("secretary pass failed: %s", type(error).__name__)
                        await database.call(
                            self.instance.receipts.set,
                            "health",
                            {"status": "retrying", "error": type(error).__name__, "checked_at": time.time()},
                        )
                        await asyncio.sleep(max(retry_seconds, self.instance.poll_seconds))
                        retry_seconds = min(300, retry_seconds * 2)
                    await asyncio.sleep(self.instance.poll_seconds)

    async def step(self, database: DatabaseWorker) -> None:
        secretary, receipts = self.instance.secretary, self.instance.receipts
        page = await database.call(read, secretary.config, secretary.policy_path, after=self.seen or 0, limit=1)
        if self.seen is None or page["deliveries"] or page["context"]:
            result = await secretary.decide(budget_seconds=self.instance.budget_seconds)
            await database.call(receipts.set, "pending", result["decision_id"] if result["brief"] else None)
            self.seen = result["through_pos"]
        else:
            self.seen = max(self.seen, page["through_pos"])
        decision = await database.call(receipts.get, "pending")
        pending = await secretary.pending(decision) if decision else None
        if decision and pending is None:
            await database.call(receipts.set, "pending", None)
        await database.call(self._signal, pending["decision_id"] if pending else None)
        session = await database.call(receipts.get, "session")
        if pending and session and self.instance.product == "codex":
            key = [session, pending["decision_id"]]
            if await database.call(receipts.get, "queued") != key and time.monotonic() >= self.retry_at:
                if await self._queue(session):
                    await database.call(receipts.set, "queued", key)
                self.retry_at = time.monotonic() + 30

    def _signal(self, decision: str | None) -> None:
        # Only a pointer is watched; summaries are read through current authorization checks.
        with datadir.opened(self.instance.path.parent) as directory:
            raw = json.dumps({"decision_id": decision}).encode()
            fd = datadir.open_file(
                directory, self.instance.signal_path.name, os.O_CREAT | os.O_RDONLY, directory=self.instance.path.parent
            )
            with os.fdopen(fd, "rb") as source:
                previous = source.read()
            if previous != raw:
                datadir.replace_file(directory, self.instance.signal_path.name, raw)

    async def _queue(self, session: str) -> bool:
        executable = self.instance.codex
        if executable is None:
            raise ValueError("Codex delivery requires a configured executable")
        message = (
            "Baton secretary has an attention summary. Receive it with "
            + invocation(self.instance, "receive")
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
            async with asyncio.timeout(10):
                return await process.wait() == 0
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
