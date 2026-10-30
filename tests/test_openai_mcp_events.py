"""The MCP Events integration, offline: a loopback HTTPS receiver, a stub resolver and synthetic secrets.

Callbacks go to `https://callback.test:<port>/...`, which the stub resolver maps
to 127.0.0.1. Loopback needs the test-only `allow_private_callbacks`; the
address rules themselves are tested with it off.
"""

from __future__ import annotations

import base64
import datetime as dt
import gzip
import hashlib
import hmac
import json
import logging
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import Host
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from baton import messages, wakes
from baton.config import ConfigError, parse
from baton.integrations import openai_mcp_events as integration
from baton.store import connect

NAME = "callback.test"
EVENT = integration.EVENT_NAME


def new_secret() -> str:
    return "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()


def reference_verify(secret: str, headers: dict[str, str], body: bytes) -> bool:
    """Standard Webhooks verification, written from the spec independently of the code under test."""
    key = base64.b64decode(secret.split("_", 1)[1])
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    fresh = abs(time.time() - int(headers["webhook-timestamp"])) < 300
    return fresh and any(part == f"v1,{expected}" for part in headers["webhook-signature"].split(" "))


class Receiver:
    """An HTTPS callback on loopback.

    `mode` picks the answer to a challenge: echo, wrong, error, error-echo, redirect, gone, trickle (a correct
    echo, sent slowly) or gzip (a correct echo, gzip-encoded). `statuses` maps a path to the status its
    events get, overriding the mode.
    """

    def __init__(self, tmp_path: Path) -> None:
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NAME)])
        now = dt.datetime.now(dt.UTC)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=1))
                .not_valid_after(now + dt.timedelta(hours=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(NAME)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        self.ca_file = tmp_path / "receiver-cert.pem"
        self.ca_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_file = tmp_path / "receiver-key.pem"
        key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        self.mode = "echo"
        self.statuses: dict[str, int] = {}
        self.requests: list[dict[str, Any]] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                receiver.requests.append({"path": self.path, "method": "GET", "headers": {}, "body": b"{}"})
                self.send_response(405)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                receiver.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                          "body": body, "sni_host": self.headers.get("Host")})
                payload = json.loads(body)
                if receiver.mode == "redirect" and self.path != "/hooks/elsewhere":
                    # 307 keeps the method and body, so a client that followed it would be verified there.
                    self.send_response(307)
                    self.send_header("Location", "/hooks/elsewhere")
                    self.end_headers()
                    return
                status = {"error": 500, "error-echo": 500, "gone": 410}.get(receiver.mode, 200)
                verification = payload.get("type") == "verification"
                if not verification and self.path in receiver.statuses:
                    status = receiver.statuses[self.path]
                answer = b""
                if verification and receiver.mode != "error":
                    echoed = payload["challenge"] if receiver.mode in ("echo", "error-echo", "redirect", "trickle",
                                                                       "gzip") else "not-the-challenge"
                    answer = json.dumps({"challenge": echoed}).encode()
                if verification and receiver.mode == "gzip":
                    answer = gzip.compress(answer)
                self.send_response(status)
                self.send_header("Content-Length", str(len(answer)))
                if verification and receiver.mode == "gzip":
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                if verification and receiver.mode == "trickle":
                    for byte in answer:  # one byte at a time, each well inside any per-read timeout
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.05)
                    return
                self.wfile.write(answer)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.ca_file, key_file)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str = "/hooks/a") -> str:
        return f"https://{NAME}:{self.port}{path}"

    def events(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if json.loads(r["body"]).get("type") != "verification"]


class StubResolver:
    def __init__(self) -> None:
        self.answers: dict[str, list[str]] = {NAME: ["127.0.0.1"]}
        self.calls: list[str] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append(host)
        if host not in self.answers:
            raise socket.gaierror("no such name")
        return list(self.answers[host])


@pytest.fixture
def receiver(tmp_path: Path) -> Receiver:
    receiver = Receiver(tmp_path)
    yield receiver
    receiver.server.shutdown()


TEST_ONLY = ("allow_private_callbacks", "ca_file")


