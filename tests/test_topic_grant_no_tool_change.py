"""The topic grant check changes three tools and no other, and a call that presents no key is answered as before.

The tool list, every other tool's input schema and description, every refusal text and every guest call stay
exactly as they were. `create_topic` and `set_topic` gain the optional `grant_token`, and `rename_topic` gains
`acknowledge_topic_grants`; their pins below were recomputed for exactly that. A guest that presents no key is
refused in the same words whatever grants exist. The three reference tools, which came after, are pinned here as
they were made: `edit_reference` takes `grant_token` too, and a guest may call the two that read.

GUARD tests pin today's answers and pass before and after. The last of the grant tests needs the grant records, so it
fails until they exist: with an active grant on a topic, a guest's calls that present no key are still refused,
with the same words.
"""

from __future__ import annotations

import hashlib
import json

import pytest
import test_topic_guest_surface as surface
from test_aliases import dispatch, rpc
from test_topic_guest_surface import call, claim, make_topic
from topic_grant_support import CAPABILITIES, grants_module

from baton import app
from baton.database import store

guest_host = surface.guest_host  # the credentials the topic host is built on
topic_host = surface.topic_host

# Each tool as `tools/list` shows it (name, description, input schema, annotations), shortened to 12 hex
# digits of its canonical JSON: a change to any one shows up as that one tool's line.
TOOLS = {
    "whoami": "1c78530665fc", "alias_exists": "adecd0694710", "claim_alias": "eeac78be5de2",
    "bind_events": "652b57bd4ba2", "unbind_events": "753c08daddc6", "send_message": "ac8107ee5796",
    "read_messages": "3160263c55ef", "wait": "a2490f8b3a2f", "create_job": "e6a521745eed",
    "move_job": "f242c8d7c9f3", "get_job": "cb0fd5be32be", "list_jobs": "7af25ba35768",
    "read_job_events": "1248657164ef", "create_conversation": "4c477d055d79",
    "list_conversations": "d2564ef0696d", "read_history": "a77467aef649", "add_participant": "aa43e161b34c",
    "remove_participant": "4f585683f8ad", "set_topic": "fc1d16794258", "create_topic": "e7ca129d5f35",
    "update_topic": "95e4df4d625a", "rename_topic": "383655a18830", "list_topics": "a7b2f4a13b37",
    "get_topic": "bf27aa557812", "edit_reference": "6d122b95d779", "read_reference": "673746dd5430",
    "reference_history": "91110adca101", "search": "a66624d6f016", "get_message": "d249b55ec56d",
    "read_artifacts": "240da135d827",
}
GUEST_MAY_CALL = {"whoami", "alias_exists", "claim_alias", "list_conversations", "read_history", "get_message",
                  "search", "send_message", "create_conversation", "list_topics", "get_topic", "read_reference",
                  "reference_history"}


def fingerprint(tool: dict) -> str:
    # A description comes from a docstring, whose indentation Python 3.12 keeps and 3.13 strips: compare words.
    tool = {**tool, "description": " ".join(tool["description"].split())}
    return hashlib.sha256(json.dumps(tool, sort_keys=True).encode()).hexdigest()[:12]


async def listed_tools(client, host, role):
    return (await rpc(client, host, "tools/list", {}, role=role))["result"]["tools"]


def test_the_tool_list_and_every_schema_are_unchanged(topic_host):  # GUARD
    async def scenario(client):
        for role in ("planner", "guest"):
            shown = {tool["name"]: fingerprint(tool) for tool in await listed_tools(client, topic_host, role)}
            assert list(shown) == list(TOOLS), f"{role} sees a different list of tools"
            assert shown == TOOLS, [name for name in TOOLS if shown.get(name) != TOOLS[name]]
    dispatch(topic_host, scenario)


# The only places a tool's text may name a topic grant: the key a guest presents, and a rename's acknowledgement.
GRANT_ARGUMENTS = {"grant_token": {"create_topic", "set_topic", "edit_reference"},
                   "acknowledge_topic_grants": {"rename_topic"}}


def test_a_topic_grant_is_named_only_by_the_arguments_that_use_one_and_no_tool_names_a_key(topic_host):  # GUARD
    async def scenario(client):
        for tool in await listed_tools(client, topic_host, "planner"):
            text = json.dumps(tool).lower()
            assert "grant_key" not in text and "tgk_" not in text, tool["name"]
            for argument, tools in GRANT_ARGUMENTS.items():
                assert (argument in text) == (tool["name"] in tools), (argument, tool["name"])
    dispatch(topic_host, scenario)


def test_the_guest_tools_are_the_same_eleven_and_the_two_that_read_references(topic_host):  # GUARD
    assert app.GUEST_TOOLS & set(TOOLS) == GUEST_MAY_CALL


