"""The shape of an edit's arguments and the limits on it, each with the spec's exact refusal text, and the hourly limit
a grantee's edits count against.

What this pins:

* The name grammar (1-40 characters of a-z, 0-9 and inner hyphens): what is refused is refused in F4's words without
  echoing the value, even when the value is a binding token or a grant key.
* The body: at least 1 byte, at most 32768 bytes of UTF-8 counted in bytes, never stripped. Exactly 32768 is accepted
  and 32769 is not, in one-, two- and four-byte characters. A body that cannot be encoded is refused as text.
* At most 20 documents in a topic, counted on the topic itself and only when a document is new.
* A `request_id` of 1 to 200 characters, and the existing messages for a malformed topic, with a key redacted.
* The shape checks need no database: a call refused for its shape runs no statement.
* The hourly limit: a grantee's every edit attempt is counted by the same per-alias and per-credential counters as a
  guest post, in the guard; a core role's edit and every read are not.

Mutant numbers in comments refer to the spec's list of one-line mutants the tests must kill. Q numbers refer to its open
questions: the test pins the question's default, so a ruling changes only the tests that carry that number.
"""

from __future__ import annotations

import secrets

import pytest
import reference_support as refs
import topic_grant_tools_support as support
from reference_support import (
    BOARDS,
    F4,
    F7,
    F13,
    F15,
    LEADS,
    NAME,
    ROOT,
    Bound,
    Core,
    Staging,
    board,
    body_of,
    bound_guest,
    core_edit,
    direct,
    f7b,
    f7c,
    grantee_edit,
    read_reference,
    reference_history,
    request_id,
    write_revisions,
)
from topic_grant_tools_support import only, play, refused, succeeded, thread, tool

from baton import topics
from baton.errors import BatonError, InvalidRequest

build_scene = refs.build_scene  # the fixture, under the name the tests ask for


def topic_grammar_text(value):
    """What the existing helper says of a malformed topic: the text a bad `topic` is refused with here too."""
    with pytest.raises(InvalidRequest) as raised:
        topics.check_path(value, field="topic")
    return str(raised.value)


# 1. The name ------------------------------------------------------------------------------------------------


def test_a_name_outside_the_grammar_is_refused_in_f4s_words_and_never_echoed(build_scene):
    """41 characters, empty, upper case, a trailing or a leading hyphen, an underscore, a slash, a space, a letter
    outside ASCII, a lone surrogate, a binding token and a grant key: each is refused as `invalid_request` with the
    one text, which does not repeat the value, and nothing is written."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        wrong = [
            "a" * 41,
            "",
            "Upper",
            "trailing-",
            "-leading",
            "has_underscore",
            "a/b",
            "has space",
            "caf\u00e9",
            "\ud800",
            secrets.token_urlsafe(32),
            support.new_key(),
            held.token,
            held.key,
        ]
        for value in wrong:
            for result in (
                await core_edit(client, scene, BOARDS, value, "text", 0),
                await grantee_edit(client, held, BOARDS, value, "text", 0),
            ):
                refused(result, "invalid_request", F4)
                assert not value or value not in str(result), "the refusal echoed the name"
        assert scene.docs() == [] and scene.revisions() == [] and scene.refused_uses() == []

    play(scene.host, scenario)


def test_a_name_inside_the_grammar_is_accepted_at_its_edges(build_scene):
    """One character, forty characters, digits only, and inner hyphens even doubled are names."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        for name in ("a", "a" * 40, "007", "a-b", "double--hyphen", "x" + "-" * 38 + "y"):
            made = succeeded(await core_edit(client, scene, BOARDS, name, "text", 0))
            assert (made["name"], made["revision"]) == (name, 1)
        assert len(scene.docs()) == 6

    play(scene.host, scenario)


# 2. The body ------------------------------------------------------------------------------------------------


