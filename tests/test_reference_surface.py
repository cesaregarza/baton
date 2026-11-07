"""Published reference-tool schemas, feature gating, and authenticated read/write permissions."""

from __future__ import annotations

import reference_support as refs
from conftest import make_config
from reference_support import (
    BOARDS,
    NAME,
    Anonymous,
    bound_guest,
    edit_arguments,
    read_reference,
    reference_history,
    write_revisions,
)
from test_aliases import rpc
from topic_grant_tools_support import play, stage, succeeded, tool

from baton import auth
from baton.app import guard, tools

build_scene = refs.build_scene  # the fixture, under the name the tests ask for

NEW = ["edit_reference", "read_reference", "reference_history"]


async def listed(client, host, role):
    return (await rpc(client, host, "tools/list", {}, role=role))["result"]["tools"]


def kinds(spec):
    """The JSON types a property accepts: one, or the several of an `anyOf` (a nullable property has two)."""
    return {entry.get("type") for entry in spec["anyOf"]} if "anyOf" in spec else {spec.get("type")}


def bounds(spec):
    """The `minimum` and `maximum` a property declares, on itself or on the integer arm of an `anyOf`."""
    found = {}
    for entry in spec["anyOf"] if "anyOf" in spec else [spec]:
        found.update({key: entry[key] for key in ("minimum", "maximum") if key in entry})
    return found


REAL_ACCESS_TOKEN = guard.get_access_token  # captured once, whatever is patched over it later


def only_scopes(monkeypatch, *scopes):
    """Make every call come in with just these scopes: the credential is the same, its scopes are fewer."""

    def limited():
        principal = REAL_ACCESS_TOKEN()
        return None if principal is None else principal.model_copy(update={"scopes": list(scopes)})

    for module in (guard, tools):
        monkeypatch.setattr(module, "get_access_token", limited)


# 1. The list and the schemas ------------------------------------------------------------------------------


def test_reference_tool_schemas_publish_their_arguments_and_permissions(build_scene):
    """One catalog response declares all three reference contracts, including strict bounds and read-only flags."""
    scene = build_scene()

    async def scenario(client):
        catalog = {tool["name"]: tool for tool in await listed(client, scene.host, "planner")}
        # edit_reference
        found = catalog["edit_reference"]
        schema = found["inputSchema"]
        assert set(schema["properties"]) == {
            "topic",
            "name",
            "body",
            "expected_revision",
            "request_id",
            "grant_token",
            "token",
        }
        assert set(schema.get("required", [])) == {"topic", "name", "body", "expected_revision"}
        properties = schema["properties"]
        assert [kinds(properties[name]) for name in ("topic", "name", "body")] == [{"string"}] * 3
        assert kinds(properties["expected_revision"]) == {"integer"}
        assert bounds(properties["expected_revision"]) == {"minimum": 0}
        assert "default" not in properties["expected_revision"]
        for name in ("request_id", "grant_token"):
            assert kinds(properties[name]) == {"string", "null"}, name
        assert found["annotations"] == {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}

        # read_reference
        found = catalog["read_reference"]
        schema = found["inputSchema"]
        assert set(schema["properties"]) == {"topic", "name", "revision", "token"}
        assert set(schema.get("required", [])) == {"topic"}
        properties = schema["properties"]
        assert kinds(properties["topic"]) == {"string"} and kinds(properties["name"]) == {"string", "null"}
        assert kinds(properties["revision"]) == {"integer", "null"}
        assert bounds(properties["revision"]) == {"minimum": 1}
        assert found["annotations"] == {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}

        # reference_history
        found = catalog["reference_history"]
        schema = found["inputSchema"]
        assert set(schema["properties"]) == {"topic", "name", "after_revision", "limit", "token"}
        assert set(schema.get("required", [])) == {"topic", "name"}
        properties = schema["properties"]
        assert (
            kinds(properties["after_revision"]),
            bounds(properties["after_revision"]),
            properties["after_revision"].get("default"),
        ) == ({"integer"}, {"minimum": 0}, 0)
        assert (kinds(properties["limit"]), bounds(properties["limit"]), properties["limit"].get("default")) == (
            {"integer"},
            {"minimum": 1, "maximum": 100},
            50,
        )
        assert found["annotations"] == {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}

    play(scene.host, scenario)


def test_the_reference_tools_are_listed_only_when_conversations_are_enabled(tmp_path):
    """GUARD. A host with conversations off lists none of them and does not know them, as it does not know the other
    conversation tools."""
    host = make_config(tmp_path)

    async def scenario(client):
        names = {one["name"] for one in await listed(client, host, "planner")}
        assert not names & set(NEW) and "create_topic" not in names
        result = await tool(client, host, "edit_reference", {}, role="planner")
        assert "Unknown tool" in str(result) or "unknown" in str(result).lower(), result

    play(host, scenario)


# 2. Who may call them, at the guard -----------------------------------------------------------------------


def test_a_bound_guest_is_let_through_the_guard_to_read_and_an_unbound_one_is_not(build_scene):
    """R1. The binding is the guard's to check, as for a guest post: with it both read tools answer, without it they
    are refused in the words of an unbound post, and an unbound guest's `edit_reference` is refused as a tool it may
    not call."""
    # Q1. Mutant 18: read_reference left out of the guest tools, or the guard not requiring a binding.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        await write_revisions(client, scene, BOARDS, NAME, 2)
        guest = await bound_guest(client, scene)
        assert succeeded(await read_reference(client, guest, BOARDS, NAME))["revision"] == 2
        assert succeeded(await reference_history(client, guest, BOARDS, NAME))["current_revision"] == 2
        none = Anonymous(scene.host)
        gone = {"error": "forbidden", "detail": refs.BINDING_GONE}
        assert await read_reference(client, none, BOARDS, NAME) == gone
        assert await reference_history(client, none, BOARDS, NAME) == gone
        assert await none.call(client, "edit_reference", **edit_arguments(BOARDS, NAME, "text", 2)) == {
            "error": "forbidden",
            "detail": refs.F2,
        }

    play(scene.host, scenario)


def test_each_tool_needs_its_scope_and_refuses_a_credential_without_it(build_scene, monkeypatch):
    """A credential with no scope is refused all three as `insufficient_scope`, naming the tool and the scope it needs;
    one that can only read can read and not edit, and one that can only write can edit and not read."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        await write_revisions(client, scene, BOARDS, NAME, 1)
        calls = {
            "edit_reference": (edit_arguments(BOARDS, NAME, "text", 1), auth.SCOPE_WRITE),
            "read_reference": ({"topic": BOARDS, "name": NAME}, auth.SCOPE_READ),
            "reference_history": ({"topic": BOARDS, "name": NAME}, auth.SCOPE_READ),
        }

        async def ask(name):
            return await tool(client, scene.host, name, calls[name][0], role="planner")

        def needs(name):
            return {"error": "insufficient_scope", "detail": f"{name} needs {calls[name][1]}"}

        only_scopes(monkeypatch)
        for name in calls:
            assert await ask(name) == needs(name)
        only_scopes(monkeypatch, auth.SCOPE_READ)
        assert await ask("edit_reference") == needs("edit_reference")
        succeeded(await ask("read_reference"))
        succeeded(await ask("reference_history"))
        only_scopes(monkeypatch, auth.SCOPE_WRITE)
        assert await ask("read_reference") == needs("read_reference")
        assert await ask("reference_history") == needs("reference_history")
        assert succeeded(await ask("edit_reference"))["revision"] == 2
        assert len(scene.revisions(BOARDS, NAME)) == 2

    play(scene.host, scenario)
