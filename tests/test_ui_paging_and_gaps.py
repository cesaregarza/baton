"""Every readable conversation is reachable from the seat, and both views say where messages are no longer kept."""

from __future__ import annotations

import re
from pathlib import Path

from seat_helpers import agent_says, conversation, log_in, make_seat_host
from seat_helpers import get as seat_get
from test_dashboard_pages import MINE, made, page_config
from test_dashboard_pages import get as dashboard_get

from baton import conversations
from baton.store import connect


def test_every_readable_conversation_is_reachable_through_the_seat_pager(tmp_path: Path) -> None:
    host = make_seat_host(tmp_path)
    total = conversations.MAX_LISTED + 7
    titles = {f"Thread {n:03d}" for n in range(total)}
    for title in sorted(titles):
        conversation(host, title=title)
    _, cookies = log_in(host.config, host.seat_token)
    seen, target, visited = set(), "/seat/", set()
    while target and target not in visited:
        visited.add(target)
        body = seat_get(host.config, target, cookies=cookies).body.decode()
        seen |= set(re.findall(r">(Thread \d{3})</a>", body))
        older = re.search(r'href="(/seat/\?page=\d+)">Older conversations<', body)
        target = older.group(1).replace("&amp;", "&") if older else None
    assert seen == titles, f"{len(titles - seen)} conversations unreachable"


def test_the_seat_pager_refuses_a_page_that_is_not_a_number(tmp_path: Path) -> None:
    host = make_seat_host(tmp_path)
    _, cookies = log_in(host.config, host.seat_token)
    for page in ("0", "-1", "two", "1&page=2", "9" * 7):
        assert seat_get(host.config, f"/seat/?page={page}", cookies=cookies).status == 400


def _gap_between(body: str, before: str, after: str) -> str:
    """The text between two messages' words on a page, where a gap notice belongs."""
    return body[body.index(before) + len(before):body.index(after)]


def test_the_seat_says_where_messages_are_no_longer_kept(tmp_path: Path) -> None:
    host = make_seat_host(tmp_path)
    cid = conversation(host, participants=("orchestrator", "reviewer", "operator"))
    for word in ("first note", "second note", "third note", "fourth note", "fifth note"):
        agent_says(host, cid, word)
    conn = connect(host.config.db_path)
    conn.execute("DELETE FROM messages WHERE conversation_id = ? AND conversation_seq IN (2, 3)", (cid,))
    conn.close()
    _, cookies = log_in(host.config, host.seat_token)
    body = seat_get(host.config, f"/seat/c/{cid}", cookies=cookies).body.decode()
    assert "Messages #2–#3 are no longer kept" in _gap_between(body, "first note", "fourth note")
    assert "no longer kept" not in _gap_between(body, "fourth note", "fifth note")


def test_the_dashboard_says_where_messages_are_no_longer_kept(tmp_path: Path) -> None:
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    cid = made(conn, config, "With a gap", MINE)
    for word in ("first note", "second note", "third note", "fourth note"):
        conversations.send(conn, config, sender="orchestrator", conversation_id=cid, text=word)
    conn.execute("DELETE FROM messages WHERE conversation_id = ? AND conversation_seq = 3", (cid,))
    conn.close()
    body = dashboard_get(config, f"/c/{cid}").body.decode()
    assert "Message #3 is no longer kept" in _gap_between(body, "second note", "fourth note")
    assert "no longer kept" not in _gap_between(body, "first note", "second note")
