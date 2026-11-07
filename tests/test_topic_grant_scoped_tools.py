"""What a grantee can do with a key: create a subtopic under the topic its grant names, and move a thread between
topics the grant covers. Driven over MCP, with real grants.

What this pins:

* `create_topic` (capability `topic.create_sub`): the path is two segments and the grant's own topic is the parent,
  whether or not the grant inherits. A grantee may not pass `members`; the new topic copies its parent's, once. The
  history row names the grant and the alias. A replay runs the grant check before it returns the stored result, and
  two aliases never share a request.
* `set_topic` (capability `thread.organise`): both the thread's current topic and the destination are covered, by
  exact path to existing topics. No participant requirement. A move writes a history row naming the grant and a host
  notice to the thread's participants; a move to the same topic writes nothing; a core role's move is unchanged.
* The worked cases of the design that these two tools can stand in for: lookalike names, a narrower grant, a revoke
  that leaves ordinary guest use alone, a renewed binding that still works with the key.

Mutant numbers in comments refer to the spec's list of one-line mutants the tests must kill.
"""

from __future__ import annotations

import json
import secrets

import pytest
import topic_grant_tools_support as support
from topic_grant_tools_support import ALL, PASSPHRASE, only, play, refused, stage, succeeded, thread, tool

build_scene = support.build_scene  # the fixture, under the name the tests ask for

NO_MEMBERS = "a topic grant cannot set members; a new subtopic takes its parent's members"


def not_allowed(grant_id: str, capability: str) -> str:
    return f"topic grant {grant_id} does not allow {capability} on "


async def board(client, scene, capabilities=ALL, inherit=True, topic="job-hunt", alias="lane-a"):
    """The staged board, and a grantee holding one grant on `topic`."""
    await stage(client, scene.host)
    return await scene.grantee(client, alias, topic, capabilities, inherit)


async def control_move(client, scene, held):
    """A move the grant does allow, so a refusal after it is the grant's decision and not a tool that refuses all."""
    easy = await thread(client, scene.host, "job-hunt/leads", "A control thread")
    succeeded(await held.call(client, "set_topic", conversation_id=easy, topic="job-hunt/boards"))


# 1. create_topic ------------------------------------------------------------------------------------------------


def test_granted_topic_creation_copies_members_and_ignores_inherit(build_scene):
    """Members are snapshotted from the parent; the grant's inheritance flag controls scope, not creation."""
    # Mutant 7: accepting caller-supplied members, not copying the parent's members, or requiring inheritance.
    # Mutant 10: the successful change row omits its grant id.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        plain = await scene.grantee(client, "lane-a", "job-hunt", ("topic.create_sub",), inherit=False)
        inheriting = await scene.grantee(client, "lane-b", "resume", ("topic.create_sub",), inherit=True)

        for members in (["planner"], ["planner", "worker"]):
            refused(
                await plain.call(client, "create_topic", path="job-hunt/with-members", members=members),
                "invalid_request",
                NO_MEMBERS,
            )
            assert "job-hunt/with-members" not in scene.topic_paths()
            assert scene.events("refused_use") == []

        made = succeeded(
            await plain.call(
                client, "create_topic", path="job-hunt/referrals", title="Referrals", description="Introductions"
            )
        )
        assert (made["created"], made["path"], made["title"], made["description"]) == (
            True,
            "job-hunt/referrals",
            "Referrals",
            "Introductions",
        )
        assert made["members"] == ["planner", "worker"]
        assert scene.members("job-hunt/referrals") == ["planner", "worker"]
        new_id = scene.topic_id("job-hunt/referrals")
        assert new_id not in {scene.topic_id(path) for path in support.BOARD}
        row = only([one for one in scene.changes("create_topic") if one["topic_id"] == new_id], "history row")
        assert (row["actor"], row["grant_id"]) == (plain.actor, plain.grant_id)
        assert json.loads(row["before"] if row["before"] else "null") is None
        assert json.loads(row["after"])["path"] == "job-hunt/referrals"

        succeeded(
            await tool(client, scene.host, "update_topic", {"path": "job-hunt", "members": ["worker"]}, role="planner")
        )
        assert scene.members("job-hunt/referrals") == ["planner", "worker"]
        inherited = succeeded(await inheriting.call(client, "create_topic", path="resume/next-step"))
        assert inherited["path"] == "resume/next-step"

    play(scene.host, scenario)


