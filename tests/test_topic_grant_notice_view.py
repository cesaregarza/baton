"""The `topic_moved` host notice renders where people read threads: the dashboard's thread page and the seat.

The notice is the one a grantee's move writes (see test_topic_grant_scoped_tools.py for its exact shape):
`{"event": "topic_moved", "from", "to", "by", "alias", "alias_code", "grant_id"}`, with a sentence as its text and no
sender. These tests store such a notice the way the move does, through `delivery._fan_out`, with invented names,
and look at the pages. The dashboard names a notice's `participant`, which a topic move does not have, so it must
learn the new event. The seat shows a notice's text, so it needs no change and is only pinned.

A visual check of the real pages, with the same dummy data, is the reviewer's to make; these pin the content.
"""

from __future__ import annotations

import re
import secrets

from seat_helpers import conversation, get, log_in, make_seat_host

from baton import dashboard
from baton.conversations import delivery
from baton.database.store import connect, transaction

ALIAS, CODE = "lane-a", "ab12"
FROM, TO = "job-hunt/leads", "job-hunt/boards"


def store_moved_notice(host, conversation_id: str) -> str:
    """Write a topic_moved notice into the thread to its participants, and return the grant id it names."""
    grant_id = "tgr_" + secrets.token_hex(8)
    notice = {
        "event": "topic_moved",
        "from": FROM,
        "to": TO,
        "by": "guest via alias al_" + secrets.token_hex(8),
        "alias": ALIAS,
        "alias_code": CODE,
        "grant_id": grant_id,
    }
    text = f"alias {ALIAS} ({CODE}) moved this thread from {FROM} to {TO} under topic grant {grant_id}"
    conn = connect(host.config.db_path)
    try:
        with transaction(conn):
            delivery._fan_out(
                conn, conversation_id, ["orchestrator", "operator"], sender=None, text=text, notice=notice
            )
    finally:
        conn.close()
    return grant_id


def test_the_dashboard_thread_page_renders_a_topic_moved_notice_as_a_notice_with_where_and_why(tmp_path):
    host = make_seat_host(tmp_path)
    room = conversation(host)
    grant_id = store_moved_notice(host, room)
    config = host.config
    try:
        page = dashboard.respond(config, "GET", f"/c/{room}", f"127.0.0.1:{config.dashboard.listen_port}")
    except Exception as error:
        raise AssertionError(f"the thread page failed to render a topic_moved notice: {type(error).__name__}") from None
    assert page.status == 200, page.status
    body = page.body.decode()
    notices = re.findall(r'<li class="notice">(.*?)</li>', body, re.S)
    [shown] = [one for one in notices if ALIAS in one] or [""]
    for part in (ALIAS, CODE, FROM, TO, grant_id):
        assert part in shown, (part, shown)
    assert "was added" not in shown and "was removed" not in shown, shown


def test_the_seat_shows_a_topic_moved_notice_in_the_thread_and_the_inbox(tmp_path):
    """GUARD. The seat renders a host notice's text, so a topic_moved notice reads as the sentence it carries."""
    host = make_seat_host(tmp_path)
    room = conversation(host)
    grant_id = store_moved_notice(host, room)
    _, cookies = log_in(host.config, host.seat_token)
    for target in (f"/seat/c/{room}?after_seq=0", "/seat/inbox"):
        page = get(host.config, target, cookies=cookies)
        assert page.status == 200, (target, page.status)
        body = page.body.decode()
        for part in (ALIAS, CODE, FROM, TO, grant_id):
            assert part in body, (target, part)


def test_the_seat_wraps_a_notice_whose_values_have_no_break_points(tmp_path):
    """A guest picks the alias and the subtopic slugs a topic_moved notice names, and `/` is not a break point, so
    the seat's `.notice` rule must let a long unbroken line wrap, as its `.text` rule does."""
    host = make_seat_host(tmp_path)
    style = get(host.config, "/seat/style.css").body.decode()
    rules = re.findall(r"^\.notice \{([^}]*)\}", style, re.M)
    assert len(rules) == 1, rules
    assert "overflow-wrap: anywhere" in rules[0], rules[0]
