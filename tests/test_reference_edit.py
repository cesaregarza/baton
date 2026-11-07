"""Editing a reference: who may, what a revision records, and the design's worked cases, driven over MCP with real
topic grants (proposed and authorized through the grant module, never a raw row).

What this pins:

* Cases 2, 3, 4, 9, 10 and 15-19 of the design's section 5, with `edit_reference` in the place of `create_topic`:
  a grantee's edit records its alias, its grant and a digest; a stale edit writes nothing and says what the revision
  now is; a retry returns the stored revision; a topic the grant does not cover, a key that is not the alias's, and a
  grant that is no longer active are each refused in S2's words and write S2's `refused_use` event.
* Who may edit: a core role with `can_create_topics` needs no key and any key it sends is ignored; one without the flag
  is refused; an alias mapped to a core role is that role, with the alias recorded; a grantee needs `reference.edit`.
* The compare-and-swap, as a table of what is accepted and what is stale, and the module function's refusal of an
  `expected_revision` that is missing, boolean, negative or text.
* Documents keep separate revision sequences; an undo is a new revision and changes no earlier row; a no-op edit is a
  revision too.

Mutant numbers in comments refer to the spec's list of one-line mutants the tests must kill. Q numbers refer to its open
questions: the test pins the question's default, so a ruling changes only the tests that carry that number.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
import reference_support as refs
import topic_grant_tools_support as support
from reference_support import (
    BOARDS,
    EDIT_KEYS,
    F2,
    F6,
    LEADS,
    NAME,
    NEEDS_BINDING,
    ROOT,
    Core,
    Keyed,
    Staging,
    as_the_guard_calls,
    author_of,
    board,
    body_of,
    bring_to_state,
    claim_again,
    core_edit,
    direct,
    dump,
    edit_arguments,
    f1,
    f5,
    f9,
    grantee_edit,
    not_covered,
    read_reference,
    reference_history,
    request_id,
    revoked,
    write_revisions,
)
from test_per_call_identity import owner
from topic_grant_tools_support import only, play, refused, succeeded, tool

from baton import aliases
from baton.errors import BatonError

build_scene = refs.build_scene  # the fixture, under the name the tests ask for


async def five_revisions(client, scene):
    """Four revisions by a core role and a fifth by the grantee: the state the design's cases 2-4 start from."""
    held = await board(client, scene)
    await write_revisions(client, scene, BOARDS, NAME, 4)
    succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(5), 4))
    return held


def rows_of(rows):
    return [tuple(row) for row in rows]


def without_posts(counts):
    """Row counts but for the hourly post counter, which a grantee's every attempt adds a row to."""
    return {table: count for table, count in counts.items() if table != "alias_posts"}


# 1. The design's worked cases ---------------------------------------------------------------------------------


def test_a_grantees_edit_writes_the_next_revision_with_its_alias_its_grant_and_a_digest(build_scene):
    """Case 2. Four revisions by a core role, then job-search edits with expected_revision 4: revision 5, based on 4,
    created. The response and the stored row say who wrote it and under which grant."""
    # Mutant 21: grant_id stored as NULL for a grantee, or author_role hard-coded.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        await write_revisions(client, scene, BOARDS, NAME, 4)
        earlier = scene.revisions(BOARDS, NAME)
        rid = request_id()
        made = succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(5), 4, request_id=rid))
        assert set(made) == EDIT_KEYS, sorted(made)
        assert (made["topic"], made["topic_id"], made["name"]) == (BOARDS, scene.topic_id(BOARDS), NAME)
        assert (made["revision"], made["base_revision"], made["created"]) == (5, 4, True)
        assert (made["size"], made["request_id"], made["grant_id"]) == (len(body_of(5).encode()), rid, held.grant_id)
        assert made["author"] == author_of(held)
        datetime.fromisoformat(made["at"])
        rows = scene.revisions(BOARDS, NAME)
        assert [row["revision"] for row in rows] == [1, 2, 3, 4, 5]
        assert dump(rows[:4]) == dump(earlier)
        row = rows[4]
        assert (row["body"], row["base_revision"], row["at"], row["request_id"]) == (body_of(5), 4, made["at"], rid)
        assert (row["author_role"], row["author_alias_id"], row["author_alias_name"], row["author_alias_code"]) == (
            "guest",
            held.alias_id,
            held.name,
            held.code,
        )
        assert row["grant_id"] == held.grant_id
        assert re.fullmatch("[0-9a-f]{64}", row["request_digest"]), row["request_digest"]
        doc = only(scene.docs(), "document")
        assert (doc["topic_id"], doc["name"], row["doc_id"]) == (scene.topic_id(BOARDS), NAME, doc["id"])
        assert re.fullmatch("ref_[0-9a-f]{16}", doc["id"]), doc["id"]

    play(scene.host, scenario)


