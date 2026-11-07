"""MCP tool discovery, grant arguments, and keyless guest permissions.

Public names and behavior are explicit contracts. Descriptions and registration order can change without
recomputing snapshots. Activating a grant does not change schemas or authorize a call that presents no key.
"""

from __future__ import annotations

import pytest
import test_topic_guest_surface as surface
from test_aliases import dispatch, rpc
from test_topic_guest_surface import call, claim, make_topic
from topic_grant_support import CAPABILITIES, grants_module

from baton import app
from baton.database import store

guest_host = surface.guest_host  # the credentials the topic host is built on
topic_host = surface.topic_host

# The public tool names are stable. Individual argument and permission contracts are covered at their
# behavior boundaries and in their owning test modules.
TOOL_NAMES = [
    "whoami",
    "alias_exists",
    "claim_alias",
    "bind_events",
    "unbind_events",
    "send_message",
    "read_messages",
    "wait",
    "create_job",
    "move_job",
    "get_job",
    "list_jobs",
    "read_job_events",
    "read_secretary_summaries",
    "create_conversation",
    "list_conversations",
    "read_history",
    "add_participant",
    "remove_participant",
    "set_topic",
    "create_topic",
    "update_topic",
    "rename_topic",
    "list_topics",
    "get_topic",
    "edit_reference",
    "read_reference",
    "reference_history",
    "search",
    "get_message",
    "read_artifacts",
]
GUEST_MAY_CALL = {
    "whoami",
    "alias_exists",
    "claim_alias",
    "list_conversations",
    "read_history",
    "get_message",
    "search",
    "send_message",
    "create_conversation",
    "list_topics",
    "get_topic",
    "read_reference",
    "reference_history",
}


async def listed_tools(client, host, role):
    return (await rpc(client, host, "tools/list", {}, role=role))["result"]["tools"]


def test_the_public_tool_names_are_stable(topic_host):  # GUARD
    async def scenario(client):
        for role in ("planner", "guest"):
            shown = sorted(tool["name"] for tool in await listed_tools(client, topic_host, role))
            assert shown == sorted(TOOL_NAMES), f"{role} sees a different set of tool names"

    dispatch(topic_host, scenario)


# Only the tools that consume a grant expose its input arguments.
GRANT_ARGUMENTS = {
    "grant_token": {"create_topic", "set_topic", "edit_reference"},
    "acknowledge_topic_grants": {"rename_topic"},
}


def test_only_grant_aware_tools_accept_grant_arguments(topic_host):
    async def scenario(client):
        for tool in await listed_tools(client, topic_host, "planner"):
            properties = tool["inputSchema"].get("properties", {})
            assert "grant_key" not in properties, tool["name"]
            for argument, tools in GRANT_ARGUMENTS.items():
                assert (argument in properties) == (tool["name"] in tools), (argument, tool["name"])

    dispatch(topic_host, scenario)


def test_the_guest_tools_are_the_same_eleven_and_the_two_that_read_references(topic_host):  # GUARD
    assert app.GUEST_TOOLS == GUEST_MAY_CALL


def required_arguments(tool: dict) -> dict:
    kinds = {"string": "x", "integer": 0, "number": 0, "boolean": False, "array": [], "object": {}}
    schema = tool["inputSchema"]
    return {
        key: kinds.get(spec.get("type"), "x")
        for key, spec in schema.get("properties", {}).items()
        if key in schema.get("required", [])
    }


def test_a_guest_is_refused_every_other_tool_with_the_same_words(topic_host):  # GUARD
    async def scenario(client):
        for tool in await listed_tools(client, topic_host, "guest"):
            if tool["name"] in GUEST_MAY_CALL:
                continue
            result = await call(client, topic_host, tool["name"], required_arguments(tool))
            assert result == {"error": "forbidden", "detail": f"guest may not call {tool['name']}"}, tool["name"]

    dispatch(topic_host, scenario)


