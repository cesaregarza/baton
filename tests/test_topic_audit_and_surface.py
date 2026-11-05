"""Over MCP: the audit line names the topic, the actor reaches the history, and nothing a caller sees
has changed.

In-process dispatch only, with synthetic roles and tokens. Three groups:

* The audit line (`audit.jsonl`) of the topic tools, set_topic, create_conversation and
  list_conversations. Beyond the keys it already had, it carries exactly:
    - `topic`: the path the call named, whatever the tool called its argument, and also on a refusal;
    - `topic_id`: the id of that topic, on a successful call. create_topic gives the new topic's id and
      rename_topic the renamed topic's, which a rename does not change. A refused call, an unknown path, or
      a call that names no topic (a cleared set_topic, an unfiltered list) carries no id: the key is absent
      or null;
    - `new_path` on rename_topic, the path it moved to.
  Every key the line had stays, with its value, and nothing else is added: no title, description or members.
* The history's `actor`: the calling role, exactly; for an alias-bound call the role it ran as, plus the alias's
  immutable id (not its display name) so a reader can tell the alias from a direct credential.
* GUARD: no behaviour change. Response key sets, refusal texts and tool input schemas of the topic tools, and
  of create_conversation and list_conversations, are pinned as main e17bbfb returns them. No `id` appears.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest
from conftest import make_config
from test_aliases import (  # noqa: F401 -- `guest_host` is a pytest fixture
    dispatch,
    guest_host,
    payload,
    rpc,
)
from test_per_call_identity import audit, claim, enrol

from baton import auth, conversations, topics
from baton.config import Role
from baton.database import store

BASE_KEYS = {"at", "role", "credential_role", "credential_id", "tool", "outcome"}
TOPIC_TOOLS = (
    "list_topics",
    "get_topic",
    "create_topic",
    "update_topic",
    "rename_topic",
    "set_topic",
    "create_conversation",
    "list_conversations",
)


@pytest.fixture
def host(guest_host):  # noqa: F811 -- the imported fixture
    config = guest_host.config
    manager = dict(
        can_create_conversations=True,
        can_create_topics=True,
        can_manage_participants=True,
        can_view_all_conversations=True,
    )
    roles = {
        **config.roles,
        "planner": replace(config.roles["planner"], **manager),
        "guest": Role(name="guest", can_view_all_conversations=True),
    }
    guest_host.config = replace(config, roles=roles, conversations=replace(config.conversations, enabled=True))
    return guest_host


def run(host, scenario):
    """dispatch(), but a failed assertion in the scenario surfaces as itself, not inside an ExceptionGroup."""
    failures: list[BaseException] = []

    async def guarded(client):
        try:
            await scenario(client)
        except BaseException as error:
            failures.append(error)

    dispatch(host, guarded)
    if failures:
        raise failures[0]


async def call(client, host, name, arguments=None, role="planner"):
    response = await rpc(client, host, "tools/call", {"name": name, "arguments": arguments or {}}, role=role)
    return payload(response)


def audit_lines(host) -> list[dict]:
    return [json.loads(line) for line in host.config.audit_path.read_text().splitlines()]


def last_line(host) -> dict:
    return audit_lines(host)[-1]


def database(host) -> sqlite3.Connection:
    conn = sqlite3.connect(host.config.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def topic_ids(host) -> dict[str, str]:
    with database(host) as conn:
        assert "id" in {row["name"] for row in conn.execute("PRAGMA table_info(topics)")}, "topics has no id column"
        return {row["path"]: row["id"] for row in conn.execute("SELECT path, id FROM topics")}


def changes(host) -> list[sqlite3.Row]:
    with database(host) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'topic_changes'").fetchone(), (
            "there is no topic_changes table"
        )
        return conn.execute("SELECT * FROM topic_changes ORDER BY rowid").fetchall()


def without_time(line: dict) -> dict:
    return {key: value for key, value in line.items() if key != "at"}


def planner_line(tool: str, **extra) -> dict:
    return {
        "role": "planner",
        "credential_role": "planner",
        "credential_id": "planner-token",
        "tool": tool,
        "outcome": "ok",
        **extra,
    }


async def seed(client, host):
    """baton (root) with baton/dev, and a thread in baton, made by the planner."""
    await call(client, host, "create_topic", {"path": "baton", "members": ["planner", "worker"]})
    await call(client, host, "create_topic", {"path": "baton/dev"})
    made = await call(
        client,
        host,
        "create_conversation",
        {"title": "A thread", "participants": ["planner", "worker"], "topic": "baton"},
    )
    return made["conversation_id"]


# 1. The audit line --------------------------------------------------------------------------------------


def test_create_topic_audits_the_new_topics_path_and_id(host):  # FAILS-ON-MAIN
    async def scenario(client):
        made = await call(
            client, host, "create_topic", {"path": "baton", "title": "Secret title", "description": "Secret text"}
        )
        assert made["created"] is True
        line = last_line(host)
        assert without_time(line) == planner_line("create_topic", topic="baton", topic_id=topic_ids(host)["baton"])

    run(host, scenario)


def test_update_topic_audits_the_topics_path_and_id(host):  # FAILS-ON-MAIN
    async def scenario(client):
        await seed(client, host)
        await call(client, host, "update_topic", {"path": "baton/dev", "title": "Secret title"})
        assert without_time(last_line(host)) == planner_line(
            "update_topic", topic="baton/dev", topic_id=topic_ids(host)["baton/dev"]
        )

    run(host, scenario)


def test_rename_topic_audits_the_old_path_the_new_path_and_the_unchanged_id(host):  # FAILS-ON-MAIN
    async def scenario(client):
        await seed(client, host)
        before = topic_ids(host)["baton/dev"]
        await call(client, host, "rename_topic", {"path": "baton/dev", "new_path": "baton/devx"})
        assert without_time(last_line(host)) == planner_line(
            "rename_topic", topic="baton/dev", new_path="baton/devx", topic_id=before
        )
        assert topic_ids(host)["baton/devx"] == before

    run(host, scenario)


def test_get_topic_audits_the_path_and_id(host):  # FAILS-ON-MAIN
    async def scenario(client):
        await seed(client, host)
        await call(client, host, "get_topic", {"path": "baton"})
        assert without_time(last_line(host)) == planner_line(
            "get_topic", topic="baton", topic_id=topic_ids(host)["baton"]
        )

    run(host, scenario)


def test_set_topic_audits_the_destination_path_and_id(host):  # FAILS-ON-MAIN
    async def scenario(client):
        conversation = await seed(client, host)
        moved = await call(client, host, "set_topic", {"conversation_id": conversation, "topic": "baton/dev"})
        assert moved["changed"] is True
        assert without_time(last_line(host)) == planner_line(
            "set_topic", conversation_id=conversation, topic="baton/dev", topic_id=topic_ids(host)["baton/dev"]
        )

    run(host, scenario)


def test_create_conversation_in_a_topic_audits_the_topics_path_and_id(host):  # FAILS-ON-MAIN
    async def scenario(client):
        await seed(client, host)
        await call(
            client,
            host,
            "create_conversation",
            {"title": "Another", "participants": ["planner", "worker"], "topic": "baton/dev"},
        )
        assert without_time(last_line(host)) == planner_line(
            "create_conversation", topic="baton/dev", topic_id=topic_ids(host)["baton/dev"]
        )

    run(host, scenario)


def test_list_conversations_filtered_by_topic_audits_the_path_and_id(host):  # FAILS-ON-MAIN
    async def scenario(client):
        await seed(client, host)
        await call(client, host, "list_conversations", {"topic": "baton"})
        assert without_time(last_line(host)) == planner_line(
            "list_conversations", topic="baton", topic_id=topic_ids(host)["baton"]
        )

    run(host, scenario)


def test_a_call_that_names_no_topic_audits_none(host):  # GUARD
    async def scenario(client):
        conversation = await seed(client, host)
        await call(client, host, "list_conversations", {})
        assert without_time(last_line(host)) == planner_line("list_conversations")
        await call(client, host, "list_topics", {})
        assert without_time(last_line(host)) == planner_line("list_topics")
        cleared = await call(client, host, "set_topic", {"conversation_id": conversation, "topic": None})
        assert cleared["topic"] is None
        line = last_line(host)
        assert line.get("topic") is None and line.get("topic_id") is None
        assert line["conversation_id"] == conversation

    run(host, scenario)


@pytest.mark.parametrize(
    "tool,arguments,named",
    [
        ("get_topic", {"path": "nowhere"}, "nowhere"),
        ("update_topic", {"path": "nowhere", "title": "x"}, "nowhere"),
        ("rename_topic", {"path": "nowhere", "new_path": "elsewhere"}, "nowhere"),
    ],
)
def test_a_refusal_for_an_unknown_topic_names_the_path_and_no_id(host, tool, arguments, named):  # FAILS-ON-MAIN
    async def scenario(client):
        await seed(client, host)
        refusal = await call(client, host, tool, arguments)
        assert "error" in refusal, refusal
        line = last_line(host)
        assert line["tool"] == tool and line["outcome"] == refusal["error"]
        assert line.get("topic") == named, line
        assert line.get("topic_id") is None, line

    run(host, scenario)


def test_a_refused_set_topic_names_the_destination_path_and_no_id(host):  # GUARD for the path, new for the id
    async def scenario(client):
        conversation = await seed(client, host)
        refusal = await call(client, host, "set_topic", {"conversation_id": conversation, "topic": "nowhere"})
        assert refusal["error"] == "invalid_request"
        line = last_line(host)
        assert (line["tool"], line["conversation_id"], line["topic"]) == ("set_topic", conversation, "nowhere")
        assert line.get("topic_id") is None

    run(host, scenario)


def test_a_refused_create_topic_names_the_path_it_was_asked_for(host):  # FAILS-ON-MAIN
    async def scenario(client):
        refusal = await call(client, host, "create_topic", {"path": "baton"}, role="worker")
        assert refusal["error"] == "forbidden"
        assert last_line(host).get("topic") == "baton"
        assert last_line(host).get("topic_id") is None, "a topic that was not made has no id"

    run(host, scenario)


def test_a_guest_refusal_never_audits_its_binding_token_in_a_topic_field(host):  # FAILS-ON-MAIN
    async def scenario(client):
        conversation = await seed(client, host)
        token = (await claim(client, host, "lane-a"))["binding_token"]
        refusals = [
            await call(
                client,
                host,
                "set_topic",
                {"conversation_id": conversation, "topic": token, "token": token},
                role="guest",
            ),
            await call(client, host, "create_topic", {"path": token, "token": token}, role="guest"),
            await call(
                client, host, "rename_topic", {"path": "baton", "new_path": token, "token": token}, role="guest"
            ),
        ]
        assert [refusal["error"] for refusal in refusals] == ["forbidden"] * 3, refusals
        assert [line["tool"] for line in audit_lines(host)[-3:]] == ["set_topic", "create_topic", "rename_topic"]
        leaked = [line["tool"] for line in audit_lines(host) if token in json.dumps(line)]
        assert not leaked, f"the binding token reached the audit line of {leaked}"

    run(host, scenario)


# 2. The actor in the history --------------------------------------------------------------------------------


def test_a_direct_credentials_changes_name_exactly_its_role(host):  # FAILS-ON-MAIN
    async def scenario(client):
        conversation = await seed(client, host)
        await call(client, host, "update_topic", {"path": "baton", "title": "Baton"})
        await call(client, host, "rename_topic", {"path": "baton/dev", "new_path": "baton/devx"})
        await call(client, host, "set_topic", {"conversation_id": conversation, "topic": "baton/devx"})
        rows = changes(host)
        assert [row["operation"] for row in rows] == [
            "create_topic",
            "create_topic",
            "update_topic",
            "rename_topic",
            "set_topic",
        ]
        assert {row["actor"] for row in rows} == {"planner"}
        assert {row["grant_id"] for row in rows} == {None}

    run(host, scenario)


def test_a_refused_call_over_mcp_writes_no_history(host):  # FAILS-ON-MAIN
    async def scenario(client):
        conversation = await seed(client, host)
        before = [tuple(row) for row in changes(host)]
        for tool, arguments, role in (
            ("create_topic", {"path": "other"}, "worker"),
            ("create_topic", {"path": "baton"}, "planner"),
            ("update_topic", {"path": "nowhere", "title": "x"}, "planner"),
            ("rename_topic", {"path": "baton", "new_path": "baton/dev"}, "planner"),
            ("set_topic", {"conversation_id": conversation, "topic": "nowhere"}, "planner"),
            ("set_topic", {"conversation_id": conversation, "topic": "baton/dev"}, "worker"),
        ):
            refusal = await call(client, host, tool, arguments, role=role)
            assert "error" in refusal, (tool, refusal)
        assert [tuple(row) for row in changes(host)] == before

    run(host, scenario)


# An enrolled alias: its token makes the call run as its mapped role, so the history's actor has to say which
# alias stood behind the role. The alias id is the immutable key; the display name and code are for humans.

ALIAS_ROLES = """
[roles.hilary]
description = "Designs"
job_authority = false
can_create_conversations = true
can_create_topics = true
can_manage_participants = true
can_view_all_conversations = true
[roles.guest]
description = "Shared login"
can_view_all_conversations = true
[conversations]
enabled = true
[aliases]
max_claims_per_alias_per_hour = 100
"""


@pytest.fixture
def alias_host(tmp_path):
    made = {role: auth.new_token() for role in ("hilary", "guest")}
    entries = "".join(
        f'[[auth.tokens]]\nname = "{role}-token"\nrole = "{role}"\nsha256 = "{digest}"\n'
        for role, (_, digest) in made.items()
    )
    built = make_config(tmp_path, extra=ALIAS_ROLES + entries)
    built.tokens.update({role: token for role, (token, _) in made.items()})
    built.config_path = tmp_path / "baton.toml"
    return built


def test_an_alias_bound_call_names_its_role_and_its_alias_in_the_history(alias_host):  # FAILS-ON-MAIN
    host = alias_host

    async def scenario(client):
        enrolled = await enrol(client, host)
        proof = (await claim(client, host))["binding_token"]
        made = await call(
            client,
            host,
            "create_topic",
            {"path": "baton", "members": ["hilary", "planner"], "token": proof},
            role="guest",
        )
        assert made.get("created") is True, made
        thread = await call(
            client,
            host,
            "create_conversation",
            {"title": "T", "participants": ["hilary", "planner"], "token": proof},
            role="guest",
        )
        assert thread.get("created") is True, thread
        moved = await call(
            client,
            host,
            "set_topic",
            {"conversation_id": thread["conversation_id"], "topic": "baton", "token": proof},
            role="guest",
        )
        assert moved.get("changed") is True, moved
        rows = changes(host)
        assert [row["operation"] for row in rows] == ["create_topic", "set_topic"]
        for row in rows:
            assert row["actor"].startswith("hilary"), row["actor"]
            assert enrolled["alias_id"] in row["actor"], "the alias's id is not in the actor"
        line = [one for one in audit(host) if one["tool"] == "set_topic"][-1]
        assert line["alias_id"] == enrolled["alias_id"]
        assert (line["topic"], line["topic_id"]) == ("baton", topic_ids(host)["baton"])

    run(host, scenario)


def test_the_enrolled_role_acting_on_its_own_credential_is_just_the_role(alias_host):  # FAILS-ON-MAIN
    host = alias_host

    async def scenario(client):
        await enrol(client, host)
        made = await call(client, host, "create_topic", {"path": "baton"}, role="hilary")
        assert made.get("created") is True, made
        [row] = changes(host)
        assert row["actor"] == "hilary"

    run(host, scenario)


# 3. No behaviour change: GUARD ------------------------------------------------------------------------------

TOPIC_KEYS = {"path", "title", "description", "members", "created_by", "created_at"}


def test_every_topic_response_keeps_exactly_its_keys(host):  # GUARD
    async def scenario(client):
        made = await call(client, host, "create_topic", {"path": "baton", "members": ["planner", "worker"]})
        assert set(made) == TOPIC_KEYS | {"request_id", "created"}
        sub = await call(client, host, "create_topic", {"path": "baton/dev"})
        assert set(sub) == TOPIC_KEYS | {"request_id", "created"}
        updated = await call(client, host, "update_topic", {"path": "baton", "title": "B"})
        assert set(updated) == TOPIC_KEYS
        renamed = await call(client, host, "rename_topic", {"path": "baton/dev", "new_path": "baton/devx"})
        assert set(renamed) == TOPIC_KEYS | {"renamed_from", "subtopics"}
        assert (renamed["path"], renamed["renamed_from"], renamed["subtopics"]) == ("baton/devx", "baton/dev", 0)
        shown = await call(client, host, "get_topic", {"path": "baton"})
        assert set(shown) == TOPIC_KEYS | {"threads", "subtopics"}
        assert shown["subtopics"] == ["baton/devx"], "subtopics are paths, not ids"
        listed = await call(client, host, "list_topics", {})
        assert set(listed) == {"topics"}
        assert [set(entry) for entry in listed["topics"]] == [TOPIC_KEYS | {"threads", "last_activity_at"}] * 2
        under = await call(client, host, "list_topics", {"parent": "baton"})
        assert [entry["path"] for entry in under["topics"]] == ["baton/devx"]
        for body in (made, sub, updated, renamed, shown, *listed["topics"]):
            assert "id" not in body and "topic_id" not in body

    run(host, scenario)


def test_conversation_responses_with_a_topic_keep_exactly_their_keys(host):  # GUARD
    summary = {
        "conversation_id",
        "title",
        "topic",
        "created_by",
        "created_at",
        "participants",
        "revision",
        "last_seq",
        "last_activity_at",
    }

    async def scenario(client):
        await call(client, host, "create_topic", {"path": "baton", "members": ["planner", "worker"]})
        await call(client, host, "create_topic", {"path": "baton/dev"})
        made = await call(
            client,
            host,
            "create_conversation",
            {"title": "A thread", "participants": ["planner", "worker"], "topic": "baton"},
        )
        assert set(made) == summary | {"request_id", "created"}
        assert made["topic"] == "baton"
        listed = await call(client, host, "list_conversations", {"topic": "baton"})
        assert set(listed) == {"conversations", "next_cursor"}
        assert [set(entry) for entry in listed["conversations"]] == [summary]
        moved = await call(
            client, host, "set_topic", {"conversation_id": made["conversation_id"], "topic": "baton/dev"}
        )
        assert moved == {"conversation_id": made["conversation_id"], "topic": "baton/dev", "changed": True}
        same = await call(client, host, "set_topic", {"conversation_id": made["conversation_id"], "topic": "baton/dev"})
        assert same["changed"] is False
        cleared = await call(client, host, "set_topic", {"conversation_id": made["conversation_id"], "topic": None})
        assert cleared == {"conversation_id": made["conversation_id"], "topic": None, "changed": True}
        for body in (made, moved, *listed["conversations"]):
            assert "id" not in body and "topic_id" not in body

    run(host, scenario)


def test_a_replayed_create_topic_answers_as_before(host):  # GUARD
    async def scenario(client):
        arguments = {"path": "baton", "request_id": "same-request", "members": ["planner", "worker"]}
        first = await call(client, host, "create_topic", arguments)
        again = await call(client, host, "create_topic", arguments)
        assert (first["created"], again["created"]) == (True, False)
        assert {key: value for key, value in again.items() if key != "created"} == {
            key: value for key, value in first.items() if key != "created"
        }

    run(host, scenario)


def test_the_topic_tools_take_the_same_arguments_as_before(host):  # GUARD
    expected = {
        "create_conversation": ({"participants", "request_id", "title", "token", "topic"}, {"title"}),
        "list_conversations": ({"changed_since", "cursor", "limit", "token", "topic"}, set()),
        "set_topic": ({"conversation_id", "grant_token", "token", "topic"}, {"conversation_id", "topic"}),
        "create_topic": ({"description", "grant_token", "members", "path", "request_id", "title", "token"}, {"path"}),
        "update_topic": ({"description", "members", "path", "title", "token"}, {"path"}),
        "rename_topic": ({"acknowledge_topic_grants", "new_path", "path", "token"}, {"new_path", "path"}),
        "list_topics": ({"parent", "token"}, set()),
        "get_topic": ({"path", "token"}, {"path"}),
    }

    async def scenario(client):
        listed = await rpc(client, host, "tools/list", {}, role="planner")
        tools = {tool["name"]: tool["inputSchema"] for tool in listed["result"]["tools"]}
        for name, (properties, required) in expected.items():
            assert set(tools[name]["properties"]) == properties, name
            assert set(tools[name].get("required", [])) == required, name

    run(host, scenario)


REFUSAL_TEXTS = [
    (
        "a duplicate create",
        "create_topic",
        {"path": "baton"},
        "planner",
        "invalid_request",
        "topic 'baton' already exists",
    ),
    (
        "a create by a role that may not",
        "create_topic",
        {"path": "elsewhere"},
        "worker",
        "forbidden",
        "worker may not create or change topics (roles.worker.can_create_topics)",
    ),
    (
        "a subtopic without its parent",
        "create_topic",
        {"path": "nowhere/child"},
        "planner",
        "invalid_request",
        "topic 'nowhere/child' needs its parent 'nowhere' to exist first",
    ),
    (
        "an unknown topic to update",
        "update_topic",
        {"path": "nowhere", "title": "x"},
        "planner",
        "invalid_request",
        "topic 'nowhere' is not a topic; a core role opens one with create_topic",
    ),
    (
        "an unknown topic to rename",
        "rename_topic",
        {"path": "nowhere", "new_path": "x"},
        "planner",
        "invalid_request",
        "topic 'nowhere' is not a topic; a core role opens one with create_topic",
    ),
    (
        "a rename onto a taken path",
        "rename_topic",
        {"path": "baton", "new_path": "reviews"},
        "planner",
        "invalid_request",
        "new_path 'reviews' is already a topic",
    ),
    (
        "a rename into itself",
        "rename_topic",
        {"path": "baton", "new_path": "baton/x"},
        "planner",
        "invalid_request",
        "new_path 'baton/x' is 'baton' itself or inside it",
    ),
    (
        "a rename to three levels",
        "rename_topic",
        {"path": "baton", "new_path": "reviews/baton"},
        "planner",
        "invalid_request",
        "'baton' has subtopics, so moving it under 'reviews' would make three levels",
    ),
    ("an unknown topic to read", "get_topic", {"path": "nowhere"}, "planner", "unknown_topic", "no topic 'nowhere'"),
    (
        "a set_topic to an unknown topic",
        "set_topic",
        {"conversation_id": "$c", "topic": "nowhere"},
        "planner",
        "invalid_request",
        "topic 'nowhere' is not a topic; a core role opens one with create_topic",
    ),
    (
        "a set_topic by a role that may not",
        "set_topic",
        {"conversation_id": "$c", "topic": "reviews"},
        "worker",
        "forbidden",
        "worker may not change a conversation's topic (roles.worker.can_manage_participants)",
    ),
]


@pytest.mark.parametrize("label,tool,arguments,role,code,detail", REFUSAL_TEXTS, ids=[row[0] for row in REFUSAL_TEXTS])
def test_refusals_read_as_they_did(host, label, tool, arguments, role, code, detail):  # GUARD
    async def scenario(client):
        await call(client, host, "create_topic", {"path": "baton", "members": ["planner", "worker"]})
        await call(client, host, "create_topic", {"path": "baton/dev"})
        await call(client, host, "create_topic", {"path": "reviews"})
        made = await call(
            client,
            host,
            "create_conversation",
            {"title": "A thread", "participants": ["planner", "worker"], "topic": "baton"},
        )
        arguments_ = {key: made["conversation_id"] if value == "$c" else value for key, value in arguments.items()}
        assert await call(client, host, tool, arguments_, role=role) == {"error": code, "detail": detail}

    run(host, scenario)


# 4. What the line may carry, and which topic it names ---------------------------------------------------------

# Every topic argument, with the rest of a call that would otherwise be well formed. "$c" is a conversation.
TOKEN_CALLS = [
    ("get_topic", "path", {"path": "baton"}),
    ("create_topic", "path", {"path": "other"}),
    ("update_topic", "path", {"path": "baton", "title": "x"}),
    ("rename_topic", "path", {"path": "baton/dev", "new_path": "elsewhere"}),
    ("rename_topic", "new_path", {"path": "baton/dev", "new_path": "elsewhere"}),
    ("set_topic", "topic", {"conversation_id": "$c", "topic": "baton"}),
    ("create_conversation", "topic", {"title": "T", "participants": ["planner", "worker"]}),
    ("list_conversations", "topic", {}),
]


@pytest.mark.parametrize("role", ["guest", "planner"])
@pytest.mark.parametrize("tool,field,arguments", TOKEN_CALLS, ids=[f"{tool}-{field}" for tool, field, _ in TOKEN_CALLS])
def test_a_binding_token_in_a_topic_argument_never_reaches_the_line(host, tool, field, arguments, role):
    """The call omits `token`, so nothing tells the line which value is the secret: it must not copy the argument."""

    async def scenario(client):
        conversation = await seed(client, host)
        token = (await claim(client, host, "lane-a"))["binding_token"]
        given = {key: conversation if value == "$c" else value for key, value in arguments.items()}
        await call(client, host, tool, {**given, field: token}, role=role)
        line = last_line(host)
        assert line["tool"] == tool and line["outcome"] != "ok", line
        assert token not in json.dumps(line), f"the binding token reached the audit line: {line}"
        assert "new_path" not in line, line
        # Only the value that is not a topic path is dropped: a refused rename still names its well-formed `path`.
        assert line.get("topic") == (arguments["path"] if field == "new_path" else None), line

    run(host, scenario)


def test_a_guest_refusal_naming_a_well_formed_unknown_path_still_audits_it(host):
    async def scenario(client):
        await seed(client, host)
        refusal = await call(client, host, "get_topic", {"path": "nowhere"}, role="guest")
        assert refusal["error"] == "unknown_topic"
        line = last_line(host)
        assert line["topic"] == "nowhere" and "topic_id" not in line, line

    run(host, scenario)


# The real operations, kept apart from the ones a test replaces to interleave a writer.
REAL_CREATE, REAL_RENAME = topics.create, topics.rename
REAL_CREATE_CONVERSATION = conversations.create


def usurp(host, path, *, thread=False):
    """What another writer does in the gap: the topic at `path` moves away and a different one takes its path.

    With `thread`, the new topic also gets a conversation. The busy timeout is short on purpose: a reader that
    held the write lock here would otherwise hang the suite instead of failing it.
    """
    conn = store.connect(host.config.db_path)
    conn.execute("PRAGMA busy_timeout = 1000")
    try:
        REAL_RENAME(conn, host.config, actor="planner", path=path, new_path=path + "-old")
        REAL_CREATE(conn, host.config, actor="planner", path=path, title="the other topic")
        if thread:
            REAL_CREATE_CONVERSATION(
                conn,
                host.config,
                actor="planner",
                title="the other thread",
                participants=["planner", "worker"],
                topic=path,
            )
    finally:
        conn.close()


# (tool, arguments, module and function the body calls, the path that call resolved, extra line keys)
INTERLEAVED = [
    ("create_topic", {"path": "late"}, topics, "create", "late", {}),
    ("update_topic", {"path": "baton/dev", "title": "x"}, topics, "update", "baton/dev", {}),
    (
        "rename_topic",
        {"path": "baton/dev", "new_path": "baton/devx"},
        topics,
        "rename",
        "baton/devx",
        {"new_path": "baton/devx"},
    ),
    ("get_topic", {"path": "baton/dev"}, topics, "get", "baton/dev", {}),
    ("set_topic", {"conversation_id": "$c", "topic": "baton/dev"}, conversations, "set_topic", "baton/dev", {}),
    (
        "create_conversation",
        {"title": "T", "participants": ["planner", "worker"], "topic": "baton/dev"},
        conversations,
        "create",
        "baton/dev",
        {},
    ),
    ("list_conversations", {"topic": "baton/dev"}, conversations, "list_readable", "baton/dev", {}),
]


@pytest.mark.parametrize(
    "tool,arguments,module,function,resolved,extra", INTERLEAVED, ids=[row[0] for row in INTERLEAVED]
)
def test_the_line_names_the_topic_the_call_changed_not_whoever_holds_the_path_later(
    host, monkeypatch, tool, arguments, module, function, resolved, extra
):
    """Between the body's commit and the line, another writer moves that topic away and reuses its path."""
    original = getattr(module, function)

    def then_usurp(*args, **kwargs):
        result = original(*args, **kwargs)
        usurp(host, resolved)
        return result

    async def scenario(client):
        conversation = await seed(client, host)
        given = {key: conversation if value == "$c" else value for key, value in arguments.items()}
        monkeypatch.setattr(module, function, then_usurp)
        answer = await call(client, host, tool, given)
        assert "error" not in answer, answer
        ids = topic_ids(host)
        assert ids[resolved] != ids[resolved + "-old"], "the other writer did not take the path"
        line = last_line(host)
        assert line["topic_id"] == ids[resolved + "-old"], f"the line names the later topic: {line}"
        named = arguments.get("path", arguments.get("topic"))
        assert (line["topic"], {key: line[key] for key in extra}) == (named, extra)

    run(host, scenario)


