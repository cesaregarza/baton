"""The dashboard once a topic is a container: a directory on the index, a header on a topic page, a 404.

A topic that does not exist is now a missing page rather than an empty list, which is the difference
between a label and a container. A guest's thread says which alias started it, so a reader can tell a
thread a core role opened from one a guest did. Everything stays read-only and still shows only what
the configured viewer may already see.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_dashboard import text
from test_dashboard_pages import get, page_config

from baton import conversations, dashboard, topics
from baton.config import Config
from baton.database.store import connect

ALIAS = {"alias_id": "al_0000000000000001", "alias": "lane-a", "badge": "guest", "code": "ab12",
         "effective_role": "guest"}
MINE = ["orchestrator", "reviewer", "operator"]


def seeded(tmp_path: Path, *, view_all: bool = True) -> tuple[Config, sqlite3.Connection]:
    config = page_config(tmp_path, view_all=view_all, guest=True)
    conn = connect(config.db_path)
    with conn.transaction():
        conn.execute("INSERT INTO aliases (id, name, skeleton, verifier, created_at)"
                     " VALUES (?, 'lane-a', 'lane-a', 'synthetic', 0.0)", (ALIAS["alias_id"],))
    topics.create(conn, config, actor="orchestrator", path="baton", title="The board",
                  description="Where the work is discussed", members=["orchestrator", "reviewer"])
    topics.create(conn, config, actor="orchestrator", path="baton/development", members=MINE)
    return config, conn


@pytest.fixture
def host(tmp_path: Path):
    config, conn = seeded(tmp_path)
    yield config, conn
    conn.close()


def test_an_unknown_topic_is_not_found(host):
    config, _ = host
    answer = get(config, "/t/absent")
    assert answer.status == 404, "a topic that does not exist is a missing page, not an empty list"
    assert get(config, "/t/baton").status == 200


def test_an_unknown_subtopic_is_not_found(host):
    config, _ = host
    assert get(config, "/t/baton/absent").status == 404
    assert get(config, "/t/baton/development").status == 200


def test_a_malformed_topic_path_is_still_a_bad_request(host):
    """The grammar check comes first, so a path that is not a topic at all stays a 400."""
    config, _ = host
    assert get(config, "/t/Absent").status == 400


def test_the_index_is_a_directory_of_topics(host):
    config, conn = host
    conversations.create(conn, config, actor="orchestrator", title="A thread", participants=MINE,
                         topic="baton")
    page = text(get(config, "/"))
    assert "The board" in page, "a topic's title heads its entry"
    assert "Where the work is discussed" in page
    assert "/t/baton" in page
    assert "/t/baton/development" in page, "a subtopic is listed under its parent"


def test_the_index_counts_threads_and_keeps_the_untopiced(host):
    config, conn = host
    conversations.create(conn, config, actor="orchestrator", title="Filed", participants=MINE, topic="baton")
    conversations.create(conn, config, actor="orchestrator", title="Loose", participants=MINE)
    page = text(get(config, "/"))
    assert "Loose" in page, "a conversation with no topic is still reachable from the index"
    assert "No topic" in page


def test_a_topic_page_heads_with_its_own_details(host):
    config, conn = host
    conversations.create(conn, config, actor="orchestrator", title="A thread", participants=MINE,
                         topic="baton")
    page = text(get(config, "/t/baton"))
    assert "The board" in page
    assert "Where the work is discussed" in page
    assert "orchestrator" in page, "the topic says who opened it and who its members are"
    assert "/t/baton/development" in page, "its subtopics come before its threads"
    assert "A thread" in page


def test_a_topic_page_does_not_load_the_whole_directory(host, monkeypatch):
    config, conn = host
    conversations.create(conn, config, actor="orchestrator", title="A thread", participants=MINE,
                         topic="baton")

    def unexpected_directory(*args, **kwargs):
        pytest.fail("A topic page should not query the whole topic directory")

    monkeypatch.setattr(topics, "listing", unexpected_directory)
    response = get(config, "/t/baton")
    assert response.status == 200
    page = text(response)
    assert "The board" in page and "Where the work is discussed" in page
    assert "A thread" in page and "/t/baton/development" in page


def test_a_guest_thread_shows_the_alias_that_started_it(host):
    config, conn = host
    conversations.create(conn, config, actor="guest", title="A guest thread", participants=None,
                         topic="baton", identity=ALIAS)
    for target in ("/", "/t/baton"):
        page = text(get(config, target))
        assert "lane-a" in page, f"{target} must name the alias"
        assert "ab12" in page, f"{target} must show its code"


def test_a_core_thread_names_no_alias(host):
    config, conn = host
    conversations.create(conn, config, actor="orchestrator", title="Core thread", participants=MINE,
                         topic="baton")
    page = text(get(config, "/t/baton"))
    assert "Core thread" in page
    assert "lane-a" not in page


def test_a_topic_page_never_widens_what_the_viewer_sees(tmp_path):
    """The directory counts and the listing both stay the viewer's own, exactly as before."""
    config, conn = seeded(tmp_path, view_all=False)
    try:
        conversations.create(conn, config, actor="orchestrator", title="Not mine",
                             participants=["orchestrator", "assistant"], topic="baton")
        assert "Not mine" not in text(get(config, "/t/baton"))
        assert "Not mine" not in text(get(config, "/"))
    finally:
        conn.close()