def test_an_empty_body_is_refused_and_a_body_of_whitespace_alone_is_not(build_scene):
    """Q4. Nothing is stripped, so spaces and newlines are a body; an empty string has no use and is refused with F7."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        refused(await core_edit(client, scene, BOARDS, NAME, "", 0), "invalid_request", F7)
        assert scene.docs() == []
        made = succeeded(await core_edit(client, scene, BOARDS, NAME, " \n\t \r\n", 0))
        assert made["size"] == 6 and scene.revisions(BOARDS, NAME)[0]["body"] == " \n\t \r\n"

    play(scene.host, scenario)


@pytest.mark.parametrize("bad", ["\ud800", "\udfff", "text\udc00text"], ids=["high", "low", "inside"])
def test_a_body_that_cannot_be_encoded_as_utf8_is_refused_as_text(build_scene, bad):
    """A lone surrogate reaches the tool from JSON. It is not text of UTF-8 bytes, so it is refused in F7's words
    rather than failing inside the database, and nothing is written."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        refused(await core_edit(client, scene, BOARDS, NAME, bad, 0), "invalid_request", F7)
        assert scene.docs() == [] and scene.revisions() == []

    play(scene.host, scenario)


@pytest.mark.parametrize("bad", [None, 5, b"bytes"], ids=["null", "number", "bytes"])
def test_a_body_that_is_not_a_string_is_refused_by_the_module(build_scene, bad):
    """The schema makes `body` text, but the body called directly gets the same refusal."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        with pytest.raises(BatonError) as raised:
            direct(scene, "edit_reference", "planner", topic=BOARDS, name=NAME, body=bad, expected_revision=0)
        assert (raised.value.code, str(raised.value)) == ("invalid_request", F7)
        assert scene.docs() == []

    play(scene.host, scenario)


@pytest.mark.parametrize(
    "unit,count", [("a", 32768), ("\u00e9", 16384), ("\U0001f600", 8192)], ids=["one-byte", "two-byte", "four-byte"]
)
def test_a_body_of_exactly_32768_bytes_is_accepted_and_stored_whole(build_scene, unit, count):
    """R20. The limit is in bytes of UTF-8, not characters: 32768 ASCII characters, 16384 two-byte ones and 8192
    four-byte ones are the same size, all accepted, and the response and the row say so."""
    # Mutant 15: the size counted as len(body), or the limit as `>=`.
    scene = build_scene()
    text = unit * count

    async def scenario(client):
        await support.stage(client, scene.host)
        made = succeeded(await core_edit(client, scene, BOARDS, NAME, text, 0))
        assert made["size"] == 32768
        assert only(scene.revisions(BOARDS, NAME), "revision")["body"] == text

    play(scene.host, scenario)


@pytest.mark.parametrize(
    "unit,count,size",
    [("a", 32769, 32769), ("\u00e9", 16385, 32770), ("\U0001f600", 8193, 32772)],
    ids=["one-byte", "two-byte", "four-byte"],
)
def test_a_body_over_32768_bytes_is_refused_with_its_size_and_nothing_is_written(build_scene, unit, count, size):
    """R20, F7b. One byte over, in each width, is `limit_exceeded` naming the limit and the size of this body. The
    two-byte case has fewer characters than the limit, so a count of characters would let it through."""
    # Mutant 15: the size counted as len(body), or the limit as `>=`.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        expected = {"error": "limit_exceeded", "detail": f7b(size), "limit": "reference_body_bytes"}
        assert await core_edit(client, scene, BOARDS, NAME, unit * count, 0) == expected
        assert await grantee_edit(client, held, BOARDS, NAME, unit * count, 0) == expected
        assert scene.docs() == [] and scene.revisions() == [] and scene.refused_uses() == []

    play(scene.host, scenario)


# 3. Documents per topic -----------------------------------------------------------------------------------


def test_the_twentieth_document_in_a_topic_is_created_and_the_twenty_first_is_refused(build_scene):
    """R20, F7c. Twenty documents are made in a topic. The twenty-first is refused with the limit named and nothing
    written, an edit to one of the twenty still works, and twenty in a sibling topic and one in the parent do not
    count: the limit is on the topic's own id."""
    # Mutant 16: `>=` 20 changed to `>` 20, or the limit applied to an edit of an existing document.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        for number in range(1, 21):
            succeeded(await core_edit(client, scene, BOARDS, f"ref-{number}", "text", 0))
        tables, counts = scene.tables(), scene.counts()
        for result in (
            await core_edit(client, scene, BOARDS, "ref-21", "text", 0),
            await grantee_edit(client, held, BOARDS, "ref-21", "text", 0),
        ):
            assert result == {"error": "limit_exceeded", "detail": f7c(BOARDS), "limit": "references_per_topic"}
        assert scene.tables() == tables
        assert {key: value for key, value in scene.counts().items() if key != "alias_posts"} == {
            key: value for key, value in counts.items() if key != "alias_posts"
        }
        assert scene.refused_uses() == [], "the limit names no grant"
        edited = succeeded(await core_edit(client, scene, BOARDS, "ref-1", "second text", 1))
        assert edited["revision"] == 2
        by_grantee = succeeded(await grantee_edit(client, held, BOARDS, "ref-2", "second text", 1))
        assert by_grantee["revision"] == 2
        for number in range(1, 21):
            succeeded(await core_edit(client, scene, LEADS, f"ref-{number}", "text", 0))
        succeeded(await core_edit(client, scene, ROOT, "ref-1", "text", 0))
        assert len(scene.docs()) == 41

    play(scene.host, scenario)