def test_a_second_edit_on_the_revision_already_replaced_is_stale_and_writes_nothing(build_scene):
    """Case 3. A new request, expected_revision 4 while the document is at 5: refused as stale, with the current
    number as an extra field and the spec's words. Both tables, the events and every other table are as they were.
    Reading again and editing on 5 writes revision 6."""
    # Mutant 22: the refusal without current_revision. Mutant 1: expected lower than current accepted.
    scene = build_scene()

    async def scenario(client):
        held = await five_revisions(client, scene)
        tables, counts = scene.tables(), without_posts(scene.counts())
        stale = await grantee_edit(client, held, BOARDS, NAME, "text from a stale reader", 4, request_id=request_id())
        assert stale == {"error": "stale_revision", "detail": f5(NAME, BOARDS, 5, 4), "current_revision": 5}
        assert scene.tables() == tables and without_posts(scene.counts()) == counts
        assert scene.refused_uses() == [], "a stale edit names no grant, so it writes no grant event"
        current = succeeded(await read_reference(client, Keyed(held), BOARDS, NAME))
        assert current["current_revision"] == 5
        again = succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(6), current["current_revision"]))
        assert (again["revision"], again["base_revision"]) == (6, 5)

    play(scene.host, scenario)


def test_a_retry_with_the_same_request_id_returns_the_stored_revision_though_it_is_behind(build_scene):
    """Case 4. Revision 6 was written with expected_revision 5. The same request again answers revision 6, not created,
    and writes no row, although 5 is behind the current 6. The grant is active throughout."""
    # Mutant 7: the replay lookup below the compare-and-swap.
    scene = build_scene()

    async def scenario(client):
        held = await five_revisions(client, scene)
        rid = request_id()
        six = succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(6), 5, request_id=rid))
        tables = scene.tables()
        retry = succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(6), 5, request_id=rid))
        assert retry == {**six, "created": False}
        assert (retry["revision"], retry["created"]) == (6, False)
        assert scene.tables() == tables

    play(scene.host, scenario)


def test_a_grantee_cannot_edit_a_sibling_lookalike_or_an_unrelated_topic(build_scene):
    """Case 9, with real edits. `job-hunting` and `resume` are not under G1: refused as not covered, in S2's words,
    each writes one `refused_use` event naming `edit_reference`, and nothing is written."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        await support.stage(client, scene.host, paths=("job-hunting", "resume"))
        for path in ("job-hunting", "resume"):
            tables, events = scene.tables(), len(scene.refused_uses())
            refused(
                await grantee_edit(client, held, path, NAME, "text", 0), "forbidden", not_covered(held.grant_id, path)
            )
            assert scene.tables() == tables
            found = scene.refused_uses()
            assert len(found) == events + 1
            assert (found[-1]["grant_id"], found[-1]["actor"]) == (held.grant_id, held.actor)
            assert json.loads(found[-1]["detail"]) == {"tool": "edit_reference", "reason": "not_covered"}

    play(scene.host, scenario)


def test_a_topic_with_no_row_is_unknown_to_a_core_role_and_denied_to_a_grantee(build_scene):
    """A path no topic holds. A core role is told so, as for any unknown topic. A grantee is denied as for a topic
    its grant does not cover, and the denial is logged against the grant, because an unmatched string resolves to
    nothing and nothing is covered (design 4, Resolution)."""
    # Q16. Mutant 23: unknown_topic for a grantee on a missing topic, and no refused_use event.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        for path in ("job-hunt/ghost", "ghost"):
            events = len(scene.refused_uses())
            refused(
                await grantee_edit(client, held, path, NAME, "text", 0), "forbidden", not_covered(held.grant_id, path)
            )
            found = scene.refused_uses()
            assert len(found) == events + 1
            assert json.loads(found[-1]["detail"]) == {"tool": "edit_reference", "reason": "not_covered"}
            refused(await core_edit(client, scene, path, NAME, "text", 0), "unknown_topic", f9(path))
            assert len(scene.refused_uses()) == events + 1, "a core role's refusal names no grant"
        assert scene.docs() == [] and scene.revisions() == []

    play(scene.host, scenario)


def test_a_grant_on_one_subtopic_covers_neither_its_ancestor_nor_its_sibling(build_scene):
    """Case 10. G1b on `job-hunt/boards` alone: its own topic works, the ancestor `job-hunt` and the sibling
    `job-hunt/leads` are refused."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=(*refs.REFERENCE_TOPICS, LEADS))
        narrow = await scene.grantee(client, "job-search", BOARDS, ("reference.edit",), inherit=False)
        succeeded(await grantee_edit(client, narrow, BOARDS, NAME, "on the topic itself", 0))
        for path in (ROOT, LEADS):
            refused(
                await grantee_edit(client, narrow, path, NAME, "beside it", 0),
                "forbidden",
                not_covered(narrow.grant_id, path),
            )
        assert [row["revision"] for row in scene.revisions(BOARDS, NAME)] == [1]
        assert scene.docs()[0]["topic_id"] == scene.topic_id(BOARDS) and len(scene.docs()) == 1

    play(scene.host, scenario)