def test_a_guest_thread_can_be_started_in_a_grantee_made_subtopic(build_scene):
    """Why members are copied: an empty subtopic takes no guest thread, so a new one would be unusable until a core
    role stepped in. Another guest, with no grant, starts a thread there at once."""
    # Mutant 7: not copying the parent's members.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene, ("topic.create_sub",))
        succeeded(await held.call(client, "create_topic", path="job-hunt/referrals"))
        other = await tool(client, scene.host, "claim_alias", {"name": "lane-b", "passphrase": PASSPHRASE})
        started = succeeded(
            await tool(
                client,
                scene.host,
                "create_conversation",
                {"title": "A guest thread", "topic": "job-hunt/referrals", "token": other["binding_token"]},
            )
        )
        assert started["created"] is True and started["topic"] == "job-hunt/referrals"

    play(scene.host, scenario)


def test_a_grantee_cannot_create_anywhere_but_directly_under_the_topic_its_grant_names(build_scene):
    """Cases 6 and 10. Root level, a lookalike root, an unrelated root and another root's subtopic are refused as
    not covered, each with its event. A third level is refused too. A grant on a subtopic names that subtopic, so it
    does not reach its parent, and no third level is made under it."""
    # Mutant 7: creating under a topic the grant merely covers rather than names.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene, ("topic.create_sub",))
        for path in ("jobs", "job-hunting/x", "resume/x"):
            result = await held.call(client, "create_topic", path=path)
            assert result.get("error") == "forbidden", (path, result)
            assert result["detail"].startswith(not_allowed(held.grant_id, "topic.create_sub")), result
        assert len(scene.events("refused_use")) == 3
        third = await held.call(client, "create_topic", path="job-hunt/boards/x")
        assert third.get("error") in ("forbidden", "invalid_request"), third
        narrow = await scene.grantee(client, "lane-b", "job-hunt/boards", ("topic.create_sub",), inherit=True)
        for path in ("job-hunt/by-the-narrow-grant", "job-hunt/boards/x"):
            result = await narrow.call(client, "create_topic", path=path)
            assert result.get("error") in ("forbidden", "invalid_request"), (path, result)
        added = set(scene.topic_paths()) - set(support.BOARD)
        assert added == set(), added

    play(scene.host, scenario)


# 2. Replays ------------------------------------------------------------------------------------------------------


