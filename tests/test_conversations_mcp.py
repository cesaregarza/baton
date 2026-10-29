"""Group conversations through real clients: two MCP clients on separate OAuth grants and one CLI agent.

Synthetic and offline: a real host on a loopback port, generic roles, a throwaway passphrase.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import anyio
import httpx
import httpx2
import pytest
import uvicorn
from conftest import PASSPHRASE, REDIRECT, free_port
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_cli import run as run_cli
from test_integration import TOOLS, payload

from baton import cli, messages
from baton import config as config_module
from baton.auth import hash_passphrase, new_token
from baton.config import Config
from baton.store import connect

CONVERSATION_TOOLS = {"create_conversation", "list_conversations", "read_history", "add_participant",
                      "remove_participant"}


class GroupHost:
    def __init__(self, config: Config, path: Path, base: str, tokens: dict[str, str]) -> None:
        self.config, self.path, self.base, self.tokens = config, path, base, tokens

    @property
    def mcp_url(self) -> str:
        return self.config.resource_url


@pytest.fixture
def group_host(tmp_path: Path) -> Iterator[GroupHost]:
    """A real host: `reviewer` and `assistant` sign in with OAuth, `orchestrator` is local and uses the CLI."""
    from baton.app import create_app

    port = free_port()
    token, digest = new_token()
    path = tmp_path / "baton.toml"
    path.write_text(textwrap.dedent(f"""
        [host]
        public_url = "http://127.0.0.1:{port}"
        listen_port = {port}
        data_dir = "{tmp_path / 'data'}"

        [roles.orchestrator]
        description = "Local orchestrator"
        can_create_conversations = true
        can_manage_participants = true
        [roles.reviewer]
        description = "Remote reviewer"
        [roles.assistant]
        description = "Remote assistant"
        can_create_conversations = true
        [roles.outsider]
        description = "Never invited"

        [conversations]
        enabled = true

        [[auth.tokens]]
        name = "outsider-token"
        role = "outsider"
        sha256 = "{digest}"

        [auth.oauth]
        enabled = true
        passphrase_hash = "{hash_passphrase(PASSPHRASE)}"
        roles = ["reviewer", "assistant"]
        redirect_uris = ["{REDIRECT}"]
    """))
    config = config_module.load(path)
    server = uvicorn.Server(uvicorn.Config(create_app(config), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    yield GroupHost(config, path, f"http://127.0.0.1:{port}", {"outsider": token})
    server.should_exit = True


def grant(host: GroupHost, role: str) -> str:
    """Register a client, approve it on the consent page as `role`, and return its access token."""
    client_id = httpx.post(host.base + "/register", json={
        "redirect_uris": [REDIRECT], "client_name": f"Example {role} client",
        "token_endpoint_auth_method": "none"}).json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    started = httpx.get(host.base + "/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": "s1", "resource": host.mcp_url})
    login_id = parse_qs(urlparse(started.headers["location"]).query)["id"][0]
    approved = httpx.post(host.base + "/login", data={"id": login_id, "passphrase": PASSPHRASE, "role": role})
    code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
    tokens = httpx.post(host.base + "/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id, "redirect_uri": REDIRECT,
        "code_verifier": verifier}).json()
    return tokens["access_token"]


def client(host: GroupHost, token: str) -> Client:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60)
    return Client(streamable_http_client(host.mcp_url, http_client=http))


def cli_as(host: GroupHost, capsys, *argv: str) -> tuple[int, Any]:
    return run_cli(capsys, "--config", str(host.path), *argv, "--as", "orchestrator")


def texts(page: dict[str, Any]) -> list[tuple[str | None, str]]:
    return [(m["from_role"], m["text"]) for m in page["messages"]]


# 12 ------------------------------------------------------------------------------------------------------


def test_12_two_oauth_clients_and_a_cli_agent_hold_one_conversation(group_host, capsys) -> None:
    host = group_host
    reviewer_token, assistant_token = grant(host, "reviewer"), grant(host, "assistant")
    assert reviewer_token != assistant_token
    code, made = cli_as(host, capsys, "conversation", "new", "--title", "Release plan",
                        "--with", "orchestrator,reviewer,assistant")
    assert code == 0 and made["created"] is True
    conversation_id = made["conversation_id"]

    async def run() -> dict[str, Any]:
        async with client(host, reviewer_token) as reviewer, client(host, assistant_token) as assistant:
            assert {t.name for t in (await reviewer.list_tools()).tools} == TOOLS | CONVERSATION_TOOLS
            assert payload(await reviewer.call_tool("whoami", {}))["role"] == "reviewer"
            listed = payload(await assistant.call_tool("list_conversations", {}))
            assert [c["conversation_id"] for c in listed] == [conversation_id]

            assert cli_as(host, capsys, "message", "--conversation", conversation_id, "--text", "1 from cli")[0] == 0
            sent = payload(await reviewer.call_tool("send_message", {"conversation_id": conversation_id,
                                                                     "text": "2 from reviewer"}))
            assert set(sent["seqs"]) == {"orchestrator", "assistant"}
            payload(await assistant.call_tool("send_message", {"conversation_id": conversation_id,
                                                               "text": "3 from assistant"}))

            seen = {"reviewer": payload(await reviewer.call_tool("read_messages", {})),
                    "assistant": payload(await assistant.call_tool("wait", {"timeout_seconds": 5}))}
            history = payload(await reviewer.call_tool("read_history", {"conversation_id": conversation_id}))
            assert texts(history) == [("orchestrator", "1 from cli"), ("reviewer", "2 from reviewer"),
                                      ("assistant", "3 from assistant")]  # its own message too, in order
            assert payload(await reviewer.call_tool("read_messages", {}))["messages"] == []  # history marked nothing
            return seen

    seen = anyio.run(run)
    _, cli_page = cli_as(host, capsys, "read")
    assert texts(seen["reviewer"]) == [("orchestrator", "1 from cli"), ("assistant", "3 from assistant")]
    assert [(e["message"]["from_role"], e["message"]["text"]) for e in seen["assistant"]["events"]] == \
        [("orchestrator", "1 from cli"), ("reviewer", "2 from reviewer")]
    assert texts(cli_page) == [("reviewer", "2 from reviewer"), ("assistant", "3 from assistant")]
    for page in (seen["reviewer"], cli_page):
        assert {m["conversation_id"] for m in page["messages"]} == {conversation_id}


# 4 -------------------------------------------------------------------------------------------------------


def test_4_a_client_cannot_set_from_role(group_host, capsys) -> None:
    host = group_host
    assistant_token = grant(host, "assistant")
    conversation_id = cli_as(host, capsys, "conversation", "new", "--title", "t",
                             "--with", "orchestrator,reviewer,assistant")[1]["conversation_id"]

    async def run() -> None:
        async with client(host, assistant_token) as assistant:
            schema = {t.name: t for t in (await assistant.list_tools()).tools}["send_message"].input_schema
            assert not {"role", "from_role", "sender"} & set(schema["properties"])
            for arguments in ({"conversation_id": conversation_id}, {"to": "reviewer"}):
                result = await assistant.call_tool("send_message", {**arguments, "text": "spoof",
                                                                    "from_role": "orchestrator",
                                                                    "sender": "orchestrator"})
                if not result.is_error:  # ignored rather than refused: either way it must not take effect
                    assert "error" not in payload(result)

    anyio.run(run)
    conn = connect(host.config.db_path)
    rows = conn.execute("SELECT sender FROM messages WHERE text = 'spoof'").fetchall()
    assert {row["sender"] for row in rows} <= {"assistant"}
    for message in messages.read(conn, "reviewer", after_seq=0)["messages"]:
        assert message["from_role"] == "assistant"


# Over MCP: refusals keep their codes, and the audit names the conversation ---------------------------------


def test_refusals_over_mcp_carry_the_stable_codes_and_the_audit_names_the_conversation(group_host, capsys) -> None:
    host = group_host
    reviewer_token = grant(host, "reviewer")
    conversation_id = cli_as(host, capsys, "conversation", "new", "--title", "t",
                             "--with", "orchestrator,reviewer,assistant")[1]["conversation_id"]

    async def run() -> None:
        async with client(host, reviewer_token) as reviewer, client(host, host.tokens["outsider"]) as outsider:
            forbidden = payload(await reviewer.call_tool("create_conversation", {
                "title": "t", "participants": ["reviewer", "assistant"]}))
            assert forbidden["error"] == "forbidden"
            managed = payload(await reviewer.call_tool("add_participant", {"conversation_id": conversation_id,
                                                                           "participant": "outsider"}))
            assert managed["error"] == "forbidden"
            for tool, arguments in (("send_message", {"conversation_id": conversation_id, "text": "hi"}),
                                    ("read_history", {"conversation_id": conversation_id})):
                assert payload(await outsider.call_tool(tool, arguments))["error"] == "not_a_participant"
            both = payload(await reviewer.call_tool("send_message", {"to": "assistant", "text": "x",
                                                                     "conversation_id": conversation_id}))
            neither = payload(await reviewer.call_tool("send_message", {"text": "x"}))
            assert both["error"] == neither["error"] == "invalid_request"

    anyio.run(run)
    audit = [json.loads(line) for line in host.config.audit_path.read_text().splitlines()]
    refusal = next(line for line in audit if line["outcome"] == "not_a_participant")
    assert (refusal["role"], refusal["conversation_id"]) == ("outsider", conversation_id)
    added = next(line for line in audit if line["tool"] == "add_participant")
    assert added["participant"] == "outsider"
    assert all("text" not in line for line in audit)


def test_the_cli_refuses_conversations_when_they_are_off(host, capsys) -> None:
    config = ["--config", str(host.config.data_dir.parent / "baton.toml")]
    code, refused = run_cli(capsys, *config, "conversation", "list", "--as", "planner")
    assert (code, refused["error"]) == (1, "conversations_disabled")
    code, refused = run_cli(capsys, *config, "message", "--as", "planner", "--conversation", "conv_x", "--text", "x")
    assert (code, refused["error"]) == (1, "conversations_disabled")
    assert cli.main(["--config", config[1], "message", "--as", "planner", "--text", "x"]) == 1  # neither target
    capsys.readouterr()
