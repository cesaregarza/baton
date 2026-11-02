"""Retries of an edit: the same request answers the same revision, the authority is checked again first, and two
callers never share a request.

What this pins:

* A retry with the same `request_id` and the same arguments returns the stored revision, `created` false, and writes
  nothing, even after the document has moved on and its `expected_revision` is long behind.
* The same `request_id` with a different body, name, topic or `expected_revision`, from another role, or from another
  alias is refused as `idempotency_conflict`: the digest holds the actor role, the alias id, the topic's id (not its
  path), the name, the body and the expected revision.
* The digest keys on the topic's id, so a retry follows its topic through a rename (and answers with the new path),
  and a retry that names a path another topic now holds, or no topic holds, is not answered from the first topic.
* A retry is not a way around authority: a core role whose flag was removed is refused again.
* A call with no `request_id` gets a fresh one every time.

Mutant numbers in comments refer to the spec's list of one-line mutants the tests must kill. Q numbers refer to its open
questions: the test pins the question's default, so a ruling changes only the tests that carry that number.
"""

from __future__ import annotations

import json

import pytest
import reference_support as refs
import topic_grant_tools_support as support
from reference_support import (
    BOARDS,
    NAME,
    ROOT,
    board,
    body_of,
    claim_again,
    core_edit,
    edit_arguments,
    f1,
    f8,
    f9,
    grantee_edit,
    not_covered,
    open_topic,
    rename,
    request_id,
)
from topic_grant_tools_support import play, refused, succeeded, tool

build_scene = refs.build_scene  # the fixture, under the name the tests ask for


def editing_as(who, client, scene, held):
    """One call that edits as a core role or as the grantee, so a test reads the same for both."""
    if who == "core":
        return lambda *args, **extra: core_edit(client, scene, *args, **extra)
    return lambda *args, **extra: grantee_edit(client, held, *args, **extra)


@pytest.mark.parametrize("who", ["core", "grantee"])
def test_the_same_request_twice_writes_one_row_and_answers_the_stored_revision_even_after_it_moved_on(build_scene, who):
    """The retry is `created` false and equal to the first answer in everything else, with no new row. Two more
    revisions later it still answers the first one's data, not the current revision's."""
    # Mutant 7: the replay lookup below the compare-and-swap.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        edit = editing_as(who, client, scene, held)
        rid = request_id()
        first = succeeded(await edit(BOARDS, NAME, body_of(1), 0, request_id=rid))
        tables = scene.tables()
        second = succeeded(await edit(BOARDS, NAME, body_of(1), 0, request_id=rid))
        assert (first["created"], second["created"]) == (True, False)
        assert second == {**first, "created": False}
        assert scene.tables() == tables
        succeeded(await edit(BOARDS, NAME, body_of(2), 1))
        succeeded(await edit(BOARDS, NAME, body_of(3), 2))
        third = succeeded(await edit(BOARDS, NAME, body_of(1), 0, request_id=rid))
        assert third == {**first, "created": False}
        assert len(scene.revisions(BOARDS, NAME)) == 3

    play(scene.host, scenario)


def test_the_same_request_id_with_anything_else_changed_is_an_idempotency_conflict(build_scene):
    """R12, F8. A different body, a different `expected_revision` with the same body, a different name and a
    different topic each give a different digest, so the stored request is not answered and nothing is written."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        rid = request_id()
        succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(1), 0, request_id=rid))
        tables = scene.tables()
        for arguments in ((BOARDS, NAME, "another body", 0), (BOARDS, NAME, body_of(1), 1),
                          (BOARDS, "lead-list", body_of(1), 0), (refs.LEADS, NAME, body_of(1), 0)):
            refused(await core_edit(client, scene, *arguments, request_id=rid), "idempotency_conflict", f8(rid))
            assert scene.tables() == tables

    play(scene.host, scenario)


def test_a_retry_with_a_renewed_binding_token_is_still_a_replay(build_scene):
    """R12. The digest holds the alias's id and never the binding token or the key, so an agent that claims its alias
    again (a new token) and retries a request it never saw the answer to gets the stored revision, not a conflict."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        rid = request_id()
        first = succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0, request_id=rid))
        renewed = await claim_again(client, scene)
        assert renewed["binding_token"] != held.token
        tables = scene.tables()
        again = succeeded(await tool(client, scene.host, "edit_reference", edit_arguments(
            BOARDS, NAME, body_of(1), 0, request_id=rid, token=renewed["binding_token"], grant_token=held.key)))
        assert again == {**first, "created": False}
        assert scene.tables() == tables

    play(scene.host, scenario)


