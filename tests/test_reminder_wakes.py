"""Reminder wakes: a delivered message wake that is never read is tried again, twice, then stops.

The host wakes a role once per unread batch, so a role whose read fails after a delivered wake
stays asleep until it happens to read on its own. These cover the reminders that recover from
that, and the bounds that keep them from becoming noise.

Everything here uses a fake clock. No test sleeps, and none depends on wall-clock timing.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from conftest import make_config

from baton import messages, wakes
from baton.config import Wake
from baton.store import connect

WORKER_WAKE = '[roles.worker.wake]\ncommand = ["true"]\n'
FIVE_MINUTES, TWENTY_MINUTES = 300.0, 1200.0


class Recorder:
    """A stand-in deliverer: records each wake and answers with the next scripted outcome."""

    def __init__(self, *outcomes: bool) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def __call__(self, wake: Wake, wake_id: str, body: bytes) -> bool:
        self.calls.append({"id": wake_id, **json.loads(body)})
        return self.outcomes.pop(0) if self.outcomes else True


class Clock:
    """A clock the test moves by hand, so a five-minute wait costs nothing."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch) -> Clock:
    """Move the dispatcher's clock by hand.

    Patched with `raising=False` on purpose. Before the seam exists these tests must fail on what
    they assert -- "the reminder never went out" -- rather than erroring in setup, which would hide
    every behavioural assertion behind one missing attribute. `test_the_clock_has_one_seam` is what
    catches the seam being renamed, loudly and in one place.
    """
    moved = Clock()
    monkeypatch.setattr(wakes, "_now", moved, raising=False)
    return moved


def setcap(monkeypatch, value: int) -> None:
    """Patch the cap, tolerating its absence so these fail on behaviour rather than in setup."""
    monkeypatch.setattr(wakes, "REMINDER_CAP", value, raising=False)


def test_the_policy_numbers_are_the_ones_the_design_chose() -> None:
    """The offsets, the cap and its window are the designer's decisions; pin them in one visible place."""
    assert wakes.REMINDER_OFFSETS == (FIVE_MINUTES, TWENTY_MINUTES)
    assert wakes.REMINDER_CAP == 6
    assert wakes.REMINDER_WINDOW_SECONDS == 3600.0


def test_the_clock_has_one_seam() -> None:
    """Every due time and lease is measured by one function, so a test can move time without sleeping."""
    assert callable(getattr(wakes, "_now", None)), "wakes._now is missing: the tests cannot control time"
    assert isinstance(wakes._now(), float)


def rows(conn: sqlite3.Connection, role: str = "worker") -> dict[str, str]:
    """Every wake for the role, as key -> status."""
    return {r["key"]: r["status"]
            for r in conn.execute("SELECT key, status FROM wakes WHERE role = ?", (role,))}


def woken_batch(tmp_path: Path, recorder: Recorder, extra: str = WORKER_WAKE):
    """A host whose worker has one delivered message wake for seq 1, and has not read it."""
    host = make_config(tmp_path, extra=extra)
    dispatcher = wakes.Dispatcher(host.config, recorder)
    conn = connect(host.config.db_path)
    messages.send(conn, sender="planner", recipient="worker", text="the one it must read")
    dispatcher.pass_once()
    return host, dispatcher, conn


# --------------------------------------------------------------------- timing

def test_the_first_reminder_waits_the_whole_five_minutes(tmp_path: Path, clock) -> None:
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder())
    assert len(recorder.calls) == 1

    clock.advance(FIVE_MINUTES - 1)
    dispatcher.pass_once()
    assert len(recorder.calls) == 1, "a reminder went out a second early"

    clock.advance(1)
    dispatcher.pass_once()
    assert [c["id"] for c in recorder.calls][1:] == [wakes.wake_id("worker", "messages:1:r1")]


def test_the_second_reminder_waits_twenty_minutes_and_there_is_never_a_third(tmp_path: Path, clock) -> None:
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder())
    clock.advance(FIVE_MINUTES)
    dispatcher.pass_once()
    clock.advance(TWENTY_MINUTES - FIVE_MINUTES - 1)
    dispatcher.pass_once()
    assert len(recorder.calls) == 2, "the second reminder went out early"

    clock.advance(1)
    dispatcher.pass_once()
    assert [c["id"] for c in recorder.calls][1:] == [wakes.wake_id("worker", "messages:1:r1"),
                                                     wakes.wake_id("worker", "messages:1:r2")]

    for _ in range(3):
        clock.advance(TWENTY_MINUTES)
        dispatcher.pass_once()
    assert len(recorder.calls) == 3, "a third reminder exists"


