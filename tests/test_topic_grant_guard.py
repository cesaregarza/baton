"""The guard and the check that honours a topic grant, over MCP: who a grant applies to, every refusal, the
events they write, and where the key must never appear.

What this pins:

* A grant applies only to a call whose effective role is `guest`. A core credential, or an alias enrolled as a
  core role, runs as that role and a `grant_token` on its call is ignored.
* A guest without a key gets today's refusal, byte for byte, even when its alias holds an active grant. With a key
  it is decided in the tool body, by the presented key's own grant only, and each refusal has its own text.
* Refusals 3, 4 and 5 write one `refused_use` event; refusals 1 and 2 name no grant and write none. No event,
  audit line, refusal text or log ever holds the key.
* The schemas gain exactly `grant_token` on `create_topic` and `set_topic`, and `acknowledge_topic_grants` on
  `rename_topic`. There is still no tool to propose, list or renounce a topic grant.

Tests whose docstring starts with GUARD pin behaviour that must not change, and pass before and after the change.
Mutant numbers in comments refer to the spec's list of one-line mutants the tests must kill.
"""

from __future__ import annotations

import json
import logging
import secrets

import pytest
import topic_grant_tools_support as support
from test_aliases import rpc
from test_per_call_identity import owner
from test_tool_contracts import GUEST_MAY_CALL, required_arguments
from topic_grant_support import every_cell
from topic_grant_tools_support import ALL, PASSPHRASE, new_key, only, play, refused, stage, succeeded, thread, tool

build_scene = support.build_scene  # the fixture, under the name the tests ask for

NEEDS_BINDING = "a topic grant key works only with the alias's binding token"
BINDING_GONE = "alias binding is missing, invalid or expired"


async def listed_tools(client, host, role):
    return (await rpc(client, host, "tools/list", {}, role=role))["result"]["tools"]


def todays_refusal(name):
    return {"error": "forbidden", "detail": f"guest may not call {name}"}


# 1. A guest without a key is refused as it always was -------------------------------------------------------


