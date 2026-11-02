# ruff: noqa: F811 - the receiver fixture is imported from test_openai_mcp_events
"""Events for mapped aliases: a guest grant bound to an alias that's mapped to a role receives that role's wakes.

Synthetic and offline: a real loopback host with OAuth, the loopback receiver and the stub resolver.
"""
from __future__ import annotations

import json
import time
from dataclasses import replace

import httpx
import pytest
from conftest import make_config
from test_aliases import payload
from test_auth import authorize
from test_openai_mcp_events import (
    StubResolver,
    dispatch,
    new_secret,
    receiver,  # noqa: F401 - fixture
    rpc,
    send,
    settings_table,
    state,
    subscribe,
)
from test_per_call_identity import OTHER, SECRET, db, owner

from baton import auth, store
from baton.integrations import openai_mcp_events as integration

ROLES = """
[roles.hilary]
description = "Designs"
job_authority = false
[roles.hilary.wake]
integration = "openai_mcp_events"
[roles.worker.wake]
integration = "openai_mcp_events"
[roles.guest]
description = "Shared login"
"""


@pytest.fixture
def host(start_host, receiver, tmp_path):
    made = {role: auth.new_token() for role in ("hilary", "guest")}
    entries = "".join(f'[[auth.tokens]]\nname = "{role}-token"\nrole = "{role}"\nsha256 = "{digest}"\n'
                      for role, (_, digest) in made.items())

    def build(config):
        settings = integration.parse_settings(config.integrations["openai_mcp_events"])
        return {"openai_mcp_events": integration.McpEvents(config, replace(
            settings, allow_private_callbacks=True, ca_file=str(receiver.ca_file)))}

    started = start_host(extra=ROLES + entries + settings_table(receiver, max_total=6), integrations=build,
                         oauth=True, oauth_roles=("planner", "guest"))
    started.resolver = StubResolver()
    started.integrations["openai_mcp_events"].resolver = started.resolver
    started.tokens.update({role: token for role, (token, _) in made.items()})
    started.config_path = tmp_path / "baton.toml"
    grant = authorize(started, role="guest")
    started.tokens["grant"] = grant["access_token"]
    started.refresh = grant
    return started


