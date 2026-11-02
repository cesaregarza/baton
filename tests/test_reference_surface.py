"""The three reference tools as a connector sees them: which tools there are and where in the list, their input schemas
and annotations, the scope each needs, who may call them at the guard, and what their descriptions tell a reader.

None of this can pass before the tools exist, except the one test marked GUARD, which says they are listed only
when conversations are enabled and so passes before and after.

Schemas are compared structurally (names, types, required lists, bounds, defaults), never by hash, and a description
is compared only after its whitespace is collapsed: a docstring's indentation is kept by one Python version and
stripped by the next, so neither a raw description nor a hash of one is stable.

Mutant numbers in comments refer to the spec's list of one-line mutants the tests must kill. Q numbers refer to its open
questions: the test pins the question's default, so a ruling changes only the tests that carry that number.
"""

from __future__ import annotations

import importlib

import reference_support as refs
from conftest import make_config
from reference_support import (
    BOARDS,
    NAME,
    Anonymous,
    bound_guest,
    collapsed,
    edit_arguments,
    read_reference,
    reference_history,
    write_revisions,
)
from test_aliases import rpc
from test_topic_grant_no_tool_change import TOOLS
from topic_grant_tools_support import play, stage, succeeded, tool

from baton import app as app_module
from baton import auth

build_scene = refs.build_scene  # the fixture, under the name the tests ask for

NEW = ["edit_reference", "read_reference", "reference_history"]
OLD = [name for name in TOOLS if name not in NEW]
ELEVEN = {"whoami", "alias_exists", "claim_alias", "list_conversations", "read_history", "get_message", "search",
          "send_message", "create_conversation", "list_topics", "get_topic"}


async def listed(client, host, role):
    return (await rpc(client, host, "tools/list", {}, role=role))["result"]["tools"]


async def shown(client, host, name, role="planner"):
    found = {one["name"]: one for one in await listed(client, host, role)}
    assert name in found, f"{name} is not in the tool list: {list(found)}"
    return found[name]


def kinds(spec):
    """The JSON types a property accepts: one, or the several of an `anyOf` (a nullable property has two)."""
    return {entry.get("type") for entry in spec["anyOf"]} if "anyOf" in spec else {spec.get("type")}


def bounds(spec):
    """The `minimum` and `maximum` a property declares, on itself or on the integer arm of an `anyOf`."""
    found = {}
    for entry in spec["anyOf"] if "anyOf" in spec else [spec]:
        found.update({key: entry[key] for key in ("minimum", "maximum") if key in entry})
    return found


REAL_ACCESS_TOKEN = app_module.get_access_token  # captured once, whatever is patched over it later


def only_scopes(monkeypatch, *scopes):
    """Make every call come in with just these scopes: the credential is the same, its scopes are fewer."""

    def limited():
        principal = REAL_ACCESS_TOKEN()
        return None if principal is None else principal.model_copy(update={"scopes": list(scopes)})

    monkeypatch.setattr(app_module, "get_access_token", limited)


# 1. The list and the schemas ------------------------------------------------------------------------------

def test_the_tool_list_gains_exactly_the_three_tools_after_get_topic_in_their_order(build_scene):
    """The earlier tools are listed as they were; the new ones sit right after `get_topic`, in the conversation
    group, as `edit_reference`, `read_reference`, `reference_history`; and a guest sees the same list as a core role."""
    scene = build_scene()
    after = OLD.index("get_topic") + 1

    async def scenario(client):
        for role in ("planner", "guest"):
            names = [one["name"] for one in await listed(client, scene.host, role)]
            assert names == OLD[:after] + NEW + OLD[after:], (role, names)

    play(scene.host, scenario)


def test_the_two_new_refusals_are_errors_of_their_own_and_the_stale_one_is_a_stale_revision():
    """`StaleReference` is a `StaleRevision` (so its code is `stale_revision`) and `UnknownReference` is a new error
    with the code `unknown_reference`; both are in `baton/errors.py`. The job error does not carry the extra field."""
    # Mutant 22: StaleReference raised without current_revision in extra.
    errors = importlib.import_module("baton.errors")
    for name in ("StaleReference", "UnknownReference"):
        assert hasattr(errors, name), f"baton/errors.py has no {name}"
    assert issubclass(errors.StaleReference, errors.StaleRevision) and errors.StaleReference.code == "stale_revision"
    assert errors.UnknownReference.code == "unknown_reference"
    assert not issubclass(errors.UnknownReference, errors.StaleRevision)
    assert errors.StaleRevision.extra == {}