def test_a_valid_key_opens_no_other_tool(build_scene):
    """GUARD. Only `create_topic`, `set_topic` and `edit_reference` look at a key. Every other tool a guest may not
    call keeps its refusal, whatever the guest presents, so a grantee cannot update, rename, or change a thread's
    participants."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        held = await scene.grantee(client, "lane-a", "job-hunt", inherit=True)
        shown = await listed_tools(client, scene.host, "guest")
        names = [one["name"] for one in shown]
        assert {"update_topic", "rename_topic", "add_participant", "create_job"} <= set(names)
        for one in shown:
            if one["name"] in GUEST_MAY_CALL | {"create_topic", "set_topic", "edit_reference"}:
                continue
            result = await tool(client, scene.host, one["name"], held.arguments(**required_arguments(one)))
            assert result == todays_refusal(one["name"]), (one["name"], result)
        assert scene.topic_paths() == sorted(support.BOARD)

    play(scene.host, scenario)


# 2. The five refusals ---------------------------------------------------------------------------------------


def test_missing_binding_and_unknown_grant_keys_refuse_without_side_effects(build_scene):
    """Missing, unknown, or mismatched credentials never identify a grant or change a topic/thread.

    GUARD. A bound guest that presents no key, or a null key, gets today's refusal byte for byte even though its
    alias holds an active grant: the grant belongs to a key, not to the alias."""
    # Mutant 3: accepting any key or treating a grant key as proof of the alias identity.
    # Mutants 3 and 6: no key, or a null key, passing the guard.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        held = await scene.grantee(client, "lane-a", "job-hunt", inherit=True)
        moving = await thread(client, scene.host, "job-hunt/leads")
        before_changes = scene.changes()
        nearly = held.key[:-1] + ("A" if held.key[-1] != "A" else "B")
        cases = []
        for name, arguments in (
            ("create_topic", {"path": "job-hunt/new"}),
            ("set_topic", {"conversation_id": moving, "topic": "job-hunt/boards"}),
        ):
            for extra, label in (({}, "no key"), ({"grant_token": None}, "a null key")):
                # No detail: the answer must be today's refusal exactly.
                sent = {**arguments, "token": held.token, **extra}
                cases.append((f"bound guest with {label} on {name}", name, sent, None, held.key))
        cases += [
            (
                "key without a binding on create_topic",
                "create_topic",
                {"path": "job-hunt/new", "grant_token": held.key},
                NEEDS_BINDING,
                held.key,
            ),
            (
                "key without a binding on set_topic",
                "set_topic",
                {"conversation_id": moving, "topic": "job-hunt/boards", "grant_token": held.key},
                NEEDS_BINDING,
                held.key,
            ),
            (
                "key with an unknown binding token",
                "create_topic",
                {"path": "job-hunt/new", "token": secrets.token_urlsafe(32), "grant_token": held.key},
                (NEEDS_BINDING, BINDING_GONE),
                held.key,
            ),
        ]
        for key in (new_key(), nearly):
            for name, arguments in (
                ("create_topic", {"path": "job-hunt/new"}),
                ("set_topic", {"conversation_id": moving, "topic": "job-hunt/boards"}),
            ):
                cases.append(
                    (
                        f"unknown grant key on {name}",
                        name,
                        {**arguments, "token": held.token, "grant_token": key},
                        "unknown topic grant key",
                        key,
                    )
                )

        for label, name, arguments, detail, secret in cases:
            result = await tool(client, scene.host, name, arguments)
            if detail is None:
                assert result == todays_refusal(name), (label, result)
            else:
                assert result.get("error") == "forbidden", (label, result)
                assert result.get("detail") in (detail if isinstance(detail, tuple) else (detail,)), (label, result)
            assert secret not in json.dumps(result), (label, result)
            assert "job-hunt/new" not in scene.topic_paths(), label
            assert scene.thread_topic(moving) == "job-hunt/leads", label
            assert scene.changes() == before_changes, label
            assert scene.events("refused_use") == [], label

    play(scene.host, scenario)


def test_another_aliases_key_is_refused_and_recorded_against_the_grant_it_belongs_to(build_scene):
    """Refusal 3. A second grantee presenting the first one's key, with its own binding, gets nothing. The event
    goes on the key's grant and names the alias that tried."""
    # Mutant 3: accepting another alias's grant.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        owner_of_key = await scene.grantee(client, "lane-a", "job-hunt", inherit=True)
        other = await scene.grantee(client, "lane-b", "resume")
        moving = await thread(client, scene.host, "job-hunt/leads")
        borrowed = {"token": other.token, "grant_token": owner_of_key.key}
        said = "this topic grant key is not for this alias"
        refused(await tool(client, scene.host, "create_topic", {"path": "job-hunt/new", **borrowed}), "forbidden", said)
        refused(
            await tool(
                client, scene.host, "set_topic", {"conversation_id": moving, "topic": "job-hunt/boards", **borrowed}
            ),
            "forbidden",
            said,
        )
        assert "job-hunt/new" not in scene.topic_paths()
        assert scene.thread_topic(moving) == "job-hunt/leads"
        events = scene.events("refused_use")
        assert [row["grant_id"] for row in events] == [owner_of_key.grant_id] * 2
        assert {row["actor"] for row in events} == {other.actor}
        assert [json.loads(row["detail"]) for row in events] == [
            {"tool": "create_topic", "reason": "other_alias"},
            {"tool": "set_topic", "reason": "other_alias"},
        ]

    play(scene.host, scenario)


