"""References and topics: a document hangs on its topic's id, so a rename carries it, a path freed by a rename starts
again with nothing, and a grantee's reach follows the topic's position.

What this pins:

* A rename keeps every document and revision of the topic and of its moved subtopics, with the same numbers, texts,
  times and ids, reachable at the new path; the old path is an unknown topic; and no row of either table changes.
* A new topic opened at the freed path has a new id, so it starts with no references, and the grantee's grant, which
  follows the old topic's id, does not reach it.
* A topic renamed out of a granted root loses the grantee's edits but keeps its documents, readable by anyone and
  editable by a core role; a topic renamed into a granted root gains them.
* A grantee's subtopic is editable only if its grant inherits (Q7); a thread moving between topics leaves the
  documents where they are.
* `reference_docs.topic_id` is the topic's id, never its path.

Mutant numbers in comments refer to the spec's list of one-line mutants the tests must kill. Q numbers refer to its open
questions: the test pins the question's default, so a ruling changes only the tests that carry that number.
"""

from __future__ import annotations

import reference_support as refs
import topic_grant_tools_support as support
from reference_support import (
    BOARDS,
    LEADS,
    NAME,
    ROOT,
    Bound,
    Core,
    Keyed,
    author_of,
    board,
    body_of,
    core_edit,
    f9,
    f10,
    grantee_edit,
    not_covered,
    open_topic,
    read_reference,
    reference_history,
    rename,
    write_revisions,
)
from topic_grant_tools_support import play, refused, succeeded, thread

build_scene = refs.build_scene  # the fixture, under the name the tests ask for


async def everything_about(client, scene, path):
    """What a reader is told of the document at `path`: the list, the current text, each revision and the history."""
    who = Core(scene.host, "worker")
    answer = {"list": succeeded(await read_reference(client, who, path))}
    answer["current"] = succeeded(await read_reference(client, who, path, NAME))
    for revision in (1, 2, 3):
        answer[revision] = succeeded(await read_reference(client, who, path, NAME, revision))
    answer["history"] = succeeded(await reference_history(client, who, path, NAME))
    return answer


def dropping_topic(value):
    """An answer without the `topic` path in it, anywhere: the one thing a rename is meant to change."""
    if isinstance(value, dict):
        return {key: dropping_topic(item) for key, item in value.items() if key != "topic"}
    if isinstance(value, list):
        return [dropping_topic(item) for item in value]
    return value


def test_a_rename_carries_the_documents_with_their_ids_texts_and_times_and_changes_no_row(build_scene):
    """R14, R16. Three revisions at `job-hunt/boards`, then a core role renames `job-hunt` to `job-hunt-old`, naming
    the grant it is told it changes the reach of. At the new path the same document reads the same, revision for
    revision, with the same `topic_id`; the old path is an unknown topic; both tables are byte for byte as they were."""
    # Mutant 10: reference_docs.topic_id filled with the path text.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        await write_revisions(client, scene, BOARDS, NAME, 2)
        succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(3), 2))
        topic_id, tables = scene.topic_id(BOARDS), scene.tables()
        before = await everything_about(client, scene, BOARDS)
        unacknowledged = await rename(client, scene, ROOT, "job-hunt-old")
        assert unacknowledged.get("error") == "invalid_request", unacknowledged
        assert held.grant_id in unacknowledged["detail"], unacknowledged
        succeeded(await rename(client, scene, ROOT, "job-hunt-old", [held.grant_id]))
        renamed = "job-hunt-old/boards"
        assert scene.topic_id(renamed) == topic_id
        after = await everything_about(client, scene, renamed)
        assert dropping_topic(after) == dropping_topic(before)
        assert after["current"]["topic"] == renamed and after["history"]["topic"] == renamed
        assert after["current"]["topic_id"] == topic_id
        assert scene.tables() == tables
        who = Core(scene.host, "worker")
        assert await read_reference(client, who, BOARDS, NAME) == {"error": "unknown_topic", "detail": f9(BOARDS)}
        assert await read_reference(client, who, BOARDS) == {"error": "unknown_topic", "detail": f9(BOARDS)}

    play(scene.host, scenario)