def test_inherit_decides_whether_a_grant_reaches_the_topics_under_its_own(build_scene):
    """Case 10. G1 on `job-hunt` without `inherit` covers `job-hunt` and not `job-hunt/boards`; with `inherit` it
    covers both, and a lookalike root is covered by neither."""
    # Mutant 14: the coverage rule ignoring inherit.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=(*refs.REFERENCE_TOPICS, "job-hunting"))
        flat = await scene.grantee(client, "job-search", ROOT, ("reference.edit",), inherit=False)
        succeeded(await grantee_edit(client, flat, ROOT, NAME, "on the root", 0))
        refused(
            await grantee_edit(client, flat, BOARDS, NAME, "under it", 0),
            "forbidden",
            not_covered(flat.grant_id, BOARDS),
        )
        inheriting = await scene.grantee(client, "lead-scout", ROOT, ("reference.edit",), inherit=True)
        succeeded(await grantee_edit(client, inheriting, BOARDS, NAME, "under it", 0))
        succeeded(await grantee_edit(client, inheriting, ROOT, "second-list", "on the root", 0))
        refused(
            await grantee_edit(client, inheriting, "job-hunting", NAME, "lookalike", 0),
            "forbidden",
            not_covered(inheriting.grant_id, "job-hunting"),
        )

    play(scene.host, scenario)


def test_a_binding_without_the_key_the_wrong_key_and_another_aliases_key_are_each_refused(build_scene):
    """Case 15. Someone claims `job-search` with its (non-secret) passphrase and gets a binding of their own. With no
    key the guard refuses as it does every guest tool. With a key nobody has: refusal 2. With the key of a second
    alias's own grant: refusal 3, logged against that grant with the alias that tried. Nothing is written."""
    # Mutant 12: the grantee branch granting without the key.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        other = await scene.grantee(client, "lead-scout", BOARDS, ("reference.edit",), inherit=False)
        claimed = await claim_again(client, scene)
        attempt = edit_arguments(BOARDS, NAME, "text", 0, token=claimed["binding_token"])
        assert await tool(client, scene.host, "edit_reference", attempt) == {"error": "forbidden", "detail": F2}
        refused(
            await tool(client, scene.host, "edit_reference", {**attempt, "grant_token": support.new_key()}),
            "forbidden",
            "unknown topic grant key",
        )
        refused(
            await tool(client, scene.host, "edit_reference", {**attempt, "grant_token": other.key}),
            "forbidden",
            "this topic grant key is not for this alias",
        )
        assert scene.revisions() == [] and scene.docs() == []
        found = scene.refused_uses()
        assert [(row["grant_id"], row["actor"]) for row in found] == [(other.grant_id, held.actor)]
        assert json.loads(found[0]["detail"]) == {"tool": "edit_reference", "reason": "other_alias"}

    play(scene.host, scenario)