@pytest.mark.parametrize("state", ["revoked", "expired", "pending", "lapsed", "invalid"])
def test_a_grant_that_is_not_active_is_refused_with_its_state(build_scene, state):
    """Refusal 4, once for each state it names. A key is minted only by authorizing, so a grant that reads as
    pending or lapsed again here has been edited behind the module's back, which is what the test does."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        held = await scene.grantee(client, "lane-a", "job-hunt", ("topic.create_sub",))
        if state == "revoked":
            scene.revoke(held.grant_id)
        elif state == "expired":
            scene.clock.advance(days=7)  # a grant lasts until, not through, its last instant
        elif state == "invalid":
            scene.edit_grant(held.grant_id, capabilities=json.dumps(list(ALL)))
        else:
            scene.edit_grant(held.grant_id, authorized_digest=None)
            if state == "lapsed":
                scene.clock.advance(hours=25)
        result = await held.call(client, "create_topic", path="job-hunt/late")
        refused(result, "forbidden", f"topic grant {held.grant_id} is {state}")
        assert "job-hunt/late" not in scene.topic_paths()
        row = only(scene.events("refused_use"), "refused_use event")
        assert (row["grant_id"], row["actor"]) == (held.grant_id, held.actor)
        assert json.loads(row["detail"]) == {"tool": "create_topic", "reason": state}

    play(scene.host, scenario)


def test_a_revoked_grant_is_refused_by_set_topic_too(build_scene):
    """Refusal 4 on the other tool: a revoke ends the grant for both."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        held = await scene.grantee(client, "lane-a", "job-hunt", ("thread.organise",), inherit=True)
        moving = await thread(client, scene.host, "job-hunt/leads")
        scene.revoke(held.grant_id)
        result = await held.call(client, "set_topic", conversation_id=moving, topic="job-hunt/boards")
        refused(result, "forbidden", f"topic grant {held.grant_id} is revoked")
        assert scene.thread_topic(moving) == "job-hunt/leads"
        row = only(scene.events("refused_use"), "refused_use event")
        assert json.loads(row["detail"]) == {"tool": "set_topic", "reason": "revoked"}

    play(scene.host, scenario)


def test_a_grant_works_until_the_instant_it_expires(build_scene):
    """The last instant before `expires_at` is still active; the instant itself is expired."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        held = await scene.grantee(client, "lane-a", "job-hunt", ("topic.create_sub",))
        scene.clock.advance(days=7, seconds=-1)
        succeeded(await held.call(client, "create_topic", path="job-hunt/last"))
        scene.clock.advance(seconds=1)
        refused(
            await held.call(client, "create_topic", path="job-hunt/after"),
            "forbidden",
            f"topic grant {held.grant_id} is expired",
        )

    play(scene.host, scenario)


def test_a_grant_without_the_capability_is_refused_as_not_covered(build_scene):
    """Refusal 5: the key's grant is active and on the topic, but does not list what the call needs."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        held = await scene.grantee(client, "lane-a", "job-hunt", ("thread.organise", "reference.edit"), inherit=True)
        result = await held.call(client, "create_topic", path="job-hunt/new")
        assert result.get("error") == "forbidden", result
        assert result["detail"].startswith(f"topic grant {held.grant_id} does not allow topic.create_sub on "), result
        assert "job-hunt/new" not in scene.topic_paths()
        row = only(scene.events("refused_use"), "refused_use event")
        assert (row["grant_id"], row["actor"]) == (held.grant_id, held.actor)
        assert json.loads(row["detail"]) == {"tool": "create_topic", "reason": "not_covered"}

    play(scene.host, scenario)


def test_the_presented_key_is_never_replaced_by_another_grant_the_alias_holds(build_scene):
    """An alias with two grants is decided by the one whose key it presents. Where the other would allow the call,
    the call is still refused, and the refusal and its event name the presented grant."""
    # Mutant 3: falling back to another grant the alias holds.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        narrow = await scene.grantee(client, "lane-a", "resume", ("topic.create_sub",))
        wide = scene.another_grant(narrow, "job-hunt", ("topic.create_sub", "thread.organise"), inherit=True)
        moving = await thread(client, scene.host, "job-hunt/leads")
        succeeded(await wide.call(client, "create_topic", path="job-hunt/allowed"))
        succeeded(await wide.call(client, "set_topic", conversation_id=moving, topic="job-hunt/boards"))

        result = await narrow.call(client, "create_topic", path="job-hunt/not-with-this-key")
        assert result.get("error") == "forbidden", result
        assert result["detail"].startswith(f"topic grant {narrow.grant_id} does not allow topic.create_sub on "), result
        assert "job-hunt/not-with-this-key" not in scene.topic_paths()

        result = await narrow.call(client, "set_topic", conversation_id=moving, topic="job-hunt/leads")
        assert result.get("error") == "forbidden", result
        assert result["detail"].startswith(f"topic grant {narrow.grant_id} does not allow thread.organise on "), result
        assert scene.thread_topic(moving) == "job-hunt/boards"
        assert {row["grant_id"] for row in scene.events("refused_use")} == {narrow.grant_id}

    play(scene.host, scenario)