def test_a_new_topic_at_the_freed_path_has_no_references_and_is_not_under_the_old_grant(build_scene):
    """R14, case 14. After the rename a core role opens a new `job-hunt` and `job-hunt/boards`. They have new ids and
    no references: the list is empty and the document is unknown there. The grantee's edit there is refused as not
    covered, while its edit at `job-hunt-old/boards` still works under the same grant."""
    # Mutant 10: documents looked up by path text.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        await write_revisions(client, scene, BOARDS, NAME, 3)
        old_id = scene.topic_id(BOARDS)
        succeeded(await rename(client, scene, ROOT, "job-hunt-old", [held.grant_id]))
        await open_topic(client, scene, ROOT)
        await open_topic(client, scene, BOARDS)
        assert scene.topic_id(BOARDS) != old_id
        who = Core(scene.host, "worker")
        assert succeeded(await read_reference(client, who, BOARDS))["references"] == []
        assert await read_reference(client, who, BOARDS, NAME) == {
            "error": "unknown_reference",
            "detail": f10(NAME, BOARDS),
        }
        tables = scene.tables()
        refused(
            await grantee_edit(client, held, BOARDS, NAME, "into the new topic", 0),
            "forbidden",
            not_covered(held.grant_id, BOARDS),
        )
        assert scene.tables() == tables
        under_the_grant = succeeded(await grantee_edit(client, held, "job-hunt-old/boards", NAME, body_of(4), 3))
        assert (under_the_grant["revision"], under_the_grant["topic_id"]) == (4, old_id)
        fresh = succeeded(await core_edit(client, scene, BOARDS, NAME, "the new topic's own", 0))
        assert (fresh["revision"], fresh["topic_id"]) == (1, scene.topic_id(BOARDS))
        assert [row["revision"] for row in scene.revisions("job-hunt-old/boards", NAME)] == [1, 2, 3, 4]

    play(scene.host, scenario)


def test_renaming_a_leaf_alone_with_no_grant_carries_its_documents(build_scene):
    """R14. Nothing is granted, so the rename needs no acknowledgement; the document is at the new path with its
    revisions, and the sibling's document is untouched."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=(*refs.REFERENCE_TOPICS, LEADS))
        await write_revisions(client, scene, LEADS, NAME, 3)
        await write_revisions(client, scene, BOARDS, NAME, 1)
        before = await everything_about(client, scene, LEADS)
        succeeded(await rename(client, scene, LEADS, "job-hunt/prospects"))
        after = await everything_about(client, scene, "job-hunt/prospects")
        assert dropping_topic(after) == dropping_topic(before)
        assert after["current"]["topic"] == "job-hunt/prospects"
        assert await read_reference(client, Core(scene.host, "worker"), LEADS) == {
            "error": "unknown_topic",
            "detail": f9(LEADS),
        }
        assert [row["revision"] for row in scene.revisions(BOARDS, NAME)] == [1]

    play(scene.host, scenario)


def test_a_topic_renamed_out_of_the_granted_root_keeps_its_document_but_not_the_grantees_edits(build_scene):
    """R14. G1 is active on `job-hunt` and inherits. A core role renames `job-hunt/leads` to `leads`, acknowledging
    G1. The grantee's edit at `leads` is refused as not covered; the document is still readable there, a core role
    can still edit it, and the grantee's edits at `job-hunt/boards` still work."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        await support.stage(client, scene.host, paths=(LEADS,))
        succeeded(await grantee_edit(client, held, LEADS, NAME, body_of(1), 0))
        succeeded(await rename(client, scene, LEADS, "leads", [held.grant_id]))
        tables = scene.tables()
        refused(
            await grantee_edit(client, held, "leads", NAME, body_of(2), 1),
            "forbidden",
            not_covered(held.grant_id, "leads"),
        )
        assert scene.tables() == tables
        read = succeeded(await read_reference(client, Bound.of(held), "leads", NAME))
        assert (read["revision"], read["body"], read["author"]) == (1, body_of(1), author_of(held))
        by_core = succeeded(await core_edit(client, scene, "leads", NAME, body_of(2), 1))
        assert (by_core["revision"], by_core["author"]) == (2, {"role": "planner"})
        still = succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0))
        assert still["grant_id"] == held.grant_id

    play(scene.host, scenario)


