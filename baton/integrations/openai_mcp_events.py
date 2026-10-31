"""OpenAI MCP Events: wake a remote client, such as a ChatGPT connector, with a signed webhook.

Contract: "MCP Events", https://developers.openai.com/plugins/build/mcp-events,
for MCP protocol version 2026-07-28 (read for this integration on 2026-10-27;
docs/integrations/openai-mcp-events.md lists what was verified and what was not).

The client subscribes over its authenticated MCP session to one event,
`baton.attention`, giving an HTTPS callback URL and a `whsec_` signing secret.
The host verifies the callback with a signed challenge, then POSTs one signed
event per wake for the subscriber's role. An event carries only what the core
wake carries, `role`, `reason` and a sequence or job key, never message text,
so the client reads the content with its own credential.

Defences, all of them on every request:

* the subscriber's role comes only from its credential, and its subscriptions
  belong to that role: it can't subscribe, unsubscribe or receive for another.
  Each delivery needs that same credential to still hold that role;
* a callback URL must already be canonical (no `.` or `..` segments, encoded periods,
  slashes or backslashes, empty segments, userinfo, query or fragment), and is
  compared with the operator's allowlist segment by segment. The exact string
  checked is the one stored and sent;
* a callback is verified with a signed challenge before anything is stored,
  and a refused subscribe keeps no state;
* lifetimes are bounded, and subscriptions are capped per role and overall;
* each request has one deadline covering resolution, connection, TLS, sending
  and reading. The name is resolved once, every address must be global, and the
  connection goes only to those addresses, with TLS and `Host` keeping the
  name. Redirects are not followed, proxies from the environment are ignored,
  responses must not be content-encoded, and at most 64 KiB is read;
* the secret is kept only in this integration's 0600 state file, and is never
  logged, audited, put in a wake or returned.

Retries, backoff, coalescing and `baton wakes` stay in the core. `deliver`
keeps an outcome for each (wake, subscription): a destination that answered
2xx, 410 or 413 is done with that wake, and the wake counts as delivered only
when every live destination is done and at least one accepted it.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import queue
import re
import secrets
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpcore
from mcp import types
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from baton import auth, datadir, wakes
from baton.config import Config, ConfigError
from baton.store import connect

log = logging.getLogger("baton.integrations.openai_mcp_events")

EVENT_NAME = "baton.attention"
CALLBACK_ENDPOINT_ERROR = -32015
INVALID_PARAMS = -32602
MAX_BODY_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
STATE_FILE = "openai-mcp-events.json"
LOCK_FILE = "openai-mcp-events.lock"
# The process-wide resolver pool: at most this many lookups run at once, and this many may wait.
RESOLVER_WORKERS = 4
RESOLVER_QUEUE = 4
# Answers after which a destination is done with a wake: accepted, gone, or a body it will never take.
FINAL_STATUSES = (410, 413)
# The fields of a core wake that may appear in an event. Anything else stays out.
WAKE_FIELDS = ("role", "reason", "first_unread_seq", "job_id", "event_seq")

EVENT_DEFINITION = {
    "name": EVENT_NAME,
    "description": "Something on the baton host is waiting for your role. Call read_messages or wait.",
    "delivery": ["webhook"],
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "payloadSchema": {
        "type": "object",
        "properties": {
            "role": {"type": "string"},
            "reason": {"type": "string"},
            "first_unread_seq": {"type": "integer"},
            "job_id": {"type": "string"},
            "event_seq": {"type": "integer"},
        },
        "required": ["role", "reason"],
        "additionalProperties": False,
    },
}

Resolver = Callable[[str, int], list[str]]


def system_resolver(host: str, port: int) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)]


@dataclass(frozen=True)
class Settings:
    callback_prefixes: tuple[str, ...]
    default_ttl_seconds: int = 7 * 86400
    min_ttl_seconds: int = 300
    max_ttl_seconds: int = 30 * 86400
    max_per_role: int = 5
    max_total: int = 20
    timeout_seconds: float = 10.0
    # How long a replaced secret keeps signing beside the new one, and the least time between two rotations
    # of one subscription. The contract asks for "a short rotation window" and names no length; one hour is
    # this integration's choice, and less than a minute is refused.
    rotation_overlap_seconds: int = 3600
    # Test-only escapes, for a stub receiver on loopback with its own certificate. Never read from the
    # config: parse_settings refuses them, so only test code can set them, through the constructor.
    allow_private_callbacks: bool = False
    ca_file: str | None = None


# (setting, type, minimum, maximum) for everything the config may set besides callback_prefixes.
_NUMBERS = (
    ("default_ttl_seconds", int, 1, 365 * 86400),
    ("min_ttl_seconds", int, 1, 365 * 86400),
    ("max_ttl_seconds", int, 1, 365 * 86400),
    ("max_per_role", int, 1, 1000),
    ("max_total", int, 1, 10000),
    ("timeout_seconds", (int, float), 0.1, 60),
    ("rotation_overlap_seconds", int, 60, 86400),
)


def parse_settings(table: dict[str, Any]) -> Settings:
    """The production settings from `[integrations.openai_mcp_events]`; anything else is refused."""
    name = "integrations.openai_mcp_events"
    unknown = set(table) - {"enabled", "callback_prefixes", *(setting for setting, *_ in _NUMBERS)}
    if unknown:
        raise ConfigError(f"{name}: unknown or test-only settings {sorted(unknown)}")
    prefixes = table.get("callback_prefixes")
    if not isinstance(prefixes, list) or not prefixes or not all(isinstance(p, str) for p in prefixes):
        raise ConfigError(f"{name}.callback_prefixes must list the HTTPS URL prefixes callbacks may use")
    for prefix in prefixes:
        _parse_prefix(prefix)
    values: dict[str, Any] = {}
    for setting, kind, low, high in _NUMBERS:
        if setting not in table:
            continue
        value = table[setting]
        if isinstance(value, bool) or not isinstance(value, kind) or not low <= value <= high:
            raise ConfigError(f"{name}.{setting} must be a number from {low} to {high}")
        values[setting] = value
    settings = Settings(callback_prefixes=tuple(prefixes), **values)
    if not settings.min_ttl_seconds <= settings.default_ttl_seconds <= settings.max_ttl_seconds:
        raise ConfigError(f"{name}: need min_ttl_seconds <= default_ttl_seconds <= max_ttl_seconds")
    return settings


def load(config: Config, table: dict[str, Any]) -> McpEvents:
    return McpEvents(config, parse_settings(table))


class CallbackError(Exception):
    """A callback that can't be used; `reason` becomes the -32015 error's `data.reason`."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail

    def data(self) -> dict[str, str]:
        return {"reason": self.reason, **({"detail": self.detail} if self.detail else {})}


