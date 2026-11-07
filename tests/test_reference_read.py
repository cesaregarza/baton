"""Reading references: who may read, the three modes, what is refused, and that a read has no side effects.

What this pins:

* Every identified reader may list a topic's references, read the current text, read an old revision and read the
  history: a guest bound to an alias with no grant, a grantee whatever the state of its grant, a core role whatever
  its flags. A guest with no binding, or with one that is malformed, unknown or expired, is refused as an unbound
  guest post is.
* A read never consults `topic_grants`, writes no row anywhere, and works for a reader with no thread in the topic.
* The list is ordered by name and describes each document's current revision; one document reads as its current
  revision or as any old one, with exactly the stored text; the history pages forward, oldest first, with no bodies.
* An unknown topic, an unknown reference and an unknown revision are three different refusals, and an unknown reference
  never reads as an empty body.
* Text comes back byte for byte: nothing is stripped or normalised.
* The module checks `history`'s paging arguments itself, so a limit or a cursor the schema would refuse is refused when
  the tool body is called past the schema.
* A read and a history answer from one view of the database: a revision committed while the call is running is not mixed
  into its answer.

Mutant numbers in comments refer to the spec's list of one-line mutants the tests must kill. Q numbers refer to its open
questions: the test pins the question's default, so a ruling changes only the tests that carry that number.
"""

from __future__ import annotations

import secrets
import time
import unicodedata
from types import SimpleNamespace

import pytest
import reference_support as refs
import topic_grant_tools_support as support
from reference_support import (
    BINDING_GONE,
    BOARDS,
    F12,
    HISTORY_ENTRY_KEYS,
    HISTORY_KEYS,
    LEADS,
    LIST_KEYS,
    LISTED_KEYS,
    NAME,
    READ_KEYS,
    ROOT,
    Anonymous,
    Bound,
    Core,
    Staging,
    author_of,
    body_of,
    bound_guest,
    bring_to_state,
    core_edit,
    direct,
    f9,
    f10,
    f11,
    grantee_edit,
    other_writer_adds_revision,
    read_reference,
    reference_history,
    refused_by_schema,
    write_revisions,
)
from topic_grant_tools_support import play, succeeded, tool

from baton import aliases
from baton.errors import BatonError

build_scene = refs.build_scene  # the fixture, under the name the tests ask for

READERS = [
    "bound alias",
    "grantee for thread.organise only",
    "revoked grantee",
    "expired grantee",
    "pending grantee",
    "invalid grantee",
    "core role with no flags",
    "core role with both flags",
]


async def reader(kind, client, scene):
    """Someone of the named kind, as a caller that reads. A grantee reads with its binding token and no key."""
    if kind == "core role with no flags":
        return Core(scene.host, "worker")
    if kind == "core role with both flags":
        return Core(scene.host)
    if kind == "bound alias":
        return await bound_guest(client, scene)
    capabilities = ("thread.organise",) if "thread.organise" in kind else ("reference.edit",)
    held = await scene.grantee(client, "lead-scout", ROOT, capabilities, inherit=True)
    if kind.split()[0] in ("revoked", "expired", "pending", "invalid"):
        bring_to_state(scene, held, kind.split()[0])
    return Bound.of(held)


class ReadStaging(Staging):
    """A `Staging` for a read, which takes no lock and writes nothing, so nothing marks a moment in it but its own
    statements. `before(fragment, action)` runs `action` just before the first SELECT whose text holds `fragment`, and
    keeps the statement that ran before it, so a test can say where the write landed."""

    def before(self, fragment, action):
        self.moment, self.action, self.fired, self.outcome = "select", action, False, None
        self.fragment, self.landed_after = fragment.upper(), None
        return self

    def _trace(self, sql, conn):
        previous = self.statements[-1][0] if self.statements else None
        self.statements.append((sql, conn.in_transaction))
        if (
            self.moment == "select"
            and not self.fired
            and sql.lstrip().upper().startswith("SELECT")
            and self.fragment in sql.upper()
        ):
            self.fired, self.landed_after = True, previous
            self.outcome = self.action()