def settings_table(receiver: Receiver, **overrides: Any) -> str:
    """The production settings, as TOML. Test-only settings never go here; see `events_host`."""
    values = {"enabled": True, "callback_prefixes": [f"https://{NAME}:{receiver.port}/hooks/"],
              "timeout_seconds": 5, "max_per_role": 2, "max_total": 3, "min_ttl_seconds": 1, **overrides}
    lines = [f"{key} = {json.dumps(value)}" for key, value in values.items()]
    return "[integrations.openai_mcp_events]\n" + "\n".join(lines) + "\n"


WAKES = ('[roles.planner.wake]\nintegration = "openai_mcp_events"\n'
         '[roles.worker.wake]\nintegration = "openai_mcp_events"\n')


@pytest.fixture
def events_host(start_host, receiver: Receiver):
    resolver = StubResolver()

    def start(oauth: bool = False, **overrides: Any) -> Host:
        # Test-only settings are injected through the constructor, never parsed from the config.
        test_only = {"allow_private_callbacks": True, "ca_file": str(receiver.ca_file),
                     **{key: overrides.pop(key) for key in TEST_ONLY if key in overrides}}

        def build(config: Any) -> dict[str, Any]:
            settings = integration.parse_settings(config.integrations["openai_mcp_events"])
            return {"openai_mcp_events": integration.McpEvents(config, replace(settings, **test_only))}

        host = start_host(extra=WAKES + settings_table(receiver, **overrides), integrations=build, oauth=oauth)
        host.integrations["openai_mcp_events"].resolver = resolver
        host.resolver = resolver  # type: ignore[attr-defined]
        return host

    return start


