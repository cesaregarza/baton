"""Schedule fresh alerts, recoverable failures, and grouped digests for one secretary."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from baton.config.secretary import Policy, load_policy, validate_decision_budget

from ..inbox.references import ReferenceWindow
from ..inbox.sources import trigger_position
from ..inbox.window import InboxTooLarge, InboxWindow
from .backlog import Backlog, Page
from .decide import DecisionRun, SecretaryResponseInvalid, SecretaryUnavailable
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
    async def open(cls, secretary: Secretary, database: DatabaseWorker, *, budget_seconds: float) -> AttentionQueue:
        policy = await database.call(load_policy, secretary.config, secretary.policy_path)
        validate_decision_budget(policy, budget_seconds)
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

    async def assess(self, budget: float, *, trigger: str | None = None, hourly: bool = False) -> dict | None:
        """The host API resumes retained work before scanning another page."""
        position = await self.database.call(trigger_position, self.secretary.config, self.policy.principal, trigger)
        due = await self.database.call(
            self.backlog.due, time.time(), position=position, mode="digest" if hourly else None
        )
        if due:
            return await self._attempt(due[0], budget, trigger=trigger if due[0].mode == "alert" else None)
        if position is not None and await self.database.call(self.backlog.waiting_retry, position):
            raise SecretaryUnavailable("the retained page is waiting for retry backoff")
        return await self.digest(budget) if hourly else await self.scan(budget, trigger=trigger)

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
        scheduled = (
            ((True, self.digest), (False, self.retry)) if digest_first else ((False, self.retry), (True, self.digest))
        )
        for held, action in scheduled:
            if await self.database.call(self.backlog.due, time.time(), held=held):
                await action(budget)
                break
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
        restored = await self._restore_notice(page)
        if restored is not None:
            return restored
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
            content=isinstance(error, (SecretaryResponseInvalid, InboxTooLarge)),
        )
        if failed.content_failures < self.policy.max_attempts:
            return None
        split = await self.database.call(
            InboxWindow.split_position, self.secretary.config, self.policy, page.since, page.through
        )
        if split is not None:
            children = [Page(page.since, split, page.mode), Page(split, page.through, page.mode)]
            await self.database.call(self.backlog.replace, [page], children, time.time())
            return None
        result = await self._failure_notice(page)
        if result is None:
            await self.database.call(self.backlog.discard_empty, page)
            return None
        return await self._finish_notice(page, result)

    async def _failure_notice(self, page: Page) -> dict | None:
        refs = await self.database.call(
            ReferenceWindow.read, self.secretary.config, self.policy, page.since, page.through
        )
        if not refs.refs:
            return None
        reason = (
            "This event was previously held for a digest, but the secretary could not summarize it "
            "after repeated invalid responses."
            if page.mode == "digest"
            else "The secretary could not summarize this event after repeated invalid responses. "
            "No relevance judgment was made."
        )
        return Outcome(self.policy, refs, self.floor, page.mode).notice(
            reason, kind="failure_notice", decision="keep" if page.mode == "digest" else "wake"
        )

    async def _restore_notice(self, page: Page) -> dict | None:
        """Complete an interrupted notice write before attempting another model summary."""
        if page.content_failures < self.policy.max_attempts:
            return None
        candidate = await self._failure_notice(page)
        if candidate is None:
            return None
        saved = await self.database.call(self.backlog.log.get, candidate["decision_id"])
        return await self._finish_notice(page, saved) if saved is not None else None

    async def _finish_notice(self, page: Page, result: dict) -> dict:
        result = await self.database.call(persist, self.secretary, self.policy, result)
        await self.database.call(self.backlog.finish, page, result, time.time(), self.policy.digest_seconds)
        return result