def test_a_renewed_binding_works_with_the_same_key_the_next_day(build_scene, monkeypatch):
    """Case 16. A day passes on both clocks, the grant's and the binding's. The old binding token has expired; claiming
    the alias again gives a new one, and the same key is allowed with it."""
    scene = build_scene()
    shift = SimpleNamespace(offset=0.0)
    real = time.time
    monkeypatch.setattr(aliases, "time", SimpleNamespace(time=lambda: real() + shift.offset))

    async def scenario(client):
        held = await board(client, scene)
        succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0))
        scene.clock.advance(hours=25)
        shift.offset = 25 * 3600
        expired = await grantee_edit(client, held, BOARDS, NAME, body_of(2), 1)
        refused(expired, "forbidden", "alias binding is missing, invalid or expired")
        renewed = await claim_again(client, scene)
        assert renewed["binding_token"] and renewed["binding_token"] != held.token
        made = succeeded(
            await tool(
                client,
                scene.host,
                "edit_reference",
                edit_arguments(BOARDS, NAME, body_of(2), 1, token=renewed["binding_token"], grant_token=held.key),
            )
        )
        assert made["revision"] == 2 and made["grant_id"] == held.grant_id

    play(scene.host, scenario)


@pytest.mark.parametrize("state", ["revoked", "expired", "pending", "lapsed", "invalid"])
def test_a_grant_that_is_no_longer_active_is_refused_with_its_state_and_reading_still_works(build_scene, state):
    """Case 17, once for each state it names (the expired one at its exact instant). The binding token is still valid:
    the next edit is refused in S2's words, logged as a refused use, and writes nothing, while the same caller still
    reads the reference."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0))
        bring_to_state(scene, held, state)
        tables = scene.tables()
        refused(
            await grantee_edit(client, held, BOARDS, NAME, body_of(2), 1),
            "forbidden",
            f"topic grant {held.grant_id} is {state}",
        )
        assert scene.tables() == tables
        found = only(scene.refused_uses(), "refused_use event")
        assert (found["grant_id"], found["actor"]) == (held.grant_id, held.actor)
        assert json.loads(found["detail"]) == {"tool": "edit_reference", "reason": state}
        read = succeeded(await read_reference(client, Keyed(held), BOARDS, NAME))
        assert (read["revision"], read["body"]) == (1, body_of(1))

    play(scene.host, scenario)


def test_a_grant_works_until_the_instant_it_expires(build_scene):
    """The last instant before `expires_at` is still active; the instant itself is expired."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        scene.clock.advance(days=7, seconds=-1)
        succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0))
        scene.clock.advance(seconds=1)
        refused(
            await grantee_edit(client, held, BOARDS, NAME, body_of(2), 1),
            "forbidden",
            f"topic grant {held.grant_id} is expired",
        )

    play(scene.host, scenario)


@pytest.mark.parametrize("ending", ["revoked", "expired"])
def test_a_replay_after_the_grant_ends_is_refused_and_returns_nothing_from_storage(build_scene, ending):
    """Case 18. The retry of a request that succeeded runs the grant check first, so after the grant ended it is
    refused in S2's words and the stored revision is not in the answer."""
    # Mutant 6: the replay lookup above the authority check.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        rid = request_id()
        succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0, request_id=rid))
        if ending == "revoked":
            scene.revoke(held.grant_id)
        else:
            scene.clock.advance(days=7)
        result = await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0, request_id=rid)
        assert result == {"error": "forbidden", "detail": f"topic grant {held.grant_id} is {ending}"}

    play(scene.host, scenario)


