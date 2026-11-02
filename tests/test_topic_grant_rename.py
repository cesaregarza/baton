"""Renaming a topic that active topic grants cover: the acknowledgement, the `rename` events, and the history of the
subtopics a rename moves.

A grant follows its topic's id, so a rename can change what a grant covers. When it does, the core role renaming must
name exactly the affected grants, or the rename is refused. A grant is *affected* when it is active and either
(1) its topic is the renamed topic or one of the subtopics the rename moves, or (2) it inherits and the renamed
topic's parent path, before or after the rename, equals the grant topic's path, before or after. Each affected
grant then gets one `rename` event, and the history gets one row for every topic whose path changed.

Topics: `job-hunt` with `job-hunt/boards` and `job-hunt/leads`, the lookalike `job-hunting`, and `resume` and
`reviews`. Mutant numbers in comments refer to the spec's list of one-line mutants the tests must kill.
"""

from __future__ import annotations

import json
import secrets

import topic_grant_tools_support as support
from topic_grant_tools_support import ALL, only, play, refused, stage, succeeded, thread, tool

build_scene = support.build_scene  # the fixture, under the name the tests ask for

TOPICS = (*support.BOARD, "reviews", "job-hunting/x")
TAIL = "; repeat the call with acknowledge_topic_grants set to exactly those ids"


async def rename(client, scene, path, new_path, acknowledge=None):
    arguments = {"path": path, "new_path": new_path}
    if acknowledge is not None:
        arguments["acknowledge_topic_grants"] = acknowledge
    return await tool(client, scene.host, "rename_topic", arguments, role="planner")


def must_name(result, path, affected, unaffected=()):
    """The refusal tells the caller which grants are affected: all of them, and none of the others."""
    assert result.get("error") == "invalid_request", result
    detail = result["detail"]
    assert detail.startswith(f"renaming {path} changes what topic grants "), result
    assert detail.endswith(f" cover{TAIL}"), result
    for grant_id in affected:
        assert grant_id in detail, (grant_id, detail)
    for grant_id in unaffected:
        assert grant_id not in detail, (grant_id, detail)


def rename_events(scene):
    return [(row["grant_id"], row["actor"], json.loads(row["detail"])) for row in scene.events("rename")]


# 1. The worked cases ---------------------------------------------------------------------------------------------

def test_renaming_a_subtopic_out_of_the_root_needs_the_acknowledgement_and_ends_the_grantees_reach(build_scene):
    """The case of a topic renamed out of the granted root: the grantee loses it from the next call. The rename is
    refused until the core role names the grant, then goes through and logs one event for it."""
    # Mutant 5: a rename skipping the acknowledgement.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host, TOPICS)
        held = await scene.grantee(client, "lane-a", "job-hunt", ALL, inherit=True)
        leads = scene.topic_id("job-hunt/leads")
        inside = await thread(client, scene.host, "job-hunt/leads")

        must_name(await rename(client, scene, "job-hunt/leads", "leads"), "job-hunt/leads", [held.grant_id])
        assert "job-hunt/leads" in scene.topic_paths() and "leads" not in scene.topic_paths()
        assert scene.events("rename") == []

        done = succeeded(await rename(client, scene, "job-hunt/leads", "leads", [held.grant_id]))
        assert (done["path"], done["renamed_from"]) == ("leads", "job-hunt/leads")
        assert rename_events(scene) == [(held.grant_id, "planner",
                                         {"topic_id": leads, "from": "job-hunt/leads", "to": "leads"})]
        assert scene.thread_topic(inside) == "leads"

        # Refusal 5 now: the topic left the root, so the grant no longer reaches it, in either direction.
        out = await held.call(client, "set_topic", conversation_id=inside, topic="job-hunt/boards")
        assert out.get("error") == "forbidden" and out["detail"].startswith(
            f"topic grant {held.grant_id} does not allow thread.organise on "), out
        assert "leads" in out["detail"]
        elsewhere = await thread(client, scene.host, "job-hunt/boards")
        into = await held.call(client, "set_topic", conversation_id=elsewhere, topic="leads")
        assert into.get("error") == "forbidden", into
        sub = await held.call(client, "create_topic", path="leads/x")
        assert sub.get("error") == "forbidden", sub
        # What is still inside the root is still the grantee's.
        succeeded(await held.call(client, "set_topic", conversation_id=elsewhere, topic="job-hunt"))

    play(scene.host, scenario)


