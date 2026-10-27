# ruff: noqa: F811 - the receiver and events_host fixtures are imported from test_openai_mcp_events
"""MCP Events, round 4: an absolute deadline per request, a bounded resolver pool, wake state kept until the
core wake finishes, bounded key rotation, per-attempt liveness, the grant's own resource, and an exact read cap.

Synthetic and offline. Misbehaving callbacks are raw TLS servers on loopback using the receiver's throwaway
certificate.
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from test_auth import authorize
from test_openai_mcp_events import (
    NAME,
    StubResolver,
    dispatch,
    events_host,  # noqa: F401 - fixture
    new_secret,
    receiver,  # noqa: F401 - fixture
    send,
    state,
    subscribe,
)

from baton import auth, wakes
from baton.config import ConfigError, parse
from baton.integrations import openai_mcp_events as integration
from baton.store import connect

BUDGET = 0.3
EPSILON = 0.1


class BadServer:
    """A TLS callback on loopback that misbehaves in one chosen way."""

    def __init__(self, cert: Path, key: Path, behaviour: Callable[[ssl.SSLContext, socket.socket], None]) -> None:
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.behaviour = behaviour
        self.stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        self.listener.settimeout(0.2)
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return  # closed
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            self.behaviour(self.context, conn)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self.stop.set()
        self.listener.close()


def read_request(tls: ssl.SSLSocket) -> None:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = tls.recv(4096)
        if not chunk:
            return
        data += chunk


def wrap(context: ssl.SSLContext, conn: socket.socket) -> ssl.SSLSocket:
    return context.wrap_socket(conn, server_side=True)


def header_trickle(context: ssl.SSLContext, conn: socket.socket) -> None:
    tls = wrap(context, conn)
    read_request(tls)
    tls.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
    for _ in range(60):  # 3 s of one header byte at a time, never finishing the headers
        tls.sendall(b"a")
        time.sleep(0.05)


def late_headers_then_stall(context: ssl.SSLContext, conn: socket.socket) -> None:
    tls = wrap(context, conn)
    read_request(tls)
    time.sleep(0.2)
    tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n{")
    time.sleep(3)


def slow_tls(context: ssl.SSLContext, conn: socket.socket) -> None:
    time.sleep(3)  # accept TCP, never answer the TLS handshake


def never_read(context: ssl.SSLContext, conn: socket.socket) -> None:
    tls = wrap(context, conn)
    time.sleep(3)  # complete the handshake, then never read the request
    tls.close()


def oversized(context: ssl.SSLContext, conn: socket.socket) -> None:
    tls = wrap(context, conn)
    read_request(tls)
    body = b"x" * (1024 * 1024)
    tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
    time.sleep(1)


@pytest.fixture
def bad_server(receiver, tmp_path: Path) -> Iterator[Callable[..., Any]]:
    servers: list[BadServer] = []

    def start(behaviour: Callable[[ssl.SSLContext, socket.socket], None]) -> tuple[BadServer, integration.McpEvents]:
        server = BadServer(receiver.ca_file, tmp_path / "receiver-key.pem", behaviour)
        servers.append(server)
        config = parse({"host": {"public_url": "https://baton.example.test", "data_dir": str(tmp_path / "data")},
                        "roles": {"worker": {}}})
        settings = integration.Settings(callback_prefixes=(f"https://{NAME}:{server.port}/hooks/",),
                                        timeout_seconds=BUDGET, allow_private_callbacks=True,
                                        ca_file=str(receiver.ca_file))
        return server, integration.McpEvents(config, settings, resolver=StubResolver())

    yield start
    for server in servers:
        server.close()


def attempt(server: BadServer, events: integration.McpEvents, body: bytes = b"{}") -> tuple[float, str]:
    started = time.monotonic()
    try:
        events._post(f"https://{NAME}:{server.port}/hooks/a", [new_secret()], "sub_x", "msg_verification_x", body)
        outcome = "ok"
    except integration.CallbackError as error:
        outcome = error.reason
    except Exception as error:  # noqa: BLE001 - any other failure is reported, not hidden
        outcome = type(error).__name__
    return time.monotonic() - started, outcome


# --- R3-1: the deadline is absolute ------------------------------------------------------------


@pytest.mark.parametrize("behaviour", [header_trickle, late_headers_then_stall, slow_tls],
                         ids=["header-trickle", "late-headers-then-stall", "slow-tls"])
def test_every_blocking_step_is_bounded_by_the_one_deadline(bad_server, behaviour) -> None:
    server, events = bad_server(behaviour)
    elapsed, outcome = attempt(server, events)
    assert elapsed < BUDGET + EPSILON, f"{behaviour.__name__} took {elapsed:.3f}s on a {BUDGET}s budget"
    assert outcome == "timeout"


def test_a_slow_send_is_bounded_by_the_deadline(bad_server) -> None:
    server, events = bad_server(never_read)
    elapsed, outcome = attempt(server, events, body=b"x" * (32 * 1024 * 1024))
    assert elapsed < BUDGET + EPSILON, f"the send took {elapsed:.3f}s on a {BUDGET}s budget"
    assert outcome == "timeout"


# --- R3-8: the read cap is exact --------------------------------------------------------------


def test_no_byte_past_the_cap_is_read_from_the_socket(bad_server, monkeypatch) -> None:
    server, events = bad_server(oversized)
    consumed = [0]
    real_recv, real_recv_into = ssl.SSLSocket.recv, ssl.SSLSocket.recv_into

    client = threading.current_thread()  # the server reads in its own threads; count the client only

    def recv(self, *args, **kwargs):
        data = real_recv(self, *args, **kwargs)
        if threading.current_thread() is client:
            consumed[0] += len(data)
        return data

    def recv_into(self, buffer, *args, **kwargs):
        count = real_recv_into(self, buffer, *args, **kwargs)
        if threading.current_thread() is client:
            consumed[0] += count
        return count

    monkeypatch.setattr(ssl.SSLSocket, "recv", recv)
    monkeypatch.setattr(ssl.SSLSocket, "recv_into", recv_into)
    attempt(server, events)
    assert 0 < consumed[0] <= integration.MAX_RESPONSE_BYTES, f"read {consumed[0]} bytes"


def test_a_small_cap_is_exact_too(bad_server, monkeypatch) -> None:
    # At 64 KiB, TLS record sizes can hide an over-read; a cap that isn't a multiple of a record can't.
    monkeypatch.setattr(integration, "MAX_RESPONSE_BYTES", 1000)
    server, events = bad_server(oversized)
    consumed = [0]
    real_recv = ssl.SSLSocket.recv
    client = threading.current_thread()

    def recv(self, *args, **kwargs):
        data = real_recv(self, *args, **kwargs)
        if threading.current_thread() is client:
            consumed[0] += len(data)
        return data

    monkeypatch.setattr(ssl.SSLSocket, "recv", recv)
    attempt(server, events)
    assert 0 < consumed[0] <= 1000, f"read {consumed[0]} bytes with a 1000-byte cap"


# --- R3-2: one bounded resolver pool ----------------------------------------------------------


def resolver_threads() -> int:
    return sum(thread.name.startswith("callback-resolve") and thread.is_alive() for thread in threading.enumerate())


def test_timed_out_lookups_never_exceed_the_pool_and_a_full_pool_fails_fast(tmp_path: Path) -> None:
    release = threading.Event()

    def stuck(host: str, port: int) -> list[str]:
        release.wait(10)
        return ["127.0.0.1"]

    config = parse({"host": {"public_url": "https://baton.example.test", "data_dir": str(tmp_path / "d")},
                    "roles": {"worker": {}}})
    events = integration.McpEvents(config, integration.Settings(callback_prefixes=(f"https://{NAME}/",),
                                                                timeout_seconds=0.02), resolver=stuck)
    before = resolver_threads()
    try:
        reasons = []
        for _ in range(8):
            try:
                events._resolve(NAME, 443)
            except integration.CallbackError as error:
                reasons.append(error.reason)
        assert resolver_threads() - before <= 4, f"{resolver_threads() - before} resolver threads alive"
        started = time.monotonic()
        try:
            events._resolve(NAME, 443)
            outcome = "resolved"
        except integration.CallbackError as error:
            outcome = error.reason
        assert (outcome, time.monotonic() - started < 0.01) == ("unreachable", True)
        assert set(reasons) <= {"timeout", "unreachable"}
    finally:
        release.set()
        deadline = time.monotonic() + 5
        while resolver_threads() > before + 4 and time.monotonic() < deadline:
            time.sleep(0.05)


# --- R3-3: wake state lives as long as the core wake ------------------------------------------


def test_a_restart_two_days_later_resends_only_unfinished_destinations(events_host, receiver, monkeypatch) -> None:
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 0.0)
    host = events_host(max_per_role=3, max_total=5)
    for path in ("/hooks/ok", "/hooks/big", "/hooks/flaky"):
        subscribe(host, "worker", receiver.url(path), new_secret())
    receiver.statuses = {"/hooks/ok": 204, "/hooks/big": 413, "/hooks/flaky": 503}
    send(host, "worker", "hello")
    dispatch(host)
    first = {r["path"]: json.loads(r["body"])["timestamp"] for r in receiver.events()}
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 48 * 3600 + 1)
    receiver.statuses = {}
    restarted = integration.McpEvents(host.config, host.integrations["openai_mcp_events"].settings,
                                      resolver=host.resolver)
    dispatch(host, restarted)
    later = [r for r in receiver.events()][3:]
    assert [r["path"] for r in later] == ["/hooks/flaky"]
    assert json.loads(later[0]["body"])["timestamp"] == first["/hooks/flaky"]


# --- R3-4: bounded key rotation ---------------------------------------------------------------


def test_alternating_refreshes_keep_one_previous_key_and_rotate_once_per_window(events_host, receiver) -> None:
    host = events_host()
    a, b = new_secret(), new_secret()
    assert "result" in subscribe(host, "worker", receiver.url(), a)
    assert "result" in subscribe(host, "worker", receiver.url(), b)
    refused = [("error" in subscribe(host, "worker", receiver.url(), key)) for key in (a, b, a, b, a, b)]
    assert refused[0] is True  # back to the old key inside the window is a second rotation
    [sub] = state(host)["subscriptions"].values()
    assert len(sub.get("previous_secrets", [])) <= 1
    send(host, "worker", "hello")
    dispatch(host)
    [event] = receiver.events()
    assert event["headers"]["webhook-signature"].count("v1,") <= 2


def test_a_rotation_after_the_window_replaces_the_previous_key(events_host, receiver, monkeypatch) -> None:
    host = events_host()
    a, b, c = new_secret(), new_secret(), new_secret()
    subscribe(host, "worker", receiver.url(), a)
    subscribe(host, "worker", receiver.url(), b)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 2 * 3600)
    assert "result" in subscribe(host, "worker", receiver.url(), c)
    [sub] = state(host)["subscriptions"].values()
    assert [p["secret"] for p in sub["previous_secrets"]] == [b]


@pytest.mark.parametrize("overlap", [0, 59, "60"])
def test_a_rotation_overlap_under_a_minute_is_refused(overlap: Any) -> None:
    with pytest.raises(ConfigError):
        integration.parse_settings({"enabled": True, "callback_prefixes": [f"https://{NAME}/hooks/"],
                                    "rotation_overlap_seconds": overlap})


# --- R3-6: liveness is rechecked before each attempt -----------------------------------------


class HookList(list):
    def __init__(self, hook: Callable[[dict], None]) -> None:
        super().__init__()
        self.hook = hook

    def append(self, item: dict) -> None:
        super().append(item)
        self.hook(item)


def test_a_revoke_during_the_first_callback_stops_the_second(events_host, receiver) -> None:
    host = events_host(oauth=True)
    host.tokens["planner"] = authorize(host)["access_token"]
    subscribe(host, "planner", receiver.url("/hooks/one"), new_secret())
    subscribe(host, "planner", receiver.url("/hooks/two"), new_secret())

    def revoke_on_first_event(request: dict) -> None:
        if json.loads(request["body"]).get("type") != "verification":
            auth.revoke_all(host.config.oauth_state_path)

    receiver.requests = HookList(revoke_on_first_event)
    send(host, "planner", "hello")
    dispatch(host)
    assert len(receiver.events()) == 1


# --- R3-7: the grant's own resource must match ------------------------------------------------


def test_a_grant_moved_to_another_resource_does_not_count(events_host, receiver) -> None:
    host = events_host(oauth=True)
    host.tokens["planner"] = authorize(host)["access_token"]
    subscribe(host, "planner", receiver.url("/hooks/p"), new_secret())
    store = auth.TokenStore.load(host.config.oauth_state_path)
    with store.mutation():
        for table in (store.access, store.refresh):
            for grant in table.values():
                grant["resource"] = "https://other.example.test/mcp"
    send(host, "planner", "hello")
    dispatch(host)
    assert receiver.events() == []
    assert connect(host.config.db_path).execute("SELECT COUNT(*) FROM wakes").fetchone()[0] == 1