def test_after_a_revoke_a_new_claim_gives_a_guest_with_no_authority_and_a_new_grant_needs_a_new_proposal(build_scene):
    """Case 19. The owner revokes G1. Claiming the alias again gives a binding and nothing more: the old key is still
    refused as revoked. A new proposal, authorized, gives a new key, and only that one works."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0))
        scene.revoke(held.grant_id)
        again = await claim_again(client, scene)
        old_key = edit_arguments(BOARDS, NAME, body_of(2), 1, token=again["binding_token"], grant_token=held.key)
        refused(await tool(client, scene.host, "edit_reference", old_key), "forbidden", revoked(held.grant_id))
        refused(await tool(client, scene.host, "edit_reference", {**old_key, "grant_token": None}), "forbidden", F2)
        new = scene.grant("job-search", ROOT, ("reference.edit",), inherit=True)
        assert new["id"] != held.grant_id and new["grant_key"] != held.key
        made = succeeded(await tool(client, scene.host, "edit_reference", {**old_key, "grant_token": new["grant_key"]}))
        assert (made["revision"], made["grant_id"]) == (2, new["id"])
        refused(
            await tool(
                client,
                scene.host,
                "edit_reference",
                edit_arguments(BOARDS, NAME, body_of(3), 2, token=again["binding_token"], grant_token=held.key),
            ),
            "forbidden",
            revoked(held.grant_id),
        )

    play(scene.host, scenario)


# 2. What a grant lists ------------------------------------------------------------------------------------------

OTHER_CAPABILITIES = [("thread.organise",), ("topic.create_sub",), ("thread.organise", "topic.create_sub")]


@pytest.mark.parametrize("listed", OTHER_CAPABILITIES)
def test_a_grant_without_reference_edit_is_refused_and_the_refusal_names_the_capability(build_scene, listed):
    """A grant for the other capabilities only is active and on the topic, but does not allow this edit."""
    # Mutant 13: S2's grant check called with thread.organise instead of reference.edit.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene, listed)
        refused(
            await grantee_edit(client, held, BOARDS, NAME, "text", 0),
            "forbidden",
            not_covered(held.grant_id, BOARDS, "reference.edit"),
        )
        assert scene.docs() == []
        assert json.loads(only(scene.refused_uses(), "event")["detail"])["reason"] == "not_covered"

    play(scene.host, scenario)


def test_a_grant_for_reference_edit_alone_is_enough(build_scene):
    """The capability the edit asks for is the one the grant lists, and no other is needed."""
    # Mutant 13: the check asking for thread.organise.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene, ("reference.edit",))
        made = succeeded(await grantee_edit(client, held, BOARDS, NAME, "text", 0))
        assert (made["revision"], made["grant_id"]) == (1, held.grant_id)

    play(scene.host, scenario)


def test_the_presented_keys_own_grant_decides_and_never_another_the_alias_holds(build_scene):
    """R9. An alias with two grants is decided by the one whose key it presents. Where the wide grant would allow the
    edit, the narrow key still gets only the narrow grant's answer, naming the narrow grant; and the wide key works."""
    # Mutant 12: the grantee branch granting without the key, or falling back to another grant of the alias.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=(*refs.REFERENCE_TOPICS, LEADS))
        narrow = await scene.grantee(client, "job-search", BOARDS, ("reference.edit",), inherit=False)
        wide = scene.another_grant(narrow, ROOT, ("reference.edit",), inherit=True)
        refused(
            await grantee_edit(client, narrow, LEADS, NAME, "text", 0), "forbidden", not_covered(narrow.grant_id, LEADS)
        )
        assert scene.docs() == []
        made = succeeded(await grantee_edit(client, wide, LEADS, NAME, "text", 0))
        assert made["grant_id"] == wide.grant_id
        assert {row["grant_id"] for row in scene.refused_uses()} == {narrow.grant_id}

    play(scene.host, scenario)


# 3. A core role ----------------------------------------------------------------------------------------------


def test_a_core_role_with_the_flag_edits_with_no_key_and_its_row_names_the_role_and_no_grant(build_scene):
    """R8. The planner holds `can_create_topics`: no key, no alias. The response and the row carry the role, the alias
    columns are all null, and `grant_id` is null."""
    # Mutant 21: author_role hard-coded.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        made = succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(1), 0))
        assert set(made) == EDIT_KEYS
        assert (made["author"], made["grant_id"], made["revision"], made["created"]) == (
            {"role": "planner"},
            None,
            1,
            True,
        )
        row = only(scene.revisions(BOARDS, NAME), "revision")
        assert (
            row["author_role"],
            row["author_alias_id"],
            row["author_alias_name"],
            row["author_alias_code"],
            row["grant_id"],
        ) == ("planner", None, None, None, None)
        assert scene.refused_uses() == []

    play(scene.host, scenario)