# 1. Who may read ---------------------------------------------------------------------------------------------


def test_a_guest_with_no_valid_binding_is_refused_as_an_unbound_post_is_and_sees_nothing(build_scene, monkeypatch):
    """R1. No token, an empty, malformed, too long or unknown one, and one that has expired a day later: each gets the
    existing refusal from both read tools, and the answer holds no reference name and no text."""
    # Q1. Mutant 18: the guard not requiring a binding for a guest's read.
    scene = build_scene()
    real = time.time

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        await write_revisions(client, scene, BOARDS, NAME, 2)
        expiring = await bound_guest(client, scene)
        monkeypatch.setattr(aliases, "time", SimpleNamespace(time=lambda: real() + 25 * 3600))
        calls = [
            ("read_reference", {"topic": BOARDS}),
            ("read_reference", {"topic": BOARDS, "name": NAME}),
            ("read_reference", {"topic": BOARDS, "name": NAME, "revision": 1}),
            ("reference_history", {"topic": BOARDS, "name": NAME}),
        ]
        for name, arguments in calls:
            for who in (Anonymous(scene.host), expiring):
                result = await who.call(client, name, **arguments)
                assert result == {"error": "forbidden", "detail": BINDING_GONE}, (name, who, result)
            for token in ("", "not a token", "x" * 300, "\ud800", secrets.token_urlsafe(32)):
                result = await tool(client, scene.host, name, {**arguments, "token": token})
                assert result == {"error": "forbidden", "detail": BINDING_GONE}, (name, token[:12], result)
                assert NAME not in str(result) and "board list" not in str(result).lower()

    play(scene.host, scenario)


def test_reads_consult_no_grant_and_write_no_row_anywhere(build_scene, monkeypatch):
    """R1, R2. A bound guest with no grant, a grantee whose grant lacks `reference.edit` or is revoked, expired,
    pending or invalid, a core role with no flags and one with both: each lists, reads the current text, reads
    revision 2 and reads the history, none of them a participant of any thread here. Across every kind of reader and
    every mode, no statement the app runs, the guard's included, mentions `topic_grants` or writes a row, and every
    table has the row count it had. The key is never asked for."""
    # Q1: a guest needs its binding to read. Mutant 18: read_reference left out of the guest tool list.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        await write_revisions(client, scene, BOARDS, NAME, 3)
        readers = [await reader(kind, client, scene) for kind in READERS]
        before = scene.counts()
        staging = Staging(monkeypatch, everything=True)
        for kind, who in zip(READERS, readers, strict=True):
            listing = succeeded(await read_reference(client, who, BOARDS))
            assert set(listing) == LIST_KEYS and [entry["name"] for entry in listing["references"]] == [NAME], kind
            current = succeeded(await read_reference(client, who, BOARDS, NAME))
            assert set(current) == READ_KEYS, kind
            assert (current["revision"], current["current_revision"], current["body"]) == (3, 3, body_of(3)), kind
            old = succeeded(await read_reference(client, who, BOARDS, NAME, 2))
            assert (old["revision"], old["current_revision"], old["body"]) == (2, 3, body_of(2)), kind
            history = succeeded(await reference_history(client, who, BOARDS, NAME))
            assert set(history) == HISTORY_KEYS, kind
            assert [entry["revision"] for entry in history["revisions"]] == [1, 2, 3], kind
        statements = [sql.upper() for sql in staging.texts()]
        assert len(statements) > 8, "the reads ran no statements at all"
        assert not [sql for sql in statements if "TOPIC_GRANTS" in sql], "a read consulted the grants"
        assert not [sql for sql in statements if sql.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))]
        assert scene.counts() == before

    play(scene.host, scenario)


# 2. The three modes --------------------------------------------------------------------------------------------


