"""Wakes: tell a role that something is waiting for it, through its configured webhook or command.

A wake carries no message text or job body, only what to look at:
`{"role", "reason", "job_id"?, ...}`. The woken agent then reads with its own
credentials, so a duplicated, late or forged wake is harmless.

Messages coalesce: a role is woken once per unread batch, keyed by its first
unread seq, and a read supersedes a pending wake. A role configured with
`only_when_addressed` is woken only by a message that singles it out, and
then still from its first unread seq, so one wake covers the quiet ones
already waiting. Every wake is recorded
before delivery and retried with backoff until it succeeds, is superseded, or
runs out of attempts.

Because one wake covers a whole batch, a role whose read fails after a
delivered wake would otherwise hear nothing more until it happened to read on
its own. So a delivered message wake schedules two reminders for the same
batch, at `REMINDER_OFFSETS` after the moment it was durably delivered. Each
is an ordinary wake with the batch's key and `:r1` or `:r2`, so it retries and
is recorded like any other, under an id that is stable across its own attempts
and distinct from the other ordinal's. Delivery stays at-least-once: a receiver
dedupes by id.

A reminder goes out only while the role has not read past the position the
batch had when its original wake was delivered. A read retires every reminder
for that batch that has not already been sent, in the same transaction, and
they are never recreated. Reminders are bounded twice over: never more than two
per batch, and never more than `REMINDER_CAP` logical reminders for one role in
`REMINDER_WINDOW_SECONDS`. Over that cap a reminder waits rather than
disappearing, and comes back when the window moves. The later ordinal retires
the earlier one, so a host that was down past both deadlines sends one wake and
not two; an attempt already in flight may still arrive, which is harmless
because a wake carries nothing.

Webhooks are signed with the Standard Webhooks scheme when the role names a
secret: HMAC-SHA256 over `id.timestamp.body`, in the `webhook-signature`
header. The URL comes from the operator's config, never from a client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from baton import events, messages
from baton.config import Config, Wake
from baton.store import connect, transaction

log = logging.getLogger("baton.wakes")

CURSOR = "wake-dispatcher"
PAGE = 100
MAX_ATTEMPTS = 8
# A delivered message wake that is never read is tried again at these offsets from its durable
# delivery, and then never again. They are recovery attempts after a failed read, not a latency
# guarantee: a due time is when a reminder becomes eligible, not when it is promised to arrive.
REMINDER_OFFSETS = (300.0, 1200.0)
# At most this many logical reminders per role in a rolling window. Attempts are not counted, and
# original message and job wakes never charge it. Over the cap a reminder waits rather than vanishing.
REMINDER_CAP = 6
REMINDER_WINDOW_SECONDS = 3600.0
LEASE_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 3600.0

# (wake config, wake id, JSON body) -> delivered?
Deliver = Callable[[Wake, str, bytes], bool]


def _now() -> float:
    """The clock every lease and due time is measured by, in one place so a test can move it."""
    return time.time()


def sign(secret: str, message_id: str, timestamp: int, body: bytes) -> str:
    """Standard Webhooks v1 signature; a `whsec_` secret is base64, anything else is used as raw bytes."""
    key = base64.b64decode(secret.removeprefix("whsec_")) if secret.startswith("whsec_") else secret.encode()
    digest = hmac.new(key, f"{message_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def deliver(wake: Wake, wake_id: str, body: bytes) -> bool:
    """Deliver one wake by its configured channel. Failures are logged without the body or any secret."""
    try:
        if wake.webhook:
            return _post(wake, wake_id, body)
        completed = subprocess.run(list(wake.command), input=body, capture_output=True,
                                   timeout=wake.timeout_seconds, check=False)
        if completed.returncode != 0:
            log.warning("wake command for %s exited %d", wake_id, completed.returncode)
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError, httpx.HTTPError) as error:
        log.warning("wake %s failed: %s", wake_id, type(error).__name__)
        return False


def _post(wake: Wake, wake_id: str, body: bytes) -> bool:
    headers = {"Content-Type": "application/json", "webhook-id": wake_id}
    if wake.secret_env:
        secret = os.environ.get(wake.secret_env)
        if not secret:
            log.warning("wake %s not sent: $%s is unset", wake_id, wake.secret_env)
            return False
        timestamp = int(time.time())
        headers |= {"webhook-timestamp": str(timestamp), "webhook-signature": sign(secret, wake_id, timestamp, body)}
    with httpx.Client(follow_redirects=False, timeout=wake.timeout_seconds) as client:
        response = client.post(wake.webhook or "", content=body, headers=headers)
    if not response.is_success:
        log.warning("wake %s: webhook answered %d", wake_id, response.status_code)
    return response.is_success


class Dispatcher:
    """Turns events into wakes and delivers them. One runs beside the MCP server, on its own thread."""

    def __init__(self, config: Config, deliver: Deliver = deliver,
                 integrations: dict[str, Any] | None = None) -> None:
        self._config = config
        self._deliver = deliver
        # Roles whose wake names an integration hand it the last step; see baton.integrations.
        self._integrations = integrations or {}

    def run(self, stop: threading.Event, poll_seconds: float = 1.0) -> None:
        while not stop.is_set():
            try:
                self.pass_once()
            except Exception:
                log.exception("wake dispatch pass failed")
            stop.wait(poll_seconds)

    def pass_once(self) -> int:
        """Retry wakes whose lease ran out, then wake for every new event; return how many deliveries were tried."""
        conn = connect(self._config.db_path)
        try:
            messages.sweep(conn)
            tried = 0
            for row in conn.execute("SELECT role, key FROM wakes WHERE status = 'pending' AND lease_until <= ?"
                                    " ORDER BY lease_until LIMIT ?", (_now(), PAGE)).fetchall():
                tried += self._attempt(conn, row["role"], row["key"])
            tried += self._due_reminders(conn)
            while rows := conn.execute("SELECT * FROM events WHERE pos > ? ORDER BY pos LIMIT ?",
                                       (events.cursor(conn, CURSOR), PAGE)).fetchall():
                for event in rows:
                    claimed = self._claim(conn, event)
                    if claimed is not None:
                        tried += self._attempt(conn, *claimed)
                    with transaction(conn):
                        events.set_cursor(conn, CURSOR, event["pos"])
            return tried
        finally:
            conn.close()

    def _claim(self, conn: sqlite3.Connection, event: sqlite3.Row) -> tuple[str, str] | None:
        """Record the wake an event calls for, if its audience has a hook; None if there is nothing new to send."""
        role = self._config.roles.get(event["audience"] or "")
        if role is None or not role.wake.enabled:
            return None
        if event["kind"] == "message":
            seq = json.loads(event["data"])["seq"]
            if role.wake.only_when_addressed and not messages.addressed_to(conn, role.name, seq):
                return None  # it is in the role's mailbox, waiting; it just does not interrupt it
            first_unread = messages.first_unread_seq(conn, role.name)
            if first_unread is None:
                return None
            key, payload = f"messages:{first_unread}", {"reason": "message", "first_unread_seq": first_unread}
        elif event["job_id"] is None:  # a conversation over its hourly limit, the one event outside any job
            key = f"event:{event['pos']}"
            payload = {"reason": event["kind"], "conversation_id": json.loads(event["data"])["conversation_id"]}
        else:
            key, payload = (f"job:{event['job_id']}:{event['seq']}",
                            {"reason": event["kind"], "job_id": event["job_id"], "event_seq": event["seq"]})
        body = {"type": "baton.wake", "role": role.name, "host": self._config.public_url, **payload}
        with transaction(conn):
            inserted = conn.execute(
                "INSERT INTO wakes (role, key, payload, status, lease_until, attempts, created_at)"
                " VALUES (?, ?, ?, 'pending', 0, 0, ?) ON CONFLICT (role, key) DO NOTHING",
                (role.name, key, json.dumps(body, separators=(",", ":")), events.now()),
            ).rowcount
        return (role.name, key) if inserted else None

    def _attempt(self, conn: sqlite3.Connection, role: str, key: str) -> int:
        with transaction(conn):
            row = conn.execute("SELECT * FROM wakes WHERE role = ? AND key = ?", (role, key)).fetchone()
            if row is None or row["status"] != "pending":
                return 0
            if row["ordinal"] is not None and self._reminder_is_spent(conn, row):
                return 0
            configured = self._config.roles.get(role)
            if configured is None or not configured.wake.enabled:
                # The hook can be taken away while an attempt is still waiting to be retried. The
                # claim refuses one that has not gone yet, but a retry arrives here instead, and
                # there is still nothing to send to: a deliverer handed an empty target raises on
                # the way out and ends the pass.
                conn.execute("UPDATE wakes SET status = 'skipped', finished_at = ? WHERE role = ?"
                             " AND key = ? AND status = 'pending'", (events.now(), role, key))
                log.info("wake %s %s skipped: the role has no wake to send to", role, key)
                return 0
            if row["attempts"] >= MAX_ATTEMPTS:
                # Giving up is gated on the lease, exactly as sending is. A live lease means the
                # last allowed attempt is still in flight and its callback may yet report success;
                # retiring the row here would record a delivered wake as failed and lose its
                # reminders, because the callback lands on a row that is no longer pending.
                if conn.execute(
                        "UPDATE wakes SET status = 'failed', finished_at = ? WHERE role = ? AND key = ?"
                        " AND status = 'pending' AND lease_until <= ?",
                        (events.now(), role, key, _now())).rowcount:
                    log.error("wake %s %s gave up after %d attempts", role, key, row["attempts"])
                return 0
            # Taking the lease is the claim to send, so it is one conditional update: a worker that
            # lost the race finds the row leased and does nothing. Checking the status and writing
            # the lease separately would let two workers that both read it as pending both send.
            if not conn.execute(
                    "UPDATE wakes SET attempts = attempts + 1, lease_until = ? WHERE role = ? AND key = ?"
                    " AND status = 'pending' AND lease_until <= ?",
                    (_now() + LEASE_SECONDS, role, key, _now())).rowcount:
                return 0
        delivered = self._send(role, wake_id(role, key), row["payload"].encode())
        with transaction(conn):
            if delivered:
                # Only a wake that was still pending becomes delivered, and only that wake gets
                # reminders. The callback is made outside the transaction, so the role can read
                # while it is in flight; that read supersedes the wake, and a batch already read
                # must not come back with reminders scheduled for it.
                landed = conn.execute(
                    "UPDATE wakes SET status = 'delivered', finished_at = ? WHERE role = ? AND key = ?"
                    " AND status = 'pending'", (events.now(), role, key)).rowcount
                if landed:
                    self._schedule_reminders(conn, role, key, row)
            else:
                backoff = min(MAX_BACKOFF_SECONDS, LEASE_SECONDS * 2 ** row["attempts"])
                conn.execute("UPDATE wakes SET lease_until = ? WHERE role = ? AND key = ?",
                             (_now() + backoff, role, key))
        return 1

    def _schedule_reminders(self, conn: sqlite3.Connection, role: str, key: str,
                            row: sqlite3.Row) -> None:
        """Set both reminders for a message batch whose original wake has just been delivered.

        Anchored here, at durable delivery, so a restart needs no reconstruction and a later message
        joining the same batch cannot push the times back. A reminder never schedules its own: only a
        row with no ordinal gets them.
        """
        if row["ordinal"] is not None or not key.startswith("messages:"):
            return
        batch = int(key.removeprefix("messages:"))
        delivered_at = _now()
        for ordinal, offset in enumerate(REMINDER_OFFSETS, start=1):
            conn.execute(
                "INSERT INTO wakes (role, key, payload, status, lease_until, attempts, created_at,"
                " due_at, baseline_seq, ordinal) VALUES (?, ?, ?, 'scheduled', 0, 0, ?, ?, ?, ?)"
                " ON CONFLICT (role, key) DO NOTHING",
                (role, f"{key}:r{ordinal}", row["payload"], events.now(),
                 delivered_at + offset, batch - 1, ordinal))

    def _due_reminders(self, conn: sqlite3.Connection) -> int:
        """Claim and send every reminder that has come due; return how many were tried.

        Runs on the ordinary pass rather than a loop of its own, so the original wakes keep their
        cadence. `capped` rows are revisited here too, which is how a deferred reminder comes back
        without needing new mail.
        """
        tried = 0
        # Only the latest due ordinal of each batch is offered, and that is decided here rather than
        # after the page limit: with more due rows than one page holds, a batch could otherwise have
        # its first reminder inside the page and its second outside, and the first would be sent
        # although the second was already due. A batch is (role, baseline_seq), so no key is parsed.
        due = conn.execute(
            "SELECT role, key, ordinal, baseline_seq FROM wakes AS w"
            " WHERE w.ordinal IS NOT NULL AND w.status IN ('scheduled', 'capped') AND w.due_at <= ?"
            " AND NOT EXISTS (SELECT 1 FROM wakes AS later WHERE later.role = w.role"
            "                 AND later.baseline_seq = w.baseline_seq AND later.ordinal > w.ordinal"
            "                 AND later.status IN ('scheduled', 'capped') AND later.due_at <= ?)"
            # Deferred work sorts last. A `capped` row keeps its original due time, so ordering by
            # due time alone puts it at the front of every pass, where it is offered and refused
            # again; enough of them fill the page and a role whose reminder has just come due with
            # cap to spare is never reached. Work that can be served goes first.
            " ORDER BY w.status = 'capped', w.due_at LIMIT ?", (_now(), _now(), PAGE)).fetchall()
        for row in due:
            if self._claim_reminder(conn, row["role"], row["key"], row["ordinal"], row["baseline_seq"]):
                tried += self._attempt(conn, row["role"], row["key"])
        return tried

    def _reminder_is_spent(self, conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
        """Whether a pending reminder has lost its turn, retiring it if so. Checked before each send.

        Retries run before the due scan in a pass, so without this an earlier ordinal could be sent
        again at the very moment the later one became due -- past the point the design says it stops.
        A read landing since the claim retires it here too.
        """
        role, key = row["role"], row["key"]
        later_is_due = conn.execute(
            "SELECT 1 FROM wakes WHERE role = ? AND baseline_seq = ? AND ordinal > ?"
            " AND status IN ('scheduled', 'capped', 'pending') AND due_at <= ?",
            (role, row["baseline_seq"], row["ordinal"], _now())).fetchone() is not None
        if not later_is_due and messages.read_seq(conn, role) <= row["baseline_seq"]:
            return False
        conn.execute("UPDATE wakes SET status = 'superseded', finished_at = ? WHERE role = ? AND key = ?"
                     " AND status = 'pending'", (events.now(), role, key))
        return True

    def _claim_reminder(self, conn: sqlite3.Connection, role: str, key: str, ordinal: int,
                        baseline: int) -> bool:
        """Take an exclusive claim on one due reminder, charging its cap slot once. True if won.

        Everything that decides whether it may go happens in one transaction: the role must not have
        read past the batch's baseline, the later ordinal retires the earlier one, and the cap is
        counted and reserved here rather than at send time. A second worker meeting the same row
        finds it no longer `scheduled` and stands down.
        """
        batch = key.rsplit(":r", 1)[0]
        with transaction(conn):
            configured = self._config.roles.get(role)
            if configured is None or not configured.wake.enabled:
                # The hook can be taken away after the original was delivered. There is nothing to
                # send to, so the reminder ends here: no attempt, no cap slot, and nothing raised
                # out of the pass by a deliverer handed an empty target.
                conn.execute("UPDATE wakes SET status = 'skipped', finished_at = ? WHERE role = ?"
                             " AND key = ? AND status IN ('scheduled', 'capped')",
                             (events.now(), role, key))
                log.info("reminder %s %s skipped: the role has no wake to send to", role, key)
                return False
            if messages.read_seq(conn, role) > baseline:
                conn.execute("UPDATE wakes SET status = 'superseded', finished_at = ?"
                             " WHERE role = ? AND key = ? AND status IN ('scheduled', 'capped')",
                             (events.now(), role, key))
                return False
            if ordinal == max(range(1, len(REMINDER_OFFSETS) + 1)):
                # The last ordinal is due, so anything earlier has had its turn. Unsent work is
                # skipped and work still retrying is retired, but a delivered record is left alone.
                conn.execute("UPDATE wakes SET status = 'skipped', finished_at = ? WHERE role = ?"
                             " AND key LIKE ? AND key != ? AND status IN ('scheduled', 'capped')",
                             (events.now(), role, f"{batch}:r%", key))
                conn.execute("UPDATE wakes SET status = 'superseded', finished_at = ? WHERE role = ?"
                             " AND key LIKE ? AND key != ? AND status = 'pending'",
                             (events.now(), role, f"{batch}:r%", key))
            charged = conn.execute(
                "SELECT COUNT(*) FROM wakes WHERE role = ? AND ordinal IS NOT NULL"
                " AND claimed_at IS NOT NULL AND claimed_at > ?",
                (role, _now() - REMINDER_WINDOW_SECONDS)).fetchone()[0]
            already = conn.execute("SELECT claimed_at FROM wakes WHERE role = ? AND key = ?",
                                   (role, key)).fetchone()
            if already is None:
                return False
            if already["claimed_at"] is None and charged >= REMINDER_CAP:
                conn.execute("UPDATE wakes SET status = 'capped' WHERE role = ? AND key = ?"
                             " AND status = 'scheduled'", (role, key))
                return False
            return bool(conn.execute(
                "UPDATE wakes SET status = 'pending', lease_until = 0, claimed_at = COALESCE(claimed_at, ?)"
                " WHERE role = ? AND key = ? AND status IN ('scheduled', 'capped')",
                (_now(), role, key)).rowcount)

    def _send(self, role: str, wake_id: str, body: bytes) -> bool:
        wake = self._config.roles[role].wake
        if wake.integration is None:
            return self._deliver(wake, wake_id, body)
        integration = self._integrations.get(wake.integration)
        if integration is None:
            log.warning("wake %s not sent: integration %s is not loaded", wake_id, wake.integration)
            return False
        try:
            return integration.deliver(role, wake_id, body)
        except Exception:
            log.exception("wake %s: integration %s failed", wake_id, wake.integration)
            return False


def wake_id(role: str, key: str) -> str:
    """The stable id a wake is delivered under, the same on every attempt."""
    return "wake_" + hashlib.sha256(f"{role}:{key}".encode()).hexdigest()[:24]


def finished_ids(conn: sqlite3.Connection) -> set[str]:
    """Ids of wakes the core is done with (delivered, failed or superseded). Read-only; for integrations."""
    rows = conn.execute("SELECT role, key FROM wakes WHERE status IN ('delivered', 'failed', 'superseded')")
    return {wake_id(row["role"], row["key"]) for row in rows}


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """The latest wakes and how they ended, for `baton wakes`."""
    rows = conn.execute("SELECT role, key, status, attempts, created_at, finished_at FROM wakes"
                        " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]