# A replay is answered from the original request, and a read is answered from one snapshot -------------------------


def path_of(host, topic_id):
    return {identifier: path for path, identifier in topic_ids(host).items()}[topic_id]


def thread_ids_under(host, path):
    with database(host) as conn:
        return {
            row["id"]
            for row in conn.execute("SELECT id FROM conversations WHERE topic = ? OR topic LIKE ?", (path, path + "/%"))
        }


def interleave_after(monkeypatch, module, name, host, path):
    """Once, right after `module.name` reads `path`, another writer moves that topic and takes its path."""
    original, fired = getattr(module, name), []

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        if not fired and path in args:
            fired.append(path)
            usurp(host, path, thread=True)
        return result

    monkeypatch.setattr(module, name, wrapper)
    return fired


def test_a_replayed_create_topic_names_the_requested_path_and_the_topic_it_returns(host):
    async def scenario(client):
        arguments = {"path": "alpha", "request_id": "same-request", "members": ["planner", "worker"]}
        first = await call(client, host, "create_topic", arguments)
        assert first["created"] is True
        original = topic_ids(host)["alpha"]
        await call(client, host, "rename_topic", {"path": "alpha", "new_path": "beta"})
        await call(client, host, "create_topic", {"path": "alpha", "title": "the other topic"})
        other = topic_ids(host)["alpha"]
        assert other != original
        replay = await call(client, host, "create_topic", arguments)
        assert (replay["created"], replay["path"]) == (False, "beta")
        line = last_line(host)
        assert (line["topic"], line["topic_id"]) == ("alpha", original), line
        assert other not in json.dumps(line), f"the line names the topic that now holds the path: {line}"

    run(host, scenario)