GUEST_ANSWERS = [
    ("get_topic", {"path": "nope"}, {"error": "unknown_topic", "detail": "no topic 'nope'"}),
    ("read_history", {"conversation_id": "missing"}, {"error": "unknown_conversation", "detail": "missing"}),
    (
        "get_message",
        {"message_id": "missing"},
        {"error": "unknown_message", "detail": "there is no message missing that you can read"},
    ),
    ("alias_exists", {"name": "lane-a"}, {"exists": False}),
    ("list_conversations", {}, {"conversations": [], "next_cursor": None}),
    (
        "create_conversation",
        {"title": "t", "topic": "reviews"},
        {"error": "forbidden", "detail": "alias binding is missing, invalid or expired"},
    ),
    (
        "send_message",
        {"to": "planner", "text": "x"},
        {"error": "forbidden", "detail": "alias binding is missing, invalid or expired"},
    ),
]


@pytest.mark.parametrize(
    "name,arguments,expected", GUEST_ANSWERS, ids=[f"{row[0]}-{number}" for number, row in enumerate(GUEST_ANSWERS)]
)
def test_a_guests_calls_are_answered_as_they_were(topic_host, name, arguments, expected):  # GUARD
    async def scenario(client):
        await make_topic(client, topic_host)
        assert await call(client, topic_host, name, arguments) == expected

    dispatch(topic_host, scenario)


def test_a_bound_guest_is_still_refused_the_topic_tools_in_the_same_words(topic_host):  # GUARD
    async def scenario(client):
        await make_topic(client, topic_host)
        token = (await claim(client, topic_host))["binding_token"]
        for name, arguments in (
            ("create_topic", {"path": "mine", "members": ["planner", "worker"]}),
            ("update_topic", {"path": "reviews", "title": "t"}),
            ("rename_topic", {"path": "reviews", "new_path": "y"}),
            ("set_topic", {"conversation_id": "conv_x", "topic": "reviews"}),
        ):
            result = await call(client, topic_host, name, {**arguments, "token": token})
            assert result == {"error": "forbidden", "detail": f"guest may not call {name}"}, name
        unknown = await call(
            client, topic_host, "create_conversation", {"title": "t", "topic": "nowhere", "token": token}
        )
        assert unknown == {
            "error": "invalid_request",
            "detail": "topic 'nowhere' is not a topic; a core role opens one with create_topic",
        }

    dispatch(topic_host, scenario)


def test_an_active_grant_changes_no_answer_to_a_call_that_presents_no_key(topic_host):
    """The grant belongs to a key, not to the alias holding it: calls with a binding token alone are answered as
    they were, and the guest's tool list is the same."""
    tg = grants_module()

    async def scenario(client):
        await make_topic(client, topic_host)
        token = (await claim(client, topic_host))["binding_token"]
        asked = [
            ("create_topic", {"path": "mine", "members": ["planner", "worker"], "token": token}),
            ("set_topic", {"conversation_id": "conv_x", "topic": "reviews", "token": token}),
            ("update_topic", {"path": "reviews", "title": "t", "token": token}),
            ("rename_topic", {"path": "reviews", "new_path": "y", "token": token}),
            ("create_conversation", {"title": "t", "topic": "nowhere", "token": token}),
        ]
        before = [await call(client, topic_host, name, arguments) for name, arguments in asked]
        shown_before = [
            (tool["name"], tool["inputSchema"], tool["annotations"])
            for tool in await listed_tools(client, topic_host, "guest")
        ]

        conn = store.connect(topic_host.config.db_path)
        try:
            proposal = tg.propose(conn, "lane-a", "reviews", list(CAPABILITIES), True, 7)
            granted = tg.authorize(conn, proposal["id"], proposal["digest"])
        finally:
            conn.close()
        assert granted["grant_key"], "the grant is active"

        assert [await call(client, topic_host, name, arguments) for name, arguments in asked] == before
        shown_after = [
            (tool["name"], tool["inputSchema"], tool["annotations"])
            for tool in await listed_tools(client, topic_host, "guest")
        ]
        assert shown_after == shown_before

    dispatch(topic_host, scenario)


def test_the_core_roles_topic_refusals_are_unchanged(topic_host):  # GUARD
    async def scenario(client):
        await make_topic(client, topic_host)
        assert await call(client, topic_host, "create_topic", {"path": "reviews"}, role="planner") == {
            "error": "invalid_request",
            "detail": "topic 'reviews' already exists",
        }
        assert await call(client, topic_host, "create_topic", {"path": "other"}, role="worker") == {
            "error": "forbidden",
            "detail": "worker may not create or change topics (roles.worker.can_create_topics)",
        }

    dispatch(topic_host, scenario)