def test_reminders_need_no_new_message_and_repeated_passes_do_not_repeat_them(tmp_path: Path, clock) -> None:
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder())
    clock.advance(FIVE_MINUTES)
    for _ in range(5):
        dispatcher.pass_once()          # no new mail arrives at any point
    assert len(recorder.calls) == 2, "one due reminder was sent more than once"


def test_a_later_message_in_the_same_batch_does_not_restart_the_clock(tmp_path: Path, clock) -> None:
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder())
    clock.advance(FIVE_MINUTES - 60)
    messages.send(conn, sender="planner", recipient="worker", text="joins the same unread batch")
    dispatcher.pass_once()
    assert len(recorder.calls) == 1, "a new message in the batch sent a wake of its own"

    clock.advance(60)
    dispatcher.pass_once()
    assert len(recorder.calls) == 2, "the reminder was pushed back by the later message"


# ---------------------------------------------------------------- the read

def test_a_read_before_the_first_due_time_cancels_both_reminders(tmp_path: Path, clock) -> None:
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder())
    messages.read(conn, "worker")
    clock.advance(TWENTY_MINUTES * 2)
    dispatcher.pass_once()
    assert len(recorder.calls) == 1, "a reminder went out after the role had read"
    # Both reminders must exist and be cancelled. Asserting only that nothing was sent would pass
    # just as well if reminders had never been built at all.
    assert rows(conn) == {"messages:1": "delivered",
                          "messages:1:r1": "superseded",
                          "messages:1:r2": "superseded"}


def test_a_read_between_the_reminders_cancels_the_second(tmp_path: Path, clock) -> None:
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder())
    clock.advance(FIVE_MINUTES)
    dispatcher.pass_once()
    assert len(recorder.calls) == 2

    messages.read(conn, "worker")
    clock.advance(TWENTY_MINUTES)
    dispatcher.pass_once()
    assert len(recorder.calls) == 2, "the second reminder went out after the read"
    assert rows(conn)["messages:1:r2"] == "superseded"
    assert rows(conn)["messages:1:r1"] == "delivered", "a delivered reminder was rewritten by the read"


def test_a_partial_page_counts_as_a_read(tmp_path: Path, clock) -> None:
    host = make_config(tmp_path, extra=WORKER_WAKE)
    recorder = Recorder()
    dispatcher = wakes.Dispatcher(host.config, recorder)
    conn = connect(host.config.db_path)
    for text in ("a", "b", "c"):
        messages.send(conn, sender="planner", recipient="worker", text=text)
    dispatcher.pass_once()

    page = messages.read(conn, "worker", limit=1)
    assert len(page["messages"]) == 1, "this case needs a page that leaves messages unread"
    clock.advance(TWENTY_MINUTES * 2)
    dispatcher.pass_once()
    assert len(recorder.calls) == 1, "a partial page did not cancel the batch's reminders"
    assert [status for key, status in rows(conn).items() if key.endswith((":r1", ":r2"))] \
        == ["superseded", "superseded"], "there were no reminders for the partial page to cancel"


def test_reading_the_history_does_not_cancel_reminders(tmp_path: Path, clock) -> None:
    """`read_history` is explicitly bookmark-neutral, so it must not look like a read here."""
    host = make_config(tmp_path, extra=WORKER_WAKE)
    recorder = Recorder()
    dispatcher = wakes.Dispatcher(host.config, recorder)
    conn = connect(host.config.db_path)
    messages.send(conn, sender="planner", recipient="worker", text="still unread")
    dispatcher.pass_once()

    before = messages.read_seq(conn, "worker")
    clock.advance(FIVE_MINUTES)
    dispatcher.pass_once()
    assert messages.read_seq(conn, "worker") == before
    assert len(recorder.calls) == 2, "the reminder did not fire for a batch nobody has read"


# ------------------------------------------------- restart, overlap, identity

def test_when_both_are_overdue_only_the_second_goes_out(tmp_path: Path, clock) -> None:
    """The host was down past both deadlines; catching up must not send two wakes at once."""
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder())
    clock.advance(TWENTY_MINUTES + 1)
    dispatcher.pass_once()
    assert [c["id"] for c in recorder.calls][1:] == [wakes.wake_id("worker", "messages:1:r2")]
    assert rows(conn)["messages:1:r1"] == "skipped"