# 3. The key stays out of everything --------------------------------------------------------------------------


def test_the_key_is_in_no_event_audit_line_refusal_row_or_log(build_scene, caplog):
    """After a mix of successes and every refusal, neither key appears in any response, event, audit line,
    table cell or log record. Only its digest is stored."""
    # Mutant 9: a refused_use event carrying the key.
    caplog.set_level(logging.DEBUG)
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        first = await scene.grantee(client, "lane-a", "job-hunt", inherit=True)
        second = await scene.grantee(client, "lane-b", "resume")
        moving = await thread(client, scene.host, "job-hunt/leads")
        replies = [
            await first.call(client, "create_topic", path="job-hunt/fine"),
            await tool(client, scene.host, "create_topic", {"path": "job-hunt/x1", "grant_token": first.key}),
            await tool(
                client,
                scene.host,
                "create_topic",
                {"path": "job-hunt/x2", "token": first.token, "grant_token": new_key()},
            ),
            await tool(
                client,
                scene.host,
                "create_topic",
                {"path": "job-hunt/x3", "token": second.token, "grant_token": first.key},
            ),
            await first.call(client, "create_topic", path="resume/not-covered"),
            await first.call(client, "set_topic", conversation_id=moving, topic="resume"),
        ]
        assert replies[0].get("created") is True, replies[0]
        scene.revoke(first.grant_id)
        replies.append(await first.call(client, "create_topic", path="job-hunt/revoked"))
        replies.append(await first.call(client, "set_topic", conversation_id=moving, topic="job-hunt/boards"))
        with scene.db() as conn:
            cells = every_cell(conn)
        for key in (first.key, second.key):
            assert key not in json.dumps(replies)
            assert key not in scene.audit_text()
            assert not [cell for cell in cells if key in cell]
            assert key not in caplog.text
        assert len(scene.events("refused_use")) >= 4, "the refusals that name a grant each wrote an event"

    play(scene.host, scenario)


def test_a_value_that_contains_the_key_never_reaches_the_audit_line_or_a_refusal(build_scene):
    """The audit line drops any audited value that holds the key, as it drops one that holds the binding token,
    and a refusal text never echoes it."""
    # Mutant 10: the audit filter not dropping a value that contains the grant key.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        held = await scene.grantee(client, "lane-a", "job-hunt", inherit=True)
        moving = await thread(client, scene.host, "job-hunt/leads")
        replies = [
            await held.call(client, "create_topic", path="job-hunt/fine", request_id=held.key),
            await held.call(client, "set_topic", conversation_id=held.key, topic="job-hunt/boards"),
            await held.call(client, "create_topic", path=held.key),
            await held.call(client, "set_topic", conversation_id=moving, topic=held.key),
        ]
        assert replies[0].get("created") is True, replies[0]
        assert held.key not in json.dumps(replies[1:]), "a refusal echoed the key"
        assert held.key not in scene.audit_text()
        assert [line["tool"] for line in scene.audit()[-4:]] == [
            "create_topic",
            "set_topic",
            "create_topic",
            "set_topic",
        ]

    play(scene.host, scenario)


# 4. The audit line names the grant ------------------------------------------------------------------------------