def tool(host, credential, name, arguments=None):
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "events-target-test", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}
    response = httpx.post(host.mcp_url, headers={
        "Authorization": f"Bearer {host.tokens[credential]}", "Accept": "application/json, text/event-stream",
        "Mcp-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": name,
    }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": name, "arguments": arguments or {}, "_meta": meta}}, timeout=30).json()
    try:
        return payload(response)
    except (KeyError, ValueError, TypeError):
        return {"error": "unparsed", "response": response}


def enrol(host, role):
    assert owner(host, "alias", "arm-enrol", role) == 0
    assert tool(host, role, "claim_alias", {"name": role, "passphrase": SECRET}).get("effective_role") == role


def proof(host, name, passphrase=SECRET, credential="grant"):
    return tool(host, credential, "claim_alias", {"name": name, "passphrase": passphrase})["binding_token"]


def targets(host):
    with db(host) as conn:
        return [tuple(row) for row in conn.execute("SELECT a.name FROM events_targets t JOIN aliases a"
                                                   " ON a.id = t.alias_id")]


def woken_roles(receiver):
    return [json.loads(event["body"])["data"]["role"] for event in receiver.events()]


@pytest.mark.parametrize("bind_first", [True, False])
def test_a_bound_guest_grant_receives_its_mapped_roles_wakes_in_either_order(host, receiver, bind_first):
    enrol(host, "hilary")
    token = proof(host, "hilary")
    if bind_first:
        assert tool(host, "grant", "bind_events", {"token": token}).get("bound") is True
    subscribed = subscribe(host, "grant", receiver.url("/hooks/g"), new_secret())
    assert "result" in subscribed, subscribed
    if not bind_first:
        assert tool(host, "grant", "bind_events", {"token": token}).get("bound") is True
    send(host, "hilary", "private text")
    dispatch(host)
    assert woken_roles(receiver) == ["hilary"]
    assert b"private text" not in receiver.events()[0]["body"]


def test_an_unbound_guest_subscription_receives_nothing(host, receiver):
    enrol(host, "hilary")
    assert "result" in subscribe(host, "grant", receiver.url("/hooks/g"), new_secret())
    send(host, "hilary", "hello")
    dispatch(host)
    assert receiver.events() == []


def test_bind_needs_a_mapped_proof_on_an_oauth_grant(host):
    enrol(host, "hilary")
    unmapped = proof(host, "lane-u")
    assert tool(host, "grant", "bind_events", {"token": unmapped}).get("error") == "forbidden"
    mapped = proof(host, "hilary", credential="guest")
    static = tool(host, "guest", "bind_events", {"token": mapped})
    assert static.get("error") == "forbidden", "a static guest has no grant"
    assert tool(host, "hilary", "bind_events", {}).get("error") == "forbidden"
    assert tool(host, "grant", "bind_events", {}).get("error") == "forbidden"
    assert targets(host) == []


def test_a_grant_binds_one_alias_until_it_is_unbound(host):
    enrol(host, "hilary")
    enrol(host, "worker")
    hilary, worker = proof(host, "hilary"), proof(host, "worker")
    assert tool(host, "grant", "bind_events", {"token": hilary}).get("bound") is True
    again = tool(host, "grant", "bind_events", {"token": hilary})
    assert again.get("bound") is True, "the same target is idempotent"
    assert tool(host, "grant", "bind_events", {"token": worker}).get("error") == "forbidden"
    assert tool(host, "grant", "unbind_events", {"token": worker}).get("error") == "forbidden"
    assert targets(host) == [("hilary",)]
    refreshed = httpx.post(host.base + "/token", data={"grant_type": "refresh_token", "client_id": host.refresh[
        "client_id"], "refresh_token": host.refresh["refresh_token"]}).json()
    host.tokens["grant"] = refreshed["access_token"]
    assert tool(host, "grant", "bind_events", {"token": hilary}).get("bound") is True, "a refresh keeps the grant"
    assert tool(host, "grant", "unbind_events", {"token": hilary}).get("unbound") is True
    assert tool(host, "grant", "unbind_events", {"token": hilary}).get("unbound") is False, "no target is a no-op"
    assert tool(host, "grant", "bind_events", {"token": worker}).get("bound") is True
    assert targets(host) == [("worker",)]


def test_map_and_unmap_drop_the_target_and_a_rekey_keeps_it(host):
    enrol(host, "hilary")
    assert tool(host, "grant", "bind_events", {"token": proof(host, "hilary")}).get("bound") is True
    assert owner(host, "alias", "arm-rekey", "hilary") == 0
    reset = tool(host, "grant", "claim_alias", {"name": "hilary", "passphrase": OTHER})
    assert reset.get("effective_role") == "hilary"
    assert targets(host) == [("hilary",)], "a rekey is a credential change, not a mapping change"
    assert owner(host, "alias", "unmap", "hilary") == 0
    assert targets(host) == []
    assert owner(host, "alias", "map", "hilary", "hilary") == 0
    assert tool(host, "grant", "bind_events", {"token": proof(host, "hilary", OTHER)}).get("bound") is True
    assert owner(host, "alias", "map", "hilary", "hilary") == 0
    assert targets(host) == [], "a remap drops it too"


def test_an_ended_grant_stops_delivery_at_once_and_its_target_is_swept(host, receiver):
    enrol(host, "hilary")
    assert tool(host, "grant", "bind_events", {"token": proof(host, "hilary")}).get("bound") is True
    assert "result" in subscribe(host, "grant", receiver.url("/hooks/g"), new_secret())
    auth.revoke_all(host.config.oauth_state_path)
    send(host, "hilary", "hello")
    dispatch(host)
    assert receiver.events() == []
    assert targets(host) == []


def test_guest_subscriptions_belong_to_their_grant(host, receiver):
    url = receiver.url("/hooks/g")
    subscribed = subscribe(host, "grant", url, new_secret())
    assert "result" in subscribed, f"a guest OAuth grant can subscribe: {subscribed}"
    first = subscribed["result"]["id"]
    host.tokens["other"] = authorize(host, role="guest")["access_token"]
    other = subscribe(host, "other", url, new_secret())
    assert other["result"]["id"] != first, "a subscription's id comes from its grant, not the guest role"
    rpc(host, "other", "events/unsubscribe", {"name": integration.EVENT_NAME, "arguments": {},
                                              "delivery": {"url": url}})
    assert first in state(host)["subscriptions"], "another grant can't unsubscribe it"
    assert "result" in subscribe(host, "grant", receiver.url("/hooks/h"), new_secret())
    capped = subscribe(host, "grant", receiver.url("/hooks/i"), new_secret())
    assert "error" in capped, "the per-subscriber cap counts this grant's subscriptions"
    static = subscribe(host, "guest", receiver.url("/hooks/j"), new_secret())
    assert static.get("error", {}).get("code") == -32600, "a static guest credential has no grant to subscribe with"


def test_the_owner_status_shows_receipts_but_no_secrets_and_unbind_recovers(host, receiver, capsys):
    enrol(host, "hilary")
    assert tool(host, "grant", "bind_events", {"token": proof(host, "hilary")}).get("bound") is True
    secret = new_secret()
    sub_id = subscribe(host, "grant", receiver.url("/hooks/g"), secret)["result"]["id"]
    send(host, "hilary", "hello")
    dispatch(host)
    [event] = receiver.events()
    assert "result" in subscribe(host, "grant", receiver.url("/hooks/g"), secret), "a refresh runs the state cleanup"
    capsys.readouterr()
    assert owner(host, "events", "status") == 0
    printed = capsys.readouterr().out
    shown = json.loads(printed)
    [grant] = {entry["grant"] for entry in auth.TokenStore.load(host.config.oauth_state_path).access.values()
               if entry["role"] == "guest"}
    assert {"id": sub_id, "owner": f"grant:{grant}"}.items() <= shown["subscriptions"][0].items()
    [receipt] = [r for r in shown["receipts"] if r["subscription"] == sub_id]
    assert (receipt["wake_id"], receipt["outcome"]) == (json.loads(event["body"])["eventId"], "accepted")
    for private in (secret, receiver.url("/hooks/g"), host.tokens["grant"], "whsec_"):
        assert private not in printed
    assert owner(host, "events", "unbind", grant) == 0
    assert targets(host) == []


@pytest.fixture
def offline_events(tmp_path):
    """An events integration with no server or receiver, for checks on its own state."""
    table = '[integrations.openai_mcp_events]\nenabled = true\ncallback_prefixes = ["https://callback.test/hooks/"]\n'
    config = make_config(tmp_path, extra=table).config
    store.connect(config.db_path).close()
    return integration.McpEvents(config, integration.parse_settings(config.integrations["openai_mcp_events"]))


def receipt(at):
    return {"at": at, "wake_id": "wake_synthetic", "subscription": "sub", "owner": "role:worker",
            "outcome": "accepted"}


def test_receipts_never_exceed_their_cap(offline_events):
    with offline_events._state() as state:
        state["receipts"] = [receipt(time.time()) for _ in range(integration.MAX_RECEIPTS)]
    offline_events._record("wake_new", {"id": "sub", "role": "worker"}, "answered 500")
    assert len(offline_events._read()["receipts"]) == integration.MAX_RECEIPTS
    assert offline_events._read()["receipts"][-1]["wake_id"] == "wake_new"


def test_status_never_shows_a_receipt_older_than_its_window(offline_events):
    with offline_events._state() as state:
        state["receipts"] = [receipt(time.time() - integration.RECEIPT_SECONDS - 60), receipt(time.time())]
    shown = integration.status(offline_events.config)["receipts"]
    assert len(shown) == 1 and shown[0]["at"] > time.time() - integration.RECEIPT_SECONDS