def test_the_list_of_an_empty_topic_is_empty_and_documents_are_listed_by_name_with_their_current_revision(build_scene):
    """R3. A topic with no references lists as `references: []`. With documents it lists them ordered by name, each
    described by its current revision: that revision's number, size, time, author and grant. Documents in the
    topic's subtopics are not in the list."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=(*refs.REFERENCE_TOPICS, LEADS))
        who = Core(scene.host, "worker")
        empty = succeeded(await read_reference(client, who, BOARDS))
        assert empty == {"topic": BOARDS, "topic_id": scene.topic_id(BOARDS), "references": []}
        held = await scene.grantee(client, "job-search", ROOT, ("reference.edit",), inherit=True)
        succeeded(await core_edit(client, scene, BOARDS, "zeta-list", "zeta, first", 0))
        second = "zeta, second text caf\u00e9"  # two bytes for its last letter, so a size in characters is wrong
        zeta_2 = succeeded(await grantee_edit(client, held, BOARDS, "zeta-list", second, 1))
        alpha = succeeded(await core_edit(client, scene, BOARDS, "alpha-list", "alpha", 0))
        mid = succeeded(await core_edit(client, scene, BOARDS, "mid-1", "mid", 0))
        succeeded(await core_edit(client, scene, LEADS, "elsewhere", "in a sibling", 0))
        succeeded(await core_edit(client, scene, ROOT, "in-the-parent", "in the parent", 0))
        listing = succeeded(await read_reference(client, who, BOARDS))
        assert set(listing) == LIST_KEYS
        assert (listing["topic"], listing["topic_id"]) == (BOARDS, scene.topic_id(BOARDS))
        assert all(set(entry) == LISTED_KEYS for entry in listing["references"])
        expected = [
            {
                "name": "alpha-list",
                "current_revision": 1,
                "at": alpha["at"],
                "size": len(b"alpha"),
                "author": {"role": "planner"},
                "grant_id": None,
            },
            {
                "name": "mid-1",
                "current_revision": 1,
                "at": mid["at"],
                "size": len(b"mid"),
                "author": {"role": "planner"},
                "grant_id": None,
            },
            {
                "name": "zeta-list",
                "current_revision": 2,
                "at": zeta_2["at"],
                "size": len(second.encode()),
                "author": author_of(held),
                "grant_id": held.grant_id,
            },
        ]
        assert listing["references"] == expected
        assert [entry["name"] for entry in succeeded(await read_reference(client, who, ROOT))["references"]] == [
            "in-the-parent"
        ]

    play(scene.host, scenario)


def test_the_current_revision_and_an_old_one_each_return_their_own_text_and_provenance(build_scene):
    """R3. With revisions of different text and different authors, the current one and revision 1 differ, and each
    answer is exactly what that revision was written with: its text, size, base, time, author and grant."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        held = await scene.grantee(client, "job-search", ROOT, ("reference.edit",), inherit=True)
        first = succeeded(await core_edit(client, scene, BOARDS, NAME, "the first text", 0))
        second = succeeded(await grantee_edit(client, held, BOARDS, NAME, "the second text, longer caf\u00e9", 1))
        third = succeeded(await core_edit(client, scene, BOARDS, NAME, "the third", 2))
        who = Core(scene.host, "worker")
        topic = {"topic": BOARDS, "topic_id": scene.topic_id(BOARDS), "name": NAME, "current_revision": 3}

        def expected(made, body):
            return {
                **topic,
                "revision": made["revision"],
                "body": body,
                "size": len(body.encode()),
                "base_revision": made["base_revision"],
                "at": made["at"],
                "author": made["author"],
                "grant_id": made["grant_id"],
            }

        assert succeeded(await read_reference(client, who, BOARDS, NAME)) == expected(third, "the third")
        assert succeeded(await read_reference(client, who, BOARDS, NAME, 3)) == expected(third, "the third")
        assert succeeded(await read_reference(client, who, BOARDS, NAME, 1)) == expected(first, "the first text")
        assert succeeded(await read_reference(client, who, BOARDS, NAME, 2)) == expected(
            second, "the second text, longer caf\u00e9"
        )
        history = succeeded(await reference_history(client, who, BOARDS, NAME))
        assert history["revisions"] == [
            {key: made[key] for key in ("revision", "base_revision", "at", "size", "author", "grant_id")}
            for made in (first, second, third)
        ]

    play(scene.host, scenario)