def test_a_delivered_first_reminder_is_never_rewritten_as_skipped(tmp_path: Path, clock) -> None:
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder())
    clock.advance(FIVE_MINUTES)
    dispatcher.pass_once()
    assert rows(conn)["messages:1:r1"] == "delivered"

    clock.advance(TWENTY_MINUTES)
    dispatcher.pass_once()
    assert rows(conn)["messages:1:r1"] == "delivered", "delivered history was rewritten"
    assert rows(conn)["messages:1:r2"] == "delivered"


def test_the_second_reminder_supersedes_a_first_that_is_still_retrying(tmp_path: Path, clock,
                                                                       monkeypatch) -> None:
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 0.0)
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder(True, False, False))
    clock.advance(FIVE_MINUTES)
    dispatcher.pass_once()                      # r1 is claimed and fails, so it stays pending
    assert rows(conn)["messages:1:r1"] == "pending"

    clock.advance(TWENTY_MINUTES - FIVE_MINUTES)
    dispatcher.pass_once()
    assert rows(conn)["messages:1:r1"] == "superseded", "the first reminder kept its retry chain"
    assert rows(conn)["messages:1:r2"] in {"pending", "delivered"}


def test_an_id_is_stable_across_retries_and_distinct_across_ordinals(tmp_path: Path, clock,
                                                                     monkeypatch) -> None:
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 0.0)
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder(True, False, True))
    clock.advance(FIVE_MINUTES)
    dispatcher.pass_once()
    dispatcher.pass_once()
    first = [c["id"] for c in recorder.calls[1:]]
    assert len(first) == 2 and len(set(first)) == 1, "a retry changed the id"

    clock.advance(TWENTY_MINUTES)
    dispatcher.pass_once()
    assert recorder.calls[-1]["id"] != first[0], "both ordinals share one id"
    assert recorder.calls[-1]["id"] == wakes.wake_id("worker", "messages:1:r2")


# ------------------------------------------------------------------ the cap

def test_the_cap_defers_rather_than_drops_and_frees_when_the_window_moves(tmp_path: Path, clock,
                                                                          monkeypatch) -> None:
    setcap(monkeypatch, 1)
    host = make_config(tmp_path, extra=WORKER_WAKE)
    recorder = Recorder()
    dispatcher = wakes.Dispatcher(host.config, recorder)
    conn = connect(host.config.db_path)
    messages.send(conn, sender="planner", recipient="worker", text="one")
    dispatcher.pass_once()
    clock.advance(FIVE_MINUTES)
    dispatcher.pass_once()                       # r1 charges the only slot

    clock.advance(TWENTY_MINUTES - FIVE_MINUTES)
    dispatcher.pass_once()
    assert rows(conn)["messages:1:r2"] == "capped", "over the cap, a reminder was not deferred"
    assert len(recorder.calls) == 2

    clock.advance(wakes.REMINDER_WINDOW_SECONDS)
    dispatcher.pass_once()
    assert rows(conn)["messages:1:r2"] == "delivered", "a deferred reminder never came back"
    assert len(recorder.calls) == 3


def test_a_read_cancels_a_capped_reminder(tmp_path: Path, clock, monkeypatch) -> None:
    setcap(monkeypatch, 1)
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder())
    clock.advance(FIVE_MINUTES)
    dispatcher.pass_once()
    clock.advance(TWENTY_MINUTES - FIVE_MINUTES)
    dispatcher.pass_once()
    assert rows(conn)["messages:1:r2"] == "capped"

    messages.read(conn, "worker")
    clock.advance(wakes.REMINDER_WINDOW_SECONDS)
    dispatcher.pass_once()
    assert rows(conn)["messages:1:r2"] == "superseded", "a read left a capped reminder to fire later"
    assert len(recorder.calls) == 2


def test_the_cap_counts_logical_reminders_not_attempts(tmp_path: Path, clock, monkeypatch) -> None:
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 0.0)
    setcap(monkeypatch, 1)
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder(True, False, False, True))
    clock.advance(FIVE_MINUTES)
    for _ in range(3):
        dispatcher.pass_once()                   # r1 fails twice then succeeds: three attempts, one reminder
    assert rows(conn)["messages:1:r1"] == "delivered"

    clock.advance(TWENTY_MINUTES - FIVE_MINUTES)
    dispatcher.pass_once()
    assert rows(conn)["messages:1:r2"] == "capped", "retries were charged against the cap"