def rpc(host: Host, role: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "example-client", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}
    response = httpx.post(host.mcp_url, headers={
        "Authorization": f"Bearer {host.tokens[role]}", "Accept": "application/json, text/event-stream",
        "Mcp-Protocol-Version": "2026-07-28", "Mcp-Method": method,
    }, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": meta}}, timeout=30)
    return response.json()


def subscribe(host: Host, role: str, url: str, secret: str, **extra: Any) -> dict[str, Any]:
    return rpc(host, role, "events/subscribe", {"name": EVENT, "arguments": {},
                                                "delivery": {"mode": "webhook", "url": url, "secret": secret},
                                                "cursor": None, **extra})


def state(host: Host) -> dict[str, Any]:
    path = host.config.data_dir / integration.STATE_FILE
    return json.loads(path.read_text()) if path.exists() else {"subscriptions": {}}


def dispatch(host: Host, events: Any = None) -> wakes.Dispatcher:
    dispatcher = wakes.Dispatcher(host.config, integrations={"openai_mcp_events": events or
                                                             host.integrations["openai_mcp_events"]})
    dispatcher.pass_once()
    return dispatcher


def send(host: Host, to: str, text: str) -> None:
    conn = connect(host.config.db_path)
    try:
        messages.send(conn, sender="planner" if to == "worker" else "worker", recipient=to, text=text)
    finally:
        conn.close()


# --- the contract ------------------------------------------------------------------------


def test_discover_advertises_events_and_list_has_the_contract_shape(events_host) -> None:
    host = events_host()
    discovered = rpc(host, "worker", "server/discover", {})["result"]
    listed = rpc(host, "worker", "events/list", {})["result"]
    assert discovered["capabilities"]["events"] == {}
    assert (listed["events"], listed["nextCursor"]) == ([integration.EVENT_DEFINITION], None)
    assert set(integration.EVENT_DEFINITION["payloadSchema"]["properties"]) == set(integration.WAKE_FIELDS)


def test_subscribe_verifies_with_a_signed_challenge_then_stores_privately(events_host, receiver) -> None:
    host = events_host()
    secret = new_secret()
    result = subscribe(host, "worker", receiver.url(), secret)["result"]
    [challenge] = receiver.requests
    assert json.loads(challenge["body"])["type"] == "verification"
    assert reference_verify(secret, challenge["headers"], challenge["body"])
    assert challenge["headers"]["x-mcp-subscription-id"] == result["id"]
    assert challenge["headers"]["host"] == f"{NAME}:{receiver.port}"
    assert (result["cursor"], result["truncated"]) == (None, False)
    assert dt.datetime.fromisoformat(result["refreshBefore"]) > dt.datetime.now(dt.UTC)
    [stored] = state(host)["subscriptions"].values()
    assert (stored["role"], stored["url"], stored["active"]) == ("worker", receiver.url(), True)
    assert (host.config.data_dir / integration.STATE_FILE).stat().st_mode & 0o777 == 0o600
    # Subscribing again with the same identity refreshes the same subscription.
    assert subscribe(host, "worker", receiver.url(), secret)["result"]["id"] == result["id"]
    assert len(state(host)["subscriptions"]) == 1


@pytest.mark.parametrize("mode", ["wrong", "error", "error-echo", "redirect"])
def test_a_failed_challenge_is_refused_and_keeps_no_state(events_host, receiver, mode: str) -> None:
    host = events_host()
    receiver.mode = mode
    error = subscribe(host, "worker", receiver.url(), new_secret())["error"]
    assert (error["code"], error["data"]["reason"]) == (integration.CALLBACK_ENDPOINT_ERROR, "challenge_failed")
    assert state(host)["subscriptions"] == {}
    assert len(receiver.requests) == 1  # a redirect is not followed


@pytest.mark.parametrize(("url", "reason"), [
    ("http://callback.test:{port}/hooks/a", "invalid_url"),
    ("https://someone@example.test:{port}/hooks/a", "invalid_url"),  # any userinfo is refused
    ("https://elsewhere.test:{port}/hooks/a", "not_allowed"),
    ("https://callback.test:1/hooks/a", "not_allowed"),
    ("https://callback.test:{port}/other", "not_allowed"),
    ("https://callback.test:{port}/hooksevil", "not_allowed"),
])
def test_callbacks_off_the_allowlist_are_refused_without_any_request(events_host, receiver, url, reason) -> None:
    host = events_host()
    error = subscribe(host, "worker", url.format(port=receiver.port), new_secret())["error"]
    assert (error["code"], error["data"]["reason"]) == (integration.CALLBACK_ENDPOINT_ERROR, reason)
    assert receiver.requests == [] and host.resolver.calls == []
    assert state(host)["subscriptions"] == {}


@pytest.mark.parametrize("secret", ["no-prefix", "whsec_short", "whsec_" + base64.b64encode(b"x" * 65).decode(),
                                    "whsec_not*base64!"])
def test_a_malformed_secret_is_refused(events_host, receiver, secret: str) -> None:
    host = events_host()
    assert subscribe(host, "worker", receiver.url(), secret)["error"]["code"] == integration.INVALID_PARAMS
    assert receiver.requests == []


@pytest.mark.parametrize("answers", [["127.0.0.1"], ["10.0.0.7"], ["192.168.1.20"], ["172.16.0.3"],
                                     ["169.254.169.254"], ["::1"], ["fe80::1"], ["fd00::1"], ["::ffff:127.0.0.1"],
                                     ["198.18.0.1"], ["0.0.0.0"], ["93.184.216.34", "10.0.0.7"]])
def test_non_global_or_mixed_answers_are_refused_with_the_flag_off(answers: list[str]) -> None:
    config = parse({"host": {"public_url": "https://baton.example.test"}, "roles": {"worker": {}}})
    resolver = StubResolver()
    resolver.answers[NAME] = answers
    events = integration.McpEvents(config, integration.Settings(callback_prefixes=(f"https://{NAME}/",)),
                                   resolver=resolver)
    with pytest.raises(integration.CallbackError, match="private_address"):
        events._resolve(NAME, 443)
    resolver.answers[NAME] = ["93.184.216.34"]
    assert events._resolve(NAME, 443) == ["93.184.216.34"]


def test_subscribe_to_a_private_address_is_refused_with_the_flag_off(events_host, receiver) -> None:
    host = events_host(allow_private_callbacks=False)
    error = subscribe(host, "worker", receiver.url(), new_secret())["error"]
    assert error["data"]["reason"] == "private_address"
    assert receiver.requests == []


# --- delivery ------------------------------------------------------------------------------


def test_a_message_wakes_the_subscribed_role_with_a_signed_content_free_event(events_host, receiver) -> None:
    host = events_host()
    secret = new_secret()
    sub_id = subscribe(host, "worker", receiver.url(), secret)["result"]["id"]
    send(host, "worker", "the private message text")
    dispatch(host)
    [event] = receiver.events()
    body = json.loads(event["body"])
    assert reference_verify(secret, event["headers"], event["body"])
    assert event["headers"]["webhook-id"] == body["eventId"]
    assert event["headers"]["x-mcp-subscription-id"] == sub_id
    assert (body["name"], body["cursor"]) == (EVENT, None)
    assert body["data"] == {"role": "worker", "reason": "message", "first_unread_seq": 1}
    assert b"private message" not in event["body"]
    assert sorted(w["status"] for w in wakes.recent(connect(host.config.db_path))) \
        == ["delivered", "scheduled", "scheduled"]  # the wake, and the two reminders it sets


def test_pinning_the_http_client_never_resolves_the_callback_name(events_host, receiver, monkeypatch) -> None:
    host = events_host()
    subscribe(host, "worker", receiver.url(), new_secret())
    seen: list[str] = []
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", lambda host_, *a, **k: seen.append(str(host_)) or real(host_, *a, **k))
    host.resolver.calls.clear()
    send(host, "worker", "hello")
    dispatch(host)
    assert len(receiver.events()) == 1
    assert host.resolver.calls == [NAME]  # resolved exactly once for the one request
    assert NAME not in seen  # the connection went to the validated address, not a fresh lookup


def test_a_name_repointed_at_a_private_address_after_subscribe_is_refused(events_host, receiver) -> None:
    host = events_host()
    subscribe(host, "worker", receiver.url(), new_secret())
    host.resolver.answers[NAME] = ["10.0.0.9"]
    strict = host.integrations["openai_mcp_events"].with_settings(allow_private_callbacks=False)
    send(host, "worker", "hello")
    dispatch(host, strict)
    assert receiver.events() == []
    [wake] = wakes.recent(connect(host.config.db_path))
    assert (wake["status"], wake["attempts"]) == ("pending", 1)  # the core retries later, then marks it failed


def test_wakes_never_cross_roles_and_roles_come_only_from_credentials(events_host, receiver) -> None:
    host = events_host()
    subscribe(host, "worker", receiver.url("/hooks/worker"), new_secret())
    subscribe(host, "planner", receiver.url("/hooks/planner"), new_secret())
    send(host, "worker", "for the worker")
    dispatch(host)
    assert [r["path"] for r in receiver.events()] == ["/hooks/worker"]
    roles = {sub["url"].rsplit("/", 1)[1]: sub["role"] for sub in state(host)["subscriptions"].values()}
    assert roles == {"worker": "worker", "planner": "planner"}
    forged = rpc(host, "worker", "events/subscribe", {
        "name": EVENT, "arguments": {"role": "planner"},
        "delivery": {"mode": "webhook", "url": receiver.url("/hooks/x"), "secret": new_secret()}})
    assert forged["error"]["code"] == integration.INVALID_PARAMS


def test_only_the_owning_role_can_unsubscribe(events_host, receiver) -> None:
    host = events_host()
    subscribe(host, "worker", receiver.url(), new_secret())
    params = {"name": EVENT, "arguments": {}, "delivery": {"mode": "webhook", "url": receiver.url()}}
    assert "error" not in rpc(host, "planner", "events/unsubscribe", params)
    assert len(state(host)["subscriptions"]) == 1
    result = rpc(host, "worker", "events/unsubscribe", params)["result"]
    assert {key: value for key, value in result.items() if key not in ("_meta", "resultType")} == {}
    assert state(host)["subscriptions"] == {}


def test_caps_per_role_and_overall(events_host, receiver) -> None:
    host = events_host(max_per_role=1, max_total=2)
    first = subscribe(host, "worker", receiver.url("/hooks/1"), new_secret())["result"]["id"]
    assert subscribe(host, "worker", receiver.url("/hooks/1"), new_secret())["result"]["id"] == first  # refresh
    assert subscribe(host, "worker", receiver.url("/hooks/2"), new_secret())["error"]["code"] == \
        integration.INVALID_PARAMS
    subscribe(host, "planner", receiver.url("/hooks/3"), new_secret())
    host.integrations["openai_mcp_events"].settings = replace(host.integrations["openai_mcp_events"].settings,
                                                              max_per_role=5)
    requests_before = len(receiver.requests)
    assert subscribe(host, "planner", receiver.url("/hooks/4"), new_secret())["error"]["code"] == \
        integration.INVALID_PARAMS
    assert len(receiver.requests) == requests_before  # refused before any outbound request
    assert len(state(host)["subscriptions"]) == 2


def test_expiry_bounds_and_the_end_of_delivery(events_host, receiver) -> None:
    host = events_host(default_ttl_seconds=600, max_ttl_seconds=3600)
    no_expiry = subscribe(host, "planner", receiver.url("/hooks/p"), new_secret(), ttlMs=None)["result"]
    granted = dt.datetime.fromisoformat(no_expiry["refreshBefore"]) - dt.datetime.now(dt.UTC)
    assert dt.timedelta(minutes=59) < granted <= dt.timedelta(hours=1)  # never unbounded
    subscribe(host, "worker", receiver.url(), new_secret(), ttlMs=1000)
    time.sleep(1.2)
    send(host, "worker", "too late")
    dispatch(host)
    assert receiver.events() == []


def test_a_revoked_credential_ends_delivery(events_host, receiver) -> None:
    host = events_host()
    subscribe(host, "worker", receiver.url(), new_secret())
    revoked = replace(host.config, tokens=tuple(t for t in host.config.tokens if t.role != "worker"))
    events = integration.McpEvents(revoked, host.integrations["openai_mcp_events"].settings,
                                   resolver=host.resolver)
    send(host, "worker", "hello")
    dispatch(host, events)
    assert receiver.events() == []


def test_a_410_makes_the_subscription_inactive(events_host, receiver) -> None:
    host = events_host()
    subscribe(host, "worker", receiver.url(), new_secret())
    receiver.mode = "gone"
    send(host, "worker", "hello")
    dispatch(host)
    [sub] = state(host)["subscriptions"].values()
    assert sub["active"] is False


def test_the_secret_appears_in_no_log_audit_line_wake_or_result(events_host, receiver, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    host = events_host()
    secret = new_secret()
    result = subscribe(host, "worker", receiver.url(), secret)
    send(host, "worker", "hello")
    dispatch(host)
    receiver.mode = "error"
    send(host, "worker", "again")
    messages.read(connect(host.config.db_path), "worker")
    send(host, "worker", "and again")
    dispatch(host)
    key = secret.removeprefix("whsec_")
    places = {
        "result": json.dumps(result),
        "logs": caplog.text,
        "audit": host.config.audit_path.read_text() if host.config.audit_path.exists() else "",
        "wakes": json.dumps([dict(r) for r in connect(host.config.db_path).execute("SELECT * FROM wakes")]),
        "events": json.dumps([r["body"].decode() for r in receiver.requests]),
    }
    assert {name: key in text for name, text in places.items()} == {name: False for name in places}


# --- configuration and core isolation ---------------------------------------------------------


@pytest.mark.parametrize(("table", "message"), [
    ({"enabled": True}, "callback_prefixes"),
    ({"enabled": True, "callback_prefixes": ["http://hooks.example.test/"]}, "https"),
    ({"enabled": True, "callback_prefixes": ["https://*.example.test/"]}, "wildcard"),
    ({"enabled": True, "callback_prefixes": ["https://hooks.example.test/"], "surprise": 1}, "unknown"),
])
def test_integration_settings_are_validated(table: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        integration.parse_settings(table)


def test_a_role_may_only_name_an_enabled_integration() -> None:
    raw = {"host": {"public_url": "https://baton.example.test"},
           "roles": {"worker": {"wake": {"integration": "openai_mcp_events"}}}}
    with pytest.raises(ConfigError, match="not enabled"):
        parse(raw)
    with pytest.raises(ConfigError, match="only one"):
        parse({**raw, "roles": {"worker": {"wake": {"integration": "x", "command": ["true"]}}}})


def test_the_core_never_imports_the_integration_unless_enabled(tmp_path: Path) -> None:
    script = (
        "import sys; from pathlib import Path; from baton.config import parse; from baton.app import create_app\n"
        f"config = parse({{'host': {{'public_url': 'http://127.0.0.1:1', 'data_dir': {str(tmp_path / 'd')!r}}},"
        " 'roles': {'worker': {}}})\n"
        "create_app(config)\n"
        "import baton.cli, baton.wakes\n"
        "print(sorted(m for m in sys.modules if m.startswith('baton.integrations.')))\n"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"