def test_text_comes_back_byte_for_byte(build_scene):
    """R5. Carriage returns, tabs, trailing spaces, leading and trailing newlines, composed and decomposed accents,
    characters outside the basic plane and whitespace alone are all returned exactly as written, to a core role and
    to a guest, and their size is their UTF-8 bytes. Nothing is stripped or normalised on the way in or out."""
    scene = build_scene()
    bodies = {
        "crlf-tabs": "line one\r\nline two\r\n\ttabbed\t\n  trailing spaces   \n",
        "edges": "\n\n  leading and trailing newlines\n\n",
        "composed": unicodedata.normalize("NFC", "caf\u00e9 na\u00efve"),
        "decomposed": unicodedata.normalize("NFD", "caf\u00e9 na\u00efve"),
        "astral": "clef \U0001d11e and smile \U0001f600 end",
        "spaces": "   ",
    }

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        guest = await bound_guest(client, scene)
        for name, text in bodies.items():
            succeeded(await core_edit(client, scene, BOARDS, name, text, 0))
        for who in (Core(scene.host, "worker"), guest):
            for name, text in bodies.items():
                read = succeeded(await read_reference(client, who, BOARDS, name))
                assert read["body"] == text and read["size"] == len(text.encode()), name
        assert bodies["composed"] != bodies["decomposed"]

    play(scene.host, scenario)


# 3. What is not there ---------------------------------------------------------------------------------------


def test_an_unknown_topic_an_unknown_reference_and_an_unknown_revision_are_three_different_refusals(build_scene):
    """R4. All three tools say `unknown_topic` for a path with no topic. In a known topic, a name with no document is
    `unknown_reference` for the current text, the history and a revision request alike, and never an empty body.
    A revision the document does not have is told apart, with the number it is at."""
    # Mutant 19: an unknown name read as an empty body; a revision beyond the current read as the current.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=(*refs.REFERENCE_TOPICS, LEADS))
        await write_revisions(client, scene, BOARDS, NAME, 6)
        who = Core(scene.host, "worker")
        ghost = "job-hunt/ghost"
        for result in (
            await read_reference(client, who, ghost),
            await read_reference(client, who, ghost, NAME),
            await read_reference(client, who, ghost, NAME, 1),
            await reference_history(client, who, ghost, NAME),
        ):
            assert result == {"error": "unknown_topic", "detail": f9(ghost)}
        missing = {"error": "unknown_reference", "detail": f10("no-such", BOARDS)}
        assert await read_reference(client, who, BOARDS, "no-such") == missing
        assert await read_reference(client, who, BOARDS, "no-such", 1) == missing
        assert await reference_history(client, who, BOARDS, "no-such") == missing
        assert await read_reference(client, who, LEADS, NAME) == {
            "error": "unknown_reference",
            "detail": f10(NAME, LEADS),
        }
        for revision in (7, 8, 1000):
            assert await read_reference(client, who, BOARDS, NAME, revision) == {
                "error": "unknown_reference",
                "detail": f11(NAME, BOARDS, revision, 6),
            }
        assert await read_reference(client, who, BOARDS, NAME, 6) != await read_reference(client, who, BOARDS, NAME, 7)

    play(scene.host, scenario)