# 4. The request id and the topic ---------------------------------------------------------------------------


def test_a_request_id_of_one_to_two_hundred_characters_is_kept_and_any_other_is_refused(build_scene):
    """F13. An empty request id and one of 201 characters are refused (the empty one is not replaced by a made-up
    id); 1 and 200 characters are accepted and stored as given."""
    # Q-free: the spec's test list says empty is refused, where `create_topic` would quietly replace it.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        for bad in ("", "r" * 201):
            refused(await core_edit(client, scene, BOARDS, NAME, "text", 0, request_id=bad), "invalid_request", F13)
        assert scene.docs() == []
        for number, good in enumerate(("r", "r" * 200)):
            made = succeeded(await core_edit(client, scene, BOARDS, NAME, "text", number, request_id=good))
            assert made["request_id"] == good
        assert [row["request_id"] for row in scene.revisions(BOARDS, NAME)] == ["r", "r" * 200]

    play(scene.host, scenario)


def test_a_malformed_topic_is_refused_with_the_existing_messages_and_a_key_is_redacted_from_them(build_scene):
    """F14, R22. The topic checks are the existing ones, whose text echoes the topic. When the value is the grant
    key, mistakenly passed there along with the key as `grant_token`, the guard's redaction covers it: the refusal
    does not hold the key."""
    # Q11: the key in a refusal that echoes `topic`.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        for value in ("Upper", "a/b/c", "", "has space", "job-hunt/", "/job-hunt"):
            refused(
                await core_edit(client, scene, value, NAME, "text", 0), "invalid_request", topic_grammar_text(value)
            )
        redacted = {"error": "invalid_request", "detail": topic_grammar_text(held.key).replace(held.key, "[redacted]")}
        for result in (
            await core_edit(client, scene, held.key, NAME, "text", 0, grant_token=held.key),
            await grantee_edit(client, held, held.key, NAME, "text", 0),
        ):
            assert result == redacted, result
            assert held.key not in str(result)
        assert scene.docs() == [] and scene.refused_uses() == []

    play(scene.host, scenario)