def replayed_conversation(host, *, clear_topic=False):
    """A conversation made in `alpha`, then `alpha` renamed away and reused, then the same request replayed."""

    async def scenario(client):
        await call(client, host, "create_topic", {"path": "alpha", "members": ["planner", "worker"]})
        arguments = {
            "title": "A thread",
            "participants": ["planner", "worker"],
            "topic": "alpha",
            "request_id": "same-request",
        }
        first = await call(client, host, "create_conversation", arguments)
        assert first["created"] is True
        await call(client, host, "rename_topic", {"path": "alpha", "new_path": "beta"})
        if clear_topic:
            await call(client, host, "set_topic", {"conversation_id": first["conversation_id"], "topic": None})
        await call(client, host, "create_topic", {"path": "alpha", "title": "the other topic"})
        replay = await call(client, host, "create_conversation", arguments)
        assert (replay["created"], replay["conversation_id"]) == (False, first["conversation_id"])
        scenario.replay = replay

    run(host, scenario)
    return scenario.replay


def test_a_replayed_create_conversation_names_the_requested_path_and_the_topic_the_thread_is_in(host):
    replay = replayed_conversation(host)
    ids = topic_ids(host)
    assert replay["topic"] == "beta"
    line = last_line(host)
    assert line["topic"] == "alpha", line
    assert line["topic_id"] == ids[replay["topic"]], f"the line is not the returned thread's topic: {line}"
    assert line["topic_id"] != ids["alpha"], f"the line names the topic that now holds the path: {line}"


