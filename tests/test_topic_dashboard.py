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

from baton import conversations, topics
from baton.config import Config
from baton.store import connect

ALIAS = {"alias_id": "al_0000000000000001", "alias": "lane-a", "badge": "guest", "code": "ab12",
         "effective_role": "guest"}
MINE = ["orchestrator", "reviewer", "operator"]


def seeded(tmp_path: Path, *, view_all: bool = True) -> tuple[Config, sqlite3.Connection]:
    config = page_config(tmp_path, view_all=view_all, guest=True)
    conn = connect(config.db_path)
    with conn:
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
        with conn:
            conn.execute("UPDATE conversations SET last_activity_at = ? WHERE id = ?",
                         ("2026-11-01T10:00:00+00:00", mine["conversation_id"]))
            conn.execute("UPDATE conversations SET last_activity_at = ? WHERE id = ?",
                         ("2026-11-02T10:00:00+00:00", theirs["conversation_id"]))
        page = text(get(config, "/"))
        assert "2026-11-01T10:00:00+00:00" in page, "the readable thread's time is what dates the topic"
        assert "2026-11-02T10:00:00+00:00" not in page, "the unreadable thread's time must not appear"
    finally:
        conn.close()

