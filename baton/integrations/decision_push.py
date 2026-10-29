"""Decision push: tell one role's channel, without saying what, that a decision is waiting for it.

Enabled by `[integrations.decision_push]`:

    enabled = true
    role = "operator"                          # the role whose decisions are pushed
    url = "https://push.example.com/decisions" # where the notice is POSTed
    secret_env = "BATON_PUSH_TOKEN"            # optional: the variable holding a bearer token
    link_base = "https://dash.example.com"     # optional: the dashboard, for a link to the item
    max_per_hour = 10
    timeout_seconds = 10

A message pushes when the configured role received it and it carries
`needs_decision`; nothing else pushes. The request is a plain POST: its body is
a fixed notice and, when `link_base` is set, the link to the message on the
dashboard; its `Title` and `Click` headers are what some push services read,
and any other webhook may ignore them. It never carries the message's words, its relayed
words, its conversation's title or who sent it, because a push passes through
someone else's servers. The URL and the token come only from the operator's
config and environment, and neither is ever logged.

It runs on its own thread beside the MCP server, reading the role's mailbox
after a watermark kept in this integration's state file. A send never waits on
it, so a slow or failing channel can't delay or fail one. Each message is
pushed at most once: the watermark moves past a message before its push is
attempted, so a retried send (which stores nothing new), a restart or a failed
push never pushes it again. A failure is logged and counted, without content.
At most `max_per_hour` pushes go out in any hour; what the cap holds back is
counted, not sent later. Enabling it sends nothing for decisions already
waiting: the first pass starts at the end of the mailbox.
"""

from __future__ import annotations

import fcntl
import http.client
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote, urlsplit

from mcp.server.mcpserver import MCPServer

from baton import datadir
from baton.config import Config, ConfigError
from baton.store import connect

NAME = "decision_push"
TITLE = "A decision is waiting in Baton"
STATE_FILE = "decision_push.json"
LOCK_FILE = "decision_push.lock"
HOUR = 3600.0
PAGE = 100
KEYS = {"enabled", "role", "url", "secret_env", "link_base", "max_per_hour", "timeout_seconds"}
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")

log = logging.getLogger("baton.decision_push")

# (url, headers, body) -> the HTTP status; raises (OSError, http.client.HTTPException) when no answer came.
Post = Callable[[str, dict[str, str], bytes], int]


@dataclass(frozen=True)
class Settings:
    role: str
    url: str
    secret_env: str | None
    link_base: str | None
    max_per_hour: int
    timeout_seconds: float


def parse_settings(config: Config, table: dict[str, Any]) -> Settings:
    """The table, checked. An error names the field and never repeats the URL or a token."""
    where = f"integrations.{NAME}"
    unknown = sorted(set(table) - KEYS)
    if unknown:
        hint = " (put a token in an environment variable and name it in secret_env)" if "token" in unknown else ""
        raise ConfigError(f"{where}.{unknown[0]} is not a setting of this integration{hint}")
    role = table.get("role")
    if role not in config.roles:
        raise ConfigError(f"{where}.role must name a configured role")
    url = table.get("url")
    if not _http_url(url) or _has_credentials(url):
        raise ConfigError(f"{where}.url must be an http(s) URL with no credentials in it")
    secret_env = table.get("secret_env")
    if secret_env is not None:
        if not isinstance(secret_env, str) or not ENV_NAME.fullmatch(secret_env):
            raise ConfigError(f"{where}.secret_env must be the name of an environment variable, such as "
                              "BATON_PUSH_TOKEN")
        if not os.environ.get(secret_env):
            raise ConfigError(f"the environment variable {secret_env}, named by {where}.secret_env, is not set")
    link_base = table.get("link_base")
    if link_base is not None:
        parts = urlsplit(link_base) if isinstance(link_base, str) else None
        if not _http_url(link_base) or parts.query or parts.fragment or _has_credentials(link_base):
            raise ConfigError(f"{where}.link_base must be the dashboard's http(s) address, with no query, "
                              "fragment or credentials")
        link_base = link_base.rstrip("/")
    return Settings(role, url, secret_env, link_base,
                    _bounded(table, "max_per_hour", 10, 1, 1000, f"{where}.max_per_hour", integer=True),
                    _bounded(table, "timeout_seconds", 10.0, 0.1, 60, f"{where}.timeout_seconds"))


def load(config: Config, table: dict[str, Any]) -> DecisionPush:
    settings = parse_settings(config, table)
    for role in config.roles.values():
        if role.wake.integration == NAME:
            raise ConfigError(f"roles.{role.name}.wake names {NAME}, which pushes decisions and is not for wakes")
    return DecisionPush(config, settings)


def post(timeout: float) -> Post:
    """A plain HTTP POST through the standard library, which follows no redirect and verifies TLS.

    Not httpx: its logger records each request's URL, and this URL is never to be logged.
    """
    def send(url: str, headers: dict[str, str], body: bytes) -> int:
        parts = urlsplit(url)
        kind = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        connection = kind(parts.hostname, parts.port, timeout=timeout)
        try:
            connection.request("POST", (parts.path or "/") + (f"?{parts.query}" if parts.query else ""),
                               body=body, headers=headers)
            return connection.getresponse().status
        finally:
            connection.close()
    return send


