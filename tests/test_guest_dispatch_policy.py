"""Dispatch policy at real MCP boundaries, using synthetic fixed roles and tokens only."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import uvicorn
from conftest import Host, free_port, make_config

from baton import app as app_module
from baton import auth, jobs
from baton import config as config_module
from baton import messages as messages_module
from baton.config import Role, StaticToken
from baton.integrations import openai_mcp_events
from baton.store import connect

PROTOCOL = "2026-07-28"


def _host(tmp_path, *, events: bool = False, guest_view_all: bool = False) -> tuple[Host, uvicorn.Server]:
    port = free_port()
    extra = """
        [conversations]
        enabled = true
        [search]
        enabled = true
    """
    if events:
        extra += '\n[integrations.openai_mcp_events]\nenabled = true\ncallback_prefixes = ["https://callbacks.example.test/hooks"]\n'
    host = make_config(tmp_path, port=port, extra=extra)
    guest_token, _ = auth.new_token()
    hilary_token, _ = auth.new_token()
    roles = dict(host.config.roles)
    roles["planner"] = replace(roles["planner"], can_create_conversations=True, can_manage_participants=True)
    roles["guest"] = Role(name="guest", can_view_all_conversations=guest_view_all)
    hilary_options = {"can_create_conversations": True, "can_manage_participants": True}
    if "job_authority" in Role.__dataclass_fields__:
        hilary_options["job_authority"] = False
    roles["hilary"] = Role(name="hilary", **hilary_options)
    tokens = (*host.config.tokens,
              StaticToken("guest-test", "guest", hashlib.sha256(guest_token.encode()).hexdigest()),
              StaticToken("hilary-test", "hilary", hashlib.sha256(hilary_token.encode()).hexdigest()))
    host.config = replace(host.config, roles=roles, tokens=tokens)
    host.tokens.update(guest=guest_token, hilary=hilary_token)
    integrations = None
    if events:
        integration = openai_mcp_events.McpEvents(host.config, openai_mcp_events.parse_settings(
            host.config.integrations["openai_mcp_events"]))
        integration._validated = lambda params: (_ for _ in ()).throw(AssertionError("callback validation ran"))
        registrations = []
        register = integration.register

        def capture_registrations(mcp_server):
            lowlevel = mcp_server._lowlevel_server
            add_request_handler = lowlevel.add_request_handler

            def capture(method, params_type, handler):
                registrations.append((method, params_type, handler))
                return add_request_handler(method, params_type, handler)

            lowlevel.add_request_handler = capture
            try:
                register(mcp_server)
            finally:
                lowlevel.add_request_handler = add_request_handler

        integration.register = capture_registrations
        integrations = {"openai_mcp_events": integration}
        host.integrations = integrations
        host.integration_handlers = registrations
    server = uvicorn.Server(uvicorn.Config(app_module.create_app(host.config, integrations), host="127.0.0.1",
                                            port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    host.base = f"http://127.0.0.1:{port}"
    return host, server


def _rpc(host: Host, role: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    meta = {"io.modelcontextprotocol/protocolVersion": PROTOCOL,
            "io.modelcontextprotocol/clientInfo": {"name": "policy-test", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}
    headers = {"Authorization": f"Bearer {host.tokens[role]}",
        "Accept": "application/json, text/event-stream", "Mcp-Protocol-Version": PROTOCOL,
        "Mcp-Method": method}
    if method == "tools/call":
        headers["Mcp-Name"] = params["name"]
    response = httpx.post(host.mcp_url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                      "params": {**params, "_meta": meta}}, timeout=10)
    return response.json()


def _call(host: Host, role: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = _rpc(host, role, "tools/call", {"name": name, "arguments": arguments})
    if "result" not in response:
        raise AssertionError(response)
    result = response["result"]
    if isinstance(result.get("structuredContent"), dict):
        payload = result["structuredContent"]
        return payload.get("result", payload)
    if result.get("content"):
        try:
            decoded = json.loads(result["content"][0]["text"])
            return decoded.get("result", decoded) if isinstance(decoded, dict) else decoded
        except (ValueError, KeyError, TypeError):
            pass
    return result.get("structuredContent", result)


def _finish(server: uvicorn.Server) -> None:
    server.should_exit = True


def test_guest_policy_enumerates_every_registered_tool_and_allows_only_the_read_slice(tmp_path) -> None:
    host, server = _host(tmp_path)
    try:
        listed = _rpc(host, "guest", "tools/list", {})["result"]["tools"]
        registered = {tool["name"] for tool in listed}
        allowed = {"whoami", "list_conversations", "read_history", "get_message", "search"}
        if hasattr(app_module, "GUEST_TOOLS"):
            assert app_module.GUEST_TOOLS & registered == allowed
        assert allowed <= registered
        assert _call(host, "guest", "whoami", {})["role"] == "guest"
        listed_conversations = _call(host, "guest", "list_conversations", {})
        assert listed_conversations == {"conversations": [], "next_cursor": None}
        assert _call(host, "guest", "search", {"query": "needle"}).get("error") is None
        assert _call(host, "guest", "read_history", {"conversation_id": "missing"})["error"] == \
            "unknown_conversation"
        assert _call(host, "guest", "get_message", {"message_id": "missing"})["error"] == "unknown_message"

        for tool in listed:
            name = tool["name"]
            if name in allowed:
                continue
            arguments = {key: {"string": "x", "integer": 0, "number": 0, "boolean": False,
                               "array": [], "object": {}}.get(spec.get("type"), "x")
                         for key, spec in tool["inputSchema"].get("properties", {}).items()
                         if key in tool["inputSchema"].get("required", [])}
            if name == "send_message":
                arguments["to"] = "planner"
            outcome = _call(host, "guest", name, arguments)
            assert outcome.get("error") == "forbidden", f"{name} returned {outcome!r}"

        for restricted in ({"kinds": ["summary"]}, {"producer": "indexer"}, {"model": "test-model"},
                           {"query_vector": [0.1], "model": "test-model"}):
            assert _call(host, "guest", "search", {"query": "needle", **restricted})["error"] == "forbidden"
        for fields in ({"to": "planner"}, {"to": "planner", "job_id": "job_x"},
                       {"conversation_id": "conv_x", "needs_decision": True},
                       {"to": "planner", "token": "unverified-nonempty"}):
            assert _call(host, "guest", "send_message", {"text": "hello", **fields})["error"] == "forbidden"
    finally:
        _finish(server)


def test_raw_identity_fields_are_refused_before_sdk_argument_validation(tmp_path) -> None:
    host, server = _host(tmp_path)
    try:
        for field in ("alias", "alias_id", "identity", "code"):
            response = _rpc(host, "guest", "tools/call", {"name": "send_message",
                "arguments": {"text": "hello", "to": "planner", field: "forged"}})["result"]
            assert response["isError"] is True
            assert json.loads(response["content"][0]["text"])["error"] == "forbidden_field"
    finally:
        _finish(server)


def test_job_authority_false_blocks_before_replay_and_keeps_hilary_other_tools(tmp_path) -> None:
    host, server = _host(tmp_path)
    try:
        created = _call(host, "planner", "create_job", {"title": "synthetic policy check"})
        move_request_id = messages_module.new_request_id()
        create_request_id = messages_module.new_request_id()
        conn = connect(host.config.db_path)
        try:
            jobs.move(conn, host.config.jobs, created["job_id"], "working", actor="hilary",
                      request_id=move_request_id)
            jobs.create(conn, host.config.jobs, request_id=create_request_id, actor="hilary",
                        title="existing retry")
        finally:
            conn.close()
        for name, args in (("move_job", {"job_id": created["job_id"], "to_state": "working",
                                          "request_id": move_request_id}),
                           ("create_job", {"title": "existing retry",
                                           "request_id": create_request_id})):
            assert _call(host, "hilary", name, args)["error"] == "forbidden"

        assert _call(host, "hilary", "send_message", {"to": "planner", "text": "ordinary message"}).get("error") is None
        assert _call(host, "hilary", "wait", {"timeout_seconds": 0}).get("error") is None
        conversation = _call(host, "hilary", "create_conversation", {
            "title": "ordinary conversation", "participants": ["hilary", "planner"]})
        assert conversation.get("error") is None
        assert _call(host, "hilary", "set_topic", {"conversation_id": conversation["conversation_id"],
                                                     "topic": "policy"}).get("error") is None
    finally:
        _finish(server)


def test_guest_job_write_refuses_a_request_created_by_an_allowed_principal_before_replay(tmp_path) -> None:
    host, server = _host(tmp_path)
    try:
        request_id = messages_module.new_request_id()
        created = _call(host, "planner", "create_job", {
            "title": "request created before guest retry", "request_id": request_id})
        assert created["created"] is True

        retried = _call(host, "guest", "create_job", {
            "title": "request created before guest retry", "request_id": request_id})
        assert retried["error"] == "forbidden"
        assert len(_call(host, "planner", "list_jobs", {})) == 1
    finally:
        _finish(server)


def test_guest_integration_mutations_are_refused_at_handler_entry(tmp_path) -> None:
    host, server = _host(tmp_path, events=True)
    try:
        core_lifecycle_discovery = {"tools/list", "server/discover"}
        for method in core_lifecycle_discovery:
            assert _rpc(host, "guest", method, {})["result"]
        assert _rpc(host, "guest", "server/discover", {})["result"]["capabilities"]["events"] == {}
        # The integration's read-only discovery method is available alongside core lifecycle/discovery.
        available_integration_methods = {"events/list"}
        assert _rpc(host, "guest", "events/list", {})["result"]["events"]
        handlers = {method: (params_type, handler)
                    for method, params_type, handler in host.integration_handlers}
        assert handlers
        lifecycle_and_discovery = available_integration_methods
        mutation_params = {
            "events/subscribe": {"name": openai_mcp_events.EVENT_NAME, "arguments": {},
                                 "delivery": {"mode": "webhook", "url": "bad", "secret": "bad"}},
            "events/unsubscribe": {"name": openai_mcp_events.EVENT_NAME, "arguments": {},
                                   "delivery": {"url": "bad"}},
        }
        assert set(handlers) == lifecycle_and_discovery | set(mutation_params)
        for method in set(handlers) - lifecycle_and_discovery:
            response = _rpc(host, "guest", method, mutation_params[method])
            assert response["error"]["code"] == -32600, method
        assert not host.config.data_dir.joinpath(openai_mcp_events.STATE_FILE).exists()
    finally:
        _finish(server)


def test_guest_cannot_be_enrolled_or_directly_messaged(tmp_path) -> None:
    host, server = _host(tmp_path)
    try:
        conversation = _call(host, "planner", "create_conversation", {
            "title": "board", "participants": ["planner", "worker"]})
        assert _call(host, "planner", "create_conversation", {
            "title": "no guest", "participants": ["planner", "worker", "guest"]})["error"] == "invalid_request"
        assert _call(host, "planner", "add_participant", {"conversation_id": conversation["conversation_id"],
                                                              "participant": "guest"})["error"] == "invalid_request"
        assert _call(host, "planner", "send_message", {"to": "guest", "text": "private"})["error"] == "forbidden"
        conn = connect(host.config.db_path)
        try:
            direct = messages_module.send(conn, sender="planner", recipient="guest", text="legacy direct message")
        finally:
            conn.close()
        assert _call(host, "guest", "get_message", {"message_id": direct["message_id"]})["error"] == \
            "unknown_message"
        assert _call(host, "guest", "search", {"query": "legacy"})["hits"] == []
    finally:
        _finish(server)


def test_view_all_guest_reads_conversations_but_never_legacy_direct_messages(tmp_path) -> None:
    host, server = _host(tmp_path, guest_view_all=True)
    try:
        conversation = _call(host, "planner", "create_conversation", {
            "title": "retained board conversation", "participants": ["planner", "worker"]})
        posted = _call(host, "planner", "send_message", {
            "conversation_id": conversation["conversation_id"], "text": "retained board needle"})
        found = _call(host, "guest", "search", {"query": "retained board needle"})["hits"]
        assert any(hit["message_id"] == posted["message_id"] for hit in found)
        assert _call(host, "guest", "get_message", {"message_id": posted["message_id"]})["text"] == \
            "retained board needle"

        conn = connect(host.config.db_path)
        try:
            direct = messages_module.send(conn, sender="planner", recipient="guest", text="private legacy note")
        finally:
            conn.close()
        assert _call(host, "guest", "get_message", {"message_id": direct["message_id"]})["error"] == \
            "unknown_message"
        assert _call(host, "guest", "search", {"query": "private legacy"})["hits"] == []
    finally:
        _finish(server)


def test_legacy_guest_membership_reads_only_conversation_copies_it_holds(tmp_path) -> None:
    host, server = _host(tmp_path)
    try:
        conversation = _call(host, "planner", "create_conversation", {
            "title": "legacy guest membership", "participants": ["planner", "worker"]})
        conversation_id = conversation["conversation_id"]
        pre_join = _call(host, "planner", "send_message", {
            "conversation_id": conversation_id, "text": "prejoin hidden needle"})

        conn = connect(host.config.db_path)
        try:
            conn.execute("INSERT INTO participants (conversation_id, role, added_at) VALUES (?, ?, ?)",
                         (conversation_id, "guest", datetime.now(UTC).isoformat()))
        finally:
            conn.close()

        held = _call(host, "planner", "send_message", {
            "conversation_id": conversation_id, "text": "held guest needle"})
        hits = _call(host, "guest", "search", {"query": "held guest needle"})["hits"]
        assert any(hit["message_id"] == held["message_id"] for hit in hits)
        assert _call(host, "guest", "get_message", {"message_id": held["message_id"]})["text"] == \
            "held guest needle"

        assert _call(host, "guest", "search", {"query": "prejoin hidden needle"})["hits"] == []
        assert _call(host, "guest", "get_message", {"message_id": pre_join["message_id"]})["error"] == \
            "unknown_message"
    finally:
        _finish(server)


def test_fixed_role_removes_legacy_guest_before_later_conversation_fanout(tmp_path) -> None:
    host, server = _host(tmp_path)
    try:
        conversation = _call(host, "planner", "create_conversation", {
            "title": "remove legacy guest", "participants": ["planner", "worker"]})
        conversation_id = conversation["conversation_id"]
        conn = connect(host.config.db_path)
        try:
            conn.execute("INSERT INTO participants (conversation_id, role, added_at) VALUES (?, ?, ?)",
                         (conversation_id, "guest", datetime.now(UTC).isoformat()))
            assert messages_module.unread(conn, "guest") == 0
        finally:
            conn.close()

        removed = _call(host, "planner", "remove_participant", {
            "conversation_id": conversation_id, "participant": "guest"})
        assert removed.get("error") is None, f"legacy guest removal returned {removed!r}"
        assert removed["changed"] is True
        assert "guest" not in removed["participants"]

        posted = _call(host, "planner", "send_message", {
            "conversation_id": conversation_id, "text": "after guest removal"})
        conn = connect(host.config.db_path)
        try:
            recipients = {row["recipient"] for row in conn.execute(
                "SELECT recipient FROM messages WHERE id = ?", (posted["message_id"],))}
            assert "guest" not in recipients
            assert messages_module.unread(conn, "guest") == 0
            assert conn.execute("SELECT 1 FROM participants WHERE conversation_id = ? AND role = 'guest'",
                               (conversation_id,)).fetchone() is None
        finally:
            conn.close()
        assert _call(host, "planner", "add_participant", {
            "conversation_id": conversation_id, "participant": "guest"})["error"] == "invalid_request"
    finally:
        _finish(server)


def test_config_job_authority_defaults_true_and_requires_a_boolean(tmp_path) -> None:
    host = make_config(tmp_path)
    assert host.config.roles["planner"].job_authority is True
    raw = {"host": {"public_url": "http://127.0.0.1:8765"},
           "roles": {"planner": {"job_authority": False}}}
    assert config_module.parse(raw).roles["planner"].job_authority is False
    raw["roles"]["planner"]["job_authority"] = "false"
    try:
        config_module.parse(raw)
    except config_module.ConfigError as error:
        assert "roles.planner.job_authority" in str(error)
    else:
        raise AssertionError("string job_authority must be refused")