def test_a_core_role_ignores_a_grant_token_valid_or_not(build_scene):
    """R8. A key on a core role's call changes nothing: it is not consulted, not recorded, and cannot be refused."""
    # Mutant 12: a core role's grant_token being honoured.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene, ("thread.organise",))
        for number, key in enumerate((held.key, support.new_key()), start=1):
            made = succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(number), number - 1, grant_token=key))
            assert (made["grant_id"], made["author"]) == (None, {"role": "planner"})
        assert {row["grant_id"] for row in scene.revisions(BOARDS, NAME)} == {None}
        assert scene.refused_uses() == []

    play(scene.host, scenario)


def test_a_core_role_without_the_flag_is_refused_whatever_it_presents(build_scene):
    """R8, F1. The worker has no `can_create_topics`. A key, valid or not, does not help it, and nothing is written."""
    # Mutant 12: the core branch skipping the can_create_topics check.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        for extra in ({}, {"grant_token": held.key}, {"grant_token": support.new_key()}):
            refused(
                await core_edit(client, scene, BOARDS, NAME, "text", 0, role="worker", **extra),
                "forbidden",
                f1("worker"),
            )
        assert scene.docs() == [] and scene.revisions() == [] and scene.refused_uses() == []

    play(scene.host, scenario)


def test_an_alias_mapped_to_a_core_role_runs_as_that_role_with_the_alias_recorded(build_scene):
    """R8. The steward is enrolled as an alias and mapped to its role. A call with that alias's binding token runs as
    the steward: no grant is needed or recorded, a grant key sent along is ignored, and the alias is in the response
    and in the row beside the role."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene, ("thread.organise",))
        assert owner(scene.host, "alias", "arm-enrol", "steward") == 0
        enrolled = await tool(
            client, scene.host, "claim_alias", {"name": "steward", "passphrase": support.PASSPHRASE}, role="steward"
        )
        assert enrolled.get("effective_role") == "steward", enrolled
        proof = await tool(client, scene.host, "claim_alias", {"name": "steward", "passphrase": support.PASSPHRASE})
        assert proof.get("effective_role") == "steward", proof
        arguments = edit_arguments(BOARDS, NAME, body_of(1), 0, token=proof["binding_token"], grant_token=held.key)
        made = succeeded(await tool(client, scene.host, "edit_reference", arguments))
        assert made["author"] == {
            "role": "steward",
            "alias_id": proof["alias_id"],
            "alias": "steward",
            "code": proof["code"],
        }
        assert made["grant_id"] is None
        row = only(scene.revisions(BOARDS, NAME), "revision")
        assert (
            row["author_role"],
            row["author_alias_id"],
            row["author_alias_name"],
            row["author_alias_code"],
            row["grant_id"],
        ) == ("steward", proof["alias_id"], "steward", proof["code"], None)
        assert scene.refused_uses() == []

    play(scene.host, scenario)


# 4. The compare-and-swap ---------------------------------------------------------------------------------------


def test_the_compare_and_swap_accepts_only_the_revision_the_document_is_at(build_scene):
    """R11. A missing document is at 0. Creating takes 0; 3 on a missing document, 0 on an existing one, one above the
    current and one below it are all stale, with the current number, and write nothing, not even a document.
    Revisions are exactly 1..n, each based on the one before."""
    # Mutant 1: the compare as `expected < current` (a higher number accepted) or `expected > current` (a lower one).
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)

        def stale_for(result, current, expected):
            assert result == {
                "error": "stale_revision",
                "detail": f5(NAME, BOARDS, current, expected),
                "current_revision": current,
            }, result

        stale_for(await core_edit(client, scene, BOARDS, NAME, "text", 3), 0, 3)
        assert scene.docs() == [] and scene.revisions() == [], "a stale edit creates no document"
        first = succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(1), 0))
        assert (first["revision"], first["base_revision"]) == (1, 0)
        stale_for(await core_edit(client, scene, BOARDS, NAME, "text", 0), 1, 0)
        succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(2), 1))
        succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(3), 2))
        tables = scene.tables()
        stale_for(await core_edit(client, scene, BOARDS, NAME, "text", 4), 3, 4)
        stale_for(await core_edit(client, scene, BOARDS, NAME, "text", 5), 3, 5)
        stale_for(await core_edit(client, scene, BOARDS, NAME, "text", 2), 3, 2)
        stale_for(await core_edit(client, scene, BOARDS, NAME, "text", 1), 3, 1)
        assert scene.tables() == tables
        rows = scene.revisions(BOARDS, NAME)
        assert [row["revision"] for row in rows] == [1, 2, 3]
        assert [row["base_revision"] for row in rows] == [0, 1, 2]

    play(scene.host, scenario)


def test_the_module_refuses_an_expected_revision_that_is_missing_boolean_negative_or_text(build_scene):
    """R10. The schema makes `expected_revision` required, but a caller that bypasses it (the tool body called
    directly) gets the same refusal, and nothing is written. A document is already there, so a check that treated a
    missing number as "no check" would write revision 3."""
    # Mutant 2: the compare skipped when expected_revision is None, in the module function.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        await write_revisions(client, scene, BOARDS, NAME, 2)
        tables, counts = scene.tables(), scene.counts()
        for bad in (None, True, False, -1, "4", 4.0):
            with pytest.raises(BatonError) as raised:
                direct(scene, "edit_reference", "planner", topic=BOARDS, name=NAME, body="text", expected_revision=bad)
            assert (raised.value.code, str(raised.value)) == ("invalid_request", F6), repr(bad)
            assert scene.tables() == tables and scene.counts() == counts, repr(bad)

    play(scene.host, scenario)


def test_a_missing_expected_revision_is_refused_by_the_schema(build_scene):
    """The same argument over MCP: left out, it is not defaulted to anything."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        result = await Core(scene.host).call(client, "edit_reference", topic=BOARDS, name=NAME, body="text")
        text = str(result.get("unparsed", result))
        assert "validation error" in text and "expected_revision" in text, result
        assert scene.docs() == []

    play(scene.host, scenario)