def test_a_replay_while_the_grant_is_active_returns_the_stored_topic_and_writes_nothing(build_scene):
    """A retry with the same request_id returns the topic as created, marked as not created, and adds no row."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene, ("topic.create_sub",))
        request_id = "req-" + secrets.token_hex(4)
        first = succeeded(await held.call(client, "create_topic", path="job-hunt/once", request_id=request_id))
        rows = [tuple(one) for one in scene.changes()]
        again = succeeded(await held.call(client, "create_topic", path="job-hunt/once", request_id=request_id))
        assert (first["created"], again["created"]) == (True, False)
        assert again["path"] == "job-hunt/once" and again["request_id"] == request_id
        assert [tuple(one) for one in scene.changes()] == rows

    play(scene.host, scenario)


@pytest.mark.parametrize("ending", ["revoked", "expired"])
def test_a_replay_after_the_grant_ends_is_refused_not_answered_from_the_stored_result(build_scene, ending):
    """Case 18. The replay runs the grant check first, so after a revoke or an expiry it is refused, and the stored
    result is not returned."""
    # Mutant 2: the replay returning the stored result before the grant check.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene, ("topic.create_sub",))
        request_id = "req-" + secrets.token_hex(4)
        succeeded(await held.call(client, "create_topic", path="job-hunt/once", request_id=request_id))
        if ending == "revoked":
            scene.revoke(held.grant_id)
        else:
            scene.clock.advance(days=7)
        result = await held.call(client, "create_topic", path="job-hunt/once", request_id=request_id)
        refused(result, "forbidden", f"topic grant {held.grant_id} is {ending}")
        assert "path" not in result and "created" not in result

    play(scene.host, scenario)


def test_two_aliases_never_share_a_request(build_scene):
    """Both act as `guest`, so a request digest of the role alone would be shared. The second alias, reusing the
    first one's request_id for the same topic, is not answered from the first one's result."""
    # Mutant 2 (replay): the digest missing the alias id.
    scene = build_scene()

    async def scenario(client):
        first = await board(client, scene, ("topic.create_sub",))
        second = await scene.grantee(client, "lane-b", "job-hunt", ("topic.create_sub",))
        request_id = "req-" + secrets.token_hex(4)
        succeeded(await first.call(client, "create_topic", path="job-hunt/shared", request_id=request_id))
        result = await second.call(client, "create_topic", path="job-hunt/shared", request_id=request_id)
        assert result.get("error") == "idempotency_conflict", result
        assert only(scene.changes("create_topic")[-1:], "history row")["actor"] == first.actor

    play(scene.host, scenario)


# 3. set_topic ----------------------------------------------------------------------------------------------------


def test_a_grantee_moves_a_thread_between_covered_topics_and_the_participants_are_told(build_scene):
    """Case 7. The grantee is not a participant and needs no participant flag. The move is recorded under the grant,
    and every participant of the thread gets a host notice naming who moved it, from where, to where, and why."""
    # Mutant 8: the move notice not written. Mutant 10: grant_id missing from the history row.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene, ("thread.organise",))
        moving = await thread(client, scene.host, "job-hunt/leads")
        moved = succeeded(await held.call(client, "set_topic", conversation_id=moving, topic="job-hunt/boards"))
        assert moved == {"conversation_id": moving, "topic": "job-hunt/boards", "changed": True}
        assert scene.thread_topic(moving) == "job-hunt/boards"
        row = only(scene.changes("set_topic"), "set_topic history row")
        assert (row["actor"], row["grant_id"]) == (held.actor, held.grant_id)
        assert json.loads(row["before"])["topic"] == "job-hunt/leads"
        assert json.loads(row["after"])["topic"] == "job-hunt/boards"
        expected = {
            "event": "topic_moved",
            "from": "job-hunt/leads",
            "to": "job-hunt/boards",
            "by": held.actor,
            "alias": held.name,
            "alias_code": held.code,
            "grant_id": held.grant_id,
        }
        notices = scene.moved_notices(moving)
        assert [(recipient, notice) for recipient, notice, _ in notices] == [
            ("planner", expected),
            ("worker", expected),
        ]
        for _, _, text in notices:
            for part in (held.name, held.code, "job-hunt/leads", "job-hunt/boards", held.grant_id):
                assert part in text, (part, text)
        senders = [
            one["sender"]
            for one in scene.query(
                "SELECT sender FROM messages WHERE conversation_id = ? AND notice LIKE '%topic_moved%'", moving
            )
        ]
        assert senders == [None, None], "a host notice has no sender"
        assert {
            one["role"] for one in scene.query("SELECT role FROM participants WHERE conversation_id = ?", moving)
        } == {"planner", "worker"}

    play(scene.host, scenario)


def test_a_move_needs_both_the_source_and_the_destination_in_scope(build_scene):
    """Case 8. Every outside endpoint, including lookalike paths, refuses without moving the thread."""
    # Mutant 1: thread.organise checking only the source, or only the destination.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene, ("thread.organise",))
        cases = (
            ("resume", "job-hunt/boards", "resume"),
            ("job-hunt/boards", "resume", "resume"),
            ("job-hunting", "job-hunt", "job-hunting"),
            ("job-hunt", "job-hunting", "job-hunting"),
        )
        for index, (source, destination, uncovered) in enumerate(cases, start=1):
            moving = await thread(client, scene.host, source, f"Scope case {index}")
            result = await held.call(client, "set_topic", conversation_id=moving, topic=destination)
            assert result.get("error") == "forbidden", (source, destination, result)
            assert result["detail"].startswith(not_allowed(held.grant_id, "thread.organise")), result
            # The destination is the caller's own word and is named; the thread's current topic never is.
            assert (uncovered in result["detail"]) == (uncovered == destination), result
            assert scene.thread_topic(moving) == source
            assert scene.changes("set_topic") == [] and scene.moved_notices(moving) == []
            events = scene.events("refused_use")
            assert len(events) == index
            assert json.loads(events[-1]["detail"]) == {"tool": "set_topic", "reason": "not_covered"}

    play(scene.host, scenario)


def test_a_move_needs_an_existing_source_topic_row(build_scene):
    """A null or stale source label cannot be moved into a covered destination."""
    # Mutant 1: checking only the destination.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene, ("thread.organise",))
        await control_move(client, scene, held)
        before = scene.changes("set_topic")
        for source in (None, "job-hunt/ghost"):
            moving = await thread(client, scene.host, None if source is None else "job-hunt/leads", f"Source {source}")
            if source is not None:
                with scene.db() as conn:
                    conn.execute("UPDATE conversations SET topic = ? WHERE id = ?", (source, moving))
            result = await held.call(client, "set_topic", conversation_id=moving, topic="job-hunt/boards")
            assert result.get("error") == "forbidden", (source, result)
            assert scene.thread_topic(moving) == source
            assert scene.changes("set_topic") == before and scene.moved_notices(moving) == []

    play(scene.host, scenario)


def test_a_grantee_cannot_clear_a_threads_topic_or_move_it_to_a_topic_that_does_not_exist(build_scene):
    """Clearing the topic to none, and a destination with no topic row, are refused and change nothing."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene, ("thread.organise",))
        await control_move(client, scene, held)
        before = scene.changes("set_topic")
        moving = await thread(client, scene.host, "job-hunt/leads")
        for topic in (None, "job-hunt/ghost"):
            result = await held.call(client, "set_topic", conversation_id=moving, topic=topic)
            if topic is None:
                assert result.get("error") == "forbidden", result
            else:
                assert "error" in result, result
            assert scene.thread_topic(moving) == "job-hunt/leads", topic
            assert scene.changes("set_topic") == before and scene.moved_notices(moving) == [], topic

    play(scene.host, scenario)