def test_a_topic_renamed_into_the_granted_root_gains_the_grantees_edits(build_scene):
    """R14. `resume` holds a document and is outside G1. Renamed to `job-hunt/resume` with G1 acknowledged, it is under
    the inheriting root: the grantee's edit is refused before and allowed after, on the same document."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        await support.stage(client, scene.host, paths=("resume",))
        succeeded(await core_edit(client, scene, "resume", NAME, body_of(1), 0))
        refused(
            await grantee_edit(client, held, "resume", NAME, body_of(2), 1),
            "forbidden",
            not_covered(held.grant_id, "resume"),
        )
        succeeded(await rename(client, scene, "resume", "job-hunt/resume", [held.grant_id]))
        made = succeeded(await grantee_edit(client, held, "job-hunt/resume", NAME, body_of(2), 1))
        assert (made["revision"], made["grant_id"]) == (2, held.grant_id)

    play(scene.host, scenario)


def test_a_subtopic_a_grantee_creates_is_editable_only_if_the_grant_inherits(build_scene):
    """Q7. The grantee makes `job-hunt/referrals` with `topic.create_sub` and edits a document there with expected
    revision 0 under an inheriting grant. A grantee whose grant does not inherit can make `resume/drafts` but not
    edit a reference in it: S1's scope rule is not widened."""
    # Q7. Mutant 14: the coverage rule ignoring inherit.
    scene = build_scene()
    both = ("reference.edit", "topic.create_sub")

    async def scenario(client):
        await support.stage(client, scene.host, paths=(ROOT, "resume"))
        inheriting = await scene.grantee(client, "job-search", ROOT, both, inherit=True)
        plain = await scene.grantee(client, "lead-scout", "resume", both, inherit=False)
        succeeded(await Keyed(inheriting).call(client, "create_topic", path="job-hunt/referrals"))
        succeeded(await Keyed(plain).call(client, "create_topic", path="resume/drafts"))
        made = succeeded(await grantee_edit(client, inheriting, "job-hunt/referrals", NAME, body_of(1), 0))
        assert (made["revision"], made["topic_id"]) == (1, scene.topic_id("job-hunt/referrals"))
        refused(
            await grantee_edit(client, plain, "resume/drafts", NAME, body_of(1), 0),
            "forbidden",
            not_covered(plain.grant_id, "resume/drafts"),
        )
        assert [row["topic_id"] for row in scene.docs()] == [scene.topic_id("job-hunt/referrals")]

    play(scene.host, scenario)


def test_moving_a_thread_between_topics_leaves_the_documents_where_they_are(build_scene):
    """A document belongs to its topic, not to the threads filed in it. A grantee moving a thread out of
    `job-hunt/leads` and into `job-hunt/boards` changes no list and no document."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        await support.stage(client, scene.host, paths=(LEADS,))
        await write_revisions(client, scene, LEADS, NAME, 2)
        moving = await thread(client, scene.host, LEADS)
        who = Core(scene.host, "worker")
        listed = {path: succeeded(await read_reference(client, who, path)) for path in (LEADS, BOARDS)}
        tables = scene.tables()
        succeeded(await Keyed(held).call(client, "set_topic", conversation_id=moving, topic=BOARDS))
        assert scene.thread_topic(moving) == BOARDS
        for path, before in listed.items():
            assert succeeded(await read_reference(client, who, path)) == before
        assert scene.tables() == tables
        assert [entry["name"] for entry in listed[LEADS]["references"]] == [NAME] and listed[BOARDS]["references"] == []

    play(scene.host, scenario)


def test_a_document_hangs_on_the_topics_id_and_no_path_is_stored(build_scene):
    """R14. `reference_docs.topic_id` is the id `topics` gives the path at the time of the write, in every topic a
    document is made in, and nothing in either table holds a topic's path."""
    # Mutant 10: reference_docs.topic_id filled with the path text.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=(*refs.REFERENCE_TOPICS, LEADS, "resume"))
        for path in (ROOT, BOARDS, LEADS, "resume"):
            succeeded(await core_edit(client, scene, path, NAME, "text", 0))
        docs = scene.docs()
        assert [row["topic_id"] for row in docs] == [scene.topic_id(path) for path in (ROOT, BOARDS, LEADS, "resume")]
        assert all(row["topic_id"].startswith("topic_") for row in docs)
        paths = set(support.BOARD)
        cells = {str(value) for rows in scene.tables().values() for row in rows for value in row}
        assert not paths & cells, "a topic's path is stored in a reference table"
        with scene.db() as conn:
            columns = {
                name
                for table in refs.REFERENCE_TABLES
                for name in (row["name"] for row in conn.execute(f"PRAGMA table_info({table})"))
            }
        assert not {name for name in columns if "path" in name}

    play(scene.host, scenario)