def test_another_core_role_cannot_replay_a_request_it_did_not_make(build_scene):
    """R12. The actor role is in the digest: a second role with the flag, reusing the request id with the same body,
    topic, name and number, is a conflict, not a replay of the planner's revision."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        rid = request_id()
        succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(1), 0, request_id=rid))
        tables = scene.tables()
        refused(await core_edit(client, scene, BOARDS, NAME, body_of(1), 0, role="steward", request_id=rid),
                "idempotency_conflict", f8(rid))
        assert scene.tables() == tables

    play(scene.host, scenario)


def test_two_aliases_never_share_a_request(build_scene):
    """R12. Both act as `guest`, so a digest of the role alone would be shared. A second alias with its own grant,
    reusing the first one's request id with the same body, topic, name and number, is a conflict and writes nothing,
    and a core role reusing it is a conflict too."""
    # Mutant 8: the digest dropping the alias id. Q10 (the digest's parts).
    scene = build_scene()

    async def scenario(client):
        first = await board(client, scene)
        second = await scene.grantee(client, "lead-scout", ROOT, ("reference.edit",), inherit=True)
        rid = request_id()
        made = succeeded(await grantee_edit(client, first, BOARDS, NAME, body_of(1), 0, request_id=rid))
        tables = scene.tables()
        refused(await grantee_edit(client, second, BOARDS, NAME, body_of(1), 0, request_id=rid),
                "idempotency_conflict", f8(rid))
        refused(await core_edit(client, scene, BOARDS, NAME, body_of(1), 0, request_id=rid),
                "idempotency_conflict", f8(rid))
        assert scene.tables() == tables
        assert scene.revisions(BOARDS, NAME)[0]["author_alias_id"] == made["author"]["alias_id"] == first.alias_id

    play(scene.host, scenario)


@pytest.mark.parametrize("who", ["core", "grantee"])
def test_a_retry_after_a_rename_names_the_new_path_and_is_answered_from_the_stored_revision(build_scene, who):
    """Q10. The digest holds the topic's id, not its path. After `job-hunt` becomes `job-hunt-old`, the retry that names
    `job-hunt-old/boards` finds its revision, answered with the topic's path now, and writes nothing."""
    # Mutant 8: the digest dropping the topic id, or keying on the path. Q10.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene) if who == "grantee" else await support.stage(client, scene.host)
        edit = editing_as(who, client, scene, held)
        rid = request_id()
        first = succeeded(await edit(BOARDS, NAME, body_of(1), 0, request_id=rid))
        acknowledge = [held.grant_id] if who == "grantee" else None
        succeeded(await rename(client, scene, ROOT, "job-hunt-old", acknowledge))
        tables = scene.tables()
        again = succeeded(await edit("job-hunt-old/boards", NAME, body_of(1), 0, request_id=rid))
        assert again == {**first, "topic": "job-hunt-old/boards", "created": False}
        assert scene.tables() == tables

    play(scene.host, scenario)


