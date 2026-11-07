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

    async def scan(self, budget: float, *, trigger: str | None = None) -> dict | None:
        since = await self.database.call(self.backlog.scanned)
        try:
            window = await self.database.call(InboxWindow.read, self.secretary.config, self.policy, since)
        except InboxTooLarge as error:
            page = await self.database.call(self.backlog.enqueue, Page(since, error.extra["through_pos"]), time.time())
            return await self._failed(page, error, await self._labels(page, trigger), split_now=True)
        run = DecisionRun(self.secretary, budget, since, self.floor, trigger, through=window.through)
        if window.through == since:
            return await run.run()
        page = await self.database.call(self.backlog.enqueue, Page(since, window.through), time.time())
        if not window.events:
            await self.database.call(self.backlog.discard_empty, page)
            return await run.run()
        return await self._attempt(page, budget, trigger=trigger)  # a trigger beyond this page makes it catch-up

    async def assess(self, budget: float, *, trigger: str | None = None, hourly: bool = False) -> dict | None:
        """The host API resumes retained work before scanning another page."""
        position = await self.database.call(trigger_position, self.secretary.config, self.policy.principal, trigger)
        due = await self.database.call(self.backlog.due, time.time(), mode="digest" if hourly else None)
        if due:
            # The page gets the event trigger whether or not it holds it, so the run applies its one rule: a page
            # without the trigger is catch-up, as a scanned page is. A digest page never carries one, so for a caller
            # with a trigger it is catch-up too.
            event_trigger = trigger if position is not None else None
            result = await self._attempt(due[0], budget, trigger=event_trigger)
            if result is not None or self.last_error is not None:
                return result
        if await self.database.call(self.backlog.waiting_retry, position, mode="digest" if hourly else None):
            raise SecretaryUnavailable("the retained page is waiting for retry backoff")
        return await self._digest(budget) if hourly else await self.scan(budget, trigger=trigger)

    async def retry(self, budget: float) -> dict | None:
        due = await self.database.call(self.backlog.due, time.time())
        return await self._attempt(due[0], budget) if due else None

    async def digest(self, budget: float) -> dict | None:
        """The hourly's scheduled lane: the due digest, unless a digest already passed over the earliest due retry.

        The host's scheduled lane calls this whenever a held page is due, and `retry` only when none is. With any
        traffic a held page is due almost every hour, callbacks or not: the hourly's own scan holds pages for events
        that never reach a callback, such as job events and forced wakes. So a due retry could wait there without
        bound. Now a digest marks the earliest due retry it passes over, and the next scheduled lane takes that retry
        first, clearing the mark as it does. The mark is stored with the page, since each hourly is a fresh process.
        While both are due the lane alternates: a due retry waits behind at most one digest for each retry due before
        it, and a digest behind at most one retry, in the same lane and budget, with no new timer. The scan lane never
        takes scheduled work, because its budget is the smaller: a page that fits the scheduled lane could time out
        there, and a timeout counts toward the gate's latch.
        """
        if await self.database.call(self.backlog.passed_over, time.time()):
            # Cleared as the lane takes the retry, not by the attempt's outcome: an attempt cut off by the host's lane
            # timeout records none, and a mark it left would take every later lane while the digest waited.
            await self.database.call(self.backlog.clear_passed_over)
            return await self.retry(budget)
        await self.database.call(self.backlog.pass_over, time.time())
        return await self._digest(budget)

    async def _digest(self, budget: float) -> dict | None:
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
        stored = await self.database.call(
            self.backlog.replace, group, [Page(group[0].since, group[-1].through, "digest")], time.time()
        )
        if stored is None:
            return None  # another call combined, finished or changed these pages since they were read
        return await self._attempt(stored[0], budget)

    async def poll(self, budget: float, *, digest_first: bool = False) -> list[dict]:
        """Reserve one assessment budget for fresh alerts and one for scheduled work.

        New traffic cannot consume the recovery budget. The worker alternates which
        scheduled lane goes first so retries and digests also get time under load.
        """
        await self.scan(budget)
        scheduled = (
            ((True, self._digest), (False, self.retry)) if digest_first else ((False, self.retry), (True, self._digest))
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
        labels = await self._labels(page, trigger)
        restored, own = await self._restore_result(page, labels)
        if restored is not None:
            if not own:
                return None
            acknowledged = await self.database.call(self.backlog.log.is_acknowledged, restored["decision_id"])
            return None if acknowledged else restored
        digest = page.mode == "digest"
        run = DecisionRun(
            self.secretary,
            budget,
            page.since,
            self.floor,
            None if digest else trigger,
            hourly=digest,
            through=page.through,
        )
        # A digest never carries a trigger, so a caller with one that resumes a digest page is catching up.
        run.catch_up = digest and trigger is not None
        try:
            result = await run.run()
        except (SecretaryUnavailable, InboxTooLarge) as error:
            if run.model_successes is not None:
                # Accept our own baseline update, while still rejecting a later concurrent callback's write.
                page = replace(page, model_successes=run.model_successes, model_policy=run.model_policy)
            return await self._failed(
                page, error, labels, split_now=isinstance(error, InboxTooLarge) and not run.consulted
            )
        if result["event_count"] == 0:
            await self.database.call(self.backlog.discard_empty, page)
        else:
            await self.database.call(self.backlog.finish, page, result, time.time(), self.policy.digest_seconds)
        return result

    async def _failed(
        self, page: Page, error: Exception, labels: tuple[str | None, bool], *, split_now: bool = False
    ) -> dict | None:
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
        # call, so a refusal before any model call splits it at once rather than after max_attempts backoffs. A
        # refusal after the engine or the model was asked keeps the counted path, as a timeout and an invalid answer
        # do. The split still follows the failure's compare-and-set above, so a duplicate callback that read the same
        # page splits nothing.
        if not exhausted and not split_now:
            return None
        split = await self.database.call(
            InboxWindow.split_position, self.secretary.config, self.policy, page.since, page.through
        )
        if split is not None:
            children = [Page(page.since, split, page.mode), Page(split, page.through, page.mode)]
            # The page as this failure stored it: a caller that refused it again since, or split it, changed that.
            await self.database.call(self.backlog.replace, [failed], children, time.time())
            return None
        if not exhausted:
            # One event over the bounds cannot split: it keeps its counted refusals and backoff, so a widened bound
            # can still assess it, and only after max_attempts does it get its cited failure notice.
            return None
        result = await self._failure_notice(page, labels)
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

    async def _failure_notice(self, page: Page, labels: tuple[str | None, bool]) -> dict | None:
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
        trigger, catch_up = labels
        return Outcome(self.policy, refs, self.floor, page.mode, trigger, catch_up=catch_up).notice(
            reason, kind="failure_notice", decision="keep" if page.mode == "digest" else "wake"
        )

    async def _labels(self, page: Page, trigger: str | None) -> tuple[str | None, bool]:
        """The trigger and catch-up label that this call's result for the page carries, as DecisionRun assigns them.

        A digest never names a trigger, so for a caller with one it is catch-up. An alert page names the caller's
        trigger when it holds that event, and is catch-up when the event is elsewhere. A trigger that is not a
        visible event is refused here, before any saved result is considered, as the run would refuse it.
        """
        if page.mode == "digest":
            return None, trigger is not None
        position = await self.database.call(trigger_position, self.secretary.config, self.policy.principal, trigger)
        if position is None or page.since < position <= page.through:
            return trigger, False
        return None, True

    async def _restore_result(self, page: Page, labels: tuple[str | None, bool]) -> tuple[dict | None, bool]:
        """Finish a saved assessment or notice before asking either provider again; the result, and whether it is
        this call's own.

        A result is saved before its page is finished, so a call stopped between the two leaves one for the window
        that a call of another kind can find: one with no trigger, another trigger, or the other catch-up label. Its
        page is finished with it all the same, so the window is assessed once and that call's result is what is
        pending. It is never returned as this call's, since its trigger and label are that call's.
        """
        candidates = await self.database.call(
            self.backlog.log.for_window, self.policy.digest, page.since, page.through, self.floor, page.mode
        )
        foreign = None
        for result in candidates:
            current = await self.database.call(
                _current_window, self.secretary.config, self.policy, self.backlog.log, result
            )
            if not current:
                continue
            if (result["trigger"], result.get("catch_up", False)) == labels:
                return await self._finish_result(page, result), True
            foreign = foreign or result
        if foreign is None:
            return None, False
        return await self._finish_result(page, foreign), False

    async def _finish_result(self, page: Page, result: dict) -> dict:
        result = await self.database.call(persist, self.secretary, self.policy, result)
        await self.database.call(self.backlog.finish, page, result, time.time(), self.policy.digest_seconds)
        return result