def test_a_topic_with_no_threads_still_has_a_page(host):
    config, _ = host
    answer = get(config, "/t/baton/development")
    assert answer.status == 200
    assert "None under this topic." in text(answer)

def test_the_directory_offers_a_restricted_viewer_only_what_it_can_see_into(tmp_path):
    """A topic whose only thread is hidden from the viewer is not named on the index either.

    The page has always shown what the viewer may see, and a directory built from the topics table
    could have widened that by naming a topic the viewer has no thread in. A board-reading viewer is
    different: it can see every thread anyway, so an empty topic is safe to list for it and is what
    makes the directory usable.
    """
    config, conn = seeded(tmp_path, view_all=False)
    try:
        topics.create(conn, config, actor="orchestrator", path="hidden", members=MINE)
        conversations.create(conn, config, actor="orchestrator", title="Not mine",
                             participants=["orchestrator", "assistant"], topic="hidden")
        conversations.create(conn, config, actor="orchestrator", title="Mine", participants=MINE,
                             topic="baton")
        page = text(get(config, "/"))
        assert "/t/baton" in page
        assert "/t/hidden" not in page, "a topic the viewer sees no thread in is not offered"
        assert "Not mine" not in page
    finally:
        conn.close()

    opened, board = seeded(tmp_path / "board", view_all=True)
    try:
        assert "/t/baton/development" in text(get(opened, "/")), "a board reader sees an empty topic"
    finally:
        board.close()

def test_the_directory_never_renders_an_unreadable_threads_activity(tmp_path):
    """The rendered page must not date a topic from a thread this viewer cannot read.

    The directory prints the time `topics.listing` gives it, so a time aggregated over every thread
    would tell a restricted viewer that something happened in a conversation it is not in, and when.
    """
    config, conn = seeded(tmp_path, view_all=False)
    try:
        mine = conversations.create(conn, config, actor="orchestrator", title="Mine",
                                    participants=MINE, topic="baton")
        theirs = conversations.create(conn, config, actor="orchestrator", title="Not mine",
                                      participants=["orchestrator", "assistant"], topic="baton")
        with conn.transaction():
            conn.execute("UPDATE conversations SET last_activity_at = ? WHERE id = ?",
                         ("2026-11-01T10:00:00+00:00", mine["conversation_id"]))
            conn.execute("UPDATE conversations SET last_activity_at = ? WHERE id = ?",
                         ("2026-11-02T10:00:00+00:00", theirs["conversation_id"]))
        page = text(get(config, "/"))
        assert "2026-11-01T10:00:00+00:00" in page, "the readable thread's time is what dates the topic"
        assert "2026-11-02T10:00:00+00:00" not in page, "the unreadable thread's time must not appear"
    finally:
        conn.close()

# The direct topic route, and what a page a restricted viewer CAN open may say about one it cannot.

def subtree(tmp_path, *, view_all: bool, where: str):
    """A parent topic and a subtopic, with one readable thread filed in `where` and one unreadable
    thread filed in the other. The reader is the dashboard's viewer."""
    config, conn = seeded(tmp_path, view_all=view_all)
    topics.create(conn, config, actor="orchestrator", path="work", members=MINE)
    topics.create(conn, config, actor="orchestrator", path="work/open", members=MINE)
    readable_in, hidden_in = ("work", "work/open") if where == "parent" else ("work/open", "work")
    conversations.create(conn, config, actor="orchestrator", title="Readable", participants=MINE,
                         topic=readable_in)
    conversations.create(conn, config, actor="orchestrator", title="Hidden",
                         participants=["orchestrator", "assistant"], topic=hidden_in)
    return config, conn


def test_a_topic_with_nothing_readable_in_it_is_not_found(tmp_path):
    """Least disclosure: the page a restricted viewer cannot see into does not exist for it, so not even
    the topic's existence, description or membership is readable by guessing the path."""
    config, conn = seeded(tmp_path, view_all=False)
    try:
        topics.create(conn, config, actor="orchestrator", path="secret", description="Vendor deal",
                      members=MINE)
        conversations.create(conn, config, actor="orchestrator", title="Not mine",
                             participants=["orchestrator", "assistant"], topic="secret")
        answer = get(config, "/t/secret")
        assert answer.status == 404
        assert "Vendor deal" not in text(answer)
    finally:
        conn.close()


def test_a_board_reader_still_opens_an_empty_topic(tmp_path):
    """The directory has to stay usable for the viewer it is built for."""
    config, conn = seeded(tmp_path, view_all=True)
    try:
        assert get(config, "/t/baton/development").status == 200
    finally:
        conn.close()