def test_original_message_and_job_wakes_never_charge_the_cap(tmp_path: Path, clock, monkeypatch) -> None:
    setcap(monkeypatch, 1)
    host = make_config(tmp_path, extra=WORKER_WAKE)
    recorder = Recorder()
    dispatcher = wakes.Dispatcher(host.config, recorder)
    conn = connect(host.config.db_path)
    for text in ("one", "two", "three"):
        messages.send(conn, sender="planner", recipient="worker", text=text)
        dispatcher.pass_once()
        messages.read(conn, "worker")            # each becomes its own delivered batch
    assert len(recorder.calls) == 3

    messages.send(conn, sender="planner", recipient="worker", text="unread")
    dispatcher.pass_once()
    clock.advance(FIVE_MINUTES)
    dispatcher.pass_once()
    assert rows(conn)["messages:4:r1"] == "delivered", "original wakes used up the reminder cap"


# --------------------------------------------------- when there is nothing to remind

def test_no_reminders_when_the_original_wake_was_never_delivered(tmp_path: Path, clock,
                                                                 monkeypatch) -> None:
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 0.0)
    monkeypatch.setattr(wakes, "MAX_ATTEMPTS", 1)
    host = make_config(tmp_path, extra=WORKER_WAKE)
    recorder = Recorder(False)
    dispatcher = wakes.Dispatcher(host.config, recorder)
    conn = connect(host.config.db_path)
    messages.send(conn, sender="planner", recipient="worker", text="never got through")
    dispatcher.pass_once()                       # the one allowed attempt, which fails
    dispatcher.pass_once()                       # the next pass is where it gives up
    assert rows(conn)["messages:1"] == "failed"

    clock.advance(TWENTY_MINUTES * 2)
    dispatcher.pass_once()
    assert not [k for k in rows(conn) if k.endswith(("r1", "r2"))], "an undelivered wake scheduled reminders"


def test_no_reminders_for_a_role_with_no_hook(tmp_path: Path, clock) -> None:
    host = make_config(tmp_path)                 # worker has no [roles.worker.wake]
    recorder = Recorder()
    dispatcher = wakes.Dispatcher(host.config, recorder)
    conn = connect(host.config.db_path)
    messages.send(conn, sender="planner", recipient="worker", text="nobody to tell")
    dispatcher.pass_once()
    clock.advance(TWENTY_MINUTES * 2)
    dispatcher.pass_once()
    assert recorder.calls == []
    assert rows(conn) == {}


# ------------------------------------------------------------- the payload

def test_a_reminder_carries_no_more_than_the_original(tmp_path: Path, clock) -> None:
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder())
    clock.advance(FIVE_MINUTES)
    dispatcher.pass_once()
    original, reminder = recorder.calls[0], recorder.calls[1]
    assert set(reminder) == set(original), "a reminder added a payload field"
    assert set(reminder) == {"id", "type", "role", "host", "reason", "first_unread_seq"}
    assert reminder["first_unread_seq"] == original["first_unread_seq"]
    assert "text" not in json.dumps(reminder)


# ------------------------------------------------- one worker, one send, one charge

def test_a_second_worker_does_not_send_the_same_reminder_again(tmp_path: Path, clock) -> None:
    """Two dispatchers over one database: the claim is exclusive, so only one of them sends.

    They run one after another rather than truly at once, so this pins that a worker meeting a row
    another has already claimed stands down. It does not prove anything about simultaneous writers;
    SQLite's own write locking is what carries that, and a race here would be flaky to assert.
    """
    host, first, conn = woken_batch(tmp_path, recorder := Recorder())
    second = wakes.Dispatcher(host.config, recorder)
    clock.advance(FIVE_MINUTES)
    first.pass_once()
    second.pass_once()
    assert [c["id"] for c in recorder.calls][1:] == [wakes.wake_id("worker", "messages:1:r1")]