def test_a_move_to_the_topic_the_thread_already_has_changes_nothing(build_scene):
    """The check passes and nothing follows: no history row and no notice, and the answer says it changed nothing."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene, ("thread.organise",))
        moving = await thread(client, scene.host, "job-hunt/leads")
        same = succeeded(await held.call(client, "set_topic", conversation_id=moving, topic="job-hunt/leads"))
        assert same == {"conversation_id": moving, "topic": "job-hunt/leads", "changed": False}
        assert scene.changes("set_topic") == [] and scene.moved_notices(moving) == []

    play(scene.host, scenario)


def test_a_core_roles_move_writes_no_notice_and_names_no_grant(build_scene):
    """GUARD. A core role's `set_topic` is unchanged: no notice to the participants, no grant in the history row,
    even while a grant covers both topics."""
    # Mutant 8: the notice written for a core move.
    scene = build_scene()

    async def scenario(client):
        await board(client, scene, ("thread.organise",))
        moving = await thread(client, scene.host, "job-hunt/leads")
        moved = succeeded(
            await tool(
                client, scene.host, "set_topic", {"conversation_id": moving, "topic": "job-hunt/boards"}, role="planner"
            )
        )
        assert moved["changed"] is True
        row = only(scene.changes("set_topic"), "set_topic history row")
        assert (row["actor"], row["grant_id"]) == ("planner", None)
        assert scene.moved_notices(moving) == []
        assert scene.query("SELECT 1 FROM messages WHERE conversation_id = ? AND notice IS NOT NULL", moving) == []

    play(scene.host, scenario)


def test_a_grant_for_one_capability_does_not_allow_the_other_tool(build_scene):
    """A grant lists what it allows: `topic.create_sub` does not let a grantee move threads, and `thread.organise`
    does not let it create a subtopic."""
    scene = build_scene()

    async def scenario(client):
        creator = await board(client, scene, ("topic.create_sub",))
        mover = await scene.grantee(client, "lane-b", "job-hunt", ("thread.organise",), inherit=True)
        moving = await thread(client, scene.host, "job-hunt/leads")
        result = await creator.call(client, "set_topic", conversation_id=moving, topic="job-hunt/boards")
        assert result.get("error") == "forbidden", result
        assert result["detail"].startswith(not_allowed(creator.grant_id, "thread.organise")), result
        result = await mover.call(client, "create_topic", path="job-hunt/nope")
        assert result.get("error") == "forbidden", result
        assert result["detail"].startswith(not_allowed(mover.grant_id, "topic.create_sub")), result
        assert scene.thread_topic(moving) == "job-hunt/leads" and "job-hunt/nope" not in scene.topic_paths()

    play(scene.host, scenario)


def test_a_subtopic_a_grantee_makes_is_covered_only_if_the_grant_inherits(build_scene):
    """The new topic gets its own id and is covered because it sits under the granted root. With `inherit` off the
    grant covers the named topic alone, so a thread cannot be moved into the subtopic it just made."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        inheriting = await scene.grantee(client, "lane-a", "job-hunt", ("topic.create_sub", "thread.organise"), True)
        plain = await scene.grantee(client, "lane-b", "resume", ("topic.create_sub", "thread.organise"), False)
        succeeded(await inheriting.call(client, "create_topic", path="job-hunt/referrals"))
        succeeded(await plain.call(client, "create_topic", path="resume/drafts"))
        moving = await thread(client, scene.host, "job-hunt/leads")
        succeeded(await inheriting.call(client, "set_topic", conversation_id=moving, topic="job-hunt/referrals"))
        on_resume = await thread(client, scene.host, "resume")
        result = await plain.call(client, "set_topic", conversation_id=on_resume, topic="resume/drafts")
        assert result.get("error") == "forbidden", result
        assert scene.thread_topic(on_resume) == "resume"

    play(scene.host, scenario)