def test_a_revision_below_one_or_without_a_name_and_history_paging_outside_its_bounds_are_refused(build_scene):
    """R4, R6, F12. `revision` needs a name. Revision 0 and a negative one are refused by the schema, not read as the
    current revision, and called directly, past the schema, a revision below 1 is still refused and no text is
    returned. `limit` is 1 to 100 and `after_revision` is 0 or more; anything else is refused before the tool runs,
    and the edges of both bounds are read."""
    # Mutant 19: revision 0 read as the current.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        await write_revisions(client, scene, BOARDS, NAME, 2)
        who = Core(scene.host, "worker")
        assert await read_reference(client, who, BOARDS, None, 1) == {"error": "invalid_request", "detail": F12}
        for bad in (0, -1):
            refused_by_schema(await read_reference(client, who, BOARDS, NAME, bad), "revision")
        for revision in (0, -3):
            with pytest.raises(BatonError) as raised:
                direct(scene, "read_reference", "worker", topic=BOARDS, name=NAME, revision=revision)
            assert raised.value.code in ("unknown_reference", "invalid_request"), (revision, str(raised.value))
        for paging, argument in (
            ({"limit": 0}, "limit"),
            ({"limit": 101}, "limit"),
            ({"after_revision": -1}, "after_revision"),
        ):
            refused_by_schema(await reference_history(client, who, BOARDS, NAME, **paging), argument)
        edge = succeeded(await reference_history(client, who, BOARDS, NAME, limit=100, after_revision=0))
        assert len(edge["revisions"]) == 2

    play(scene.host, scenario)


# 3a. Past the schema, and one view per call -----------------------------------------------------------------

PAGING = [
    ("limit", 0),
    ("limit", 101),
    ("limit", True),
    ("limit", "5"),
    ("after_revision", -1),
    ("after_revision", True),
    ("after_revision", "0"),
]


def test_the_module_refuses_a_limit_or_a_cursor_the_schema_would_have_refused(build_scene, monkeypatch):
    """R6. Called directly, past the schema, `history` still refuses a limit of 0 or 101, a cursor below 0 and a boolean
    or a text for either: the module's own checks hold where the schema's bounds do not run."""
    scene = build_scene()
    staging = refs.Staging(monkeypatch)

    for argument, value in PAGING:
        with pytest.raises(BatonError) as raised:
            direct(scene, "reference_history", "worker", topic=BOARDS, name=NAME, **{argument: value})
        assert (raised.value.code, str(raised.value)) == (
            "invalid_request",
            "limit is 1-100 and after_revision at least 0",
        ), (argument, value)
        assert staging.texts() == [], (argument, value, staging.texts())


def test_a_history_page_ends_at_the_current_revision_it_reports_when_a_write_lands_between_its_reads(
    build_scene, monkeypatch
):
    """R6. `history` reads the document's current revision and its page from one view, so a revision committed between
    the two is in neither: the page ends at the `current_revision` it reports, and `next_revision` is that revision."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        await write_revisions(client, scene, BOARDS, NAME, 3)
        # A writer commits revision 4 after the call has read that the document is at 3, and before it reads the page.
        staging = ReadStaging(monkeypatch).before(
            "ORDER BY revision LIMIT", other_writer_adds_revision(scene, BOARDS, NAME, 4)
        )
        page = direct(scene, "reference_history", "worker", topic=BOARDS, name=NAME)
        assert staging.fired and staging.outcome == "landed", (staging.fired, staging.outcome)
        assert "reference_docs" in staging.landed_after, staging.landed_after
        assert page["current_revision"] == 3
        assert [entry["revision"] for entry in page["revisions"]] == [1, 2, 3], page
        assert page["next_revision"] == page["current_revision"]
        later = direct(scene, "reference_history", "worker", topic=BOARDS, name=NAME)
        assert (later["current_revision"], later["next_revision"]) == (4, 4), "the staged write was committed"

    play(scene.host, scenario)


def test_a_read_answers_from_the_state_it_began_with_when_a_write_lands_after_its_first_statement(
    build_scene, monkeypatch
):
    """`read_reference` answers from one view, so a revision committed after the call has begun is not in its answer:
    it names the revision and the text the document had when the call began."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        await write_revisions(client, scene, BOARDS, NAME, 3)
        # A writer commits revision 4 after the call's first read (the topic) and before its second (the document).
        staging = ReadStaging(monkeypatch).before(
            "FROM reference_docs", other_writer_adds_revision(scene, BOARDS, NAME, 4)
        )
        answer = direct(scene, "read_reference", "worker", topic=BOARDS, name=NAME)
        assert staging.fired and staging.outcome == "landed", (staging.fired, staging.outcome)
        assert "FROM topics" in staging.landed_after, staging.landed_after
        assert (answer["revision"], answer["current_revision"], answer["body"]) == (3, 3, body_of(3)), answer
        later = direct(scene, "read_reference", "worker", topic=BOARDS, name=NAME)
        assert (later["revision"], later["current_revision"]) == (4, 4), "the staged write was committed"

    play(scene.host, scenario)