def test_a_restart_does_not_charge_the_cap_twice(tmp_path: Path, clock, monkeypatch) -> None:
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 0.0)
    setcap(monkeypatch, 2)
    host, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder(True, False))
    clock.advance(FIVE_MINUTES)
    dispatcher.pass_once()                       # r1 claimed, charged once, and failed
    assert rows(conn)["messages:1:r1"] == "pending"

    restarted = wakes.Dispatcher(host.config, recorder)   # a fresh process over the same database
    restarted.pass_once()
    clock.advance(TWENTY_MINUTES - FIVE_MINUTES)
    restarted.pass_once()
    assert rows(conn)["messages:1:r2"] != "capped", "the restart charged the cap a second time"


def test_a_read_cancels_a_reminder_that_is_already_leased(tmp_path: Path, clock, monkeypatch) -> None:
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 600.0)
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder(True, False))
    clock.advance(FIVE_MINUTES)
    dispatcher.pass_once()                       # r1 holds a live lease after failing
    assert rows(conn)["messages:1:r1"] == "pending"

    messages.read(conn, "worker")
    clock.advance(TWENTY_MINUTES)
    dispatcher.pass_once()
    assert rows(conn)["messages:1:r1"] == "superseded", "a read left leased work to retry"
    assert len(recorder.calls) == 2


# ----------------------------------------------------- the original wakes keep their cadence

def test_the_dispatcher_still_polls_every_second_and_sends_originals_in_the_same_pass(
        tmp_path: Path, clock) -> None:
    """A due scan on every pass must not slow the original wakes or defer them to a later pass."""
    import inspect

    assert inspect.signature(wakes.Dispatcher.run).parameters["poll_seconds"].default == 1.0

    host = make_config(tmp_path, extra=WORKER_WAKE)
    recorder = Recorder()
    dispatcher = wakes.Dispatcher(host.config, recorder)
    conn = connect(host.config.db_path)
    messages.send(conn, sender="planner", recipient="worker", text="first batch")
    dispatcher.pass_once()
    messages.read(conn, "worker")
    clock.advance(FIVE_MINUTES)                  # a reminder would be due had it not been read
    messages.send(conn, sender="planner", recipient="worker", text="second batch")
    assert dispatcher.pass_once() >= 1
    assert [c["first_unread_seq"] for c in recorder.calls] == [1, 2], "an original wake waited a pass"


# ------------------------------------------------------------------ migration

def test_upgrading_a_version_six_database_keeps_every_wake_row(tmp_path: Path) -> None:
    """The step that makes room for reminders is additive: existing wakes must come through untouched."""
    from baton.store import SCHEMA_VERSION

    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    raw = sqlite3.connect(data / "baton.db")
    raw.executescript((Path(__file__).parent / "fixtures" / "schema_v6.sql").read_text())
    before = [("worker", "messages:1", '{"type":"baton.wake"}', "delivered", 0.0, 1, "t0", "t1"),
              ("worker", "messages:9", '{"type":"baton.wake"}', "pending", 12.5, 3, "t2", None),
              ("planner", "job:j:1", '{"type":"baton.wake"}', "failed", 0.0, 8, "t3", "t4")]
    raw.executemany("INSERT INTO wakes (role, key, payload, status, lease_until, attempts, created_at,"
                    " finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", before)
    raw.commit()
    raw.close()
    (data / "baton.db").chmod(0o600)             # the host refuses a data directory it can share

    conn = connect(data / "baton.db")
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    after = conn.execute("SELECT role, key, payload, status, lease_until, attempts, created_at,"
                         " finished_at FROM wakes ORDER BY role, key").fetchall()
    assert [tuple(row) for row in after] == sorted(before), "the upgrade changed an existing wake"
    added = conn.execute("SELECT due_at, baseline_seq, ordinal, claimed_at FROM wakes").fetchall()
    assert all(all(value is None for value in row) for row in added), \
        "the upgrade gave an existing wake a reminder's columns"
    conn.close()


# ------------------------------- the guards that only a direct call can reach

def claim(dispatcher, conn, role: str, key: str, ordinal: int, baseline: int) -> bool:
    """One claim attempt, called directly: these invariants sit between a scan and a send."""
    return dispatcher._claim_reminder(conn, role, key, ordinal, baseline)


