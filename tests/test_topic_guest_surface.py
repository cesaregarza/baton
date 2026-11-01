"""The transport rules for guest threads and the topic tools, through in-process MCP dispatch only.

What the guard owes the topic model: `create_conversation` is a guest tool, a guest call needs a
live alias binding, and a guest may not name participants. The topic tools themselves stay core-only
at the transport, so a guest never reaches their bodies. The rules inside those bodies are tested in
test_topic_containers.py; this file is about what the wire allows.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_aliases import (
    dispatch,
    guest_host,  # noqa: F401 -- synthetic credential fixture
    payload,
    rpc,
)

from baton import app
from baton.config import Role

TOPIC = "reviews"


@pytest.fixture
def topic_host(guest_host):  # noqa: F811 -- imported pytest fixture
    config = guest_host.config
    roles = {
        **config.roles,
        "planner": replace(config.roles["planner"], can_create_conversations=True,
                           can_create_topics=True, can_view_all_conversations=True),
        "guest": Role(name="guest", can_view_all_conversations=True),
    }
    guest_host.config = replace(
        config,
        roles=roles,
        conversations=replace(config.conversations, enabled=True),
        search=replace(config.search, enabled=True),
    )
    return guest_host


async def call(client, host, name, arguments=None, role="guest"):
    """The tool's result, or the refusal as given. The shared parser expects JSON, and a refusal is text,
    so catching that here is what lets a test assert on the refusal rather than die parsing it."""
    response = await rpc(client, host, "tools/call", {"name": name, "arguments": arguments or {}}, role=role)
    try:
        return payload(response)
    except (ValueError, KeyError, IndexError):
        return {"unparsed": str(response)}


async def make_topic(client, host, path=TOPIC, members=("planner", "worker")):
    return await call(client, host, "create_topic", {"path": path, "members": list(members)}, role="planner")


async def claim(client, host, name="lane-a"):
    return await call(client, host, "claim_alias", {"name": name, "passphrase": "synthetic alias secret"})


def refusal(result):
    """The text of a refused tool call, whichever shape the server used."""
    if isinstance(result, dict) and "content" in result:
        return " ".join(part.get("text", "") for part in result["content"])
    return str(result)


def test_create_conversation_is_a_guest_tool():
    assert "create_conversation" in app.GUEST_TOOLS


def test_the_topic_tools_are_not_guest_tools():
    for name in ("create_topic", "update_topic", "rename_topic"):
        assert name not in app.GUEST_TOOLS, f"{name} must stay core-only"


def test_reading_topics_is_open_to_a_guest():
    for name in ("list_topics", "get_topic"):
        assert name in app.GUEST_TOOLS


def test_an_anonymous_guest_cannot_start_a_thread(topic_host):
    """No token means no alias binding, so the guard refuses before any topic check."""

    async def scenario(client):
        await make_topic(client, topic_host)
        result = await call(client, topic_host, "create_conversation", {"title": "Mine", "topic": TOPIC})
        assert "binding" in refusal(result).lower(), refusal(result)

    dispatch(topic_host, scenario)


def test_an_aliased_guest_starts_a_thread_and_is_attributed(topic_host):
    async def scenario(client):
        await make_topic(client, topic_host)
        bound = await claim(client, topic_host)
        made = await call(client, topic_host, "create_conversation",
                          {"title": "A guest thread", "topic": TOPIC, "token": bound["binding_token"]})
        assert made.get("created") is True, refusal(made)
        assert made.get("topic") == TOPIC, refusal(made)
        assert made.get("created_by") == "guest", refusal(made)
        assert made.get("created_by_alias") == "lane-a", refusal(made)
        assert made.get("created_by_alias_code") == bound["code"], refusal(made)

    dispatch(topic_host, scenario)


def test_an_aliased_guest_may_not_name_participants(topic_host):
    async def scenario(client):
        await make_topic(client, topic_host)
        bound = await claim(client, topic_host)
        result = await call(client, topic_host, "create_conversation",
                            {"title": "Mine", "topic": TOPIC, "participants": ["planner"],
                             "token": bound["binding_token"]})
        assert "participants" in refusal(result), refusal(result)

    dispatch(topic_host, scenario)


def test_a_guest_cannot_create_a_topic_over_the_wire(topic_host):
    async def scenario(client):
        bound = await claim(client, topic_host)
        result = await call(client, topic_host, "create_topic",
                            {"path": "mine", "members": ["planner", "worker"], "token": bound["binding_token"]})
        assert result.get("error") == "forbidden", refusal(result)
        assert "guest may not call create_topic" in refusal(result), refusal(result)

    dispatch(topic_host, scenario)


def test_a_guest_can_read_the_topic_directory(topic_host):
    async def scenario(client):
        await make_topic(client, topic_host)
        listed = await call(client, topic_host, "list_topics")
        assert [e["path"] for e in listed.get("topics", [])] == [TOPIC], refusal(listed)
        seen = await call(client, topic_host, "get_topic", {"path": TOPIC})
        assert seen.get("path") == TOPIC, refusal(seen)

    dispatch(topic_host, scenario)


def test_a_core_role_still_names_participants(topic_host):
    """The core path is unchanged: participants are given, and the topic is optional."""

    async def scenario(client):
        made = await call(client, topic_host, "create_conversation",
                          {"title": "Core", "participants": ["planner", "worker"]}, role="planner")
        assert made.get("created") is True, refusal(made)
        assert made.get("topic") is None

    dispatch(topic_host, scenario)