# 4. The history ---------------------------------------------------------------------------------------------


def test_history_pages_forward_oldest_first_with_no_bodies_and_a_cursor_that_continues_it(build_scene):
    """R6. Ten revisions, three to a page: 1-3, 4-6, 7-9, then 10, then nothing. `next_revision` is the last one
    returned, or `after_revision` when the page is empty, and feeding it back continues. Each entry says its base
    and carries no body. The default page holds them all."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        await write_revisions(client, scene, BOARDS, NAME, 10)
        who = Core(scene.host, "worker")
        seen, after = [], 0
        for expected_page in ([1, 2, 3], [4, 5, 6], [7, 8, 9], [10], []):
            page = succeeded(await reference_history(client, who, BOARDS, NAME, after_revision=after, limit=3))
            assert set(page) == HISTORY_KEYS
            assert (page["topic"], page["topic_id"], page["name"], page["current_revision"]) == (
                BOARDS,
                scene.topic_id(BOARDS),
                NAME,
                10,
            )
            assert [entry["revision"] for entry in page["revisions"]] == expected_page
            assert page["next_revision"] == (expected_page[-1] if expected_page else after)
            assert all(set(entry) == HISTORY_ENTRY_KEYS for entry in page["revisions"])
            assert all(entry["base_revision"] == entry["revision"] - 1 for entry in page["revisions"])
            seen += page["revisions"]
            after = page["next_revision"]
        assert [entry["revision"] for entry in seen] == list(range(1, 11))
        assert "body" not in str(seen)
        everything = succeeded(await reference_history(client, who, BOARDS, NAME))
        assert everything["revisions"] == seen and everything["next_revision"] == 10
        beyond = succeeded(await reference_history(client, who, BOARDS, NAME, after_revision=50))
        assert (beyond["revisions"], beyond["next_revision"]) == ([], 50)

    play(scene.host, scenario)


def test_a_document_has_no_cap_on_revisions_and_its_history_pages_past_the_page_size(build_scene):
    """Q2. 105 revisions are written, more than a page of 100 and more than a cap of 100 would allow. The current text
    is the 105th; history reads 100 and then the last 5."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        for number in range(1, 106):
            made = direct(
                scene,
                "edit_reference",
                "planner",
                topic=BOARDS,
                name=NAME,
                body=body_of(number),
                expected_revision=number - 1,
            )
            assert made["revision"] == number
        who = Core(scene.host, "worker")
        default = succeeded(await reference_history(client, who, BOARDS, NAME))
        assert (len(default["revisions"]), default["next_revision"]) == (50, 50), "the default page is 50"
        first = succeeded(await reference_history(client, who, BOARDS, NAME, limit=100))
        assert (len(first["revisions"]), first["next_revision"], first["current_revision"]) == (100, 100, 105)
        rest = succeeded(await reference_history(client, who, BOARDS, NAME, after_revision=100, limit=100))
        assert ([entry["revision"] for entry in rest["revisions"]], rest["next_revision"]) == (
            [101, 102, 103, 104, 105],
            105,
        )
        current = succeeded(await read_reference(client, who, BOARDS, NAME))
        assert (current["revision"], current["body"]) == (105, body_of(105))

    play(scene.host, scenario)
