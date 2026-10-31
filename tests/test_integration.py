"""Two MCP clients exchange messages in both directions through one host, and hand a job back and forth."""

from __future__ import annotations

import json
from typing import Any

import anyio
import httpx
import httpx2
from conftest import Host
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

# Explicit public contract, shared by the base-host and conversation-client integration checks.
TOOLS = {"whoami", "alias_exists", "claim_alias", "send_message", "read_messages", "wait", "create_job",
         "move_job", "get_job", "list_jobs", "read_job_events"}


def payload(result: Any) -> Any:
    assert not result.is_error, result.content[0].text
    if result.structured_content is not None:
        return result.structured_content.get("result", result.structured_content)
    return json.loads(result.content[0].text)


def client(host: Host, role: str) -> Client:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {host.tokens[role]}"}, timeout=60)
    return Client(streamable_http_client(host.mcp_url, http_client=http))


def test_two_clients_exchange_messages_both_ways_through_one_host(start_host) -> None:
    host = start_host()

    async def run() -> None:
        async with client(host, "planner") as planner, client(host, "worker") as worker:
            assert {tool.name for tool in (await planner.list_tools()).tools} == TOOLS
            assert payload(await planner.call_tool("whoami", {}))["role"] == "planner"
            assert payload(await worker.call_tool("whoami", {}))["role"] == "worker"

            # planner -> worker, received by a blocking wait that starts first.
            async with anyio.create_task_group() as group:
                received: dict[str, Any] = {}

                async def wait_as_worker() -> None:
                    received.update(payload(await worker.call_tool("wait", {"timeout_seconds": 10})))

                group.start_soon(wait_as_worker)
                await anyio.sleep(0.3)
                sent = payload(await planner.call_tool("send_message", {"to": "worker", "text": "hello, worker"}))
            assert sent["seq"] == 1
            [event] = received["events"]
            assert (event["kind"], event["actor"], event["message"]["text"]) == ("message", "planner", "hello, worker")

            # worker -> planner, received by read_messages.
            payload(await worker.call_tool("send_message", {"to": "planner", "text": "hello, planner"}))
            page = payload(await planner.call_tool("read_messages", {}))
            assert [(m["sender"], m["text"]) for m in page["messages"]] == [("worker", "hello, planner")]
            assert payload(await planner.call_tool("read_messages", {}))["messages"] == []

            # A job handed to the worker, then back to the planner for review.
            job = payload(await planner.call_tool("create_job", {"title": "Write the summary"}))
            await_worker = payload(await worker.call_tool("wait", {"timeout_seconds": 5}))
            assert [e["kind"] for e in await_worker["events"]] == ["job.created"]
            payload(await worker.call_tool("move_job", {"job_id": job["job_id"], "to_state": "working"}))
            moved = payload(await worker.call_tool("move_job", {"job_id": job["job_id"], "to_state": "review",
                                                                "note": "ready"}))
            assert (moved["state"], moved["waiting_on"]) == ("review", "planner")
            planner_events = payload(await planner.call_tool("wait", {"timeout_seconds": 5}))["events"]
            assert [(e["kind"], e["data"].get("to")) for e in planner_events] == [("job.moved", "review")]

    anyio.run(run)


def test_a_client_cannot_speak_for_another_role(start_host) -> None:
    host = start_host()

    async def run() -> None:
        async with client(host, "worker") as worker:
            tools = {tool.name: tool for tool in (await worker.list_tools()).tools}
            assert "role" not in tools["send_message"].input_schema["properties"]
            refused = payload(await worker.call_tool("send_message", {"to": "nobody", "text": "hi"}))
            assert refused["error"] == "unknown_role"
            payload(await worker.call_tool("send_message", {"to": "planner", "text": "from the worker"}))

    anyio.run(run)
    from baton import messages
    from baton.store import connect

    conn = connect(host.config.db_path)
    assert [m["sender"] for m in messages.read(conn, "planner")["messages"]] == ["worker"]
    audit = [json.loads(line) for line in host.config.audit_path.read_text().splitlines()]
    assert {"role": "worker", "tool": "send_message", "to": "nobody", "outcome": "unknown_role"}.items() <= \
        audit[0].items()
    assert all("text" not in line for line in audit)


def test_requests_without_a_valid_token_are_refused(start_host) -> None:
    host = start_host()
    assert httpx.post(host.mcp_url, json={}).status_code == 401
    bad = httpx.post(host.mcp_url, json={}, headers={"Authorization": "Bearer not-a-token"})
    assert bad.status_code == 401


def test_the_mcp_endpoint_refuses_a_foreign_host_header(start_host) -> None:
    host = start_host()
    headers = {"Authorization": f"Bearer {host.tokens['worker']}", "Host": "attacker.example.test",
               "Accept": "application/json, text/event-stream"}
    assert httpx.post(host.mcp_url, json={}, headers=headers).status_code == 421