def test_a_claim_is_refused_when_the_role_has_already_read_past_the_baseline(tmp_path: Path,
                                                                            clock) -> None:
    """The claim rechecks the read position rather than trusting the row it was handed.

    A read normally retires the reminders itself, so this is the second line: a row left eligible
    while the position has moved on -- a read landing between the scan and the claim, or a crash
    between the two statements of a read -- must still be refused here.
    """
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder())
    messages.read(conn, "worker")
    with conn:                                   # put the row back as a crashed read would leave it
        conn.execute("UPDATE wakes SET status = 'scheduled', finished_at = NULL"
                     " WHERE key = 'messages:1:r1'")
    assert rows(conn)["messages:1:r1"] == "scheduled"

    assert claim(dispatcher, conn, "worker", "messages:1:r1", 1, 0) is False
    assert rows(conn)["messages:1:r1"] == "superseded", "the stale row was left eligible"
    assert len(recorder.calls) == 1


def test_only_one_claimant_wins_the_same_due_reminder(tmp_path: Path, clock) -> None:
    """Two workers reaching the same row: the second must find it taken and stand down."""
    _, dispatcher, conn = woken_batch(tmp_path, Recorder())
    clock.advance(FIVE_MINUTES)
    assert claim(dispatcher, conn, "worker", "messages:1:r1", 1, 0) is True
    assert claim(dispatcher, conn, "worker", "messages:1:r1", 1, 0) is False, \
        "a second claim on a reminder already taken succeeded"


def test_a_second_claim_keeps_the_first_claim_time_so_the_cap_is_charged_once(tmp_path: Path,
                                                                             clock) -> None:
    """The cap is charged by the first claim. Anything that requeues a charged reminder later must
    not charge it again, so the claim keeps whatever time is already recorded."""
    _, dispatcher, conn = woken_batch(tmp_path, Recorder())
    clock.advance(FIVE_MINUTES)
    assert claim(dispatcher, conn, "worker", "messages:1:r1", 1, 0) is True
    charged_at = conn.execute("SELECT claimed_at FROM wakes WHERE key = 'messages:1:r1'").fetchone()[0]
    assert charged_at is not None

    with conn:                                   # a path that puts charged work back in the queue
        conn.execute("UPDATE wakes SET status = 'scheduled' WHERE key = 'messages:1:r1'")
    clock.advance(TWENTY_MINUTES)
    assert claim(dispatcher, conn, "worker", "messages:1:r1", 1, 0) is True
    again = conn.execute("SELECT claimed_at FROM wakes WHERE key = 'messages:1:r1'").fetchone()[0]
    assert again == charged_at, "reclaiming moved the charge, so the cap window would restart"


# ------------------------------------------- races a green suite does not reach

BOTH_WAKE = WORKER_WAKE + '[roles.planner.wake]\ncommand = ["true"]\n'


def test_a_first_reminder_is_not_retried_once_the_second_is_due(tmp_path: Path, clock,
                                                                monkeypatch) -> None:
    """Once the later ordinal is due, the earlier one is ineligible for any further send.

    Retries of still-pending work run before the due scan in the same pass, so a first reminder
    whose lease had run out could be sent again at the very moment the second became due -- after
    the point the design says it stops.
    """
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 0.0)
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder(True, False))
    first = wakes.wake_id("worker", "messages:1:r1")
    clock.advance(FIVE_MINUTES)
    dispatcher.pass_once()
    assert [c["id"] for c in recorder.calls][1:] == [first], "the first reminder was not sent and left pending"

    clock.advance(TWENTY_MINUTES - FIVE_MINUTES)
    dispatcher.pass_once()
    after = [c["id"] for c in recorder.calls][2:]
    assert first not in after, "the first reminder was retried after the second fell due"
    assert after == [wakes.wake_id("worker", "messages:1:r2")]


def test_a_live_lease_stops_a_second_worker_sending_the_same_reminder(tmp_path: Path, clock,
                                                                      monkeypatch) -> None:
    """The lease has to be taken by the send, not only written after it.

    Two workers that both saw the row as pending before either wrote would otherwise both send:
    the attempt checks the status but never the lease, so a live lease stops nobody.
    """
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 600.0)
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder(True, False))
    clock.advance(FIVE_MINUTES)
    dispatcher.pass_once()                       # claimed, sent, failed: the lease is now live
    assert rows(conn)["messages:1:r1"] == "pending"
    sent = len(recorder.calls)

    dispatcher._attempt(conn, "worker", "messages:1:r1")   # a second worker, stale snapshot in hand
    assert len(recorder.calls) == sent, "a second send went out inside a live lease"