def test_the_shape_checks_need_no_database(build_scene, monkeypatch):
    """R10. A call refused for its topic, name, body or request id runs no statement on its connection, whoever makes
    it: the tool does not read the database before it has checked what it was asked."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        staging = Staging(monkeypatch)  # after the set-up, which uses the tools too
        attempts = [
            lambda: core_edit(client, scene, "Upper", NAME, "text", 0),
            lambda: core_edit(client, scene, BOARDS, "Upper", "text", 0),
            lambda: core_edit(client, scene, BOARDS, NAME, "", 0),
            lambda: core_edit(client, scene, BOARDS, NAME, "a" * 32769, 0),
            lambda: core_edit(client, scene, BOARDS, NAME, "text", 0, request_id=""),
            lambda: grantee_edit(client, held, "Upper", NAME, "text", 0),
            lambda: grantee_edit(client, held, BOARDS, "Upper", "text", 0),
            lambda: grantee_edit(client, held, BOARDS, NAME, "", 0),
            lambda: grantee_edit(client, held, BOARDS, NAME, "text", 0, request_id="r" * 201),
        ]
        for attempt in attempts:
            result = await attempt()
            assert result.get("error") in ("invalid_request", "limit_exceeded"), result
        assert staging.texts() == []

    play(scene.host, scenario)


# 5. The hourly limit ----------------------------------------------------------------------------------------


def test_a_grantees_edits_and_a_guest_post_share_the_hourly_limit(build_scene):
    """R20, F15. With two attempts an hour per alias, an edit and a post use the quota, and the third attempt of either
    kind is refused as `limit_exceeded` naming the alias counter, with nothing written."""
    # Mutant 17: the hourly admission left out of the guard branch for edit_reference.
    scene = build_scene()
    scene.limit_posts(per_alias=2)

    async def scenario(client):
        held = await board(client, scene)
        conversation = await thread(client, scene.host, LEADS)
        succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0))
        succeeded(
            await tool(
                client,
                scene.host,
                "send_message",
                {"conversation_id": conversation, "text": "a post", "token": held.token},
            )
        )
        tables = scene.tables()
        over = await grantee_edit(client, held, BOARDS, NAME, body_of(2), 1)
        assert over == {"error": "limit_exceeded", "detail": F15, "limit": "posts_per_alias"}
        post = await tool(
            client,
            scene.host,
            "send_message",
            {"conversation_id": conversation, "text": "another post", "token": held.token},
        )
        assert post.get("error") == "limit_exceeded" and post.get("limit") == "posts_per_alias", post
        assert scene.tables() == tables and scene.posts_counted() == 2

    play(scene.host, scenario)


def test_a_stale_a_replayed_and_a_refused_attempt_are_counted_like_a_post_attempt(build_scene):
    """Q3. The counter charges every admitted attempt, in the guard, before the body: so a success, a stale refusal,
    a replay and an edit refused for its name each use one, and the fifth attempt is over a limit of four."""
    scene = build_scene()
    scene.limit_posts(per_alias=4)

    async def scenario(client):
        held = await board(client, scene)
        rid = request_id()
        succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0, request_id=rid))
        assert (await grantee_edit(client, held, BOARDS, NAME, "text", 0)).get("error") == "stale_revision"
        replay = succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0, request_id=rid))
        assert replay["created"] is False
        refused(await grantee_edit(client, held, BOARDS, "Upper", "text", 0), "invalid_request", F4)
        assert scene.posts_counted() == 4
        over = await grantee_edit(client, held, BOARDS, NAME, body_of(2), 1)
        assert over == {"error": "limit_exceeded", "detail": F15, "limit": "posts_per_alias"}
        assert scene.posts_counted() == 4, "the refused attempt is not counted"
        assert len(scene.revisions(BOARDS, NAME)) == 1

    play(scene.host, scenario)


def test_two_aliases_on_one_credential_share_the_per_credential_counter(build_scene):
    """R20. The shared guest login is one credential: with one attempt an hour per credential, the second alias's
    edit is refused naming the credential counter."""
    scene = build_scene()
    scene.limit_posts(per_credential=1)

    async def scenario(client):
        first = await board(client, scene)
        second = await scene.grantee(client, "lead-scout", LEADS, ("reference.edit",), inherit=False)
        succeeded(await grantee_edit(client, first, BOARDS, NAME, body_of(1), 0))
        over = await grantee_edit(client, second, LEADS, NAME, body_of(1), 0)
        assert over == {"error": "limit_exceeded", "detail": F15, "limit": "posts_per_credential"}
        assert len(scene.docs()) == 1

    play(scene.host, scenario)


def test_a_core_roles_edits_and_every_read_add_no_post_row(build_scene):
    """R20. Only a grantee's edit is charged. A core role's edits, and every read by a core role, a bound guest or a
    grantee, leave the hourly counter alone."""
    scene = build_scene()
    scene.limit_posts(per_alias=1)

    async def scenario(client):
        held = await board(client, scene)
        guest = await bound_guest(client, scene)
        await write_revisions(client, scene, BOARDS, NAME, 3)
        for who in (Core(scene.host), Core(scene.host, "worker"), guest, Bound.of(held)):
            succeeded(await read_reference(client, who, BOARDS))
            succeeded(await read_reference(client, who, BOARDS, NAME))
            succeeded(await read_reference(client, who, BOARDS, NAME, 2))
            succeeded(await reference_history(client, who, BOARDS, NAME))
        assert scene.posts_counted() == 0

    play(scene.host, scenario)