# 4. Ordinary guest use, bindings and revokes ---------------------------------------------------------------------


def test_a_renewed_binding_works_with_the_same_key_the_next_day(build_scene):
    """Case 16. Claiming the alias again gives a new binding token and nothing else; the key still works, and the
    old binding was not touched. Nothing is carried over: the check is rebuilt from the database."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene, ("topic.create_sub",))
        scene.clock.advance(days=1)
        renewed = await tool(client, scene.host, "claim_alias", {"name": "lane-a", "passphrase": PASSPHRASE})
        assert renewed["binding_token"] and renewed["binding_token"] != held.token
        succeeded(
            await tool(
                client,
                scene.host,
                "create_topic",
                {"path": "job-hunt/next-day", "token": renewed["binding_token"], "grant_token": held.key},
            )
        )
        succeeded(await held.call(client, "create_topic", path="job-hunt/old-binding"))

    play(scene.host, scenario)


def test_after_a_revoke_the_alias_still_reads_and_starts_threads_but_the_key_opens_nothing(build_scene):
    """Cases 17 and 19. The binding stays valid and ordinary guest use goes on; only the key's tools are refused,
    and claiming the alias again gives a guest and no more."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene, ALL)
        moving = await thread(client, scene.host, "job-hunt/leads")
        succeeded(await held.call(client, "create_topic", path="job-hunt/before"))
        scene.revoke(held.grant_id)
        gone = f"topic grant {held.grant_id} is revoked"
        refused(await held.call(client, "create_topic", path="job-hunt/after"), "forbidden", gone)
        refused(
            await held.call(client, "set_topic", conversation_id=moving, topic="job-hunt/boards"), "forbidden", gone
        )
        assert "job-hunt/before" in [
            one["path"] for one in succeeded(await held.call_keyless(client, "list_topics"))["topics"]
        ]
        started = succeeded(
            await held.call_keyless(client, "create_conversation", title="Still a guest", topic="job-hunt/boards")
        )
        assert started["created"] is True
        again = await tool(client, scene.host, "claim_alias", {"name": "lane-a", "passphrase": PASSPHRASE})
        refused(
            await tool(
                client,
                scene.host,
                "create_topic",
                {"path": "job-hunt/reclaimed", "token": again["binding_token"], "grant_token": held.key},
            ),
            "forbidden",
            gone,
        )
        assert scene.thread_topic(moving) == "job-hunt/leads"

    play(scene.host, scenario)