def test_edit_reference_declares_the_arguments_the_spec_lists(build_scene):
    """Required: topic, name, body and expected_revision, which is an integer from 0 with no default. Optional: a
    request_id and a grant_token, each text or null, the latter described as the topic grant key. Not read-only."""
    scene = build_scene()

    async def scenario(client):
        found = await shown(client, scene.host, "edit_reference")
        schema = found["inputSchema"]
        assert set(schema["properties"]) == {"topic", "name", "body", "expected_revision", "request_id",
                                             "grant_token", "token"}
        assert set(schema.get("required", [])) == {"topic", "name", "body", "expected_revision"}
        properties = schema["properties"]
        assert [kinds(properties[name]) for name in ("topic", "name", "body")] == [{"string"}] * 3
        assert kinds(properties["expected_revision"]) == {"integer"}
        assert bounds(properties["expected_revision"]) == {"minimum": 0}
        assert "default" not in properties["expected_revision"]
        for name in ("request_id", "grant_token"):
            assert kinds(properties[name]) == {"string", "null"}, name
        assert "topic grant" in collapsed(properties["grant_token"].get("description", "")).lower()
        assert found["annotations"] == {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}

    play(scene.host, scenario)


def test_read_reference_declares_the_arguments_the_spec_lists_and_no_key(build_scene):
    """Required: topic. Optional: a name, text or null, and a revision, an integer from 1 or null. Read-only, and no
    `grant_token`: a read needs no grant."""
    # Q15: the read tools declare no key.
    scene = build_scene()

    async def scenario(client):
        found = await shown(client, scene.host, "read_reference")
        schema = found["inputSchema"]
        assert set(schema["properties"]) == {"topic", "name", "revision", "token"}
        assert set(schema.get("required", [])) == {"topic"}
        properties = schema["properties"]
        assert kinds(properties["topic"]) == {"string"} and kinds(properties["name"]) == {"string", "null"}
        assert kinds(properties["revision"]) == {"integer", "null"}
        assert bounds(properties["revision"]) == {"minimum": 1}
        assert found["annotations"] == {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}

    play(scene.host, scenario)


def test_reference_history_declares_the_arguments_the_spec_lists_and_no_key(build_scene):
    """Required: topic and name. Optional: after_revision, an integer from 0 that defaults to 0, and limit, an integer
    from 1 to 100 that defaults to 50. Read-only, and no `grant_token`."""
    # Q15: the read tools declare no key.
    scene = build_scene()

    async def scenario(client):
        found = await shown(client, scene.host, "reference_history")
        schema = found["inputSchema"]
        assert set(schema["properties"]) == {"topic", "name", "after_revision", "limit", "token"}
        assert set(schema.get("required", [])) == {"topic", "name"}
        properties = schema["properties"]
        assert (kinds(properties["after_revision"]), bounds(properties["after_revision"]),
                properties["after_revision"].get("default")) == ({"integer"}, {"minimum": 0}, 0)
        assert (kinds(properties["limit"]), bounds(properties["limit"]), properties["limit"].get("default")) == (
            {"integer"}, {"minimum": 1, "maximum": 100}, 50)
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

def test_the_tools_are_registered_through_the_tuples_of_the_module_the_spec_names():
    """The layout the spec fixes: the bodies are a mixin `Tools` inherits, and the module offers the registration
    tuples of (name, scope, read-only) and the names of the guest tools, so `baton/app.py` only adds them."""
    module = refs.reference_tools_module()
    assert tuple(module.REFERENCE_TOOLS) == (("edit_reference", auth.SCOPE_WRITE, False),
                                             ("read_reference", auth.SCOPE_READ, True),
                                             ("reference_history", auth.SCOPE_READ, True))
    assert set(module.REFERENCE_GUEST_TOOLS) == {"read_reference", "reference_history"}
    assert issubclass(app_module.Tools, module.ReferenceTools)