class SubscribeParams(types.RequestParams):
    name: str
    arguments: dict[str, Any] = {}
    delivery: dict[str, Any]
    cursor: str | None = None
    ttlMs: int | None = None  # noqa: N815 - the contract's field name


class UnsubscribeParams(types.RequestParams):
    name: str
    arguments: dict[str, Any] = {}
    delivery: dict[str, Any]


class ListParams(types.RequestParams):
    cursor: str | None = None


def sign(secret: str, message_id: str, timestamp: int, body: bytes) -> str:
    """Standard Webhooks v1: HMAC-SHA256 over `id.timestamp.body`, keyed with the decoded `whsec_` secret."""
    key = base64.b64decode(secret.removeprefix("whsec_"))
    digest = hmac.new(key, f"{message_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def signature_header(secrets_: Sequence[str], message_id: str, timestamp: int, body: bytes) -> str:
    """One signature per secret, space-separated, as Standard Webhooks allows during a rotation."""
    return " ".join(sign(secret, message_id, timestamp, body) for secret in secrets_)


def subscription_id(role: str, url: str, name: str, arguments: dict[str, Any]) -> str:
    """Deterministic, from the principal (the role), the callback, the event and canonical arguments."""
    identity = json.dumps({"principal": role, "url": url, "name": name, "arguments": arguments},
                          sort_keys=True, separators=(",", ":"))
    return "sub_" + hashlib.sha256(identity.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Callback:
    """A canonical callback URL, split for the allowlist comparison."""

    url: str
    host: str
    port: int
    segments: tuple[str, ...]

    @property
    def target(self) -> str:
        return "/" + "/".join(self.segments)


_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
# Path segments are unreserved characters only (RFC 3986 section 2.3): anything else can be read, split or
# dropped differently along the way (httpcore's urlparse drops ";params", for one), so it is refused.
UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_SEGMENT = re.compile(r"[A-Za-z0-9\-._~]+")
_FORBIDDEN_ESCAPES = re.compile(r"%(2e|2f|5c|25)", re.IGNORECASE)


def canonical_callback(url: str) -> Callback:
    """Parse a callback URL that must already be canonical; refuse anything that could be read two ways.

    The rules: `https`, a lowercase ASCII host, an explicit port only if not
    443, a path of one or more non-empty segments that are not `.` or `..`, no
    percent-encoded period, slash, backslash or percent sign, no backslash, and no
    userinfo, query or fragment. Rebuilding the URL from its parts must give back
    exactly the input, so the string checked is the string sent.
    """
    if "\\" in url or _FORBIDDEN_ESCAPES.search(url) or any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise CallbackError("invalid_url")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise CallbackError("invalid_url") from None
    if (parts.scheme != "https" or parts.username is not None or parts.password is not None or parts.query
            or parts.fragment or "?" in url or "#" in url or not parts.hostname or not _HOST.fullmatch(parts.hostname)):
        raise CallbackError("invalid_url")
    segments = tuple(parts.path.split("/")[1:]) if parts.path.startswith("/") else ()
    if not segments or any(seg in ("", ".", "..") for seg in segments):
        raise CallbackError("invalid_url")
    for seg in segments:
        if not _SEGMENT.fullmatch(seg):
            bad = next(c for c in seg if c not in UNRESERVED)
            raise CallbackError("invalid_url", f"character {bad!r} is not allowed in a callback path; "
                                               "use only A-Z a-z 0-9 . _ ~ - between slashes")
    port = port or 443
    callback = Callback(url, parts.hostname, port, segments)
    rebuilt = f"https://{callback.host}{'' if port == 443 else f':{port}'}{callback.target}"
    if rebuilt != url:
        raise CallbackError("invalid_url")
    return callback


class McpEvents:
    def __init__(self, config: Config, settings: Settings, *, resolver: Resolver = system_resolver) -> None:
        self.config = config
        self.settings = settings
        self.resolver = resolver

    # --- the core's integration hook -------------------------------------------------

    def middleware(self) -> Sequence[Any]:
        return [_advertise_events]

    def register(self, server: MCPServer) -> None:
        # MCPServer has no public hook for custom JSON-RPC methods; the low-level server routes them.
        lowlevel = server._lowlevel_server
        lowlevel.add_request_handler("events/list", ListParams, self._list)
        lowlevel.add_request_handler("events/subscribe", SubscribeParams, self._subscribe)
        lowlevel.add_request_handler("events/unsubscribe", UnsubscribeParams, self._unsubscribe)

    def deliver(self, role: str, wake_id: str, body: bytes) -> bool:
        """Send the wake to each live destination of `role` that isn't done with it yet.

        True once every live destination has a final outcome (2xx, 410 or 413)
        and at least one accepted it; otherwise the core retries later, and only
        the destinations still without a final outcome are sent to again.
        """
        wake = json.loads(body)
        occurred_at = self._occurrence(wake_id)
        event = json.dumps({
            "eventId": wake_id, "name": EVENT_NAME, "timestamp": occurred_at,
            "data": {key: wake[key] for key in WAKE_FIELDS if key in wake}, "cursor": None,
        }, separators=(",", ":")).encode()
        if len(event) > MAX_BODY_BYTES:
            raise ValueError("an event body exceeds the 256 KiB contract limit")
        done = self._outcomes(wake_id)
        for sub_id in [sub["id"] for sub in self._live_subscriptions(role) if sub["id"] not in done]:
            # Rechecked right before each attempt: a subscription removed, deactivated, expired or whose
            # credential lost its authority during an earlier callback is not sent to.
            sub = next((s for s in self._live_subscriptions(role) if s["id"] == sub_id), None)
            if sub is None:
                continue
            try:
                status, _ = self._post(sub["url"], self._signing_secrets(sub), sub["id"], wake_id, event,
                                       want_body=False)
            except CallbackError as error:
                log.warning("wake %s to %s failed: %s", wake_id, sub["id"], error.reason)
                continue
            if 200 <= status < 300:
                self._record(wake_id, sub["id"], "accepted")
            elif status in FINAL_STATUSES:
                self._record(wake_id, sub["id"], "gone" if status == 410 else "too_large")
                if status == 410:
                    self._deactivate(sub["id"])
                    log.info("subscription %s answered 410 and is now inactive", sub["id"])
                else:
                    log.warning("wake %s to %s answered 413; not retried", wake_id, sub["id"])
            else:
                log.warning("wake %s to %s answered %d", wake_id, sub["id"], status)
        done = self._outcomes(wake_id)
        live_ids = {sub["id"] for sub in self._live_subscriptions(role)}
        return all(sub_id in done for sub_id in live_ids) and "accepted" in done.values()

    # --- JSON-RPC handlers -------------------------------------------------------------

    async def _list(self, ctx: Any, params: ListParams) -> dict[str, Any]:
        _principal()
        return {"events": [EVENT_DEFINITION], "nextCursor": None}

    async def _subscribe(self, ctx: Any, params: SubscribeParams) -> dict[str, Any]:
        token, role = _principal()
        _refuse_guest_mutation(role)
        callback, secret = self._validated(params)
        sub_id = subscription_id(role, callback.url, params.name, {})
        expires_at = self._expiry(params)
        self._check_caps(self._read(), role, sub_id)
        self._check_rotation(self._read(), sub_id, secret)
        try:
            await anyio.to_thread.run_sync(self._verify, callback.url, secret, sub_id)
        except CallbackError as error:
            raise MCPError(CALLBACK_ENDPOINT_ERROR, "callback endpoint verification failed",
                           {"reason": error.reason}) from None
        now = time.time()
        with self._state() as state:
            self._check_caps(state, role, sub_id)  # both again, under the lock
            self._check_rotation(state, sub_id, secret)
            existing = state["subscriptions"].get(sub_id)
            if existing and existing["secret"] != secret:
                # A rotation: the replaced secret is the only previous one, for one overlap window.
                previous = [{"secret": existing["secret"], "until": now + self.settings.rotation_overlap_seconds}]
                rotated_at = now
            else:
                previous = [p for p in (existing or {}).get("previous_secrets", [])[:1]
                            if p["until"] > now and p["secret"] != secret]
                rotated_at = (existing or {}).get("rotated_at")
            state["subscriptions"][sub_id] = {
                "id": sub_id, "role": role, "authority": auth.credential_authority(token), "url": callback.url,
                "secret": secret, "previous_secrets": previous, "rotated_at": rotated_at,
                "expires_at": expires_at, "active": True,
            }
        log.info("subscription %s active for role %s", sub_id, role)
        return {"id": sub_id, "refreshBefore": datetime.fromtimestamp(expires_at, UTC).isoformat(),
                "cursor": None, "truncated": False}

    async def _unsubscribe(self, ctx: Any, params: UnsubscribeParams) -> dict[str, Any]:
        _, role = _principal()
        _refuse_guest_mutation(role)
        sub_id = subscription_id(role, str(params.delivery.get("url", "")), params.name, dict(params.arguments))
        with self._state() as state:
            sub = state["subscriptions"].get(sub_id)
            if sub is not None and sub["role"] == role:
                del state["subscriptions"][sub_id]
        return {}

    # --- validation ----------------------------------------------------------------------

    def _validated(self, params: SubscribeParams) -> tuple[Callback, str]:
        if params.name != EVENT_NAME:
            raise MCPError(INVALID_PARAMS, f"unknown event {params.name}")
        if params.arguments:
            raise MCPError(INVALID_PARAMS, f"{EVENT_NAME} takes no arguments")
        if params.delivery.get("mode") != "webhook":
            raise MCPError(INVALID_PARAMS, "only webhook delivery is supported")
        secret = str(params.delivery.get("secret", ""))
        if not _valid_secret(secret):
            raise MCPError(INVALID_PARAMS, "secret must be whsec_ followed by 24 to 64 base64-encoded bytes")
        try:
            callback = self._allowed(str(params.delivery.get("url", "")))
        except CallbackError as error:
            raise MCPError(CALLBACK_ENDPOINT_ERROR, "callback URL refused", error.data()) from None
        return callback, secret

    def _allowed(self, url: str) -> Callback:
        """The canonical callback, if it falls under one of the operator's prefixes, segment by segment."""
        callback = canonical_callback(url)
        for prefix in self.settings.callback_prefixes:
            host, port, segments = _parse_prefix(prefix)
            if (callback.host, callback.port) == (host, port) and callback.segments[:len(segments)] == segments:
                return callback
        log.warning("callback refused: https://%s is not on the allowlist", callback.host)
        raise CallbackError("not_allowed")

    def _expiry(self, params: SubscribeParams) -> float:
        """Omitted ttlMs is the default; null or anything longer gets the maximum, never "no expiration"."""
        if "ttlMs" not in params.model_fields_set:
            seconds = self.settings.default_ttl_seconds
        elif params.ttlMs is None:
            seconds = self.settings.max_ttl_seconds
        else:
            seconds = min(max(params.ttlMs / 1000, self.settings.min_ttl_seconds), self.settings.max_ttl_seconds)
        return time.time() + seconds

    def _check_rotation(self, state: dict[str, Any], sub_id: str, secret: str) -> None:
        """At most one secret rotation per subscription per overlap window; a same-secret refresh is no rotation."""
        existing = state["subscriptions"].get(sub_id)
        if existing is None or existing["secret"] == secret:
            return
        if (existing.get("rotated_at") or 0) > time.time() - self.settings.rotation_overlap_seconds:
            raise MCPError(INVALID_PARAMS, "this subscription's secret was rotated less than "
                                           f"{self.settings.rotation_overlap_seconds} seconds ago; retry later")

    def _check_caps(self, state: dict[str, Any], role: str, sub_id: str) -> None:
        live = [sub for sub in state["subscriptions"].values() if _unexpired(sub)]
        if sub_id in {sub["id"] for sub in live}:
            return  # a refresh of an existing subscription
        if sum(sub["role"] == role for sub in live) >= self.settings.max_per_role:
            raise MCPError(INVALID_PARAMS, f"role {role} already has {self.settings.max_per_role} subscriptions")
        if len(live) >= self.settings.max_total:
            raise MCPError(INVALID_PARAMS, "the host has as many subscriptions as it allows")

    # --- outbound requests -----------------------------------------------------------------

    def _verify(self, url: str, secret: str, sub_id: str) -> None:
        challenge = secrets.token_urlsafe(32)
        body = json.dumps({"type": "verification", "challenge": challenge}, separators=(",", ":")).encode()
        status, content = self._post(url, [secret], sub_id, f"msg_verification_{secrets.token_hex(12)}", body)
        if not 200 <= status < 300:
            raise CallbackError("challenge_failed")
        try:
            echoed = json.loads(content).get("challenge")
        except (ValueError, AttributeError):
            echoed = None
        if not isinstance(echoed, str) or not hmac.compare_digest(echoed, challenge):
            raise CallbackError("challenge_failed")

    def _post(self, url: str, secrets_: Sequence[str], sub_id: str, message_id: str, body: bytes, *,
              want_body: bool = True) -> tuple[int, bytes]:
        """One signed POST within one absolute deadline: the status, and at most MAX_RESPONSE_BYTES of raw body.

        The deadline covers resolution, connecting, TLS, every write and every read, headers included: the
        network backend gives each operation only what is left of it.
        """
        budget = _Budget(time.monotonic() + self.settings.timeout_seconds, MAX_RESPONSE_BYTES)
        callback = self._allowed(url)  # the allowlist may have changed since the subscription was made
        addresses = self._resolve(callback.host, callback.port, budget.deadline)
        timestamp = int(time.time())
        headers = [
            (b"Host", (callback.host if callback.port == 443 else f"{callback.host}:{callback.port}").encode()),
            (b"Content-Type", b"application/json"),
            (b"Content-Length", str(len(body)).encode()),
            (b"Accept-Encoding", b"identity"),
            (b"webhook-id", message_id.encode()),
            (b"webhook-timestamp", str(timestamp).encode()),
            (b"webhook-signature", signature_header(secrets_, message_id, timestamp, body).encode()),
            (b"X-MCP-Subscription-Id", sub_id.encode()),
        ]
        context = ssl.create_default_context(cafile=self.settings.ca_file)
        for index, address in enumerate(addresses):
            literal = f"[{address}]" if ":" in address else address
            try:
                # httpcore follows no redirects and reads no proxy settings from the environment.
                with httpcore.ConnectionPool(ssl_context=context, network_backend=_DeadlineBackend(budget),
                                             max_connections=1, retries=0) as pool, \
                        pool.stream("POST", f"https://{literal}:{callback.port}{callback.target}", headers=headers,
                                    content=body, extensions={"sni_hostname": callback.host,
                                                              "timeout": budget.timeouts()}) as response:
                    if not want_body:
                        # A delivery is judged by its status alone; its body is never read or decoded.
                        return response.status, b""
                    encoding = _header(response.headers, b"content-encoding").strip().lower()
                    if encoding not in (b"", b"identity"):
                        raise CallbackError("encoded_response")
                    return response.status, _read_body(response)
            except httpcore.ConnectError:
                if index == len(addresses) - 1:
                    raise CallbackError("unreachable") from None
            except httpcore.TimeoutException:
                raise CallbackError("timeout") from None
            except _CapReached:
                raise CallbackError("response_too_large") from None
            except (httpcore.NetworkError, httpcore.ProtocolError):
                raise CallbackError("unreachable") from None
        raise CallbackError("unreachable")

    def _resolve(self, host: str, port: int, deadline: float | None = None) -> list[str]:
        """Resolve once, within the deadline; every address must be global (unless the test-only flag is set)."""
        deadline = deadline if deadline is not None else time.monotonic() + self.settings.timeout_seconds
        answers = RESOLVER.resolve(self.resolver, host, port, deadline)
        addresses = [ipaddress.ip_address(answer) for answer in answers]
        if not addresses:
            raise CallbackError("unreachable")
        if not self.settings.allow_private_callbacks and not all(_is_global(address) for address in addresses):
            raise CallbackError("private_address")
        return list(dict.fromkeys(str(address) for address in addresses))

    # --- state --------------------------------------------------------------------------------

    def _live_subscriptions(self, role: str) -> list[dict[str, Any]]:
        return [sub for sub in self._read()["subscriptions"].values()
                if sub["role"] == role and sub["active"] and _unexpired(sub)
                and auth.authority_live(self.config, sub.get("authority", {}))]

    def _signing_secrets(self, sub: dict[str, Any]) -> list[str]:
        """The current secret, then any replaced one still inside its rotation overlap."""
        now = time.time()
        previous = [p["secret"] for p in sub.get("previous_secrets", [])[:1]
                    if p["until"] > now and p["secret"] != sub["secret"]]
        return [sub["secret"], *previous]

    def _occurrence(self, wake_id: str) -> str:
        """When the host first handled this wake; every retry carries the same time."""
        with self._state() as state:
            entry = state["outcomes"].setdefault(wake_id, {"at": time.time(), "destinations": {}})
            return datetime.fromtimestamp(entry["at"], UTC).isoformat()

    def _outcomes(self, wake_id: str) -> dict[str, str]:
        return self._read()["outcomes"].get(wake_id, {}).get("destinations", {})

    def _record(self, wake_id: str, sub_id: str, outcome: str) -> None:
        with self._state() as state:
            entry = state["outcomes"].setdefault(wake_id, {"at": time.time(), "destinations": {}})
            entry["destinations"][sub_id] = outcome

    def _deactivate(self, sub_id: str) -> None:
        with self._state() as state:
            if sub_id in state["subscriptions"]:
                state["subscriptions"][sub_id] = {**state["subscriptions"][sub_id], "active": False}

    def _finished_wakes(self) -> set[str]:
        conn = connect(self.config.db_path)
        try:
            return wakes.finished_ids(conn)
        finally:
            conn.close()

    def _read(self) -> dict[str, Any]:
        directory = self.config.data_dir
        with datadir.opened(directory) as dir_fd:
            found = datadir.read_file(dir_fd, STATE_FILE, directory=directory)
        return _shape(json.loads(found[1]) if found else {})

    @contextmanager
    def _state(self) -> Iterator[dict[str, Any]]:
        """Read, change and write the state file under an exclusive lock; expired entries are dropped."""
        directory = self.config.data_dir
        with datadir.opened(directory) as dir_fd:
            lock = datadir.open_file(dir_fd, LOCK_FILE, os.O_RDWR | os.O_CREAT, directory=directory)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                found = datadir.read_file(dir_fd, STATE_FILE, directory=directory)
                state = _shape(json.loads(found[1]) if found else {})
                now = time.time()
                state["subscriptions"] = {key: sub for key, sub in state["subscriptions"].items() if _unexpired(sub)}
                for sub in state["subscriptions"].values():
                    sub["previous_secrets"] = [p for p in sub.get("previous_secrets", []) if p["until"] > now]
                if state["outcomes"]:
                    # A wake's outcomes and occurrence time live exactly as long as the core wake: they are
                    # dropped once the core reports it delivered, failed or superseded, and not before.
                    finished = self._finished_wakes()
                    state["outcomes"] = {key: entry for key, entry in state["outcomes"].items()
                                         if key not in finished}
                yield state
                datadir.replace_file(dir_fd, STATE_FILE, json.dumps(state).encode())
            finally:
                os.close(lock)

    def with_settings(self, **changes: Any) -> McpEvents:
        """A copy with changed settings (used by tests)."""
        return McpEvents(self.config, replace(self.settings, **changes), resolver=self.resolver)


def _principal() -> tuple[Any, str]:
    """The caller's access token and role, from its credential only."""
    token = get_access_token()
    role = auth.role_of(token)
    if token is None or role is None or auth.SCOPE_READ not in token.scopes:
        raise MCPError(-32600, f"events need {auth.SCOPE_READ}")
    return token, role


def _refuse_guest_mutation(role: str) -> None:
    """Guests may discover the event definition, but cannot create or change subscriptions."""
    if role == "guest":
        raise MCPError(-32600, "guest credentials cannot subscribe to or modify integration events")


async def _advertise_events(ctx: Any, call_next: Any) -> Any:
    """Add `events: {}` to `server/discover`; the SDK drops capabilities its schema doesn't know before this runs."""
    result = await call_next(ctx)
    if getattr(ctx, "method", None) == "server/discover" and isinstance(result, dict):
        result.setdefault("capabilities", {})["events"] = {}
    return result


def _shape(state: dict[str, Any]) -> dict[str, Any]:
    return {"subscriptions": state.get("subscriptions", {}), "outcomes": state.get("outcomes", {})}


def _valid_secret(secret: str) -> bool:
    if not secret.startswith("whsec_"):
        return False
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    except (binascii.Error, ValueError):
        return False
    return 24 <= len(key) <= 64


def _parse_prefix(prefix: str) -> tuple[str, int, tuple[str, ...]]:
    """An allowlist prefix: a canonical https URL whose path may end in '/' (or be just '/')."""
    try:
        callback = canonical_callback(prefix.rstrip("/") + "/x" if prefix.endswith("/") else prefix)
    except CallbackError:
        raise ConfigError(f"callback prefix {prefix!r} must be a canonical https://host[:port]/path, with no "
                          "wildcard, `.` or `..` segment, encoded character, credentials, query or fragment") from None
    segments = callback.segments[:-1] if prefix.endswith("/") else callback.segments
    return callback.host, callback.port, segments


def _is_global(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_global and (mapped is None or mapped.is_global)


def _unexpired(sub: dict[str, Any]) -> bool:
    return sub["expires_at"] > time.time()


class _CapReached(Exception):
    """The response reached MAX_RESPONSE_BYTES; nothing more is read from the socket."""


class _Budget:
    """One request's absolute deadline and its read cap, shared by every network operation it makes."""

    def __init__(self, deadline: float, cap: int) -> None:
        self.deadline = deadline
        self.cap = cap
        self.received = 0

    def remaining(self, timeout: float | None = None) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise CallbackError("timeout")
        return left if timeout is None else min(timeout, left)

    def timeouts(self) -> dict[str, float]:
        left = self.remaining()
        return {"connect": left, "read": left, "write": left, "pool": left}


class _DeadlineStream(httpcore.NetworkStream):
    """Every read, write and TLS handshake gets min(its timeout, what is left of the deadline); reads stop at the
    cap, asking the socket for no more than `cap - received` bytes."""

    def __init__(self, stream: httpcore.NetworkStream, budget: _Budget) -> None:
        self._stream = stream
        self._budget = budget

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        left = self._budget.cap - self._budget.received
        if left <= 0:
            raise _CapReached()
        data = self._stream.read(min(max_bytes, left), self._budget.remaining(timeout))
        self._budget.received += len(data)
        return data

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        # httpcore's own loop re-arms the original timeout on each partial send; this one re-arms what is left.
        sock = self._stream.get_extra_info("socket")
        view = memoryview(buffer)
        while view:
            sock.settimeout(self._budget.remaining(timeout))
            try:
                sent = sock.send(view[:65536])
            except TimeoutError:
                raise httpcore.WriteTimeout("deadline reached while sending") from None
            except OSError as error:
                raise httpcore.WriteError(str(error)) from None
            view = view[sent:]

    def close(self) -> None:
        self._stream.close()

    def start_tls(self, ssl_context: ssl.SSLContext, server_hostname: str | None = None,
                  timeout: float | None = None) -> httpcore.NetworkStream:
        return _DeadlineStream(self._stream.start_tls(ssl_context, server_hostname, self._budget.remaining(timeout)),
                               self._budget)

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


class _DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self, budget: _Budget) -> None:
        self._backend = httpcore.SyncBackend()
        self._budget = budget

    def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                    socket_options: Any = None) -> httpcore.NetworkStream:
        stream = self._backend.connect_tcp(host, port, self._budget.remaining(timeout), local_address, socket_options)
        return _DeadlineStream(stream, self._budget)

    def connect_unix_socket(self, path: str, timeout: float | None = None,
                            socket_options: Any = None) -> httpcore.NetworkStream:
        raise httpcore.ConnectError("unix sockets are not used for callbacks")

    def sleep(self, seconds: float) -> None:
        time.sleep(min(seconds, self._budget.remaining()))


class ResolverPool:
    """A fixed pool of resolver threads for the whole process, with a bounded queue.

    A lookup that times out keeps its worker (or its queue slot) until the real resolution returns; no thread is
    ever added. When every worker is busy and the queue is full, a new lookup fails at once as unreachable.
    """

    def __init__(self, workers: int = RESOLVER_WORKERS, queue_size: int = RESOLVER_QUEUE) -> None:
        self._workers = workers
        self._queue: queue.Queue[_Lookup] = queue.Queue(maxsize=queue_size)
        self._alive = 0
        self._lock = threading.Lock()

    def resolve(self, resolver: Resolver, host: str, port: int, deadline: float) -> list[str]:
        self._start()
        lookup = _Lookup(resolver, host, port)
        try:
            self._queue.put_nowait(lookup)
        except queue.Full:
            raise CallbackError("unreachable") from None
        if not lookup.done.wait(max(0.0, deadline - time.monotonic())):
            lookup.abandoned = True
            raise CallbackError("timeout")
        if lookup.answers is None:
            raise CallbackError("unreachable")
        return lookup.answers

    def _start(self) -> None:
        """Start only the workers still missing, never past the pool size. A failed start fails this lookup."""
        with self._lock:
            for number in range(self._alive, self._workers):
                worker = threading.Thread(target=self._work, daemon=True, name=f"callback-resolve-{number}")
                try:
                    worker.start()
                except RuntimeError:
                    log.warning("could not start a resolver thread; %d of %d running", self._alive, self._workers)
                    raise CallbackError("unreachable") from None
                self._alive += 1

    def _work(self) -> None:
        while True:
            lookup = self._queue.get()
            try:
                if not lookup.abandoned:
                    lookup.answers = lookup.resolver(lookup.host, lookup.port)
            except OSError:
                lookup.answers = None
            except Exception:
                log.exception("callback resolver failed")
                lookup.answers = None
            finally:
                lookup.done.set()


@dataclass
class _Lookup:
    resolver: Resolver
    host: str
    port: int
    answers: list[str] | None = None
    abandoned: bool = False
    done: threading.Event = field(default_factory=threading.Event)


RESOLVER = ResolverPool()


def _header(headers: Sequence[tuple[bytes, bytes]], name: bytes) -> bytes:
    return next((value for key, value in headers if key.lower() == name), b"")


def _read_body(response: Any) -> bytes:
    """The raw body, undecoded, up to MAX_RESPONSE_BYTES of total reads; a larger one is cut there."""
    content = b""
    try:
        for chunk in response.iter_stream():
            content += chunk
    except _CapReached:
        pass
    return content[:MAX_RESPONSE_BYTES]