def test_a_grant_on_one_subtopic_covers_neither_its_parent_nor_its_sibling(build_scene):
    """Case 10. A grant on `job-hunt/boards` alone: a thread cannot be moved into it from the sibling `job-hunt/leads`
    or out of it to the parent `job-hunt`, and the parent is not a root it may create under."""
    # Mutant 1: checking only one end. Also: a grant reaching up to an ancestor or across to a sibling.
    scene = build_scene()

    async def scenario(client):
        held = await board(
            client, scene, ("thread.organise", "topic.create_sub"), inherit=False, topic="job-hunt/boards"
        )
        sibling = await thread(client, scene.host, "job-hunt/leads")
        inside = await thread(client, scene.host, "job-hunt/boards")
        succeeded(await held.call(client, "set_topic", conversation_id=inside, topic="job-hunt/boards"))
        for moving, destination in ((sibling, "job-hunt/boards"), (inside, "job-hunt/leads"), (inside, "job-hunt")):
            result = await held.call(client, "set_topic", conversation_id=moving, topic=destination)
            assert result.get("error") == "forbidden", (destination, result)
        assert (scene.thread_topic(sibling), scene.thread_topic(inside)) == ("job-hunt/leads", "job-hunt/boards")
        assert scene.changes("set_topic") == [] and scene.moved_notices(inside) == []
        assert (await held.call(client, "create_topic", path="job-hunt/beside")).get("error") == "forbidden"

    play(scene.host, scenario)


def test_core_topic_tools_behave_as_they_did_when_no_grant_is_involved(build_scene):
    """GUARD. With a grant sitting on the topics, a core role's create, update, move and rename answer as before: the
    same fields, no notice, no grant in the history, and a role without the flag, or a mover who is not a
    participant, is refused in the same words."""
    scene = build_scene()

    async def scenario(client):
        await board(client, scene)
        made = succeeded(
            await tool(
                client,
                scene.host,
                "create_topic",
                {"path": "core-root", "members": ["worker"], "title": "Core"},
                role="planner",
            )
        )
        assert (made["members"], made["created"], set(made)) == (
            ["worker"],
            True,
            {"path", "title", "description", "members", "created_by", "created_at", "request_id", "created"},
        )
        updated = succeeded(
            await tool(
                client, scene.host, "update_topic", {"path": "core-root", "title": "Renamed title"}, role="planner"
            )
        )
        assert updated["title"] == "Renamed title"
        moving = await thread(client, scene.host, "job-hunt/leads")
        refused(
            await tool(
                client, scene.host, "set_topic", {"conversation_id": moving, "topic": "core-root"}, role="steward"
            ),
            "not_a_participant",
        )
        succeeded(
            await tool(
                client, scene.host, "set_topic", {"conversation_id": moving, "topic": "core-root"}, role="planner"
            )
        )
        renamed = succeeded(
            await tool(
                client, scene.host, "rename_topic", {"path": "core-root", "new_path": "core-kept"}, role="planner"
            )
        )
        assert (renamed["renamed_from"], renamed["subtopics"]) == ("core-root", 0)
        assert {row["grant_id"] for row in scene.changes()} == {None}
        assert scene.moved_notices(moving) == [] and scene.events("rename") == []
        refused(
            await tool(client, scene.host, "update_topic", {"path": "core-kept", "title": "x"}, role="worker"),
            "forbidden",
            "worker may not create or change topics (roles.worker.can_create_topics)",
        )

    play(scene.host, scenario)