def test_a_restricted_viewer_opens_a_topic_it_has_a_thread_in(tmp_path):
    config, conn = seeded(tmp_path, view_all=False)
    try:
        conversations.create(conn, config, actor="orchestrator", title="Mine", participants=MINE,
                             topic="baton")
        answer = get(config, "/t/baton")
        assert answer.status == 200
        assert "Mine" in text(answer)
    finally:
        conn.close()


def test_a_parent_is_openable_when_only_its_subtopic_is_readable(tmp_path):
    """Otherwise the directory, which keeps such a parent so its subtopic is not orphaned, would link
    to a page that refuses. Opening it says only that something in the subtree is readable, which the
    subtopic itself already says."""
    config, conn = subtree(tmp_path, view_all=False, where="child")
    try:
        assert get(config, "/t/work").status == 200
        assert get(config, "/t/work/open").status == 200
        parent = text(get(config, "/t/work"))
        assert "Hidden" not in parent, "the parent's own unreadable thread stays hidden"
        assert "/t/work/open" in parent, "its readable subtopic is still offered"
    finally:
        conn.close()


def test_a_page_never_links_a_subtopic_the_viewer_cannot_open(tmp_path):
    """The subtopic list is the remaining way a topic page could name something unreadable."""
    config, conn = subtree(tmp_path, view_all=False, where="parent")
    try:
        page = text(get(config, "/t/work"))
        assert "Readable" in page
        assert "/t/work/open" not in page, "a subtopic with nothing readable must not be linked"
        assert get(config, "/t/work/open").status == 404
    finally:
        conn.close()

# Presentation: a heading that outranks the one above it, a name printed twice, and a dead end.

def rule_size(selector: str) -> float | None:
    """The largest font-size in rem that any rule naming `selector` sets, or None if none does.

    Resolved by looking for the selector as one entry in each rule's comma-separated list, rather than
    by matching a literal selector string: the stylesheet groups and regroups selectors as it is
    restyled, and a test pinned to one grouping breaks without anything rendering differently. The body
    pattern excludes braces, so an `@media` wrapper is stepped over and its inner rules are read.
    """
    import re
    sizes = []
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", dashboard.STYLE):
        if selector not in [one.strip() for one in selectors.split(",")]:
            continue
        size = re.search(r"font-size:\s*([0-9.]+)rem", body)
        if size:
            sizes.append(float(size.group(1)))
    return max(sizes) if sizes else None


def test_a_subtopics_heading_does_not_outrank_the_heading_above_it(tmp_path):
    """h3 sits under h2, so it must not render larger.

    Unstyled it falls back to the browser's 1.17rem bold black and reads as the more important of the
    two. It is held down by a class the stylesheet already defines, not by a new rule for h3, so a
    restyle of the sheet elsewhere has nothing of this to merge.
    """
    config, conn = seeded(tmp_path, view_all=True)
    try:
        assert '<h3 class="meta">Subtopics</h3>' in text(get(config, "/t/baton"))
    finally:
        conn.close()
    assert rule_size("h2") is not None, "h2 is styled, so the comparison is meaningful"
    assert rule_size(".meta") <= rule_size("h2")


def test_a_topic_named_only_by_its_path_shows_that_name_once(tmp_path):
    """A topic whose title is just its last segment printed the same word as link and as metadata."""
    config, conn = seeded(tmp_path, view_all=True)
    try:
        topics.create(conn, config, actor="orchestrator", path="plain", members=MINE)
        conversations.create(conn, config, actor="orchestrator", title="A thread", participants=MINE,
                             topic="plain")
        page = text(get(config, "/"))
        entry = page[page.index('href="/t/plain"'):]
        entry = entry[:entry.index("</li>")]
        # The visible words only: the href has to carry the path, and that is not what reads as repetition.
        import re
        shown = re.sub(r"<[^>]*>", " ", entry[entry.index(">") + 1:])
        assert shown.count("plain") == 1, f"the name is shown {shown.count('plain')} times: {shown.strip()}"
    finally:
        conn.close()


def test_a_bad_request_page_offers_a_way_back(tmp_path):
    """A refusal with no link is a dead end; the not-found page already offers one."""
    config, conn = seeded(tmp_path, view_all=True)
    try:
        for target in ("/t/Bad", "/?page=x", f"/c/{'x' * 10}?after_seq=nope"):
            body = text(get(config, target))
            assert 'href="/"' in body, f"{target} gives no way back: {body[-200:]}"
    finally:
        conn.close()

def test_the_directory_counts_a_roots_subtree(tmp_path):
    """A root whose threads all sit in a subtopic showed 0, which read as an empty topic."""
    config, conn = subtree(tmp_path, view_all=False, where="child")
    try:
        page = text(get(config, "/"))
        entry = page[page.index('href="/t/work"'):]
        entry = entry[:entry.index("</li>")]
        assert "1 thread" in entry, f"the root should count its subtree: {entry}"
        assert "0 thread" not in entry
    finally:
        conn.close()