def test_a_core_retry_naming_a_path_another_topic_now_holds_is_a_conflict(build_scene):
    """Q10. `job-hunt` is renamed and a new topic is opened at `job-hunt/boards`, with a new id. The retry naming that
    path resolves to the new topic, whose digest differs: a conflict, never the old topic's revision."""
    # Mutant 8: the digest dropping the topic id. Q10.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        rid = request_id()
        succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(1), 0, request_id=rid))
        succeeded(await rename(client, scene, ROOT, "job-hunt-old"))
        await open_topic(client, scene, ROOT)
        await open_topic(client, scene, BOARDS)
        tables = scene.tables()
        refused(await core_edit(client, scene, BOARDS, NAME, body_of(1), 0, request_id=rid), "idempotency_conflict",
                f8(rid))
        assert scene.tables() == tables

    play(scene.host, scenario)


def test_a_grantees_retry_naming_a_path_another_topic_now_holds_is_denied_as_not_covered(build_scene):
    """Q10. The same, for the grantee: the new topic at `job-hunt/boards` is not under G1, whose topic is now
    `job-hunt-old`, so the retry is refused as not covered and logged against the grant."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        rid = request_id()
        succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0, request_id=rid))
        succeeded(await rename(client, scene, ROOT, "job-hunt-old", [held.grant_id]))
        await open_topic(client, scene, ROOT)
        await open_topic(client, scene, BOARDS)
        tables = scene.tables()
        refused(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0, request_id=rid), "forbidden",
                not_covered(held.grant_id, BOARDS))
        assert scene.tables() == tables
        assert json.loads(scene.refused_uses()[-1]["detail"]) == {"tool": "edit_reference", "reason": "not_covered"}

    play(scene.host, scenario)


def test_a_retry_naming_the_path_a_topic_left_is_unknown_to_a_core_role_and_denied_to_a_grantee(build_scene):
    """Q10, Q16. With the old path free, a core role's retry is told there is no such topic, and a grantee's is
    denied as not covered. Neither is answered from the stored revision."""
    # Mutant 23: unknown_topic for a grantee on a missing topic.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        rid = request_id()
        succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0, request_id=rid))
        other = request_id()
        succeeded(await core_edit(client, scene, BOARDS, "lead-list", body_of(1), 0, request_id=other))
        succeeded(await rename(client, scene, ROOT, "job-hunt-old", [held.grant_id]))
        tables = scene.tables()
        refused(await core_edit(client, scene, BOARDS, "lead-list", body_of(1), 0, request_id=other),
                "unknown_topic", f9(BOARDS))
        refused(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0, request_id=rid), "forbidden",
                not_covered(held.grant_id, BOARDS))
        assert scene.tables() == tables

    play(scene.host, scenario)


def test_a_retry_by_a_core_role_whose_flag_has_been_removed_is_refused(build_scene):
    """R12. The retry runs the authority check first, so the stored revision is not answered to a role that may no
    longer edit references."""
    # Mutant 6: the replay lookup above the authority check.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        rid = request_id()
        succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(1), 0, request_id=rid))
        scene.set_role("planner", can_create_topics=False)
        result = await core_edit(client, scene, BOARDS, NAME, body_of(1), 0, request_id=rid)
        assert result == {"error": "forbidden", "detail": f1("planner")}
        assert len(scene.revisions(BOARDS, NAME)) == 1

    play(scene.host, scenario)


def test_a_call_with_no_request_id_gets_a_fresh_one_each_time(build_scene):
    """R12. Two successful calls that leave `request_id` out are two revisions with two different request ids, each
    returned and stored; the host makes the id, so a retry of such a call is the caller's to protect with its own."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        first = succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(1), 0))
        second = succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(1), 1))
        assert (first["created"], second["created"]) == (True, True)
        assert first["request_id"] and second["request_id"] and first["request_id"] != second["request_id"]
        assert [row["request_id"] for row in scene.revisions(BOARDS, NAME)] == [first["request_id"],
                                                                                second["request_id"]]
        assert 1 <= len(first["request_id"]) <= 200

    play(scene.host, scenario)
