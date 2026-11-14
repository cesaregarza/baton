"""Finding and listing wiki pages, the wiki's one limit, and how its tables and index reach a database.

What this pins:

* search finds a word in a page's current text and never one only a replaced revision held; a hit is a pointer and a
  snippet with no body; `prefix` scopes it to a page and those under it; all of this with FTS5 and without it;
* the list pages through more than 100 pages in path order with `after`;
* the 10001st page is refused as `limit_exceeded` under its own limit name, and a full wiki can still be edited;
* a database at the previous schema upgrades to exactly the schema a fresh one is created with, and the index is
  filled from what is stored when FTS5 becomes available.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import wiki_support as wiki
from topic_grant_tools_support import play, refused, succeeded
from wiki_support import Caller, bound

from baton.database import store

FIXTURES = Path(__file__).parent / "fixtures"
PREVIOUS = 23  # the schema version before the wiki's


@pytest.fixture(params=["fts5", "like"])
def mode(request, monkeypatch):
    if request.param == "like":
        monkeypatch.setattr(store, "fts5_available", lambda conn: False)
    return request.param


@pytest.fixture
def host(tmp_path):
    return wiki.wiki_host(tmp_path)


def paths(result):
    return [hit["path"] for hit in result["hits"]]


def test_search_finds_only_current_text_and_answers_pointers_without_bodies(host, mode):
    async def scenario(client):
        guest = await bound(client, host)
        succeeded(await guest.edit(client, "ops/deploys", "The capybara runbook, step one.", 0))
        succeeded(await guest.edit(client, "ops/deploys", "The narwhal runbook, step two.", 1))
        succeeded(await Caller(host).edit(client, "ops", "Index of the procedures.", 0))
        found = succeeded(await guest.call(client, "search_wiki", query="narwhal"))
        assert found["keyword_index"] == mode
        [hit] = found["hits"]
        assert set(hit) == wiki.HIT_KEYS, "a hit is a pointer and a snippet, never the body"
        assert (hit["path"], hit["revision"], hit["author"]) == ("ops/deploys", 2, guest.author)
        assert "narwhal" in hit["snippet"] and "body" not in hit
        assert paths(succeeded(await guest.call(client, "search_wiki", query="capybara"))) == []
        assert sorted(paths(succeeded(await guest.call(client, "search_wiki", query="runbook")))) == ["ops/deploys"]

        succeeded(await guest.restore(client, "ops/deploys", 1, 2))
        assert paths(succeeded(await guest.call(client, "search_wiki", query="capybara"))) == ["ops/deploys"]
        assert paths(succeeded(await guest.call(client, "search_wiki", query="narwhal"))) == []
        if mode == "fts5":
            with wiki.database(host) as conn:
                assert conn.execute("SELECT COUNT(*) FROM wiki_fts").fetchone()[0] == 2, "one index row per page"

    play(host, scenario)


def test_a_prefix_scopes_search_to_a_page_and_the_pages_under_it(host, mode):
    async def scenario(client):
        planner = Caller(host)
        for path in ("ops", "ops/deploys", "ops/deploys/rollback", "ops-notes", "baton/ops"):
            succeeded(await planner.edit(client, path, f"The heron note for {path}.", 0))

        def under(prefix):
            return planner.call(client, "search_wiki", query="heron", prefix=prefix)

        assert sorted(paths(succeeded(await under("ops")))) == ["ops", "ops/deploys", "ops/deploys/rollback"]
        assert sorted(paths(succeeded(await under("ops/deploys")))) == ["ops/deploys", "ops/deploys/rollback"]
        assert paths(succeeded(await under("baton/ops"))) == ["baton/ops"]
        assert paths(succeeded(await under("nowhere"))) == []
        assert len(succeeded(await planner.call(client, "search_wiki", query="heron"))["hits"]) == 5
        assert len(succeeded(await planner.call(client, "search_wiki", query="heron", limit=2))["hits"]) == 2

    play(host, scenario)


def test_search_takes_the_message_searchs_query_rules_and_refusals(host, mode):
    async def scenario(client):
        planner = Caller(host)
        succeeded(await planner.edit(client, "ops", "one two three four five six seven eight", 0))
        words = "one two three four five six seven eight"
        assert paths(succeeded(await planner.call(client, "search_wiki", query=words))) == ["ops"]
        for query, detail in (
            (words + " nine", "a query is at most 8 words, and this one has 9"),
            ("needle " + " " * 200, "a query is at most 200 characters"),
            ("?!", "give at least one word to search for"),
        ):
            refused(await planner.call(client, "search_wiki", query=query), "invalid_request", detail)
        for limit in (0, 51):
            wiki.refused_by_schema(await planner.call(client, "search_wiki", query="one", limit=limit), "limit")

    play(host, scenario)


def test_the_list_pages_through_more_than_a_hundred_pages_in_path_order(host):
    names = [f"area-{index // 10}/page-{index % 10:02d}" for index in range(105)] + ["area-1", "zone"]

    async def scenario(client):
        planner, guest = Caller(host), await bound(client, host)
        for name in reversed(names):
            succeeded(await planner.edit(client, name, f"Notes for {name}.", 0))
        succeeded(await guest.edit(client, "zone", "The zone, revised.", 1))
        listed, after = [], None
        for _ in range(3):
            page = succeeded(await guest.call(client, "list_wiki", limit=100, **({"after": after} if after else {})))
            assert all(set(entry) == wiki.LISTED_KEYS for entry in page["pages"])
            listed += page["pages"]
            after = page["next_after"]
            if after is None:
                break
        assert [entry["path"] for entry in listed] == sorted(names), "every page once, in path order"
        assert len(listed) == 107 and after is None
        zone = listed[-1]
        assert (zone["current_revision"], zone["size"], zone["author"]) == (2, 18, guest.author)

        first = succeeded(await guest.call(client, "list_wiki"))
        assert len(first["pages"]) == 50 and first["next_after"] == first["pages"][-1]["path"]
        scoped = succeeded(await guest.call(client, "list_wiki", prefix="area-1"))
        assert [entry["path"] for entry in scoped["pages"]] == ["area-1", *[f"area-1/page-{n:02d}" for n in range(10)]]
        assert scoped["next_after"] is None
        exact = succeeded(await guest.call(client, "list_wiki", prefix="area-1", limit=11))
        assert exact["next_after"] is None, "a last page that is exactly full has nothing after it"
        tail = succeeded(await guest.call(client, "list_wiki", after="area-9/page-09"))
        assert [entry["path"] for entry in tail["pages"]] == ["zone"]
        for limit in (0, 101):
            wiki.refused_by_schema(await planner.call(client, "list_wiki", limit=limit), "limit")

    play(host, scenario)


def fill(host, count):
    """Store `count` pages straight into the tables, as that many edits would, with a revision each."""
    with wiki.database(host) as conn:
        wiki.require_tables(conn)
        with conn.transaction():
            conn.executemany(
                "INSERT INTO wiki_pages (path, created_at) VALUES (?, '2026-11-14T00:00:00+00:00')",
                [(f"filled/page-{index}",) for index in range(count)],
            )
            conn.execute(
                "INSERT INTO wiki_revisions (page_id, revision, body, author_role, request_id, request_digest,"
                " base_revision, at) SELECT id, 1, 'Filled.', 'planner', 'fill-' || id, 'fill', 0, created_at"
                " FROM wiki_pages WHERE path LIKE 'filled/%'"
            )


def test_the_ten_thousand_and_first_page_is_refused_under_its_own_limit(host):
    async def scenario(client):
        planner = Caller(host)
        succeeded(await planner.edit(client, "ops", "The one page made by a tool.", 0))
        fill(host, wiki.MAX_PAGES - 1)
        guest = await bound(client, host)
        full = refused(
            await guest.edit(client, "ops/new", "One too many.", 0),
            "limit_exceeded",
            f"the wiki already holds {wiki.MAX_PAGES} pages; edit one of them",
        )
        assert full["limit"] == "wiki_pages"
        assert succeeded(await guest.edit(client, "ops", "A full wiki is still editable.", 1))["revision"] == 2
        assert succeeded(await guest.edit(client, "filled/page-7", "So is this page.", 1))["revision"] == 2
        assert wiki.revisions_of(host, "ops/new") == []
        with wiki.database(host) as conn:
            assert conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0] == wiki.MAX_PAGES

    play(host, scenario)


def schema_of(path):
    raw = sqlite3.connect(path)
    try:
        return sorted(raw.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall(), key=str)
    finally:
        raw.close()


def test_a_database_at_the_previous_version_upgrades_to_the_schema_a_fresh_one_has(tmp_path):
    old, fresh = tmp_path / "old", tmp_path / "fresh"
    for directory in (old, fresh):
        directory.mkdir(mode=0o700)
    raw = sqlite3.connect(old / "baton.db")
    raw.executescript((FIXTURES / f"schema_v{PREVIOUS}.sql").read_text())
    raw.close()
    (old / "baton.db").chmod(0o600)
    for directory in (old, fresh):
        conn = store.connect(directory / "baton.db")
        wiki.require_tables(conn)
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == store.SCHEMA_VERSION > PREVIOUS
        assert conn.table_exists("wiki_fts") is store.fts5_available(conn)
        conn.close()
    assert schema_of(old / "baton.db") == schema_of(fresh / "baton.db")


def test_the_index_is_filled_from_current_text_when_fts5_becomes_available(tmp_path, monkeypatch):
    real_probe = store.fts5_available
    monkeypatch.setattr(store, "fts5_available", lambda conn: False)
    host = wiki.wiki_host(tmp_path)

    async def scanned(client):
        planner = Caller(host)
        succeeded(await planner.edit(client, "ops", "The ibis note.", 0))
        succeeded(await planner.edit(client, "ops", "The egret note.", 1))
        succeeded(await planner.edit(client, "baton", "Another egret.", 0))
        assert succeeded(await planner.call(client, "search_wiki", query="egret"))["keyword_index"] == "like"

    play(host, scanned)
    monkeypatch.setattr(store, "fts5_available", real_probe)

    async def ranked(client):
        planner = Caller(host)
        found = succeeded(await planner.call(client, "search_wiki", query="egret"))
        assert (found["keyword_index"], sorted(paths(found))) == ("fts5", ["baton", "ops"])
        assert paths(succeeded(await planner.call(client, "search_wiki", query="ibis"))) == []
        with wiki.database(host) as conn:
            assert conn.execute("SELECT COUNT(*) FROM wiki_fts").fetchone()[0] == 2

    play(host, ranked)