# 5. Documents are independent --------------------------------------------------------------------------------


def test_the_same_name_in_two_topics_and_two_names_in_one_topic_keep_separate_revisions(build_scene):
    """A document is a topic and a name. `job-board` in `boards` and in `leads` are two documents, and so are two
    names in one topic: each counts its revisions from 1 and reads its own text."""
    # Mutant 9: a document looked up by name alone.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=(*refs.REFERENCE_TOPICS, LEADS))
        await write_revisions(client, scene, BOARDS, NAME, 3)
        in_leads = succeeded(await core_edit(client, scene, LEADS, NAME, "the leads copy", 0))
        other_name = succeeded(await core_edit(client, scene, BOARDS, "lead-list", "another document", 0))
        assert (in_leads["revision"], other_name["revision"]) == (1, 1)
        assert len(scene.docs()) == 3
        assert [row["revision"] for row in scene.revisions(BOARDS, NAME)] == [1, 2, 3]
        for path, name, text, current in (
            (BOARDS, NAME, body_of(3), 3),
            (LEADS, NAME, "the leads copy", 1),
            (BOARDS, "lead-list", "another document", 1),
        ):
            read = succeeded(await read_reference(client, Core(scene.host, "worker"), path, name))
            assert (read["body"], read["revision"], read["current_revision"]) == (text, current, current)
        assert succeeded(await core_edit(client, scene, LEADS, NAME, "second", 1))["revision"] == 2
        assert [row["revision"] for row in scene.revisions(BOARDS, NAME)] == [1, 2, 3]

    play(scene.host, scenario)


# 6. Undo and no-ops -----------------------------------------------------------------------------------------


