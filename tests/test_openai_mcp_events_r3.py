# ruff: noqa: F811 - the receiver and events_host fixtures are imported from test_openai_mcp_events
"""MCP Events, round 3: canonical callbacks, credential authority, per-destination outcomes, deadlines,
config hygiene and the contract details (413, secret rotation, a stable occurrence time).

Synthetic and offline, reusing the loopback receiver and stub resolver from test_openai_mcp_events.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import replace
from typing import Any

import httpx
import pytest
from test_auth import authorize
from test_openai_mcp_events import (
    EVENT,
    NAME,
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


def signatures_valid_for(secret: str, headers: dict[str, str], body: bytes) -> bool:
    key = base64.b64decode(secret.split("_", 1)[1])
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
    expected = "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return expected in headers["webhook-signature"].split(" ")


def wake_statuses(host) -> list[str]:
    return [w["status"] for w in wakes.recent(connect(host.config.db_path))]


def events_of(host, events: Any = None) -> Any:
    return events or host.integrations["openai_mcp_events"]


# --- R2-1: one canonical callback, checked and sent as the same string ------------------------------


@pytest.mark.parametrize("path", [
    "/hooks/../outside", "/hooks/./a", "/hooks/%2e%2e/outside", "/hooks/%2E%2E/outside", "/hooks/a%2fb",
    "/hooks/a%2Fb", "/hooks/a%5cb", "/hooks/a%5Cb", "/hooks/a\\b", "/hooks//a", "/hooks/a?x=1", "/hooks/a#f",
    "/hooks/%252e%252e/outside",
])
def test_non_canonical_callbacks_are_refused_before_any_request(events_host, receiver, path: str) -> None:
    host = events_host()
    error = subscribe(host, "worker", receiver.url(path), new_secret()).get("error", {})
    assert error.get("code") == integration.CALLBACK_ENDPOINT_ERROR, error
    assert receiver.requests == []
    assert state(host)["subscriptions"] == {}


@pytest.mark.parametrize("url", ["HTTPS://callback.test:{port}/hooks/a", "https://CALLBACK.test:{port}/hooks/a"])
def test_a_url_that_is_not_already_canonical_is_refused(events_host, receiver, url: str) -> None:
    host = events_host()
    error = subscribe(host, "worker", url.format(port=receiver.port), new_secret()).get("error", {})
    assert error.get("code") == integration.CALLBACK_ENDPOINT_ERROR, error
    assert receiver.requests == []


def test_the_path_on_the_wire_is_exactly_the_one_checked_and_stored(events_host, receiver) -> None:
    host = events_host()
    url = receiver.url("/hooks/a-b_c.d/e~f")
    assert "result" in subscribe(host, "worker", url, new_secret())
    send(host, "worker", "hello")
    dispatch(host)
    assert [r["path"] for r in receiver.requests] == ["/hooks/a-b_c.d/e~f", "/hooks/a-b_c.d/e~f"]
    [stored] = state(host)["subscriptions"].values()
    assert stored["url"] == url


# --- R2-2: delivery needs the subscribing credential's continuing authority -------------------------


def test_a_static_token_replaced_under_the_same_name_ends_delivery(events_host, receiver) -> None:
    host = events_host()
    subscribe(host, "worker", receiver.url(), new_secret())
    _, other_digest = auth.new_token()
    replaced = replace(host.config, tokens=tuple(replace(t, sha256=other_digest) if t.role == "worker" else t
                                                 for t in host.config.tokens))
    send(host, "worker", "hello")
    dispatch(host, integration.McpEvents(replaced, events_of(host).settings, resolver=host.resolver))
    assert receiver.events() == []


def test_an_oauth_role_removed_from_the_config_ends_delivery(events_host, receiver) -> None:
    host = events_host(oauth=True)
    tokens = authorize(host)
    host.tokens["planner"] = tokens["access_token"]
    assert "result" in subscribe(host, "planner", receiver.url("/hooks/p"), new_secret())
    removed = replace(host.config, oauth=replace(host.config.oauth, roles=()))
    send(host, "planner", "hello")
    dispatch(host, integration.McpEvents(removed, events_of(host).settings, resolver=host.resolver))
    assert receiver.events() == []


def test_revoke_all_ends_delivery(events_host, receiver) -> None:
    host = events_host(oauth=True)
    host.tokens["planner"] = authorize(host)["access_token"]
    subscribe(host, "planner", receiver.url("/hooks/p"), new_secret())
    auth.revoke_all(host.config.oauth_state_path)
    send(host, "planner", "hello")
    dispatch(host)
    assert receiver.events() == []


def test_a_different_grant_for_the_same_client_does_not_keep_delivery_live(events_host, receiver) -> None:
    host = events_host(oauth=True)
    host.tokens["planner"] = authorize(host)["access_token"]
    subscribe(host, "planner", receiver.url("/hooks/p"), new_secret())
    # The client's grant ends, and the same client holds a different grant instead.
    store = auth.TokenStore.load(host.config.oauth_state_path)
    with store.mutation():
        for table in (store.access, store.refresh):
            for key, grant in list(table.items()):
                table[key + "-other"] = {**grant, "grant": "some-other-grant"}
                del table[key]
    send(host, "planner", "hello")
    dispatch(host)
    assert receiver.events() == []


def test_a_normal_oauth_refresh_keeps_delivery_live(events_host, receiver) -> None:
    host = events_host(oauth=True)
    tokens = authorize(host)
    host.tokens["planner"] = tokens["access_token"]
    subscribe(host, "planner", receiver.url("/hooks/p"), new_secret())
    refreshed = httpx.post(host.base + "/token", data={"grant_type": "refresh_token", "client_id": tokens["client_id"],
                                                       "refresh_token": tokens["refresh_token"]}).json()
    assert "access_token" in refreshed
    # Let the first access token lapse, so only the refreshed grant remains.
    store = auth.TokenStore.load(host.config.oauth_state_path)
    with store.mutation():
        store.access.pop(auth.token_digest(tokens["access_token"]), None)
    send(host, "planner", "hello")
    dispatch(host)
    assert len(receiver.events()) == 1


# --- R2-3 and R2-6: an outcome per destination; 410 and 413 are final -------------------------------


def test_a_mixed_outcome_retries_only_the_failed_destination(events_host, receiver, monkeypatch) -> None:
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 0.0)
    host = events_host()
    subscribe(host, "worker", receiver.url("/hooks/ok"), new_secret())
    subscribe(host, "worker", receiver.url("/hooks/flaky"), new_secret())
    receiver.statuses = {"/hooks/ok": 204, "/hooks/flaky": 503}
    send(host, "worker", "hello")
    dispatch(host)
    assert wake_statuses(host) == ["pending"]
    receiver.statuses["/hooks/flaky"] = 204
    dispatch(host)
    # The 204 destination got the event once; only the 503 one was tried again.
    assert sorted(r["path"] for r in receiver.events()) == ["/hooks/flaky", "/hooks/flaky", "/hooks/ok"]
    assert wake_statuses(host) == ["delivered"]


@pytest.mark.parametrize("final", [410, 413])
def test_a_410_or_413_stops_that_destination(events_host, receiver, monkeypatch, final: int) -> None:
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 0.0)
    host = events_host()
    subscribe(host, "worker", receiver.url("/hooks/final"), new_secret())
    subscribe(host, "worker", receiver.url("/hooks/flaky"), new_secret())
    receiver.statuses = {"/hooks/final": final, "/hooks/flaky": 503}
    send(host, "worker", "hello")
    for _ in range(3):
        dispatch(host)
    paths = [r["path"] for r in receiver.events()]
    assert paths.count("/hooks/final") == 1
    assert paths.count("/hooks/flaky") == 3


def test_the_occurrence_time_is_stable_across_retries(events_host, receiver, monkeypatch) -> None:
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 0.0)
    host = events_host()
    subscribe(host, "worker", receiver.url("/hooks/flaky"), new_secret())
    receiver.statuses = {"/hooks/flaky": 503}
    send(host, "worker", "hello")
    dispatch(host)
    time.sleep(1.1)
    receiver.statuses = {}
    dispatch(host)
    first, second = receiver.events()
    assert json.loads(first["body"])["timestamp"] == json.loads(second["body"])["timestamp"]
    assert first["headers"]["webhook-timestamp"] != second["headers"]["webhook-timestamp"]
    assert first["headers"]["webhook-id"] == second["headers"]["webhook-id"]


def test_a_refreshed_secret_signs_with_both_keys_during_the_overlap(events_host, receiver) -> None:
    host = events_host()
    old, new = new_secret(), new_secret()
    subscribe(host, "worker", receiver.url(), old)
    subscribe(host, "worker", receiver.url(), new)
    send(host, "worker", "hello")
    dispatch(host)
    [event] = receiver.events()
    assert signatures_valid_for(new, event["headers"], event["body"])
    assert signatures_valid_for(old, event["headers"], event["body"])
    assert len(event["headers"]["webhook-signature"].split(" ")) == 2


def test_after_the_overlap_only_the_new_secret_signs(events_host, receiver) -> None:
    host = events_host()
    old, new = new_secret(), new_secret()
    subscribe(host, "worker", receiver.url(), old)
    subscribe(host, "worker", receiver.url(), new)
    path = host.config.data_dir / integration.STATE_FILE
    saved = json.loads(path.read_text())
    for sub in saved["subscriptions"].values():
        for previous in sub.get("previous_secrets", []):
            previous["until"] = 0
    path.write_text(json.dumps(saved))
    send(host, "worker", "hello")
    dispatch(host)
    [event] = receiver.events()
    assert signatures_valid_for(new, event["headers"], event["body"])
    assert not signatures_valid_for(old, event["headers"], event["body"])


def test_a_refused_refresh_keeps_the_old_secret_alone(events_host, receiver) -> None:
    host = events_host()
    old = new_secret()
    subscribe(host, "worker", receiver.url(), old)
    receiver.mode = "wrong"
    assert "error" in subscribe(host, "worker", receiver.url(), new_secret())
    receiver.mode = "echo"
    send(host, "worker", "hello")
    dispatch(host)
    [event] = receiver.events()
    assert event["headers"]["webhook-signature"].count("v1,") == 1
    assert signatures_valid_for(old, event["headers"], event["body"])


# --- R2-4: one deadline per attempt; no decoding --------------------------------------------------


def test_a_response_trickling_past_the_deadline_fails(events_host, receiver) -> None:
    host = events_host(timeout_seconds=1)
    receiver.mode = "trickle"  # about 2.5 s for the whole echo
    started = time.monotonic()
    error = subscribe(host, "worker", receiver.url(), new_secret()).get("error", {})
    assert time.monotonic() - started < 2.0
    assert error.get("data", {}).get("reason") == "timeout", error
    assert state(host)["subscriptions"] == {}


def test_a_compressed_response_is_refused_without_decoding(events_host, receiver) -> None:
    host = events_host()
    receiver.mode = "gzip"
    error = subscribe(host, "worker", receiver.url(), new_secret()).get("error", {})
    assert error.get("data", {}).get("reason") == "encoded_response", error
    [challenge] = receiver.requests
    assert challenge["headers"].get("accept-encoding") == "identity"


def test_a_slow_resolver_counts_against_the_deadline(events_host, receiver) -> None:
    host = events_host(timeout_seconds=1)
    real = host.resolver

    def slow(name: str, port: int) -> list[str]:
        time.sleep(3)
        return real(name, port)

    events_of(host).resolver = slow
    started = time.monotonic()
    error = subscribe(host, "worker", receiver.url(), new_secret()).get("error", {})
    assert time.monotonic() - started < 2.0
    assert error.get("data", {}).get("reason") in ("timeout", "unreachable"), error


# --- R2-5: production config takes only production settings -----------------------------------


BASE = {"enabled": True, "callback_prefixes": [f"https://{NAME}/hooks/"]}


@pytest.mark.parametrize("change", [
    {"allow_private_callbacks": True}, {"ca_file": "/tmp/ca.pem"},
    {"max_per_role": 0}, {"max_per_role": -1}, {"max_per_role": True}, {"max_total": 0}, {"max_total": 10**6},
    {"timeout_seconds": 0}, {"timeout_seconds": 3600}, {"timeout_seconds": "10"},
    {"min_ttl_seconds": 0}, {"max_ttl_seconds": 10**9}, {"default_ttl_seconds": "60"},
])
def test_test_only_or_out_of_range_settings_are_refused(change: dict) -> None:
    try:
        integration.parse_settings({**BASE, **change})
    except ConfigError:
        return
    except Exception as error:  # any other failure is a missing check, not a refusal
        pytest.fail(f"{change} raised {type(error).__name__}, not ConfigError")
    pytest.fail(f"{change} was accepted")


@pytest.mark.parametrize("enabled", ["false", "true", 1, 0])
def test_enabled_must_be_a_toml_boolean(enabled: Any) -> None:
    raw = {"host": {"public_url": "https://baton.example.test"}, "roles": {"worker": {}},
           "integrations": {"openai_mcp_events": {**BASE, "enabled": enabled}}}
    with pytest.raises(ConfigError, match="boolean"):
        parse(raw)


def test_valid_production_settings_parse() -> None:
    settings = integration.parse_settings({**BASE, "max_per_role": 3, "timeout_seconds": 5})
    assert (settings.max_per_role, settings.allow_private_callbacks, settings.ca_file) == (3, False, None)
    assert EVENT == "baton.attention"