def test_the_latest_due_ordinal_is_chosen_before_the_page_limit(tmp_path: Path, clock,
                                                                monkeypatch) -> None:
    """Grouping by batch has to happen before the page limit, not after.

    With more due rows than one page holds, a batch can have its first reminder inside the page and
    its second outside it. Grouping afterwards then picks the first as that batch's latest and sends
    it, although the second is already due. Reproduced here with a small page rather than the
    hundred-row default and fifty-one batches: it is the same arithmetic, and far cheaper to run.
    """
    monkeypatch.setattr(wakes, "PAGE", 3)
    host = make_config(tmp_path, extra=BOTH_WAKE)
    recorder = Recorder()
    dispatcher = wakes.Dispatcher(host.config, recorder)
    conn = connect(host.config.db_path)
    messages.send(conn, sender="planner", recipient="worker", text="for the worker")
    messages.send(conn, sender="worker", recipient="planner", text="for the planner")
    dispatcher.pass_once()
    assert len(recorder.calls) == 2, "both roles need a delivered wake for this to page"

    clock.advance(TWENTY_MINUTES + 1)            # four due reminders, one page of three
    dispatcher.pass_once()
    sent = [c["id"] for c in recorder.calls][2:]
    for role, seq in (("worker", 1), ("planner", 1)):
        assert wakes.wake_id(role, f"messages:{seq}:r1") not in sent, \
            f"{role}'s first reminder went out although its second was due"


def test_a_reminder_for_a_role_that_lost_its_hook_ends_without_dispatching(tmp_path: Path, clock) -> None:
    """A wake the config no longer describes has nowhere to go, and must not be attempted.

    The hook can be taken away between the original delivery and the reminder's due time. Sending
    then fails inside the deliverer rather than over the network -- an empty command raises on the
    way out, taking an attempt and a cap slot with it and ending the whole pass.
    """
    host = make_config(tmp_path, extra=WORKER_WAKE)
    recorder = Recorder()
    conn = connect(host.config.db_path)
    messages.send(conn, sender="planner", recipient="worker", text="woken while it had a hook")
    wakes.Dispatcher(host.config, recorder).pass_once()
    assert len(recorder.calls) == 1

    (tmp_path / "second").mkdir()
    without = make_config(tmp_path / "second", data_dir=host.config.data_dir)   # the hook is gone
    assert not without.config.roles["worker"].wake.enabled
    clock.advance(FIVE_MINUTES)
    wakes.Dispatcher(without.config, recorder).pass_once()                      # must not raise

    assert len(recorder.calls) == 1, "a reminder was dispatched for a role with no wake"
    assert rows(conn)["messages:1:r1"] not in {"pending", "delivered"}, \
        "the reminder was claimed although it had nowhere to go"
    charged = conn.execute("SELECT COUNT(*) FROM wakes WHERE claimed_at IS NOT NULL").fetchone()[0]
    assert charged == 0, "a reminder with no target still took a cap slot"


def test_a_read_during_the_original_delivery_leaves_no_reminders(tmp_path: Path, clock) -> None:
    """The original's callback is made outside the transaction, so a read can land mid-flight.

    The read supersedes the original wake. Scheduling afterwards anyway would leave two reminders
    for a batch that has already been read -- stopped later by the eligibility checks, but recreated
    after a read, which the contract says must not happen.
    """
    host = make_config(tmp_path, extra=WORKER_WAKE)
    conn = connect(host.config.db_path)

    class ReadsWhileDelivering(Recorder):
        def __call__(self, wake, wake_id: str, body: bytes) -> bool:
            messages.read(conn, "worker")        # the role reads before the callback returns
            return super().__call__(wake, wake_id, body)

    recorder = ReadsWhileDelivering()
    messages.send(conn, sender="planner", recipient="worker", text="read while the wake was in flight")
    wakes.Dispatcher(host.config, recorder).pass_once()
    assert len(recorder.calls) == 1

    assert [key for key in rows(conn) if key.endswith((":r1", ":r2"))] == [], \
        "a read during the original delivery still left reminders scheduled"