def test_renaming_the_granted_root_keeps_the_grant_on_the_same_topic_and_not_on_a_new_one_at_its_old_path(build_scene):
    """Case 14. The grant is on the topic's id: after `job-hunt` becomes `job-hunt-old` the grantee acts under the
    new path, and a different topic later opened at `job-hunt` is a stranger."""
    # Mutant 5. Also: matching the grant by path instead of by topic id.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host, TOPICS)
        held = await scene.grantee(client, "lane-a", "job-hunt", ALL, inherit=True)
        old_id = scene.topic_id("job-hunt")
        succeeded(await rename(client, scene, "job-hunt", "job-hunt-old", [held.grant_id]))
        assert scene.topic_id("job-hunt-old") == old_id
        succeeded(await held.call(client, "create_topic", path="job-hunt-old/referrals"))
        assert only([row for row in scene.changes("create_topic") if row["grant_id"] == held.grant_id],
                    "grantee's history row")["topic_id"] == scene.topic_id("job-hunt-old/referrals")

        succeeded(await tool(client, scene.host, "create_topic", {"path": "job-hunt", "members": ["planner", "worker"]},
                             role="planner"))
        assert scene.topic_id("job-hunt") != old_id
        stranger = await held.call(client, "create_topic", path="job-hunt/x")
        assert stranger.get("error") == "forbidden", stranger
        assert stranger["detail"].startswith(f"topic grant {held.grant_id} does not allow topic.create_sub on ")
        assert "job-hunt/x" not in scene.topic_paths()

    play(scene.host, scenario)


# 2. Exactly the affected set -------------------------------------------------------------------------------------

def test_a_rename_must_name_exactly_the_affected_grants(build_scene):
    """Renaming `job-hunt` changes what two active grants cover: the one on it, and one on its subtopic
    `job-hunt/boards`. A revoked grant on it, and an active one on `resume`, are not affected. Naming none, a
    subset, a superset, an unknown id or a grant that is not affected is refused and leaves everything as it was;
    naming exactly the two, in any order with repeats, succeeds."""
    # Mutant 5: a rename skipping the acknowledgement, or accepting a subset or a superset of the affected ids.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host, TOPICS)
        root = await scene.grantee(client, "lane-a", "job-hunt", ALL, inherit=True)
        child = await scene.grantee(client, "lane-b", "job-hunt/boards", ALL)
        elsewhere = scene.grant("lane-b", "resume")
        ended = scene.grant("lane-b", "job-hunt")
        scene.revoke(ended["id"])
        affected = [root.grant_id, child.grant_id]
        before = scene.topic_paths()

        wrong = [None, [], [root.grant_id], [child.grant_id], [*affected, elsewhere["id"]], [*affected, ended["id"]],
                 [*affected, "tgr_" + secrets.token_hex(8)], [elsewhere["id"]]]
        for named in wrong:
            result = await rename(client, scene, "job-hunt", "job-hunt-old", named)
            must_name(result, "job-hunt", affected, unaffected=[elsewhere["id"], ended["id"]])
            assert scene.topic_paths() == before, named
            assert scene.events("rename") == [] and scene.changes("rename_topic") == [], named

        shuffled = [child.grant_id, root.grant_id, child.grant_id]
        succeeded(await rename(client, scene, "job-hunt", "job-hunt-old", shuffled))
        assert "job-hunt-old/boards" in scene.topic_paths()
        events = rename_events(scene)
        assert sorted(grant_id for grant_id, _, _ in events) == sorted(affected), events
        for _, actor, detail in events:
            assert actor == "planner" and set(detail) == {"topic_id", "from", "to"}, detail
        by_grant = {grant_id: detail for grant_id, _, detail in events}
        assert by_grant[root.grant_id] == {"topic_id": scene.topic_id("job-hunt-old"), "from": "job-hunt",
                                           "to": "job-hunt-old"}

    play(scene.host, scenario)