def test_a_grant_authorized_call_audits_its_grant_id_and_the_topic_it_resolved(build_scene):
    """The line of a successful call gains `grant_id`, beside the alias fields and topic fields it already has."""
    # Mutant 10: grant_id missing from the line.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        held = await scene.grantee(client, "lane-a", "job-hunt", inherit=True)
        moving = await thread(client, scene.host, "job-hunt/leads")
        succeeded(await held.call(client, "create_topic", path="job-hunt/new"))
        line = scene.audit()[-1]
        assert (line["tool"], line["outcome"], line["role"]) == ("create_topic", "ok", "guest"), line
        assert line.get("grant_id") == held.grant_id, line
        assert (line.get("topic"), line.get("topic_id")) == ("job-hunt/new", scene.topic_id("job-hunt/new")), line
        assert (line.get("alias_id"), line.get("alias_code")) == (held.alias_id, held.code), line
        succeeded(await held.call(client, "set_topic", conversation_id=moving, topic="job-hunt/boards"))
        line = scene.audit()[-1]
        assert (line["tool"], line["outcome"]) == ("set_topic", "ok"), line
        assert line.get("grant_id") == held.grant_id, line
        assert (line.get("topic"), line.get("topic_id")) == ("job-hunt/boards", scene.topic_id("job-hunt/boards"))

    play(scene.host, scenario)


# 5. A grant applies to a guest and to nobody else ------------------------------------------------------------


def test_a_core_credential_ignores_a_grant_token_and_a_refused_one_keeps_its_own_refusal(build_scene):
    """GUARD. A core role's call runs on its flags alone: a valid key or a junk one changes nothing, no grant is
    consulted, none is named in the history or audit line, and a role without the flag keeps its own refusal."""
    # Mutant 6: a core role's grant_token being honoured.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        held = await scene.grantee(client, "lane-a", "job-hunt", ("topic.create_sub",))
        for key in (held.key, new_key()):
            path = "root-" + secrets.token_hex(3)
            made = succeeded(
                await tool(
                    client,
                    scene.host,
                    "create_topic",
                    {"path": path, "members": ["planner"], "grant_token": key},
                    role="planner",
                )
            )
            assert (made["created"], made["members"]) == (True, ["planner"])
            assert scene.audit()[-1].get("grant_id") is None
        assert {row["grant_id"] for row in scene.changes("create_topic")} == {None}
        assert {row["actor"] for row in scene.changes("create_topic")} == {"planner"}
        refusal = await tool(
            client, scene.host, "create_topic", {"path": "job-hunt/by-worker", "grant_token": held.key}, role="worker"
        )
        assert refusal == {
            "error": "forbidden",
            "detail": "worker may not create or change topics (roles.worker.can_create_topics)",
        }
        assert scene.events("refused_use") == []

    play(scene.host, scenario)


def test_an_alias_enrolled_as_a_core_role_runs_as_that_role_and_its_grant_token_is_ignored(build_scene):
    """GUARD. The enrolled alias holds a grant as well, and presents its key. The call still runs as the role it
    maps to: it may do what a grantee may not (a root topic, with members), is recorded as the role, and no grant
    is named or consulted."""
    # Mutant 6: a core role's grant_token being honoured.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        assert owner(scene.host, "alias", "arm-enrol", "steward") == 0
        enrolled = await tool(
            client, scene.host, "claim_alias", {"name": "steward", "passphrase": PASSPHRASE}, role="steward"
        )
        assert enrolled.get("effective_role") == "steward", enrolled
        proof = await tool(client, scene.host, "claim_alias", {"name": "steward", "passphrase": PASSPHRASE})
        assert proof.get("effective_role") == "steward", proof
        key = scene.grant("steward", "job-hunt", ("topic.create_sub",))["grant_key"]
        made = succeeded(
            await tool(
                client,
                scene.host,
                "create_topic",
                {"path": "root-level", "members": ["planner"], "token": proof["binding_token"], "grant_token": key},
            )
        )
        assert (made["created"], made["members"]) == (True, ["planner"])
        row = only(scene.changes("create_topic")[-1:], "history row")
        assert row["actor"] == f"steward via alias {proof['alias_id']}" and row["grant_id"] is None
        assert scene.audit()[-1].get("grant_id") is None
        assert scene.events("refused_use") == []

    play(scene.host, scenario)