class DecisionPush:
    def __init__(self, config: Config, settings: Settings, transport: Post | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.config, self.settings = config, settings
        self._post = transport or post(settings.timeout_seconds)
        self._clock = clock

    def with_transport(self, transport: Post, clock: Callable[[], float] = time.time) -> DecisionPush:
        """A copy that sends through `transport` and reads `clock` (used by tests)."""
        return DecisionPush(self.config, replace(self.settings), transport, clock)

    # The Integration protocol: no MCP methods, and no wakes (the config refuses a role that names it).
    def middleware(self) -> Sequence[Any]:
        return ()

    def register(self, server: MCPServer) -> None:
        return None

    def deliver(self, role: str, wake_id: str, body: bytes) -> bool:
        return False

    def run(self, stop: threading.Event, poll_seconds: float = 2.0) -> None:
        """Push until `stop` is set. A pass that fails is logged by its kind alone and the next one runs."""
        while True:
            try:
                self.poll_once()
            except Exception as error:  # noqa: BLE001 - one bad pass must not end the loop
                log.warning("decision push pass failed: %s", type(error).__name__)
            if stop.wait(poll_seconds):
                return

    def poll_once(self) -> int:
        """Claim the role's new decisions, then push each one the cap allows. Returns how many were sent."""
        with self._state() as state:
            due = self._claim(state)
        sent = failed = 0
        for link in due:
            if self._push(link):
                sent += 1
            else:
                failed += 1
        if sent or failed:
            with self._state() as state:
                state["counts"]["sent"] += sent
                state["counts"]["failed"] += failed
        return sent

    def counts(self) -> dict[str, int]:
        with self._state() as state:
            return dict(state["counts"])

    def _claim(self, state: dict[str, Any]) -> list[str | None]:
        """Move the watermark past the role's new messages, and return the links of the decisions to push."""
        role, now = self.settings.role, self._clock()
        conn = connect(self.config.db_path)
        try:
            row = conn.execute("SELECT last_seq FROM mailboxes WHERE recipient = ?", (role,)).fetchone()
            last = row["last_seq"] if row else 0
            if state["after_seq"] is None:
                state["after_seq"] = last
                return []
            rows = conn.execute("SELECT seq, conversation_id, conversation_seq FROM messages WHERE recipient = ?"
                                " AND seq > ? AND needs_decision = 1 ORDER BY seq LIMIT ?",
                                (role, state["after_seq"], PAGE)).fetchall()
        finally:
            conn.close()
        # Past every row read, and past the whole mailbox unless the page was full (then more remain).
        newest = rows[-1]["seq"] if rows else state["after_seq"]
        state["after_seq"] = newest if len(rows) == PAGE else max(state["after_seq"], last, newest)
        state["window"] = [at for at in state["window"] if now - at < HOUR]
        due = []
        for found in rows:
            if len(state["window"]) >= self.settings.max_per_hour:
                state["counts"]["capped"] += 1
                continue
            state["window"].append(now)
            due.append(self._link(found["conversation_id"], found["conversation_seq"]))
        return due

    def _link(self, conversation_id: str | None, conversation_seq: int | None) -> str | None:
        if self.settings.link_base is None or conversation_id is None or conversation_seq is None:
            return None
        return f"{self.settings.link_base}/c/{quote(conversation_id, safe='')}#m-{int(conversation_seq)}"

    def _push(self, link: str | None) -> bool:
        headers = {"Title": TITLE, "Content-Type": "text/plain; charset=utf-8"}
        if link:
            headers["Click"] = link
        if self.settings.secret_env:
            headers["Authorization"] = f"Bearer {os.environ.get(self.settings.secret_env, '')}"
        body = f"{TITLE}." + (f"\n{link}" if link else "")
        try:
            status = self._post(self.settings.url, headers, body.encode())
        except Exception as error:  # noqa: BLE001 - any failure is a failed push, logged without content
            log.warning("decision push failed: %s", type(error).__name__)
            return False
        if not 200 <= status < 300:
            log.warning("decision push refused: HTTP %d", status)
            return False
        return True

    @contextmanager
    def _state(self) -> Iterator[dict[str, Any]]:
        """Read, change and write the state file under an exclusive lock. It holds no message content."""
        directory = self.config.data_dir
        with datadir.opened(directory) as dir_fd:
            lock = datadir.open_file(dir_fd, LOCK_FILE, os.O_RDWR | os.O_CREAT, directory=directory)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                found = datadir.read_file(dir_fd, STATE_FILE, directory=directory)
                state = _shape(json.loads(found[1]) if found else {})
                yield state
                datadir.replace_file(dir_fd, STATE_FILE, json.dumps(state).encode())
            finally:
                os.close(lock)


def _shape(raw: dict[str, Any]) -> dict[str, Any]:
    counts = raw.get("counts") or {}
    return {"after_seq": raw.get("after_seq"), "window": list(raw.get("window") or []),
            "counts": {key: int(counts.get(key, 0)) for key in ("sent", "failed", "capped")}}


def _has_credentials(url: str) -> bool:
    """Whether the URL names a user (and perhaps a password) before its host."""
    return urlsplit(url).username is not None


def _http_url(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"https?://[^\s/?#]+(/[^\s]*)?", value) is not None


def _bounded(table: dict[str, Any], key: str, default: float, low: float, high: float, where: str, *,
             integer: bool = False) -> Any:
    value = table.get(key, default)
    kinds = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds) or not low <= value <= high:
        raise ConfigError(f"{where} must be {'a whole number' if integer else 'a number'} from {low:g} to {high:g}")
    return value
