# ruff: noqa: F811 - the receiver and events_host fixtures are imported from test_openai_mcp_events
"""MCP Events, round 5: callback paths that round-trip byte for byte, delivery outcomes decided by status alone,
and a resolver pool that can't overshoot its size after a failed start.

Synthetic and offline, on loopback with the receiver's throwaway certificate.
"""

from __future__ import annotations

import gzip
import json
import socket
import ssl
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import Host
from test_openai_mcp_events import (
    NAME,
    WAKES,
    StubResolver,
    dispatch,
    events_host,  # noqa: F401 - fixture
    new_secret,
    receiver,  # noqa: F401 - fixture
    send,
    settings_table,
    state,
    subscribe,
)
from test_openai_mcp_events_r4 import BadServer, resolver_threads

from baton import wakes
from baton.config import parse
from baton.integrations import openai_mcp_events as integration
from baton.store import connect

# --- R4-1: only unreserved characters in callback paths ---------------------------------------


@pytest.mark.parametrize("path", ["/hooks/a", "/hooks/A1/b2", "/hooks/a-b_c.d/e~f", "/hooks/x/y/z", "/hooks/v1.2"])
def test_an_accepted_path_reaches_the_receiver_byte_for_byte(events_host, receiver, path: str) -> None:
    host = events_host()
    assert "result" in subscribe(host, "worker", receiver.url(path), new_secret())
    send(host, "worker", "hello")
    dispatch(host)
    assert [r["path"] for r in receiver.requests] == [path, path]  # the challenge and the event


@pytest.mark.parametrize("character", [";", "=", "@", ":", "+", "!", "$", "&", "'", "(", ")", "*", ","])
def test_a_reserved_character_is_refused_before_any_request(events_host, receiver, character: str) -> None:
    host = events_host()
    for path in (f"/hooks/a{character}b", f"/hooks/authorized{character}scope"):
        error = subscribe(host, "worker", receiver.url(path), new_secret()).get("error", {})
        assert error.get("code") == integration.CALLBACK_ENDPOINT_ERROR, (path, error)
        assert repr(character) in error.get("data", {}).get("detail", ""), f"the refusal should name {character!r}"
    assert receiver.requests == []
    assert state(host)["subscriptions"] == {}


# --- R4-2: delivery outcomes come from the status alone ---------------------------------------


MAX_REQUEST = 1024 * 1024


def read_request(tls: ssl.SSLSocket) -> tuple[str, bytes] | None:
    """The complete synthetic request, headers and Content-Length body, or None if the client hung up first.

    Reading the whole request before replying matters: closing a socket with unread bytes still queued makes the
    kernel send a reset instead of a clean close, and the client can then see the reset before the reply.
    Bounded in size and time, so a broken client can't stall the fixture.
    """
    tls.settimeout(5)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = tls.recv(4096)
        if not chunk or len(data) > MAX_REQUEST:
            return None
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    length = int(next((line.split(":", 1)[1] for line in lines if line.lower().startswith("content-length")), "0"))
    if length > MAX_REQUEST:
        return None
    while len(body) < length:
        chunk = tls.recv(min(4096, length - len(body)))
        if not chunk:
            return None
        body += chunk
    return lines[0].split(" ")[1], body


def gzip_events(statuses: dict[str, int], seen: list[tuple[str, str]]):
    """A callback that echoes challenges with identity encoding and answers events with a gzip body."""

    def behaviour(context: ssl.SSLContext, conn: socket.socket) -> None:
        tls = context.wrap_socket(conn, server_side=True)
        request = read_request(tls)
        if request is None:
            return
        target, body = request
        payload = json.loads(body)
        if payload.get("type") == "verification":
            seen.append((target, "challenge"))
            answer = json.dumps({"challenge": payload["challenge"]}).encode()
            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(answer) + answer)
        else:
            seen.append((target, "event"))
            answer = gzip.compress(b'{"ok": true}' * 50)
            status = statuses.get(target, 200)
            tls.sendall(b"HTTP/1.1 %d X\r\nContent-Encoding: gzip\r\nContent-Length: %d\r\n\r\n" % (status, len(answer))
                        + answer)
        tls.close()

    return behaviour