def test_a_grant_on_a_subtopic_alone_makes_renaming_its_root_need_the_acknowledgement(build_scene):
    """The subtopics a rename moves count: a grant on `job-hunt/boards` has its path changed by renaming `job-hunt`."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host, TOPICS)
        child = await scene.grantee(client, "lane-a", "job-hunt/boards", ALL)
        must_name(await rename(client, scene, "job-hunt", "job-hunt-old"), "job-hunt", [child.grant_id])
        succeeded(await rename(client, scene, "job-hunt", "job-hunt-old", [child.grant_id]))
        [(grant_id, _, detail)] = rename_events(scene)
        assert grant_id == child.grant_id and set(detail) == {"topic_id", "from", "to"}

    play(scene.host, scenario)


def test_a_rename_that_moves_a_topic_into_an_inheriting_grants_root_needs_the_acknowledgement(build_scene):
    """Coverage gained: `resume` moved under `job-hunt` becomes covered by an inheriting grant on `job-hunt`."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host, TOPICS)
        held = await scene.grantee(client, "lane-a", "job-hunt", ALL, inherit=True)
        must_name(await rename(client, scene, "resume", "job-hunt/resume"), "resume", [held.grant_id])
        succeeded(await rename(client, scene, "resume", "job-hunt/resume", [held.grant_id]))
        assert only(scene.events("rename"), "rename event")["grant_id"] == held.grant_id
        succeeded(await held.call(client, "create_topic", path="job-hunt/after-the-move"))

    play(scene.host, scenario)


def test_a_rename_under_the_same_inheriting_root_is_affected_by_the_literal_rule(build_scene):
    """The rule is literal: an inheriting grant is affected when the renamed topic's parent path, before or after,
    equals the grant topic's path. A sibling rename inside the root meets it on both sides, so it needs the
    acknowledgement even though coverage of the topic is the same before and after."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host, TOPICS)
        held = await scene.grantee(client, "lane-a", "job-hunt", ALL, inherit=True)
        must_name(await rename(client, scene, "job-hunt/leads", "job-hunt/prospects"), "job-hunt/leads",
                  [held.grant_id])
        succeeded(await rename(client, scene, "job-hunt/leads", "job-hunt/prospects", [held.grant_id]))

    play(scene.host, scenario)


# 3. When no grant is affected, a rename is exactly what it was ----------------------------------------------------

def test_a_rename_no_grant_is_affected_by_needs_no_acknowledgement_and_naming_ids_is_refused(build_scene):
    """With grants around that this rename does not touch, it behaves as it always has: the same answer, no event.
    Naming a grant is then refused with text saying none are affected, and naming no ids is the same as naming
    none."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host, TOPICS)
        held = await scene.grantee(client, "lane-a", "job-hunt", ALL, inherit=True)
        named = await rename(client, scene, "reviews", "reviews-archive", [held.grant_id])
        assert named.get("error") == "invalid_request", named
        assert "none" in named["detail"].lower() and "affected" in named["detail"], named
        assert "reviews" in scene.topic_paths()

        plain = succeeded(await rename(client, scene, "reviews", "reviews-archive"))
        assert (plain["path"], plain["renamed_from"], plain["subtopics"]) == ("reviews-archive", "reviews", 0)
        assert set(plain) == {"path", "title", "description", "members", "created_by", "created_at",
                              "renamed_from", "subtopics"}
        empty = succeeded(await rename(client, scene, "resume", "resume-archive", []))
        assert empty["path"] == "resume-archive"
        assert scene.events("rename") == []

    play(scene.host, scenario)


