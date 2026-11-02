"""A revision number too big for the database is answered like any other that is not there, never a crash.

SQLite's integers stop at 2**63 - 1, and the schema bounds a revision only from below, so a caller can send 2**63 or
more as the `revision` of a read or the `after_revision` of a history. A tool that binds such a number into a
statement raises inside its body, and the guard catches refusals only: the caller gets the SDK's "Error executing
tool" sentence in place of an answer, and the call leaves no audit line.

What this pins, all through the MCP client so that the schema runs first:

* A revision past the end of a document, 2**63 and 2**64 among them, is the same `unknown_reference` refusal as
  revision 4 of a document at 3, naming the revision asked for and where the document is.
* A history after a revision past the end is the empty page, and `next_revision` hands the cursor back.
* Each call leaves its audit line, the refusal's and the page's.
* GUARD: an edit whose `expected_revision` is that large is refused as stale and writes nothing. Its compare is made
  in Python, so it never meets the database's limit, before the fix or after.

Each answer's text is looked at before it is parsed, so that a tool that crashed fails these tests with the SDK's
sentence in view, not with a JSON decode error.

Mutant numbers and Q numbers refer to the spec, as in the other reference tests.
"""

from __future__ import annotations

import json

import pytest
import reference_support as refs
import topic_grant_tools_support as support
from reference_support import BOARDS, NAME, core_edit, f5, f11, write_revisions
from test_aliases import rpc
from topic_grant_tools_support import play

build_scene = refs.build_scene  # the fixture, under the name the tests ask for


async def answer_text(client, host, name, arguments, role="worker"):
    """What a tool call answered, as the server sent it and before anything parses it."""
    response = await rpc(client, host, "tools/call", {"name": name, "arguments": arguments}, role=role)
    return response["result"]["content"][0]["text"]


def an_answer(text):
    """The text parsed, once it is seen to be an answer: a tool that raised sends the SDK's sentence, not JSON."""
    assert text.lstrip().startswith("{"), text
    return json.loads(text)


@pytest.mark.parametrize("revision", [2**63, 2**64], ids=["2**63", "2**64"])
def test_a_revision_beyond_the_databases_integers_is_an_unknown_revision_and_the_call_is_audited(build_scene, revision):
    """R4. The document is at 3 and a revision of 2**63 or 2**64 is asked for: the refusal is the one for revision 4,
    with the number asked for and the number the document is at. It is an answer, and the call has its audit line."""
    # Mutant 19: a revision beyond the current one treated as anything but unknown.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        await write_revisions(client, scene, BOARDS, NAME, 3)
        lines = len(scene.audit())
        asked = {"topic": BOARDS, "name": NAME, "revision": revision}
        refusal = an_answer(await answer_text(client, scene.host, "read_reference", asked))
        assert refusal == {"error": "unknown_reference", "detail": f11(NAME, BOARDS, revision, 3)}
        assert len(scene.audit()) == lines + 1, "the call left no audit line"
        line = scene.audit()[-1]
        assert (line["tool"], line["outcome"], line["topic"], line["reference"]) == (
            "read_reference", "unknown_reference", BOARDS, NAME)

    play(scene.host, scenario)


def test_a_history_cursor_beyond_the_databases_integers_reads_an_empty_page_and_the_call_is_audited(build_scene):
    """R6. A cursor past the last revision, as large as 2**63, pages to nothing and comes back as `next_revision`, as a
    cursor of 50 does on a document at 3. The call has its audit line."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        await write_revisions(client, scene, BOARDS, NAME, 3)
        lines = len(scene.audit())
        asked = {"topic": BOARDS, "name": NAME, "after_revision": 2**63}
        page = an_answer(await answer_text(client, scene.host, "reference_history", asked))
        assert page == {"topic": BOARDS, "topic_id": scene.topic_id(BOARDS), "name": NAME, "current_revision": 3,
                        "revisions": [], "next_revision": 2**63}
        assert len(scene.audit()) == lines + 1, "the call left no audit line"
        line = scene.audit()[-1]
        assert (line["tool"], line["outcome"], line["topic"], line["topic_id"], line["reference"]) == (
            "reference_history", "ok", BOARDS, scene.topic_id(BOARDS), NAME)

    play(scene.host, scenario)


def test_an_edit_expecting_a_revision_beyond_the_databases_integers_is_stale_and_writes_nothing(build_scene):
    """GUARD. R11. The compare is made in Python, so an `expected_revision` of 2**63 never reaches the database: for a
    document at 3, and for one that does not exist, it is refused as stale with the number the document is at, and no
    row of either table changes."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        await write_revisions(client, scene, BOARDS, NAME, 3)
        tables = scene.tables()
        for name, current in ((NAME, 3), ("not-there", 0)):
            stale = await core_edit(client, scene, BOARDS, name, "text", 2**63)
            assert stale == {"error": "stale_revision", "detail": f5(name, BOARDS, current, 2**63),
                             "current_revision": current}, stale
        assert scene.tables() == tables

    play(scene.host, scenario)