def test_an_undo_is_a_new_revision_with_the_old_text_and_changes_no_earlier_row(build_scene, monkeypatch):
    """R13. Revisions 1-5, then revision 3's text written as revision 6 on expected_revision 5. Rows 1-5 are as they
    were, revision 3 still reads as it did, the current text is the restored one, and the new revision is based on 5,
    not 3. History has six entries. No statement updates, deletes or replaces a row of either table, and nothing
    marks the new revision as a restore."""
    # Mutant 11: undo as an UPDATE of the old row, or INSERT OR REPLACE. Q6: no marker and no restore tool.
    scene = build_scene()
    staging = Staging(monkeypatch)

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        await write_revisions(client, scene, BOARDS, NAME, 5)
        before = scene.tables()
        restored = succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(3), 5))
        assert (restored["revision"], restored["base_revision"]) == (6, 5)
        after = scene.tables()
        assert after["reference_revisions"][:5] == before["reference_revisions"]
        assert after["reference_docs"] == before["reference_docs"]
        assert len(after["reference_revisions"]) == 6
        reader = Core(scene.host, "worker")
        old = succeeded(await read_reference(client, reader, BOARDS, NAME, 3))
        assert (old["body"], old["revision"], old["current_revision"]) == (body_of(3), 3, 6)
        current = succeeded(await read_reference(client, reader, BOARDS, NAME))
        assert (current["body"], current["revision"], current["base_revision"]) == (body_of(3), 6, 5)
        listed = succeeded(await reference_history(client, reader, BOARDS, NAME))
        assert [(entry["revision"], entry["base_revision"]) for entry in listed["revisions"]] == [
            (1, 0),
            (2, 1),
            (3, 2),
            (4, 3),
            (5, 4),
            (6, 5),
        ]
        assert set(current) == {
            "topic",
            "topic_id",
            "name",
            "revision",
            "current_revision",
            "body",
            "size",
            "base_revision",
            "at",
            "author",
            "grant_id",
        }, "no marker links the restore to 3"
        assert staging.writes_to_reference_tables_other_than_inserts() == []

    play(scene.host, scenario)


def test_an_edit_that_changes_nothing_still_writes_a_revision(build_scene):
    """Q5. The text equal to the current one is a normal revision, with the next number and the same response."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(1), 0))
        same = succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(1), 1))
        assert (same["revision"], same["base_revision"], same["created"]) == (2, 1, True)
        rows = scene.revisions(BOARDS, NAME)
        assert [row["body"] for row in rows] == [body_of(1), body_of(1)]

    play(scene.host, scenario)


# 7. The guard, for a guest ----------------------------------------------------------------------------------


def test_a_guest_without_a_key_is_refused_by_the_guard_in_todays_words(build_scene):
    """R8, F2. A guest with no binding, with a binding and no key, and with a binding and a null key is refused as every
    guest is for a tool it may not call, and nothing is written, not even a counter row."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        arguments = edit_arguments(BOARDS, NAME, "text", 0)
        for extra in ({}, {"token": held.token}, {"token": held.token, "grant_token": None}):
            assert await tool(client, scene.host, "edit_reference", {**arguments, **extra}) == {
                "error": "forbidden",
                "detail": F2,
            }
        assert scene.docs() == [] and scene.revisions() == [] and scene.posts_counted() == 0
        assert scene.refused_uses() == []

    play(scene.host, scenario)


def test_a_key_without_a_binding_token_is_refused_in_refusal_ones_words_and_names_no_grant(build_scene):
    """R9. The key proves which grant, not who. Alone it is refused as S2's refusal 1, writes no event and no hourly
    counter row, and the key is not in the answer."""
    # Mutant 17: the hourly admission placed before S2's refusal 1.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        result = await tool(
            client, scene.host, "edit_reference", edit_arguments(BOARDS, NAME, "text", 0, grant_token=held.key)
        )
        refused(result, "forbidden", NEEDS_BINDING)
        assert held.key not in json.dumps(result)
        assert scene.posts_counted() == 0 and scene.refused_uses() == [] and scene.docs() == []

    play(scene.host, scenario)


def test_the_body_called_for_a_guest_with_no_key_grants_nothing(build_scene):
    """The guard keeps a keyless guest out, but the body must not grant on its own account either: called directly as
    a guest with an alias and no key, it is refused and writes nothing."""
    # Mutant 12: the grantee branch granting without the key.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        inputs = as_the_guard_calls(scene, held)
        inputs["call"] = {**inputs["call"], "grant_key": None}
        with pytest.raises(BatonError) as raised:
            direct(
                scene, "edit_reference", "guest", topic=BOARDS, name=NAME, body="text", expected_revision=0, **inputs
            )
        assert raised.value.code == "forbidden", str(raised.value)
        assert scene.docs() == [] and scene.revisions() == []

    play(scene.host, scenario)