def test_grants_that_a_rename_does_not_touch_do_not_make_it_need_an_acknowledgement(build_scene):
    """GUARD. Not affected: a lookalike root (`job-hunting` is not `job-hunt`), and a grant that does not inherit
    when only a subtopic is renamed or moved in. Such a rename is what it always was."""
    # Mutant 5 (the other way): over-asking. The lookalike also catches a prefix test in the affected rule.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host, TOPICS)
        await scene.grantee(client, "lane-a", "job-hunting", ALL, inherit=True)
        await scene.grantee(client, "lane-b", "job-hunt", ALL, inherit=False)
        succeeded(await rename(client, scene, "job-hunt/leads", "leads"))
        succeeded(await rename(client, scene, "resume", "job-hunt/resume"))
        assert scene.events("rename") == []

    play(scene.host, scenario)


def test_a_subtopic_of_a_lookalike_root_is_not_under_an_inheriting_grant_on_the_shorter_name(build_scene):
    """GUARD. `job-hunting/x` is not inside `job-hunt`: renaming it is no business of an inheriting grant on
    `job-hunt`, whose name is a text prefix of its parent's."""
    # Mutant 5 (over-asking): the affected rule comparing paths as text prefixes.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host, TOPICS)
        await scene.grantee(client, "lane-a", "job-hunt", ALL, inherit=True)
        succeeded(await rename(client, scene, "job-hunting/x", "x"))
        assert scene.events("rename") == []

    play(scene.host, scenario)


def test_a_revoked_or_expired_grant_is_not_affected_by_a_rename(build_scene):
    """GUARD. Only active grants are affected: a grant that has ended no longer covers anything to protect."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host, TOPICS)
        await tool(client, scene.host, "claim_alias", {"name": "lane-a", "passphrase": support.PASSPHRASE})
        revoked = scene.grant("lane-a", "job-hunt", ALL, True)
        scene.revoke(revoked["id"])
        short = scene.grant("lane-a", "job-hunt", ALL, True, days=1)
        scene.clock.advance(days=1)
        assert scene.state_of(short["id"]) == "expired"
        succeeded(await rename(client, scene, "job-hunt/leads", "leads"))
        assert scene.events("rename") == []

    play(scene.host, scenario)


# 4. History and atomicity ----------------------------------------------------------------------------------------

def test_a_rename_writes_one_history_row_for_every_topic_whose_path_changed(build_scene):
    """History is by topic id, so each subtopic a rename moves gets its own row, with the same actor as the row for
    the topic named and, for a core role, no grant."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host, TOPICS)
        succeeded(await rename(client, scene, "job-hunt", "job-hunt-old"))
        rows = scene.changes("rename_topic")
        by_topic = {row["topic_id"]: row for row in rows}
        assert len(rows) == len(by_topic) == 3, [tuple(row) for row in rows]
        for old, new in (("job-hunt", "job-hunt-old"), ("job-hunt/boards", "job-hunt-old/boards"),
                         ("job-hunt/leads", "job-hunt-old/leads")):
            row = by_topic[scene.topic_id(new)]
            assert (json.loads(row["before"]), json.loads(row["after"])) == ({"path": old}, {"path": new}), old
            assert (row["actor"], row["grant_id"]) == ("planner", None)

    play(scene.host, scenario)


def test_a_refused_rename_writes_no_event_even_with_the_right_acknowledgement(build_scene):
    """GUARD. The events are written with the rename, in its transaction: a rename refused for another reason, here a
    target that is already a topic, leaves no `rename` event and no history row behind."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host, TOPICS)
        held = await scene.grantee(client, "lane-a", "job-hunt", ALL, inherit=True)
        before = [tuple(row) for row in scene.changes()]
        result = await rename(client, scene, "job-hunt", "resume", [held.grant_id])
        refused(result, "invalid_request", "new_path 'resume' is already a topic")
        assert scene.events("rename") == [] and [tuple(row) for row in scene.changes()] == before
        assert "job-hunt" in scene.topic_paths()

    play(scene.host, scenario)