def required_arguments(tool: dict) -> dict:
    kinds = {"string": "x", "integer": 0, "number": 0, "boolean": False, "array": [], "object": {}}
    schema = tool["inputSchema"]
    return {key: kinds.get(spec.get("type"), "x") for key, spec in schema.get("properties", {}).items()
            if key in schema.get("required", [])}


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
    ("get_message", {"message_id": "missing"},
     {"error": "unknown_message", "detail": "there is no message missing that you can read"}),
    ("alias_exists", {"name": "lane-a"}, {"exists": False}),
    ("list_conversations", {}, {"conversations": [], "next_cursor": None}),
    ("create_conversation", {"title": "t", "topic": "reviews"},
     {"error": "forbidden", "detail": "alias binding is missing, invalid or expired"}),
    ("send_message", {"to": "planner", "text": "x"},
     {"error": "forbidden", "detail": "alias binding is missing, invalid or expired"}),
]


@pytest.mark.parametrize("name,arguments,expected", GUEST_ANSWERS,
                         ids=[f"{row[0]}-{number}" for number, row in enumerate(GUEST_ANSWERS)])
def test_a_guests_calls_are_answered_as_they_were(topic_host, name, arguments, expected):  # GUARD
    async def scenario(client):
        await make_topic(client, topic_host)
        assert await call(client, topic_host, name, arguments) == expected
    dispatch(topic_host, scenario)


def test_a_bound_guest_is_still_refused_the_topic_tools_in_the_same_words(topic_host):  # GUARD
    async def scenario(client):
        await make_topic(client, topic_host)
        token = (await claim(client, topic_host))["binding_token"]
        for name, arguments in (("create_topic", {"path": "mine", "members": ["planner", "worker"]}),
                                ("update_topic", {"path": "reviews", "title": "t"}),
                                ("rename_topic", {"path": "reviews", "new_path": "y"}),
                                ("set_topic", {"conversation_id": "conv_x", "topic": "reviews"})):
            result = await call(client, topic_host, name, {**arguments, "token": token})
            assert result == {"error": "forbidden", "detail": f"guest may not call {name}"}, name
        unknown = await call(client, topic_host, "create_conversation",
                             {"title": "t", "topic": "nowhere", "token": token})
        assert unknown == {"error": "invalid_request",
                           "detail": "topic 'nowhere' is not a topic; a core role opens one with create_topic"}
    dispatch(topic_host, scenario)


def test_an_active_grant_changes_no_answer_to_a_call_that_presents_no_key(topic_host):
    """The grant belongs to a key, not to the alias holding it: calls with a binding token alone are answered as
    they were, and the guest's tool list is the same."""
    tg = grants_module()

    async def scenario(client):
        await make_topic(client, topic_host)
        token = (await claim(client, topic_host))["binding_token"]
        asked = [("create_topic", {"path": "mine", "members": ["planner", "worker"], "token": token}),
                 ("set_topic", {"conversation_id": "conv_x", "topic": "reviews", "token": token}),
                 ("update_topic", {"path": "reviews", "title": "t", "token": token}),
                 ("rename_topic", {"path": "reviews", "new_path": "y", "token": token}),
                 ("create_conversation", {"title": "t", "topic": "nowhere", "token": token})]
        before = [await call(client, topic_host, name, arguments) for name, arguments in asked]
        shown_before = {tool["name"]: fingerprint(tool) for tool in await listed_tools(client, topic_host, "guest")}

        conn = store.connect(topic_host.config.db_path)
        try:
            proposal = tg.propose(conn, "lane-a", "reviews", list(CAPABILITIES), True, 7)
            granted = tg.authorize(conn, proposal["id"], proposal["digest"])
        finally:
            conn.close()
        assert granted["grant_key"], "the grant is active"

        assert [await call(client, topic_host, name, arguments) for name, arguments in asked] == before
        assert {tool["name"]: fingerprint(tool) for tool in await listed_tools(client, topic_host, "guest")} == \
            shown_before == TOOLS
    dispatch(topic_host, scenario)


def test_the_core_roles_topic_refusals_are_unchanged(topic_host):  # GUARD
    async def scenario(client):
        await make_topic(client, topic_host)
        assert await call(client, topic_host, "create_topic", {"path": "reviews"}, role="planner") == \
            {"error": "invalid_request", "detail": "topic 'reviews' already exists"}
        assert await call(client, topic_host, "create_topic", {"path": "other"}, role="worker") == \
            {"error": "forbidden", "detail": "worker may not create or change topics (roles.worker.can_create_topics)"}
    dispatch(topic_host, scenario)
