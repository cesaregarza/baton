"""Schedule fresh alerts, recoverable failures, and grouped digests for one secretary."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from baton.config.secretary import Policy, load_policy, validate_decision_budget

from ..inbox.references import ReferenceWindow
from ..inbox.sources import trigger_position
from ..inbox.window import InboxTooLarge, InboxWindow
from ..service import _current_window
from .backlog import Backlog, Page
from .decide import DecisionRun, SecretaryResponseInvalid, SecretaryUnavailable
from .health import ModelHealth
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

    async def scan(self, budget: float, *, trigger: str | None = None, scheduled: bool = True) -> dict | None:
        since = await self.database.call(self.backlog.scanned)
        try:
            window = await self.database.call(InboxWindow.read, self.secretary.config, self.policy, since)
        except InboxTooLarge as error:
            page = Page(since, error.extra["through_pos"])
            await self.database.call(self.backlog.enqueue, page, time.time())
            return await self._failed(page, error)
        run = DecisionRun(self.secretary, budget, since, self.floor, trigger, through=window.through)
        if window.events:
            page = Page(since, window.through)
            await self.database.call(self.backlog.enqueue, page, time.time())
            return await self._attempt(page, budget, trigger=trigger)  # a trigger beyond this page makes it catch-up
        if window.through != since:
            page = Page(since, window.through)
            await self.database.call(self.backlog.enqueue, page, time.time())
            await self.database.call(self.backlog.discard_empty, page)
        if trigger is None and scheduled:
            result = await self._scheduled(budget)
            if result is not None or self.last_error is not None:
                return result
        return await run.run()

    async def _scheduled(self, budget: float) -> dict | None:
        """An untriggered scan with nothing new to read spends its budget on scheduled work: a due retry, else a digest.

        The hourly runs its scan, then one scheduled lane, and that lane takes a digest whenever one is due. A held page
        makes one due a minute later, so with any traffic it never reached a retry, and retained work waited on
        callbacks, or with none at all on alternate hours; it also digested at most one page of held events an hour.
        A scan lane with nothing to scan now reaches either, within its own budget and deadline, with no new timer.
        Only a bare scan, the hourly's own lane, does this: `assess` has already resumed a due retry before it scans,
        and its alert decide must never return a digest; `poll`'s own scheduled lane alternates between the two.
        """
        now = time.time()
        if await self.database.call(self.backlog.due, now):
            return await self.retry(budget)
        if await self.database.call(self.backlog.due, now, held=True):
            return await self.digest(budget)
        return None

    async def assess(self, budget: float, *, trigger: str | None = None, hourly: bool = False) -> dict | None:
        """The host API resumes retained work before scanning another page."""
        position = await self.database.call(trigger_position, self.secretary.config, self.policy.principal, trigger)
        due = await self.database.call(self.backlog.due, time.time(), mode="digest" if hourly else None)
        if due:
            page = due[0]
            # An alert page gets the event trigger whether or not it holds it, so the run applies its one rule: a page
            # without the trigger is catch-up, as a scanned page is. A digest page never carries a trigger.
            event_trigger = trigger if page.mode == "alert" and position is not None else None
            result = await self._attempt(page, budget, trigger=event_trigger)
            if result is not None or self.last_error is not None:
                return result
        if await self.database.call(self.backlog.waiting_retry, position, mode="digest" if hourly else None):
            raise SecretaryUnavailable("the retained page is waiting for retry backoff")
        # This scan never takes scheduled work: the call has already resumed what was due, and must not digest.
        return await self.digest(budget) if hourly else await self.scan(budget, trigger=trigger, scheduled=False)

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
        await self.scan(budget, scheduled=False)  # the scheduled lane below alternates, so neither kind waits
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
        restored = await self._restore_result(page)
        if restored is not None:
            acknowledged = await self.database.call(self.backlog.log.is_acknowledged, restored["decision_id"])
            return None if acknowledged else restored
        run = DecisionRun(
            self.secretary,
            budget,
            page.since,
            self.floor,
            trigger,
            hourly=page.mode == "digest",
            through=page.through,
        )
        try:
            result = await run.run()
        except (SecretaryUnavailable, InboxTooLarge) as error:
            if run.model_successes is not None:
                # Accept our own baseline update, while still rejecting a later concurrent callback's write.
                page = replace(page, model_successes=run.model_successes, model_policy=run.model_policy)
            return await self._failed(page, error)
        if result["event_count"] == 0:
            await self.database.call(self.backlog.discard_empty, page)
        else:
            await self.database.call(self.backlog.finish, page, result, time.time(), self.policy.digest_seconds)
        return result

    async def _failed(self, page: Page, error: Exception) -> dict | None:
        health = await self.database.call(ModelHealth(self.backlog.log, self.policy.digest).read)
        category = error.category if isinstance(error, SecretaryUnavailable) else "oversized"
        failed = await self.database.call(
            self.backlog.failed,
            page,
            time.time(),
            category,
            initial=self.policy.retry_initial_seconds,
            maximum=self.policy.retry_max_seconds,
            content=isinstance(error, (SecretaryResponseInvalid, InboxTooLarge))
            or self._page_specific(page, category, health["successes"]),
            model_successes=health["successes"],
            model_policy=self.policy.digest,
        )
        if failed is None:
            return None  # A concurrent answer or callback already changed this page.
        self.last_error = error
        exhausted = failed.content_failures >= self.policy.max_attempts
        # A range over the input bounds stays over them however often it is retried, and halving it needs no model
        # call, so it splits on its first refusal rather than after max_attempts backoffs. The split still follows
        # the failure's compare-and-set above, so a duplicate callback that read the same page splits nothing.
        if not exhausted and not isinstance(error, InboxTooLarge):
            return None
        split = await self.database.call(
            InboxWindow.split_position, self.secretary.config, self.policy, page.since, page.through
        )
        if split is not None:
            children = [Page(page.since, split, page.mode), Page(split, page.through, page.mode)]
            await self.database.call(self.backlog.replace, [page], children, time.time())
            return None
        if not exhausted:
            # One event over the bounds cannot split: it keeps its counted refusals and backoff, so a widened bound
            # can still assess it, and only after max_attempts does it get its cited failure notice.
            return None
        result = await self._failure_notice(page)
        if result is None:
            await self.database.call(self.backlog.discard_empty, page)
            return None
        return await self._finish_result(page, result)

    def _page_specific(self, page: Page, category: str, successes: int) -> bool:
        """Another successful assessment since this page failed is evidence against a model-wide outage."""
        return (
            category in {"timeout", "rejected", "command_failed"}
            and page.attempts > 0
            and page.model_policy == self.policy.digest
            and successes > page.model_successes
        )

    async def _failure_notice(self, page: Page) -> dict | None:
        refs = await self.database.call(
            ReferenceWindow.read, self.secretary.config, self.policy, page.since, page.through
        )
        if not refs.refs:
            return None
        reason = (
            "This event was previously held for a digest, but the secretary could not summarize it "
            "after repeated failures specific to this page."
            if page.mode == "digest"
            else "The secretary could not summarize this event after repeated failures specific to this page. "
            "No relevance judgment was made."
        )
        return Outcome(self.policy, refs, self.floor, page.mode).notice(
            reason, kind="failure_notice", decision="keep" if page.mode == "digest" else "wake"
        )

    async def _restore_result(self, page: Page) -> dict | None:
        """Finish a saved assessment or notice before asking either provider again."""
        candidates = await self.database.call(
            self.backlog.log.for_window, self.policy.digest, page.since, page.through, self.floor, page.mode
        )
        for result in candidates:
            current = await self.database.call(
                _current_window, self.secretary.config, self.policy, self.backlog.log, result
            )
            if current:
                return await self._finish_result(page, result)
        return None

    async def _finish_result(self, page: Page, result: dict) -> dict:
        result = await self.database.call(persist, self.secretary, self.policy, result)
        await self.database.call(self.backlog.finish, page, result, time.time(), self.policy.digest_seconds)
        return result
