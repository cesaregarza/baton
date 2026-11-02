"""What the reference tools must leave exactly as it was. Every test here is a GUARD: it passes on the code before the
reference tools exist and must pass after.

What this pins:

* Every tool that existed before has the same name, input schema, annotations and whitespace-collapsed description, in
  the same order, and there is still no tool to propose, list or renounce a topic grant.
* A guest, with or without a binding, and a grantee that presents no key, is refused every tool that was not a guest
  tool in the same words as before.
* A run of topic, thread and message calls by core roles answers byte for byte as before (ids and times aside), and
  its audit lines are unchanged; no existing tool's response gained a field, and a stale job move still carries no
  `current_revision`, though a stale reference does.
* `baton/app.py` stays at 800 lines or fewer.

The tools that are new, and what they must be, are in `test_reference_surface.py`; they cannot pass before they exist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import reference_support as refs
import topic_grant_tools_support as support
from reference_support import Anonymous, Bound, Core, board, bound_guest
from test_aliases import rpc
from test_topic_grant_no_tool_change import GUEST_MAY_CALL, TOOLS, fingerprint, required_arguments
from topic_grant_tools_support import play, stage, tool

from baton import app as app_module
from baton import cli

build_scene = refs.build_scene  # the fixture, under the name the tests ask for

NEW_TOOLS = {"edit_reference", "read_reference", "reference_history"}
OLD_TOOLS = {name: pinned for name, pinned in TOOLS.items() if name not in NEW_TOOLS}
APP = Path(app_module.__file__)

TIME, ID = "<time>", "<id>"
VOLATILE_TIMES = {"created_at", "last_activity_at", "at", "updated_at"}
VOLATILE_IDS = {"request_id", "conversation_id", "message_id", "id", "job_id", "topic_id", "alias_id", "grant_id"}


def normal(value):
    """A response or an audit line with its times and generated ids replaced, so a run can be compared to a record."""
    if isinstance(value, dict):
        return {key: TIME if key in VOLATILE_TIMES and item else ID if key in VOLATILE_IDS and item else normal(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [normal(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"job_[0-9a-f]{16}", ID, value)
    return value


async def listed_tools(client, host, role):
    return (await rpc(client, host, "tools/list", {}, role=role))["result"]["tools"]


# 1. The tools that existed ---------------------------------------------------------------------------------

def test_every_tool_that_existed_keeps_its_schema_annotations_description_and_place(build_scene):
    """GUARD. Seen by a core role and by a guest alike: each earlier tool's fingerprint (name, description with its
    whitespace collapsed, input schema, annotations) is what the topic grant checks left it, and they come in the
    same order whatever else is listed among them."""
    scene = build_scene()

    async def scenario(client):
        for role in ("planner", "guest"):
            shown = {one["name"]: fingerprint(one) for one in await listed_tools(client, scene.host, role)}
            assert [name for name in shown if name in OLD_TOOLS] == list(OLD_TOOLS), f"{role} sees another order"
            assert {name: shown.get(name) for name in OLD_TOOLS} == OLD_TOOLS, [
                name for name in OLD_TOOLS if shown.get(name) != OLD_TOOLS[name]]

    play(scene.host, scenario)


def test_the_tool_list_still_has_no_tool_to_propose_list_or_renounce_a_topic_grant(build_scene):
    """GUARD. Grants are proposed, authorized and revoked by the owner on the host. No tool, for a core role or a guest,
    has `grant` in its name, now that the reference tools are listed too."""
    scene = build_scene()

    async def scenario(client):
        for role in ("planner", "guest"):
            names = [one["name"] for one in await listed_tools(client, scene.host, role)]
            assert [name for name in names if "grant" in name.lower()] == [], role

    play(scene.host, scenario)


def test_a_guest_is_refused_every_tool_that_was_not_a_guest_tool_in_the_same_words(build_scene):
    """GUARD. With no binding, with a binding and no key, and as a grantee that sends its binding token but no key:
    `guest may not call <tool>`, for every earlier tool a guest could not call, whatever the arguments."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        tools = [one for one in await listed_tools(client, scene.host, "guest")
                 if one["name"] in OLD_TOOLS and one["name"] not in GUEST_MAY_CALL]
        assert len(tools) == 16, [one["name"] for one in tools]
        callers = [Anonymous(scene.host), await bound_guest(client, scene), Bound.of(held)]
        for one in tools:
            for who in callers:
                result = await who.call(client, one["name"], **required_arguments(one))
                assert result == {"error": "forbidden", "detail": f"guest may not call {one['name']}"}, (
                    one["name"], result)

    play(scene.host, scenario)


# 2. A run of core calls, answered and audited as before -------------------------------------------------------