def test_the_second_retires_a_first_whose_lease_is_still_live(tmp_path: Path, clock,
                                                              monkeypatch) -> None:
    """A first reminder holding a live lease is retired by the second's claim, not by a retry.

    With its lease expired it is the retry path that stops it, which is the other test. While the
    lease is still live nothing retries it, so the claim of the later ordinal is the only thing that
    can end its chain.
    """
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 1800.0)
    _, dispatcher, conn = woken_batch(tmp_path, recorder := Recorder(True, False))
    clock.advance(FIVE_MINUTES)
    dispatcher.pass_once()                       # r1 sent, failed, lease live for another 25 minutes
    assert rows(conn)["messages:1:r1"] == "pending"

    clock.advance(TWENTY_MINUTES - FIVE_MINUTES)
    leased_until = conn.execute("SELECT lease_until FROM wakes WHERE key = 'messages:1:r1'").fetchone()[0]
    assert leased_until > wakes._now(), "this case needs the lease still live when the second falls due"

    dispatcher.pass_once()
    assert rows(conn)["messages:1:r1"] == "superseded", "a leased first reminder kept its chain"
    assert [c["id"] for c in recorder.calls][2:] == [wakes.wake_id("worker", "messages:1:r2")]


def test_a_pending_reminder_is_not_retried_for_a_role_that_lost_its_hook(tmp_path: Path,
                                                                         clock) -> None:
    """Losing the hook has to stop a reminder already retrying, not only one still waiting.

    The claim refuses a reminder that is `scheduled` or `capped`, which covers one whose turn has
    not come. A reminder that was claimed, sent, and failed sits at `pending` and comes back through
    the retry scan instead, which asks nothing about the config -- so it reaches the deliverer with
    an empty command and raises on the way out, ending the pass.
    """
    host = make_config(tmp_path, extra=WORKER_WAKE)
    recorder = Recorder(True, False)             # the original lands; the first reminder fails
    conn = connect(host.config.db_path)
    messages.send(conn, sender="planner", recipient="worker", text="woken while it had a hook")
    dispatcher = wakes.Dispatcher(host.config, recorder)
    dispatcher.pass_once()
    clock.advance(FIVE_MINUTES)
    dispatcher.pass_once()
    assert rows(conn)["messages:1:r1"] == "pending", "this case needs a reminder left retrying"
    sent = len(recorder.calls)

    (tmp_path / "second").mkdir()
    without = make_config(tmp_path / "second", data_dir=host.config.data_dir)   # the hook is gone
    assert not without.config.roles["worker"].wake.enabled
    clock.advance(wakes.LEASE_SECONDS + 1)       # past the backoff, well short of the second's turn
    wakes.Dispatcher(without.config, recorder).pass_once()                      # must not raise

    assert len(recorder.calls) == sent, "a pending reminder was retried for a role with no wake"
    assert rows(conn)["messages:1:r1"] == "skipped", \
        "the reminder stayed eligible although it had nowhere to go"


def test_a_stale_worker_does_not_fail_an_attempt_that_is_still_in_flight(tmp_path: Path, clock,
                                                                          monkeypatch) -> None:
    """Giving up has to be gated on the lease, the same way sending is.

    The attempt count is read and compared before the lease is looked at, so a worker holding a
    stale snapshot can mark the row failed while the last allowed attempt is still in flight. The
    send's callback is made outside the transaction, so it then reports success against a row that
    is no longer pending: a wake that was delivered is recorded as failed and loses its reminders.
    """
    monkeypatch.setattr(wakes, "MAX_ATTEMPTS", 2)
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 1.0)
    host = make_config(tmp_path, extra=WORKER_WAKE)
    conn = connect(host.config.db_path)
    first, stale = [False], []

    def deliver(wake: Wake, wake_id: str, body: bytes) -> bool:
        if first:
            return first.pop()                   # the first attempt fails, leaving one allowed
        stale.append(dispatcher._attempt(conn, "worker", "messages:1"))
        return True

    dispatcher = wakes.Dispatcher(host.config, deliver)
    messages.send(conn, sender="planner", recipient="worker", text="the one it must read")
    dispatcher.pass_once()
    assert rows(conn)["messages:1"] == "pending", "this case needs one failed attempt first"

    clock.advance(wakes.LEASE_SECONDS + 1)
    dispatcher.pass_once()                       # the last allowed attempt, succeeding

    assert stale == [0], "the stale worker took work that was already in flight"
    assert rows(conn)["messages:1"] == "delivered", \
        "a stale worker recorded a delivered wake as failed"
    assert sorted(k for k in rows(conn) if k.startswith("messages:1:r")) \
        == ["messages:1:r1", "messages:1:r2"], "the delivered wake lost its reminders"
