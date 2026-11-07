"""Reference-tool compatibility: keyless guest permissions, core responses, and audit records.

Public tool discovery and grant administration boundaries live in the shared tool contract tests.
Reference schemas and access rules live in test_reference_surface.py.
"""

from __future__ import annotations

import re

import reference_support as refs
import topic_grant_tools_support as support
from reference_support import Anonymous, Bound, Core, board, bound_guest
from test_aliases import rpc
from test_tool_contracts import GUEST_MAY_CALL, required_arguments
from topic_grant_tools_support import play, stage, tool

build_scene = refs.build_scene  # the fixture, under the name the tests ask for

TIME, ID = "<time>", "<id>"
VOLATILE_TIMES = {"created_at", "last_activity_at", "at", "updated_at"}
VOLATILE_IDS = {"request_id", "conversation_id", "message_id", "id", "job_id", "topic_id", "alias_id", "grant_id"}


def normal(value):
    """A response or an audit line with its times and generated ids replaced, so a run can be compared to a record."""
    if isinstance(value, dict):
        return {
            key: TIME if key in VOLATILE_TIMES and item else ID if key in VOLATILE_IDS and item else normal(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normal(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"job_[0-9a-f]{16}", ID, value)
    return value


async def listed_tools(client, host, role):
    return (await rpc(client, host, "tools/list", {}, role=role))["result"]["tools"]


# 1. The tools that existed ---------------------------------------------------------------------------------


def test_a_guest_is_refused_every_tool_that_was_not_a_guest_tool_in_the_same_words(build_scene):
    """GUARD. With no binding, with a binding and no key, and as a grantee that sends its binding token but no key:
    `guest may not call <tool>`, for every earlier tool a guest could not call, whatever the arguments."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        tools = [one for one in await listed_tools(client, scene.host, "guest") if one["name"] not in GUEST_MAY_CALL]
        assert tools, "the scenario must exercise restricted tools"
        callers = [Anonymous(scene.host), await bound_guest(client, scene), Bound.of(held)]
        for one in tools:
            for who in callers:
                result = await who.call(client, one["name"], **required_arguments(one))
                assert result == {"error": "forbidden", "detail": f"guest may not call {one['name']}"}, (
                    one["name"],
                    result,
                )

    play(scene.host, scenario)


# 2. A run of core calls, answered and audited as before -------------------------------------------------------

GOLDEN = {
    "create a topic": {
        "created": True,
        "created_at": TIME,
        "created_by": "planner",
        "description": None,
        "members": ["planner", "worker"],
        "path": "alpha",
        "request_id": ID,
        "title": "Alpha",
    },
    "create a subtopic": {
        "created": True,
        "created_at": TIME,
        "created_by": "planner",
        "description": None,
        "members": [],
        "path": "alpha/sub",
        "request_id": ID,
        "title": "sub",
    },
    "create it again": {"error": "invalid_request", "detail": "topic 'alpha' already exists"},
    "a role without the flag": {
        "error": "forbidden",
        "detail": "worker may not create or change topics (roles.worker.can_create_topics)",
    },
    "get a topic": {
        "created_at": TIME,
        "created_by": "planner",
        "description": None,
        "members": ["planner", "worker"],
        "path": "alpha",
        "subtopics": ["alpha/sub"],
        "threads": 0,
        "title": "Alpha",
    },
    "get an unknown topic": {"error": "unknown_topic", "detail": "no topic 'nope'"},
    "update a topic": {
        "created_at": TIME,
        "created_by": "planner",
        "description": "d",
        "members": ["planner", "worker"],
        "path": "alpha",
        "title": "Alpha",
    },
    "rename a subtopic": {
        "created_at": TIME,
        "created_by": "planner",
        "description": None,
        "members": [],
        "path": "alpha/other",
        "renamed_from": "alpha/sub",
        "subtopics": 0,
        "title": "sub",
    },
    "start a thread": {
        "conversation_id": ID,
        "created": True,
        "created_at": TIME,
        "created_by": "planner",
        "last_activity_at": TIME,
        "last_seq": 0,
        "participants": ["planner", "worker"],
        "request_id": ID,
        "revision": 0,
        "title": "T",
        "topic": "alpha",
    },
    "move the thread": {"changed": True, "conversation_id": ID, "topic": "alpha/other"},
    "move it to where it is": {"changed": False, "conversation_id": ID, "topic": "alpha/other"},
    "list threads": {
        "conversations": [
            {
                "conversation_id": ID,
                "created_at": TIME,
                "created_by": "planner",
                "last_activity_at": TIME,
                "last_seq": 0,
                "participants": ["planner", "worker"],
                "revision": 0,
                "title": "T",
                "topic": "alpha/other",
            }
        ],
        "next_cursor": None,
    },
    "post": {"conversation_id": ID, "created": True, "message_id": ID, "request_id": ID, "seqs": {"worker": 1}},
}


def planner_line(name, outcome, **extra):
    return {
        "role": "planner",
        "credential_role": "planner",
        "credential_id": "planner-token",
        "tool": name,
        "outcome": outcome,
        **extra,
    }


GOLDEN_AUDIT = [
    planner_line("create_topic", "ok", topic="alpha", topic_id=ID, request_id=ID),
    planner_line("create_topic", "ok", topic="alpha/sub", topic_id=ID),
    planner_line("create_topic", "invalid_request", topic="alpha"),
    {
        "role": "worker",
        "credential_role": "worker",
        "credential_id": "worker-token",
        "tool": "create_topic",
        "outcome": "forbidden",
        "topic": "beta",
    },
    planner_line("get_topic", "ok", topic="alpha", topic_id=ID),
    planner_line("get_topic", "unknown_topic", topic="nope"),
    planner_line("update_topic", "ok", topic="alpha", topic_id=ID),
    planner_line("rename_topic", "ok", topic="alpha/sub", topic_id=ID, new_path="alpha/other"),
    planner_line("create_conversation", "ok", topic="alpha", topic_id=ID),
    planner_line("set_topic", "ok", topic="alpha/other", topic_id=ID, conversation_id=ID),
    planner_line("set_topic", "ok", topic="alpha/other", topic_id=ID, conversation_id=ID),
    planner_line("list_conversations", "ok"),
    planner_line("send_message", "ok", conversation_id=ID),
]


def test_a_run_of_core_calls_with_no_reference_in_it_answers_and_audits_as_before(build_scene):
    """GUARD. Topics created, read, updated and renamed, a thread started, moved and listed, a message posted, and the
    refusals among them: each answer, ids and times aside, is what the topic grant checks answered, and the audit
    lines of those tools (S2's included, which this change widens a set for) are unchanged byte for byte."""
    scene = build_scene()
    seen = {}

    async def scenario(client):
        async def step(label, name, arguments, role="planner"):
            seen[label] = normal(await tool(client, scene.host, name, arguments, role=role))

        await step(
            "create a topic",
            "create_topic",
            {"path": "alpha", "title": "Alpha", "members": ["planner", "worker"], "request_id": "fixed-1"},
        )
        await step("create a subtopic", "create_topic", {"path": "alpha/sub"})
        await step("create it again", "create_topic", {"path": "alpha"})
        await step("a role without the flag", "create_topic", {"path": "beta"}, "worker")
        await step("get a topic", "get_topic", {"path": "alpha"})
        await step("get an unknown topic", "get_topic", {"path": "nope"})
        await step("update a topic", "update_topic", {"path": "alpha", "description": "d"})
        await step("rename a subtopic", "rename_topic", {"path": "alpha/sub", "new_path": "alpha/other"})
        made = await tool(
            client,
            scene.host,
            "create_conversation",
            {"title": "T", "participants": ["planner", "worker"], "topic": "alpha"},
            role="planner",
        )
        seen["start a thread"] = normal(made)
        move = {"conversation_id": made["conversation_id"], "topic": "alpha/other"}
        await step("move the thread", "set_topic", move)
        await step("move it to where it is", "set_topic", move)
        await step("list threads", "list_conversations", {})
        await step("post", "send_message", {"conversation_id": made["conversation_id"], "text": "hello"})

    play(scene.host, scenario)
    assert seen == GOLDEN
    lines = [normal({key: value for key, value in line.items() if key != "at"}) for line in scene.audit()]
    assert lines == GOLDEN_AUDIT


def test_no_existing_tools_response_gained_a_field_and_a_stale_job_move_has_no_current_revision(build_scene):
    """GUARD. The key sets of the other tools' answers are what they were. The stale refusal of `move_job` is still just
    its code and its words: `current_revision` is a field of the stale reference refusal alone, though the one is a
    subclass of the other."""
    scene = build_scene()
    expected = {
        "create_job": {"created", "job_id", "revision", "state"},
        "move_job": {
            "body",
            "created",
            "created_at",
            "created_by",
            "id",
            "request_id",
            "revision",
            "state",
            "title",
            "updated_at",
            "waiting_on",
        },
        "get_job": {"body", "created_at", "created_by", "id", "revision", "state", "title", "updated_at", "waiting_on"},
        "read_history": {"messages", "next_seq", "prev_seq", "retention_gaps"},
        "whoami": {
            "alias",
            "code",
            "credential_role",
            "effective_role",
            "initial_job_state",
            "job_states",
            "role",
            "roles",
            "search",
        },
        "claim_alias": {"alias_id", "alias", "badge", "code", "effective_role", "binding_token", "expires_at"},
        "list_topics": {"topics"},
    }

    async def scenario(client):
        await stage(client, scene.host)
        planner = Core(scene.host)
        job = await planner.call(client, "create_job", title="J", body="b")
        moved = await planner.call(client, "move_job", job_id=job["job_id"], to_state="working", expected_revision=1)
        conversation = await support.thread(client, scene.host, "job-hunt")
        got = {
            "create_job": job,
            "move_job": moved,
            "get_job": await planner.call(client, "get_job", job_id=job["job_id"]),
            "read_history": await planner.call(client, "read_history", conversation_id=conversation),
            "whoami": await planner.call(client, "whoami"),
            "claim_alias": await tool(
                client, scene.host, "claim_alias", {"name": "lead-scout", "passphrase": support.PASSPHRASE}
            ),
            "list_topics": await planner.call(client, "list_topics"),
        }
        assert {name: set(answer) for name, answer in got.items()} == expected
        assert set((await planner.call(client, "list_jobs"))[0]) == expected["get_job"] - {"body"}
        stale = await planner.call(client, "move_job", job_id=job["job_id"], to_state="review", expected_revision=1)
        assert stale.get("error") == "stale_revision" and set(stale) == {"error", "detail"}, stale

    play(scene.host, scenario)