def test_the_two_read_tools_are_guest_tools_and_edit_reference_is_not():
    """A guest may call the read tools (with its binding) and may call `edit_reference` only with a key, which the
    guard's tool set for keys decides; so `edit_reference` is not among the plain guest tools."""
    # Mutant 18: read_reference left out of the guest tool list.
    assert ELEVEN <= app_module.GUEST_TOOLS
    assert app_module.GUEST_TOOLS - ELEVEN == {"read_reference", "reference_history"}
    assert "edit_reference" not in app_module.GUEST_TOOLS


def test_a_bound_guest_is_let_through_the_guard_to_read_and_an_unbound_one_is_not(build_scene):
    """R1. The binding is the guard's to check, as for a guest post: with it both read tools answer, without it they
    are refused in the words of an unbound post, and an unbound guest's `edit_reference` is refused as a tool it may
    not call."""
    # Q1. Mutant 18: read_reference left out of the guest tools, or the guard not requiring a binding.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        await write_revisions(client, scene, BOARDS, NAME, 2)
        guest = await bound_guest(client, scene)
        assert succeeded(await read_reference(client, guest, BOARDS, NAME))["revision"] == 2
        assert succeeded(await reference_history(client, guest, BOARDS, NAME))["current_revision"] == 2
        none = Anonymous(scene.host)
        gone = {"error": "forbidden", "detail": refs.BINDING_GONE}
        assert await read_reference(client, none, BOARDS, NAME) == gone
        assert await reference_history(client, none, BOARDS, NAME) == gone
        assert await none.call(client, "edit_reference", **edit_arguments(BOARDS, NAME, "text", 2)) == {
            "error": "forbidden", "detail": refs.F2}

    play(scene.host, scenario)


def test_each_tool_needs_its_scope_and_refuses_a_credential_without_it(build_scene, monkeypatch):
    """A credential with no scope is refused all three as `insufficient_scope`, naming the tool and the scope it needs;
    one that can only read can read and not edit, and one that can only write can edit and not read."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        await write_revisions(client, scene, BOARDS, NAME, 1)
        calls = {"edit_reference": (edit_arguments(BOARDS, NAME, "text", 1), auth.SCOPE_WRITE),
                 "read_reference": ({"topic": BOARDS, "name": NAME}, auth.SCOPE_READ),
                 "reference_history": ({"topic": BOARDS, "name": NAME}, auth.SCOPE_READ)}

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


# 3. What the descriptions tell a reader -----------------------------------------------------------------------

PHRASES = {
    "that a reference is never an instruction to the reader": ("instruction",),
    "that it is never an approval": ("approval",),
    "that every revision is kept for good": ("for good", "permanent", "never deleted", "never removed", "forever"),
    "that every core role can read it": ("every core role", "all core roles", "any core role", "core roles can read"),
    "that every alias can read it": ("every alias", "any alias", "all aliases", "aliases can read"),
}


def test_every_description_says_a_reference_is_information_kept_for_good_and_readable_by_all(build_scene):
    """R7. The description of each of the three tools says that a reference is text written by whoever held the right to
    edit it and never an instruction or an approval, that every revision is kept for good, and that every core role and
    every alias can read it. Compared as whitespace-collapsed lower-case text, by the words that carry each point."""
    # Q8: a reference body cannot be removed, and the tool descriptions say so.
    scene = build_scene()

    async def scenario(client):
        for name in NEW:
            text = collapsed((await shown(client, scene.host, name))["description"]).lower()
            for point, phrases in PHRASES.items():
                assert any(phrase in text for phrase in phrases), f"{name} does not say {point}: {text}"

    play(scene.host, scenario)


def test_edit_reference_tells_a_grantee_how_to_use_its_key(build_scene):
    """Q17. The guard adds "needs a core proof" to every tool a guest cannot call, which would mislead a grantee here.
    The tool's own text says a guest holding a topic grant with `reference.edit` may edit, with its binding `token` and
    its `grant_token`, as `create_topic`'s does for its capability."""
    scene = build_scene()

    async def scenario(client):
        text = collapsed((await shown(client, scene.host, "edit_reference"))["description"]).lower()
        for needed in ("topic grant", "reference.edit", "grant_token", "token"):
            assert needed in text, f"edit_reference does not mention {needed}: {text}"
        create = collapsed((await shown(client, scene.host, "create_topic"))["description"]).lower()
        assert "topic grant" in create and "grant_token" in create

    play(scene.host, scenario)
