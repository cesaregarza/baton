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
from dataclasses import dataclass

from baton import datadir
from baton.agents.secretary.attention.health import EngineHealth, ModelHealth
from baton.agents.secretary.attention.queue import AttentionQueue
from baton.agents.secretary.attention.runtime import DatabaseWorker
from baton.agents.secretary.attention.state import AttentionLog
from baton.config import ConfigError
from baton.config.secretary import Policy, load_policy, require_hook_summary

from .instance import Instance

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryOffer:
    session: str
    decisions: tuple[str, ...]
    retry_initial_seconds: float
    retry_max_seconds: float


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
        self.failed_pages = 0
        self.digest_first = False
        self.worker_status = "running"
        self.worker_error: str | None = None
        self.engine_health = {"status": "not_used", "last_failure": None, "checked_at": None}
        self.model_health = {"status": "not_used", "last_failure": None, "checked_at": None}
        self.delivery_health = {"status": "not_used", "last_failure": None, "checked_at": None}
        self.delivery_offer: DeliveryOffer | None = None
        self.delivery_task: asyncio.Task[None] | None = None
        self.delivery_retry_at = 0.0
        self.delivery_retry_seconds: float | None = None
        self.health_lock = asyncio.Lock()

    def _health_status(self, *, worker_status: str = "running") -> dict:
        components = {
            "engine": self.engine_health,
            "model": self.model_health,
            "delivery": self.delivery_health,
        }
        return {
            "status": "degraded"
            if any(component["status"] == "unavailable" for component in components.values())
            else worker_status,
            **components,
        }

    async def run(self) -> None:
        with exclusive(self.instance):
            async with DatabaseWorker() as database:
                policy = await database.call(
                    load_policy, self.instance.secretary.config, self.instance.secretary.policy_path
                )
                require_hook_summary(policy)
                if policy.model["provider"] != "command" and not os.environ.get(policy.model["api_key_env"]):
                    secretary = self.instance.secretary
                    attention = AttentionLog(secretary.state_path, secretary.config.data_dir, policy.principal)
                    await database.call(ModelHealth(attention, policy.digest).failed, "missing_credential")
                elif policy.model["provider"] != "command":
                    secretary = self.instance.secretary
                    attention = AttentionLog(secretary.state_path, secretary.config.data_dir, policy.principal)
                    await database.call(ModelHealth(attention, policy.digest).credentials_present)
                retry_seconds = policy.retry_initial_seconds
                self.delivery_retry_seconds = policy.retry_initial_seconds
                try:
                    while True:
                        try:
                            await self.step(database)
                            await self._write_health(database)
                            retry_seconds = policy.retry_initial_seconds
                        except Exception as error:
                            log.warning("secretary pass failed: %s", type(error).__name__)
                            await self._write_health(database, worker_status="retrying", error=type(error).__name__)
                            await asyncio.sleep(retry_seconds)
                            retry_seconds = min(policy.retry_max_seconds, retry_seconds * 2)
                        await self._start_delivery_if_due()
                        await asyncio.sleep(self.instance.poll_seconds)
                finally:
                    await self._stop_delivery()

    async def step(self, database: DatabaseWorker) -> None:
        queue = await AttentionQueue.open(
            self.instance.secretary, database, budget_seconds=self.instance.budget_seconds
        )
        try:
            pending = await queue.poll(self.instance.budget_seconds, digest_first=self.digest_first)
        finally:
            self.engine_health = await database.call(EngineHealth(queue.backlog.log).read)
            self.model_health = await database.call(ModelHealth(queue.backlog.log, queue.policy.digest).read)
        self.digest_first = not self.digest_first
        self.failed_pages = await database.call(queue.backlog.failures)
        decisions = [result["decision_id"] for result in pending]
        receipts = self.instance.receipts
        await database.call(receipts.set, "pending", decisions)
        await database.call(self._signal, decisions)
        session = await database.call(receipts.get, "session")
        if decisions and session and self.instance.product == "codex":
            await self._capture_delivery(database, session, decisions, queue.policy)
        else:
            self.delivery_offer = None

    async def _capture_delivery(
        self, database: DatabaseWorker, session: str, decisions: list[str], policy: Policy
    ) -> None:
        previous = await database.call(self.instance.receipts.get, "queued")
        offered = (
            previous.get("decisions", []) if isinstance(previous, dict) and previous.get("session") == session else []
        )
        if set(decisions).difference(offered):
            self.delivery_offer = DeliveryOffer(
                session, tuple(decisions), policy.retry_initial_seconds, policy.retry_max_seconds
            )
        else:
            self.delivery_offer = None

    async def _write_health(
        self,
        database: DatabaseWorker,
        *,
        worker_status: str = "running",
        error: str | None = None,
        delivery_only: bool = False,
    ) -> None:
        """Serialize receipt updates so delivery cannot hide the main loop's last result."""
        async with self.health_lock:
            if not delivery_only:
                self.worker_status = worker_status
                self.worker_error = error
            health = {
                **self._health_status(worker_status=self.worker_status),
                "failed_pages": self.failed_pages,
                "checked_at": time.time(),
            }
            if self.worker_error is not None:
                health["error"] = self.worker_error
            await database.call(self.instance.receipts.set, "health", health)

    async def _start_delivery_if_due(self) -> None:
        task = self.delivery_task
        if task is not None and task.done():
            self.delivery_task = None
            try:
                task.result()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning("secretary delivery task failed: %s", type(error).__name__)
        offer = self.delivery_offer
        if offer is None or self.delivery_task is not None or time.monotonic() < self.delivery_retry_at:
            return
        async with DatabaseWorker() as database:
            queued = await database.call(self.instance.receipts.get, "queued")
        offered = (
            queued.get("decisions", []) if isinstance(queued, dict) and queued.get("session") == offer.session else []
        )
        remaining = tuple(decision for decision in offer.decisions if decision not in offered)
        if not remaining:
            self.delivery_offer = None
            return
        self.delivery_task = asyncio.create_task(self._deliver(offer), name="secretary-delivery")

    async def _deliver(self, offer: DeliveryOffer) -> None:
        success = await self._queue(offer.session, self.instance.delivery_timeout_seconds)
        async with DatabaseWorker() as database:
            if success:
                await database.call(
                    self.instance.receipts.set,
                    "queued",
                    {"session": offer.session, "decisions": list(offer.decisions)},
                )
                self._remove_delivered_offer(offer)
                self.delivery_retry_at = 0.0
                self.delivery_retry_seconds = offer.retry_initial_seconds
            else:
                self._schedule_delivery_retry(offer, time.monotonic())
            await self._write_health(database, delivery_only=True)

    def _schedule_delivery_retry(self, offer: DeliveryOffer, now: float) -> None:
        delay = self.delivery_retry_seconds or offer.retry_initial_seconds
        self.delivery_retry_at = now + delay
        self.delivery_retry_seconds = min(offer.retry_max_seconds, delay * 2)

    def _remove_delivered_offer(self, delivered: DeliveryOffer) -> None:
        current = self.delivery_offer
        if current is None or current.session != delivered.session:
            return
        # Keep the full pending snapshot when new decisions arrived during delivery. The queued
        # receipt must retain earlier offers too, or each pass would announce alternating subsets.
        if set(current.decisions) <= set(delivered.decisions):
            self.delivery_offer = None

    async def _stop_delivery(self) -> None:
        task = self.delivery_task
        self.delivery_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as error:
            log.warning("secretary delivery task failed: %s", type(error).__name__)

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
            self._record_delivery_failure("configuration")
            return False
        message = (
            "Baton secretary has an attention summary. Receive it with "
            + invocation(self.instance, "receive", "--text")
            + ". Acknowledge only after reading it. If no summary remains, no action is needed."
        )
        try:
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
        except OSError:
            self._record_delivery_failure("launch")
            return False
        try:
            async with asyncio.timeout(timeout):
                exit_code = await process.wait()
            if exit_code == 0:
                self.delivery_health = {
                    "status": "available",
                    "last_failure": self.delivery_health.get("last_failure"),
                    "last_failure_at": self.delivery_health.get("last_failure_at"),
                    "checked_at": time.time(),
                }
                return True
            self._record_delivery_failure("nonzero_exit")
            return False
        except TimeoutError:
            self._record_delivery_failure("timeout")
            return False
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()

    def _record_delivery_failure(self, category: str) -> None:
        now = time.time()
        previous = self.delivery_health
        self.delivery_health = {
            "status": "unavailable",
            "last_failure": category,
            "last_failure_at": now,
            "checked_at": now,
        }
        if previous.get("status") != "unavailable" or previous.get("last_failure") != category:
            log.warning("secretary delivery failed: %s", category)
