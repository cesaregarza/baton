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
LEASE_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 3600.0

# (wake config, wake id, JSON body) -> delivered?
Deliver = Callable[[Wake, str, bytes], bool]


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
                                    " ORDER BY lease_until LIMIT ?", (time.time(), PAGE)).fetchall():
                tried += self._attempt(conn, row["role"], row["key"])
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
            if row["attempts"] >= MAX_ATTEMPTS:
                conn.execute("UPDATE wakes SET status = 'failed', finished_at = ? WHERE role = ? AND key = ?",
                             (events.now(), role, key))
                log.error("wake %s %s gave up after %d attempts", role, key, row["attempts"])
                return 0
            conn.execute("UPDATE wakes SET attempts = attempts + 1, lease_until = ? WHERE role = ? AND key = ?",
                         (time.time() + LEASE_SECONDS, role, key))
        delivered = self._send(role, wake_id(role, key), row["payload"].encode())
        with transaction(conn):
            if delivered:
                conn.execute("UPDATE wakes SET status = 'delivered', finished_at = ? WHERE role = ? AND key = ?"
                             " AND status = 'pending'", (events.now(), role, key))
            else:
                backoff = min(MAX_BACKOFF_SECONDS, LEASE_SECONDS * 2 ** row["attempts"])
                conn.execute("UPDATE wakes SET lease_until = ? WHERE role = ? AND key = ?",
                             (time.time() + backoff, role, key))
        return 1

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