def test_a_replayed_create_conversation_whose_thread_has_no_topic_now_carries_no_id(host):
    replay = replayed_conversation(host, clear_topic=True)
    assert replay["topic"] is None
    line = last_line(host)
    assert line["topic"] == "alpha" and line.get("topic_id") is None, line


def test_list_conversations_lists_the_topic_whose_id_is_on_its_line(host, monkeypatch):
    async def scenario(client):
        await call(client, host, "create_topic", {"path": "alpha", "members": ["planner", "worker"]})
        await call(
            client,
            host,
            "create_conversation",
            {"title": "A thread", "participants": ["planner", "worker"], "topic": "alpha"},
        )
        fired = interleave_after(monkeypatch, topics, "note_path", host, "alpha")
        answer = await call(client, host, "list_conversations", {"topic": "alpha"})
        assert "error" not in answer and fired, answer
        ids = topic_ids(host)
        assert ids["alpha"] != ids["alpha-old"], "the other writer did not take the path"
        line = last_line(host)
        assert line["topic"] == "alpha", line
        listed = {entry["conversation_id"] for entry in answer["conversations"]}
        assert listed == thread_ids_under(host, path_of(host, line["topic_id"])), (
            f"the listing is not the line's topic's: {line}"
        )

    run(host, scenario)


def test_get_topic_answers_for_the_topic_whose_id_is_on_its_line(host, monkeypatch):
    async def scenario(client):
        await call(client, host, "create_topic", {"path": "alpha", "members": ["planner", "worker"]})
        await call(client, host, "create_topic", {"path": "alpha/dev"})
        for topic in ("alpha", "alpha/dev"):
            await call(
                client,
                host,
                "create_conversation",
                {"title": "A thread", "participants": ["planner", "worker"], "topic": topic},
            )
        fired = interleave_after(monkeypatch, topics, "_row", host, "alpha")
        answer = await call(client, host, "get_topic", {"path": "alpha"})
        assert "error" not in answer and fired, answer
        ids = topic_ids(host)
        assert ids["alpha"] != ids["alpha-old"], "the other writer did not take the path"
        line = last_line(host)
        assert line["topic"] == "alpha", line
        owner = path_of(host, line["topic_id"])
        assert answer["threads"] == len(thread_ids_under(host, owner)), f"the count is not the line's topic's: {line}"
        children = sorted(path for path in ids if path.startswith(owner + "/"))
        assert answer["subtopics"] == [path.replace(owner, "alpha", 1) for path in children], (
            f"the subtopics are not the line's topic's: {line}"
        )

    run(host, scenario)
