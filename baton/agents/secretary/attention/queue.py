"""Schedule fresh alerts, recoverable failures, and grouped digests for one secretary."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from baton.config.secretary import Policy, load_policy

from ..inbox.references import ReferenceWindow
from ..inbox.window import InboxTooLarge, InboxWindow
from .backlog import Backlog, Page
from .decide import DecisionRun, SecretaryUnavailable
from .outcome import Outcome, persist
from .runtime import DatabaseWorker
from .state import AttentionLog

if TYPE_CHECKING:
    from ..service import Secretary


@dataclass
class AttentionQueue:
    secretary: Secretary
    database: DatabaseWorker
    policy: Policy
    backlog: Backlog
    floor: int
    last_error: Exception | None = None

    @classmethod
    async def open(cls, secretary: Secretary, database: DatabaseWorker) -> AttentionQueue:
        policy = await database.call(load_policy, secretary.config, secretary.policy_path)
        epoch, head = await database.call(InboxWindow.head, secretary.config)
        log = AttentionLog(secretary.state_path, secretary.config.data_dir, policy.principal)
        _, floor = await database.call(log.activate, epoch, head)
        return cls(secretary, database, policy, Backlog(log), floor)

    async def scan(self, budget: float, *, trigger: str | None = None) -> dict | None:
        since = await self.database.call(self.backlog.scanned)
        try:
            window = await self.database.call(InboxWindow.read, self.secretary.config, self.policy, since)
        except InboxTooLarge as error:
            page = Page(since, error.extra["through_pos"])
            await self.database.call(self.backlog.enqueue, page, time.time())
            return await self._failed(page, error)
        run = DecisionRun(self.secretary, budget, since, self.floor, trigger, through=window.through)
        run._check_trigger(window)
        if window.through == since:
            return await run.run()
        page = Page(since, window.through)
        await self.database.call(self.backlog.enqueue, page, time.time())
        if not window.events:
            await self.database.call(self.backlog.discard_empty, page)
            return await run.run()
        return await self._attempt(page, budget, trigger=trigger)

    async def retry(self, budget: float) -> dict | None:
        due = await self.database.call(self.backlog.due, time.time())
        return await self._attempt(due[0], budget) if due else None

    async def digest(self, budget: float) -> dict | None:
        due = await self.database.call(self.backlog.due, time.time(), held=True, limit=self.policy.max_events)
        if not due:
            return None
        group = [due[0]]
        for page in due[1:]:
            if group[-1].through != page.since:
                break
            try:
                window = await self.database.call(
                    InboxWindow.read, self.secretary.config, self.policy, group[0].since, through=page.through
                )
                if not window.fits_output(self.policy):
                    break
            except InboxTooLarge:
                break
            group.append(page)
        combined = Page(group[0].since, group[-1].through, "digest")
        await self.database.call(self.backlog.replace, group, [combined], time.time())
        return await self._attempt(combined, budget)

    async def poll(self, budget: float, *, digest_first: bool = False) -> list[dict]:
        """Reserve one assessment budget for fresh alerts and one for scheduled work.

        New traffic cannot consume the recovery budget. The worker alternates which
        scheduled lane goes first so retries and digests also get time under load.
        """
        await self.scan(budget)
        deadline = time.monotonic() + budget
        scheduled = (self.digest, self.retry) if digest_first else (self.retry, self.digest)
        for action in scheduled:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await action(remaining)
        return await self.pending()

    async def pending(self) -> list[dict]:
        results = []
        for decision in await self.database.call(self.backlog.ready):
            result = await self.secretary.pending(decision)
            if result is None:
                await self.database.call(self.backlog.invalidate, decision, time.time())
            else:
                results.append(result)
        return sorted(results, key=lambda result: (result["mode"] == "digest", result["since"]))

    async def _attempt(self, page: Page, budget: float, *, trigger: str | None = None) -> dict | None:
        try:
            result = await DecisionRun(
                self.secretary,
                budget,
                page.since,
                self.floor,
                trigger,
                hourly=page.mode == "digest",
                through=page.through,
            ).run()
        except (SecretaryUnavailable, InboxTooLarge) as error:
            return await self._failed(page, error)
        if result["event_count"] == 0:
            await self.database.call(self.backlog.discard_empty, page)
        else:
            await self.database.call(self.backlog.finish, page, result, time.time(), self.policy.digest_seconds)
        return result

    async def _failed(self, page: Page, error: Exception) -> dict | None:
        self.last_error = error
        failed = await self.database.call(
            self.backlog.failed,
            page,
            time.time(),
            type(error).__name__,
            initial=self.policy.retry_initial_seconds,
            maximum=self.policy.retry_max_seconds,
        )
        if failed.attempts < self.policy.max_attempts:
            return None
        split = await self.database.call(
            InboxWindow.split_position, self.secretary.config, self.policy, page.since, page.through
        )
        if split is not None:
            children = [Page(page.since, split, page.mode), Page(split, page.through, page.mode)]
            await self.database.call(self.backlog.replace, [page], children, time.time())
            return None
        refs = await self.database.call(
            ReferenceWindow.read, self.secretary.config, self.policy, page.since, page.through
        )
        if not refs.refs:
            await self.database.call(self.backlog.discard_empty, page)
            return None
        result = Outcome(self.policy, refs, self.floor, "alert").notice(
            "The secretary could not summarize this event after repeated attempts. No relevance judgment was made.",
            kind="failure_notice",
        )
        result = await self.database.call(persist, self.secretary, self.policy, result)
        await self.database.call(self.backlog.finish, page, result, time.time(), self.policy.digest_seconds)
        return result
