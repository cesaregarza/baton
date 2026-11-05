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
from baton.agents.secretary.attention.backlog import Backlog
from baton.agents.secretary.attention.runtime import DatabaseWorker
from baton.agents.secretary.attention.state import AttentionLog
from baton.agents.secretary.inbox.window import InboxTooLarge, InboxWindow
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
        self.failure: str | None = None

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
                        failure = await self.step(database)
                        if failure:
                            await database.call(
                                self.instance.receipts.set,
                                "health",
                                {"status": "retrying", "error": failure, "checked_at": time.time()},
                            )
                            await asyncio.sleep(max(retry_seconds, self.instance.poll_seconds))
                            retry_seconds = min(300, retry_seconds * 2)
                        else:
                            await database.call(
                                self.instance.receipts.set,
                                "health",
                                {"status": "running", "checked_at": time.time()},
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

    async def step(self, database: DatabaseWorker) -> str | None:
        secretary, receipts = self.instance.secretary, self.instance.receipts
        health = await database.call(receipts.get, "health")
        if self.failure is None and health and health.get("status") == "retrying":
            self.failure = health.get("error")
        _, floor = await secretary.activate()
        policy = await database.call(load_policy, secretary.config, secretary.policy_path)
        log_store = AttentionLog(secretary.state_path, secretary.config.data_dir, policy.principal)
        backlog = Backlog(log_store)
        now = time.time()
        failure, retried = await self._retry_due(database, backlog, floor, now)
        if retried and failure is None:
            self.failure = None
        elif failure is not None:
            self.failure = failure
        new_failure = await self._scan_new(database, backlog, floor, now)
        if new_failure is not None:
            self.failure = new_failure
        pending = await self._select_pending(database, backlog, now)
        await database.call(receipts.set, "pending", pending["decision_id"] if pending else None)
        await database.call(self._signal, pending["decision_id"] if pending else None)
        session = await database.call(receipts.get, "session")
        if pending and session and self.instance.product == "codex":
            key = [session, pending["decision_id"]]
            if await database.call(receipts.get, "queued") != key and time.monotonic() >= self.retry_at:
                if await self._queue(session):
                    await database.call(receipts.set, "queued", key)
                self.retry_at = time.monotonic() + 30
        return self.failure

    async def _retry_due(
        self, database: DatabaseWorker, backlog: Backlog, floor: int, now: float
    ) -> tuple[str | None, bool]:
        secretary = self.instance.secretary
        due = await database.call(backlog.due, now)
        if due is None:
            return None, False
        since, through = due
        try:
            policy = await database.call(load_policy, secretary.config, secretary.policy_path)
            window = await database.call(InboxWindow.read, secretary.config, policy, since, through=through)
            if not window.events:
                log_store = AttentionLog(secretary.state_path, secretary.config.data_dir, policy.principal)
                await database.call(log_store.acknowledge_empty, since, through)
                await database.call(backlog.record, since, through, decision=f"empty:{since}:{through}", now=now)
                return None, True
            result = await secretary.decide(
                budget_seconds=self.instance.budget_seconds,
                since=since,
                floor=floor,
                through=through,
                hourly=True,
            )
            decision = result["decision_id"] if result["brief"] else None
        except Exception as error:
            await database.call(backlog.record, since, through, decision=None, now=now)
            return type(error).__name__, True
        await database.call(backlog.record, since, through, decision=decision, now=now)
        return None, True

    async def _scan_new(self, database: DatabaseWorker, backlog: Backlog, floor: int, now: float) -> str | None:
        secretary = self.instance.secretary
        policy = await database.call(load_policy, secretary.config, secretary.policy_path)
        since = await database.call(backlog.scanned)
        try:
            window = await database.call(InboxWindow.read, secretary.config, policy, since)
        except InboxTooLarge as error:
            through = error.extra.get("through_pos")
            if type(through) is not int:
                return type(error).__name__
            await database.call(backlog.record, since, through, decision=None, now=now)
            return type(error).__name__
        if window.through == since:
            return None
        if not window.events:
            await database.call(
                AttentionLog(secretary.state_path, secretary.config.data_dir, policy.principal).acknowledge_empty,
                since,
                window.through,
            )
            await database.call(
                backlog.record, since, window.through, decision=f"empty:{since}:{window.through}", now=now
            )
            return None
        try:
            result = await secretary.decide(
                budget_seconds=self.instance.budget_seconds,
                since=since,
                floor=floor,
                through=window.through,
            )
            decision = result["decision_id"] if result["brief"] else None
        except Exception as error:
            await database.call(backlog.record, since, window.through, decision=None, now=now)
            return type(error).__name__
        await database.call(backlog.record, since, window.through, decision=decision, now=now)
        return None

    async def _select_pending(self, database: DatabaseWorker, backlog: Backlog, now: float) -> dict | None:
        receipts, secretary = self.instance.receipts, self.instance.secretary
        previous = await database.call(receipts.get, "pending")
        candidates = await database.call(backlog.ready)
        if previous and previous in candidates:
            candidates.remove(previous)
            candidates.insert(0, previous)
        for decision in candidates:
            result = await secretary.pending(decision)
            if result is not None:
                return result
            await database.call(backlog.invalidate, decision, now)
        if previous and previous not in candidates:
            acknowledged = await database.call(backlog.log.is_acknowledged, previous)
            if acknowledged:
                return None
            result = await secretary.pending(previous)
            if result is not None:
                return result
            await database.call(backlog.invalidate, previous, now)
        return None

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