GOLDEN = {
    "create a topic": {"created": True, "created_at": TIME, "created_by": "planner", "description": None,
                       "members": ["planner", "worker"], "path": "alpha", "request_id": ID, "title": "Alpha"},
    "create a subtopic": {"created": True, "created_at": TIME, "created_by": "planner", "description": None,
                          "members": [], "path": "alpha/sub", "request_id": ID, "title": "sub"},
    "create it again": {"error": "invalid_request", "detail": "topic 'alpha' already exists"},
    "a role without the flag": {
        "error": "forbidden", "detail": "worker may not create or change topics (roles.worker.can_create_topics)"},
    "get a topic": {"created_at": TIME, "created_by": "planner", "description": None, "members": ["planner", "worker"],
                    "path": "alpha", "subtopics": ["alpha/sub"], "threads": 0, "title": "Alpha"},
    "get an unknown topic": {"error": "unknown_topic", "detail": "no topic 'nope'"},
    "update a topic": {"created_at": TIME, "created_by": "planner", "description": "d",
                       "members": ["planner", "worker"], "path": "alpha", "title": "Alpha"},
    "rename a subtopic": {"created_at": TIME, "created_by": "planner", "description": None, "members": [],
                          "path": "alpha/other", "renamed_from": "alpha/sub", "subtopics": 0, "title": "sub"},
    "start a thread": {"conversation_id": ID, "created": True, "created_at": TIME, "created_by": "planner",
                       "last_activity_at": TIME, "last_seq": 0, "participants": ["planner", "worker"],
                       "request_id": ID, "revision": 0, "title": "T", "topic": "alpha"},
    "move the thread": {"changed": True, "conversation_id": ID, "topic": "alpha/other"},
    "move it to where it is": {"changed": False, "conversation_id": ID, "topic": "alpha/other"},
    "list threads": {"conversations": [{"conversation_id": ID, "created_at": TIME, "created_by": "planner",
                                        "last_activity_at": TIME, "last_seq": 0, "participants": ["planner", "worker"],
                                        "revision": 0, "title": "T", "topic": "alpha/other"}], "next_cursor": None},
    "post": {"conversation_id": ID, "created": True, "message_id": ID, "request_id": ID, "seqs": {"worker": 1}},
}


def planner_line(name, outcome, **extra):
    return {"role": "planner", "credential_role": "planner", "credential_id": "planner-token", "tool": name,
            "outcome": outcome, **extra}


GOLDEN_AUDIT = [
    planner_line("create_topic", "ok", topic="alpha", topic_id=ID, request_id=ID),
    planner_line("create_topic", "ok", topic="alpha/sub", topic_id=ID),
    planner_line("create_topic", "invalid_request", topic="alpha"),
    {"role": "worker", "credential_role": "worker", "credential_id": "worker-token", "tool": "create_topic",
     "outcome": "forbidden", "topic": "beta"},
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

        await step("create a topic", "create_topic", {"path": "alpha", "title": "Alpha",
                                                      "members": ["planner", "worker"], "request_id": "fixed-1"})
        await step("create a subtopic", "create_topic", {"path": "alpha/sub"})
        await step("create it again", "create_topic", {"path": "alpha"})
        await step("a role without the flag", "create_topic", {"path": "beta"}, "worker")
        await step("get a topic", "get_topic", {"path": "alpha"})
        await step("get an unknown topic", "get_topic", {"path": "nope"})
        await step("update a topic", "update_topic", {"path": "alpha", "description": "d"})
        await step("rename a subtopic", "rename_topic", {"path": "alpha/sub", "new_path": "alpha/other"})
        made = await tool(client, scene.host, "create_conversation", {
            "title": "T", "participants": ["planner", "worker"], "topic": "alpha"}, role="planner")
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
        "move_job": {"body", "created", "created_at", "created_by", "id", "request_id", "revision", "state", "title",
                     "updated_at", "waiting_on"},
        "get_job": {"body", "created_at", "created_by", "id", "revision", "state", "title", "updated_at",
                    "waiting_on"},
        "read_history": {"messages", "next_seq", "prev_seq", "retention_gaps"},
        "whoami": {"alias", "code", "credential_role", "effective_role", "initial_job_state", "job_states", "role",
                   "roles", "search"},
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
            "create_job": job, "move_job": moved,
            "get_job": await planner.call(client, "get_job", job_id=job["job_id"]),
            "read_history": await planner.call(client, "read_history", conversation_id=conversation),
            "whoami": await planner.call(client, "whoami"),
            "claim_alias": await tool(client, scene.host, "claim_alias", {
                "name": "lead-scout", "passphrase": support.PASSPHRASE}),
            "list_topics": await planner.call(client, "list_topics"),
        }
        assert {name: set(answer) for name, answer in got.items()} == expected
        assert set((await planner.call(client, "list_jobs"))[0]) == expected["get_job"] - {"body"}
        stale = await planner.call(client, "move_job", job_id=job["job_id"], to_state="review", expected_revision=1)
        assert stale.get("error") == "stale_revision" and set(stale) == {"error", "detail"}, stale

    play(scene.host, scenario)


def test_there_is_no_command_line_command_for_references():
    """GUARD. Q12: the reference tools are for agents; a core human gets no CLI command or page for them in this change,
    which a later ruling would add with its own tests and its own visual review."""
    parser = cli._parser()
    for argv in (["reference"], ["references"], ["reference", "list"], ["references", "show", "job-hunt"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


# 3. The size of the wiring ---------------------------------------------------------------------------------

def test_app_py_is_at_most_800_lines():
    """GUARD. `baton/app.py` stays one screen of wiring: the reference tools' bodies are not in it."""
    lines = APP.read_text().splitlines()
    assert len(lines) <= 800, f"baton/app.py is {len(lines)} lines"