@pytest.fixture
def gzip_host(start_host, receiver, tmp_path: Path):
    servers: list[BadServer] = []

    def start(statuses: dict[str, int], seen: list) -> Host:
        server = BadServer(receiver.ca_file, tmp_path / "receiver-key.pem", gzip_events(statuses, seen))
        servers.append(server)
        place = type("Place", (), {"port": server.port, "ca_file": receiver.ca_file})

        def build(config: Any) -> dict[str, Any]:
            settings = integration.parse_settings(config.integrations["openai_mcp_events"])
            settings = replace(settings, allow_private_callbacks=True, ca_file=str(receiver.ca_file))
            return {"openai_mcp_events": integration.McpEvents(config, settings, resolver=StubResolver())}

        host = start_host(extra=WAKES + settings_table(place, max_per_role=3, max_total=5), integrations=build)
        host.url = lambda path: f"https://{NAME}:{server.port}{path}"  # type: ignore[attr-defined]
        return host

    yield start
    for server in servers:
        server.close()


def test_gzip_bodied_delivery_answers_are_judged_by_status_alone(gzip_host, monkeypatch) -> None:
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 0.0)
    seen: list[tuple[str, str]] = []
    host = gzip_host({"/hooks/ok": 200, "/hooks/gone": 410, "/hooks/big": 413}, seen)
    for path in ("/hooks/ok", "/hooks/gone", "/hooks/big"):
        assert "result" in subscribe(host, "worker", host.url(path), new_secret())
    send(host, "worker", "hello")
    for _ in range(3):
        dispatch(host)
    events = [target for target, kind in seen if kind == "event"]
    assert sorted(events) == ["/hooks/big", "/hooks/gone", "/hooks/ok"]  # each exactly once: no retries
    subs = {sub["url"].rsplit("/", 1)[1]: sub for sub in state(host)["subscriptions"].values()}
    assert (subs["gone"]["active"], subs["ok"]["active"], subs["big"]["active"]) == (False, True, True)
    assert sorted(w["status"] for w in wakes.recent(connect(host.config.db_path))) \
        == ["delivered", "scheduled", "scheduled"]  # the wake, and the two reminders it sets


def test_a_gzip_challenge_answer_is_still_refused(bad_server_gzip_challenge) -> None:
    server, events = bad_server_gzip_challenge
    try:
        events._post(f"https://{NAME}:{server.port}/hooks/a", [new_secret()], "sub_x", "msg_verification_x", b"{}")
        outcome = "ok"
    except integration.CallbackError as error:
        outcome = error.reason
    assert outcome == "encoded_response"


@pytest.fixture
def bad_server_gzip_challenge(receiver, tmp_path: Path):
    def behaviour(context: ssl.SSLContext, conn: socket.socket) -> None:
        tls = context.wrap_socket(conn, server_side=True)
        if read_request(tls) is None:
            return
        answer = gzip.compress(b'{"challenge": "x"}')
        tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: %d\r\n\r\n" % len(answer) + answer)
        tls.close()

    server = BadServer(receiver.ca_file, tmp_path / "receiver-key.pem", behaviour)
    config = parse({"host": {"public_url": "https://baton.example.test", "data_dir": str(tmp_path / "data")},
                    "roles": {"worker": {}}})
    settings = integration.Settings(callback_prefixes=(f"https://{NAME}:{server.port}/hooks/",), timeout_seconds=2,
                                    allow_private_callbacks=True, ca_file=str(receiver.ca_file))
    yield server, integration.McpEvents(config, settings, resolver=StubResolver())
    server.close()


# --- R4-3: a failed worker start can't push the pool past its size ------------------------------


def test_repeated_partial_startup_failures_never_exceed_the_pool_size(monkeypatch) -> None:
    real_start = threading.Thread.start
    calls = [0]

    def flaky_start(self) -> None:
        if self.name.startswith("callback-resolve"):
            calls[0] += 1
            if calls[0] % 2 == 0:  # every second worker start fails
                raise RuntimeError("can't start new thread")
        real_start(self)

    before = resolver_threads()
    pool = integration.ResolverPool(workers=4, queue_size=4)
    monkeypatch.setattr(threading.Thread, "start", flaky_start)
    outcomes = []
    for _ in range(6):
        try:
            pool.resolve(lambda host, port: ["127.0.0.1"], NAME, 443, deadline=_soon())
            outcomes.append("resolved")
        except integration.CallbackError as error:
            outcomes.append(error.reason)
        except Exception as error:  # noqa: BLE001 - a raw start failure is a missing guard, reported as such
            outcomes.append(type(error).__name__)
        assert resolver_threads() - before <= 4, f"{resolver_threads() - before} workers after {outcomes}"
    assert set(outcomes) <= {"unreachable", "resolved"}, outcomes
    assert "resolved" in outcomes


def _soon() -> float:
    return time.monotonic() + 2
