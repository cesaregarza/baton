"""Topics across every surface that offers them: MCP, the local CLI, the remote CLI and the dashboard.

The remote command builds its own tool call, so an argument added to the local command has to be added
there too; that is its own test here rather than an assumption. Synthetic and offline: a real host on a
loopback port, generic roles (`operator` is the human), throwaway tokens.
"""

from __future__ import annotations

import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
import uvicorn
from conftest import free_port
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_dashboard import get, text, view_config
from test_human_seat import SeatHost, local, remote
from test_integration import payload

from baton import config as config_module
from baton import conversations
from baton.auth import new_token
from baton.store import connect

GROUP = "operator,orchestrator,reviewer,assistant"


@pytest.fixture
def topic_host(tmp_path: Path) -> Iterator[SeatHost]:
    """`operator` is the human and may manage, `orchestrator` manages locally, `assistant` only opens,
    and `reviewer` may do neither."""
    from baton.app import create_app

    port = free_port()
    tokens = {role: new_token() for role in ("operator", "reviewer", "assistant")}
    entries = "".join(f'[[auth.tokens]]\nname = "{role}-token"\nrole = "{role}"\nsha256 = "{digest}"\n'
                      for role, (_, digest) in tokens.items())
    path = tmp_path / "baton.toml"
    path.write_text(textwrap.dedent(f"""
        [host]
        public_url = "http://127.0.0.1:{port}"
        listen_port = {port}
        data_dir = "{tmp_path / 'data'}"

        [roles.operator]
        description = "The person running this host"
        human = true
        can_create_conversations = true
        can_manage_participants = true

        [roles.orchestrator]
        description = "An assistant that may open conversations and manage them"
        can_create_conversations = true
        can_manage_participants = true

        [roles.reviewer]
        description = "An assistant that may neither open nor manage"

        [roles.assistant]
        description = "An assistant that may open conversations"
        can_create_conversations = true

        [conversations]
        enabled = true
    """) + entries)
    config = config_module.load(path)
    server = uvicorn.Server(uvicorn.Config(create_app(config), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    yield SeatHost(config, path, f"http://127.0.0.1:{port}", {role: token for role, (token, _) in tokens.items()})
    server.should_exit = True


def client(host: SeatHost, role: str) -> Client:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {host.tokens[role]}"}, timeout=60)
    return Client(streamable_http_client(host.config.resource_url, http_client=http))


def opened(host: SeatHost, capsys, title: str, *extra: str) -> str:
    code, made = local(host, capsys, "assistant", "conversation", "new", "--title", title, "--with", GROUP,
                       *extra)
    assert code == 0, made
    return made["conversation_id"]


def topics(listed: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    return sorted((item["title"], item["topic"]) for item in listed)


# MCP ------------------------------------------------------------------------------------------------------


def test_the_tools_carry_a_topic_and_filter_by_it(topic_host, capsys) -> None:
    host = topic_host

    async def run() -> dict[str, Any]:
        async with client(host, "operator") as operator, client(host, "reviewer") as reviewer:
            names = {tool.name for tool in (await operator.list_tools()).tools}
            assert "set_topic" in names
            schema = {t.name: t for t in (await operator.list_tools()).tools}["create_conversation"].input_schema
            assert "topic" in schema["properties"]

            made = payload(await operator.call_tool("create_conversation", {
                "title": "the project", "participants": ["operator", "reviewer"], "topic": "project"}))
            sub = payload(await operator.call_tool("create_conversation", {
                "title": "a subtopic", "participants": ["operator", "reviewer"], "topic": "project/alpha"}))
            payload(await operator.call_tool("create_conversation", {
                "title": "a longer word", "participants": ["operator", "reviewer"], "topic": "projectile"}))
            payload(await operator.call_tool("create_conversation", {
                "title": "untagged", "participants": ["operator", "reviewer"]}))
            assert made["topic"] == "project" and sub["topic"] == "project/alpha"

            renamed = payload(await operator.call_tool("set_topic", {
                "conversation_id": made["conversation_id"], "topic": "project/renamed"}))
            assert (renamed["conversation_id"], renamed["topic"]) == (made["conversation_id"],
                                                                      "project/renamed")
            cleared = payload(await operator.call_tool("set_topic", {
                "conversation_id": sub["conversation_id"], "topic": None}))
            assert cleared["topic"] is None

            return {"all": payload(await reviewer.call_tool("list_conversations", {})),
                    "filtered": payload(await reviewer.call_tool("list_conversations", {"topic": "project"})),
                    "forbidden": payload(await reviewer.call_tool("set_topic", {
                        "conversation_id": made["conversation_id"], "topic": "mine"})),
                    "malformed": payload(await operator.call_tool("set_topic", {
                        "conversation_id": made["conversation_id"], "topic": "Bad"})),
                    # Leaving the argument out is not a way to clear a topic: it has no default, so an
                    # omission is a bad call rather than a quiet erasure.
                    "omitted": (await operator.call_tool("set_topic", {
                        "conversation_id": made["conversation_id"]})).is_error}

    seen = anyio.run(run)
    assert topics(seen["all"]) == [("a longer word", "projectile"), ("a subtopic", None),
                                   ("the project", "project/renamed"), ("untagged", None)]
    assert topics(seen["filtered"]) == [("the project", "project/renamed")]
    assert seen["forbidden"]["error"] == "forbidden"
    assert seen["malformed"]["error"] == "invalid_request"
    assert seen["omitted"] is True, "omitting the topic was accepted, which would clear it by accident"


# The local CLI --------------------------------------------------------------------------------------------


def test_the_local_command_opens_filters_and_changes_a_topic(topic_host, capsys) -> None:
    host = topic_host
    first = opened(host, capsys, "the project", "--topic", "project")
    opened(host, capsys, "a subtopic", "--topic", "project/alpha")
    opened(host, capsys, "a longer word", "--topic", "projectile")
    opened(host, capsys, "untagged")

    code, listed = local(host, capsys, "reviewer", "conversation", "list")
    assert (code, topics(listed)) == (0, [("a longer word", "projectile"), ("a subtopic", "project/alpha"),
                                          ("the project", "project"), ("untagged", None)])
    code, filtered = local(host, capsys, "reviewer", "conversation", "list", "--topic", "project")
    assert (code, topics(filtered)) == (0, [("a subtopic", "project/alpha"), ("the project", "project")])

    code, set_ = local(host, capsys, "orchestrator", "conversation", "topic", first, "--set",
                       "project/renamed")
    assert (code, set_["topic"], set_["conversation_id"]) == (0, "project/renamed", first), set_
    code, cleared = local(host, capsys, "orchestrator", "conversation", "topic", first, "--clear")
    assert (code, cleared["topic"]) == (0, None), cleared
    code, refused = local(host, capsys, "orchestrator", "conversation", "topic", first, "--set", "Bad")
    assert (code, refused["error"]) == (1, "invalid_request"), refused
    code, forbidden = local(host, capsys, "reviewer", "conversation", "topic", first, "--set", "mine")
    assert (code, forbidden["error"]) == (1, "forbidden"), forbidden


# The remote CLI -------------------------------------------------------------------------------------------


def test_the_remote_command_carries_a_topic_as_far_as_the_local_one(topic_host, capsys, monkeypatch) -> None:
    host = topic_host
    code, made = remote(host, capsys, monkeypatch, "conversation", "new", "--title", "the project",
                        "--with", GROUP, "--topic", "project")
    assert code == 0 and made["topic"] == "project", made
    code, sub = remote(host, capsys, monkeypatch, "conversation", "new", "--title", "a subtopic",
                       "--with", GROUP, "--topic", "project/alpha")
    assert code == 0 and sub["topic"] == "project/alpha", sub
    code, other = remote(host, capsys, monkeypatch, "conversation", "new", "--title", "a longer word",
                         "--with", GROUP, "--topic", "projectile")
    assert code == 0, other

    db = connect(host.config.db_path)
    try:
        assert sorted(row["topic"] for row in db.execute("SELECT topic FROM conversations")) == \
            ["project", "project/alpha", "projectile"]
    finally:
        db.close()

    code, filtered = remote(host, capsys, monkeypatch, "conversation", "list", "--topic", "project")
    assert (code, topics(filtered)) == (0, [("a subtopic", "project/alpha"), ("the project", "project")])
    code, every = remote(host, capsys, monkeypatch, "conversation", "list")
    assert (code, len(every)) == (0, 3)

    code, renamed = remote(host, capsys, monkeypatch, "conversation", "topic", made["conversation_id"],
                           "--set", "project/renamed")
    assert (code, renamed["topic"], renamed["conversation_id"]) == (0, "project/renamed",
                                                                    made["conversation_id"])
    code, cleared = remote(host, capsys, monkeypatch, "conversation", "topic", made["conversation_id"],
                           "--clear")
    assert (code, cleared["topic"]) == (0, None)
    code, refused = remote(host, capsys, monkeypatch, "conversation", "topic", made["conversation_id"],
                           "--set", "Bad")
    assert (code, refused["error"]) == (1, "invalid_request")
    code, forbidden = remote(host, capsys, monkeypatch, "conversation", "topic", sub["conversation_id"],
                             "--set", "mine", token=host.tokens["reviewer"])
    assert (code, forbidden["error"]) == (1, "forbidden")


def test_the_host_judges_a_remote_caller_by_the_same_rules_as_a_local_one(topic_host, capsys,
                                                                         monkeypatch) -> None:
    """Opening a conversation remotely is still the host's decision: the permission and membership hold."""
    host = topic_host
    code, forbidden = remote(host, capsys, monkeypatch, "conversation", "new", "--title", "not mine to open",
                             "--with", GROUP, "--topic", "project", token=host.tokens["reviewer"])
    assert (code, forbidden["error"]) == (1, "forbidden"), forbidden
    code, outside = remote(host, capsys, monkeypatch, "conversation", "new", "--title", "without me",
                           "--with", "orchestrator,reviewer,assistant", "--topic", "project")
    assert (code, outside["error"]) == (1, "invalid_request"), outside
    db = connect(host.config.db_path)
    try:
        assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    finally:
        db.close()


# The dashboard --------------------------------------------------------------------------------------------


def test_the_dashboard_groups_by_topic_and_still_shows_the_untagged(tmp_path) -> None:
    config = view_config(tmp_path, view_all=True)
    conn = connect(config.db_path)
    try:
        for title, topic in (("the project", "project"), ("a subtopic", "project/alpha"),
                             ("a longer word", "projectile"), ("untagged", None)):
            conversations.create(conn, config, actor="orchestrator", title=title,
                                 participants=["orchestrator", "reviewer", "operator"], topic=topic)
    finally:
        conn.close()

    page = text(get(config, "/"))
    for title in ("the project", "a subtopic", "a longer word", "untagged"):
        assert title in page, title
    for topic in ("project", "project/alpha", "projectile"):
        assert topic in page, topic
    # Grouped, not just labelled: each topic heads the conversations under it, and the untagged have a
    # group of their own rather than disappearing.
    assert page.index("project/alpha") < page.index("projectile")
    assert "No topic" in page
    assert page.index("No topic") > page.index("projectile")
    assert "<script" not in page
