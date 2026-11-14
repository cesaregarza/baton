"""Writing, reading and restoring wiki pages over MCP: the compare, retries, exact restores and history.

What this pins:

* a core role with no flag and a guest alias with a binding each create a page, read it, edit it and read it again,
  and every revision names its author as the call ran;
* two writers from one revision: one is written and the other is `stale_revision` with `current_revision`;
* a retry answers the stored revision with `created: false`, and a request id reused for anything else, a restore
  for an edit's included, is `idempotency_conflict`;
* a restore writes the exact bytes of an earlier revision as the next one, records `restored_from`, and can itself be
  restored; restoring the current revision is a new revision too;
* history is oldest first, paged as reference history is, and shows `restored_from`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import wiki_support as wiki
from reference_support import run_together
from topic_grant_tools_support import play, refused, succeeded
from wiki_support import Caller, bound

PAGE = "ops/deploys"


@pytest.fixture
def host(tmp_path):
    return wiki.wiki_host(tmp_path)


def test_a_core_role_and_a_bound_guest_each_create_read_edit_and_read_again(host):
    async def scenario(client):
        for writer, path in ((Caller(host, "worker"), "ops"), (await bound(client, host), PAGE)):
            made = succeeded(await writer.edit(client, path, "First notes.\n", 0))
            assert set(made) == wiki.EDIT_KEYS
            assert (made["path"], made["revision"], made["base_revision"], made["created"]) == (path, 1, 0, True)
            assert (made["restored_from"], made["size"], made["author"]) == (None, 13, writer.author)
            first = succeeded(await writer.call(client, "read_wiki", path=path))
            assert set(first) == wiki.READ_KEYS
            assert (first["body"], first["revision"], first["current_revision"]) == ("First notes.\n", 1, 1)

            succeeded(await writer.edit(client, path, "Second notes.\n", 1))
            again = succeeded(await writer.call(client, "read_wiki", path=path))
            assert (again["body"], again["revision"], again["current_revision"]) == ("Second notes.\n", 2, 2)
            assert (again["base_revision"], again["author"]) == (1, writer.author)
            old = succeeded(await writer.call(client, "read_wiki", path=path, revision=1))
            assert (old["body"], old["revision"], old["current_revision"]) == ("First notes.\n", 1, 2)

            rows = wiki.revisions_of(host, path)
            assert [(row["revision"], row["base_revision"], row["restored_from"]) for row in rows] == [
                (1, 0, None),
                (2, 1, None),
            ]
            stored = {
                key: rows[0][column]
                for key, column in (
                    ("role", "author_role"),
                    ("alias_id", "author_alias_id"),
                    ("alias", "author_alias_name"),
                    ("code", "author_alias_code"),
                )
                if rows[0][column] is not None
            }
            assert stored == writer.author

    play(host, scenario)


def test_two_writers_from_the_same_revision_one_wins_and_the_other_is_stale(host):
    async def scenario(client):
        planner, guest = Caller(host), await bound(client, host)
        succeeded(await planner.edit(client, PAGE, "Base.", 0))
        succeeded(await guest.edit(client, PAGE, "The guest's.", 1))
        lost = refused(await planner.edit(client, PAGE, "The planner's.", 1), "stale_revision", wiki.stale(PAGE, 2, 1))
        assert lost["current_revision"] == 2
        assert [row["body"] for row in wiki.revisions_of(host, PAGE)] == ["Base.", "The guest's."]
        # Creating a page that exists is as stale as editing one that moved.
        taken = refused(await guest.edit(client, PAGE, "Again.", 0), "stale_revision", wiki.stale(PAGE, 2, 0))
        assert taken["current_revision"] == 2
        ahead = refused(await guest.edit(client, PAGE, "Ahead.", 7), "stale_revision", wiki.stale(PAGE, 2, 7))
        assert ahead["current_revision"] == 2
        missing = refused(await guest.edit(client, "ops/none", "x", 1), "stale_revision", wiki.stale("ops/none", 0, 1))
        assert missing["current_revision"] == 0
        assert len(wiki.revisions_of(host, PAGE)) == 2

    play(host, scenario)


def test_writers_racing_from_one_revision_on_their_own_connections_leave_one_winner(host, monkeypatch):
    """Four edits and a restore released together from revision 1: one is written, the others are stale at 2, and
    none raises anything but a refusal."""

    async def scenario(client):
        succeeded(await Caller(host).edit(client, PAGE, "Base.", 0))

    play(host, scenario)
    jobs = [
        (lambda tools, n=n: tools.edit_wiki("planner", path=PAGE, body=f"Racer {n}.", expected_revision=1))
        for n in range(4)
    ]
    jobs.append(lambda tools: tools.restore_wiki("planner", path=PAGE, revision=1, expected_revision=1))
    results = run_together(SimpleNamespace(host=host), monkeypatch, jobs)
    assert [kind for kind, _ in results].count("ok") == 1, results
    losers = [answer for kind, answer in results if kind != "ok"]
    assert [(error.code, error.extra) for error in losers] == [("stale_revision", {"current_revision": 2})] * 4
    assert [row["revision"] for row in wiki.revisions_of(host, PAGE)] == [1, 2]


def test_a_retry_answers_the_stored_revision_and_a_reused_request_id_is_a_conflict(host):
    async def scenario(client):
        guest = await bound(client, host)
        edit_id, restore_id = wiki.request_id(), wiki.request_id()
        first = succeeded(await guest.edit(client, PAGE, "One.", 0, request_id=edit_id))
        succeeded(await guest.edit(client, PAGE, "Two.", 1))
        retried = succeeded(await guest.edit(client, PAGE, "One.", 0, request_id=edit_id))
        assert retried == {**first, "created": False}, "the retry answers revision 1 though the page is at 2"
        refused(await guest.edit(client, PAGE, "Other.", 0, request_id=edit_id), "idempotency_conflict")
        refused(await Caller(host).edit(client, PAGE, "One.", 0, request_id=edit_id), "idempotency_conflict")

        restored = succeeded(await guest.restore(client, PAGE, 1, 2, request_id=restore_id))
        assert (restored["revision"], restored["restored_from"], restored["created"]) == (3, 1, True)
        succeeded(await guest.edit(client, PAGE, "Four.", 3))
        again = succeeded(await guest.restore(client, PAGE, 1, 2, request_id=restore_id))
        assert again == {**restored, "created": False}
        for reused, arguments in (
            (restore_id, {"revision": 2, "expected_revision": 2}),
            (edit_id, {"revision": 1, "expected_revision": 4}),
        ):
            result = await guest.call(client, "restore_wiki", path=PAGE, request_id=reused, **arguments)
            refused(result, "idempotency_conflict", wiki.conflict(reused))
        refused(await guest.edit(client, PAGE, "Five.", 4, request_id=restore_id), "idempotency_conflict")
        assert [row["revision"] for row in wiki.revisions_of(host, PAGE)] == [1, 2, 3, 4]

    play(host, scenario)


BODIES = {
    "trailing whitespace": "Keep these spaces   \n\tand this tab\t \n\n   ",
    "non-ASCII": "naïve café, 日本語, é decomposed, Å precomposed, emoji 🧭 and a zero width​space ",
    "at the limit": "é" * (wiki.MAX_BODY // 2 - 2) + "ok  ",
}


@pytest.mark.parametrize("body", BODIES.values(), ids=BODIES.keys())
def test_a_restore_writes_the_exact_bytes_and_can_itself_be_restored(host, body):
    async def scenario(client):
        guest = await bound(client, host)
        succeeded(await guest.edit(client, PAGE, body, 0))
        succeeded(await guest.edit(client, PAGE, "Something else entirely.", 1))
        restored = succeeded(await guest.restore(client, PAGE, 1, 2))
        assert (restored["revision"], restored["base_revision"], restored["restored_from"]) == (3, 2, 1)
        assert (restored["size"], restored["author"]) == (len(body.encode()), guest.author)
        assert succeeded(await guest.call(client, "read_wiki", path=PAGE))["body"] == body

        succeeded(await guest.edit(client, PAGE, "Changed once more.", 3))
        twice = succeeded(await Caller(host).restore(client, PAGE, 3, 4))
        assert (twice["revision"], twice["restored_from"], twice["author"]) == (5, 3, {"role": "planner"})
        current = succeeded(await guest.restore(client, PAGE, 5, 5))
        assert (current["revision"], current["restored_from"]) == (6, 5), "a restore of the current is a revision"

        rows = wiki.revisions_of(host, PAGE)
        assert [row["restored_from"] for row in rows] == [None, None, 1, None, 3, 5]
        for row in (rows[0], rows[2], rows[4], rows[5]):
            assert (row["kind"], row["body"], bytes(row["bytes"])) == ("text", body, body.encode())

    play(host, scenario)


def test_a_restore_names_what_is_missing_and_compares_before_it_copies(host):
    async def scenario(client):
        planner = Caller(host)
        refused(await planner.restore(client, PAGE, 1, 1), "unknown_wiki_page", wiki.no_page(PAGE))
        succeeded(await planner.edit(client, PAGE, "One.", 0))
        succeeded(await planner.edit(client, PAGE, "Two.", 1))
        refused(await planner.restore(client, PAGE, 3, 2), "unknown_wiki_page", wiki.no_revision(PAGE, 3, 2))
        behind = refused(await planner.restore(client, PAGE, 1, 1), "stale_revision", wiki.stale(PAGE, 2, 1))
        assert behind["current_revision"] == 2
        # Behind the page and naming a revision it lacks: the caller learns where the page is first.
        refused(await planner.restore(client, PAGE, 9, 1), "stale_revision", wiki.stale(PAGE, 2, 1))
        refused(await planner.call(client, "read_wiki", path=PAGE, revision=3), "unknown_wiki_page")
        refused(await planner.call(client, "read_wiki", path="ops/none"), "unknown_wiki_page", wiki.no_page("ops/none"))
        refused(await planner.call(client, "wiki_history", path="ops/none"), "unknown_wiki_page")
        for name, arguments, argument in (
            ("restore_wiki", {"path": PAGE, "revision": 0, "expected_revision": 2}, "revision"),
            ("restore_wiki", {"path": PAGE, "revision": 1, "expected_revision": 0}, "expected_revision"),
            ("restore_wiki", {"path": PAGE, "revision": 1}, "expected_revision"),
            ("read_wiki", {"path": PAGE, "revision": True}, "revision"),
        ):
            wiki.refused_by_schema(await planner.call(client, name, **arguments), argument)
        assert len(wiki.revisions_of(host, PAGE)) == 2

    play(host, scenario)


def test_history_is_oldest_first_paged_as_reference_history_and_shows_restores(host):
    async def scenario(client):
        guest = await bound(client, host)
        for number in range(4):
            succeeded(await guest.edit(client, PAGE, f"Revision {number + 1}.", number))
        succeeded(await guest.restore(client, PAGE, 2, 4))
        whole = succeeded(await guest.call(client, "wiki_history", path=PAGE))
        assert set(whole) == wiki.HISTORY_KEYS and whole["current_revision"] == 5
        assert all(set(entry) == wiki.HISTORY_ENTRY_KEYS for entry in whole["revisions"])
        assert [(entry["revision"], entry["restored_from"]) for entry in whole["revisions"]] == [
            (1, None),
            (2, None),
            (3, None),
            (4, None),
            (5, 2),
        ]
        assert whole["revisions"][4]["size"] == len(b"Revision 2.")
        pages, after = [], 0
        while True:
            page = succeeded(await guest.call(client, "wiki_history", path=PAGE, after_revision=after, limit=2))
            if not page["revisions"]:
                assert page["next_revision"] == after
                break
            pages.append([entry["revision"] for entry in page["revisions"]])
            after = page["next_revision"]
        assert pages == [[1, 2], [3, 4], [5]]
        past = succeeded(await guest.call(client, "wiki_history", path=PAGE, after_revision=2**63))
        assert (past["revisions"], past["next_revision"]) == ([], 2**63)
        for limit in (0, 101):  # a core caller's, since the guard checks a call with a token against the schema itself
            wiki.refused_by_schema(await Caller(host).call(client, "wiki_history", path=PAGE, limit=limit), "limit")

    play(host, scenario)


@pytest.mark.parametrize("path", ["Ops", "ops/", "/ops", "a//b", "a/b/c/d/e", "-ops", "ops-", "o_ps", "x" * 41, ""])
def test_a_malformed_path_is_refused_on_every_tool_that_takes_one(host, path):
    async def scenario(client):
        planner = Caller(host)
        detail = f"path {wiki.PATH_RULE}"
        refused(await planner.edit(client, path, "Text.", 0), "invalid_request", detail)
        refused(await planner.restore(client, path, 1, 1), "invalid_request", detail)
        refused(await planner.call(client, "read_wiki", path=path), "invalid_request", detail)
        refused(await planner.call(client, "wiki_history", path=path), "invalid_request", detail)
        refused(await planner.call(client, "list_wiki", prefix=path), "invalid_request", f"prefix {wiki.PATH_RULE}")
        refused(await planner.call(client, "list_wiki", after=path), "invalid_request", f"after {wiki.PATH_RULE}")
        result = await planner.call(client, "search_wiki", query="word", prefix=path)
        refused(result, "invalid_request", f"prefix {wiki.PATH_RULE}")

    play(host, scenario)


def test_a_body_follows_the_reference_body_rule(host):
    async def scenario(client):
        planner = Caller(host)
        refused(
            await planner.edit(client, PAGE, "", 0), "invalid_request", "body must be text of 1-32768 bytes of UTF-8"
        )
        over = refused(
            await planner.edit(client, PAGE, "é" * (wiki.MAX_BODY // 2) + "!", 0),
            "limit_exceeded",
            "a wiki page body is at most 32768 bytes of UTF-8; this one is 32769",
        )
        assert over["limit"] == "wiki_body_bytes"
        spaces = succeeded(await planner.edit(client, PAGE, "   ", 0))
        assert spaces["size"] == 3, "text of spaces alone is a body"
        wiki.refused_by_schema(await planner.edit(client, PAGE, "x", -1), "expected_revision")
        refused(
            await planner.edit(client, PAGE, "x", 1, request_id=""), "invalid_request", "request_id is 1-200 characters"
        )
        wiki.refused_by_schema(await planner.call(client, "edit_wiki", path=PAGE, body="x"), "expected_revision")
        wiki.refused_by_schema(await planner.edit(client, PAGE, "x", "1"), "expected_revision")
        literal = succeeded(await planner.edit(client, "null", "null", 0, request_id="null"))
        assert (literal["path"], literal["request_id"]) == ("null", "null"), "arguments are taken as sent"
        assert succeeded(await planner.call(client, "read_wiki", path="null"))["body"] == "null"

    play(host, scenario)
