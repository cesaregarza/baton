"""The seat's inbox: reading is neutral, and only an explicit acknowledgment of one rendered page moves the cursor."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from seat_helpers import agent_says, conversation, get, hidden, log_in, mailbox, make_seat_host, post, stored_by_seat

from baton import conversations
from baton.errors import ConversationFull
from baton.store import connect

try:
    from baton import seat
except ImportError:  # written before the module: each test then fails on its own instead of the whole run stopping
    seat = None  # type: ignore[assignment]


@pytest.fixture
def inbox_host(tmp_path: Path):
    host = make_seat_host(tmp_path)
    room = conversation(host)
    _, cookies = log_in(host.config, host.seat_token)
    return host, room, cookies


def _ack(host, cookies, page):
    return post(host.config, "/seat/ack", {"receipt": hidden(page.body, "receipt"), "csrf": hidden(page.body, "csrf")},
                cookies=cookies)


def test_reading_the_inbox_moves_nothing(inbox_host) -> None:
    host, room, cookies = inbox_host
    for n in range(3):
        agent_says(host, room, f"update {n}")
    before = mailbox(host)
    page = get(host.config, "/seat/inbox", cookies=cookies)
    assert page.status == 200 and b"update 0" in page.body and b"update 2" in page.body
    get(host.config, f"/seat/c/{room}", cookies=cookies)
    assert mailbox(host) == before == (3, 0)


def test_acknowledging_a_page_moves_exactly_that_page(inbox_host) -> None:
    host, room, cookies = inbox_host
    for n in range(3):
        agent_says(host, room, f"update {n}")
    page = get(host.config, "/seat/inbox", cookies=cookies)
    agent_says(host, room, "arrived while the page was open")
    assert _ack(host, cookies, page).status == 303
    assert mailbox(host) == (4, 3)  # the late arrival stays unread
    assert b"arrived while the page was open" in get(host.config, "/seat/inbox", cookies=cookies).body


def test_a_page_is_at_most_fifty_and_acknowledges_only_what_it_showed(inbox_host) -> None:
    host, room, cookies = inbox_host
    for n in range(70):
        agent_says(host, room, f"step {n}")
    page = get(host.config, "/seat/inbox", cookies=cookies)
    assert b"step 49" in page.body and b"step 50" not in page.body
    _ack(host, cookies, page)
    assert mailbox(host) == (70, 50)


def test_a_byte_limited_page_acknowledges_its_actual_last_item(inbox_host) -> None:
    host, room, cookies = inbox_host
    for n in range(12):
        agent_says(host, room, f"long {n:02d} " + "x" * 30_000)
    page = get(host.config, "/seat/inbox", cookies=cookies)
    shown = sum(f"long {n:02d} ".encode() in page.body for n in range(12))
    assert 0 < shown < 12
    _ack(host, cookies, page)
    assert mailbox(host) == (12, shown)


def test_a_stale_receipt_is_refused_and_moves_nothing(inbox_host) -> None:
    host, room, cookies = inbox_host
    agent_says(host, room, "one")
    first = get(host.config, "/seat/inbox", cookies=cookies)
    second = get(host.config, "/seat/inbox", cookies=cookies)
    assert _ack(host, cookies, first).status == 303
    agent_says(host, room, "two")
    response = _ack(host, cookies, second)  # it started at a cursor that has since moved
    assert response.status == 409 and b"out of date" in response.body
    assert mailbox(host) == (2, 1)


def test_a_receipt_is_bound_to_its_session(inbox_host) -> None:
    host, room, cookies = inbox_host
    agent_says(host, room, "one")
    page = get(host.config, "/seat/inbox", cookies=cookies)
    _, other = log_in(host.config, host.seat_token)
    other_page = get(host.config, "/seat/inbox", cookies=other)
    response = post(host.config, "/seat/ack", {"receipt": hidden(page.body, "receipt"),
                                               "csrf": hidden(other_page.body, "csrf")}, cookies=other)
    assert response.status in (403, 409) and mailbox(host) == (1, 0)


def test_an_expired_receipt_is_refused(inbox_host, monkeypatch) -> None:
    host, room, cookies = inbox_host
    agent_says(host, room, "one")
    page = get(host.config, "/seat/inbox", cookies=cookies)
    now = seat._now()
    monkeypatch.setattr(seat, "_now", lambda: now + seat.RECEIPT_TTL + 1)
    response = _ack(host, cookies, page)
    assert response.status in (303, 409) and mailbox(host) == (1, 0)


def test_a_receipt_works_once(inbox_host) -> None:
    host, room, cookies = inbox_host
    agent_says(host, room, "one")
    page = get(host.config, "/seat/inbox", cookies=cookies)
    assert _ack(host, cookies, page).status == 303
    assert _ack(host, cookies, page).status == 409


def test_messages_from_a_conversation_the_seat_left_are_content_free_and_acknowledged(inbox_host) -> None:
    host, room, cookies = inbox_host
    other = conversation(host, participants=("orchestrator", "reviewer", "operator"), title="Left behind")
    agent_says(host, other, "private words in the conversation it left")
    agent_says(host, room, "still here")
    conn = connect(host.config.db_path)
    conversations.remove(conn, host.config, other, "operator", actor="orchestrator")
    conn.close()
    page = get(host.config, "/seat/inbox", cookies=cookies)
    assert b"private words" not in page.body and b"no longer in" in page.body
    assert b"still here" in page.body
    _ack(host, cookies, page)
    last, read = mailbox(host)
    assert read == last  # the placeholder doesn't stall the mailbox


def test_a_full_mailbox_blocks_sends_until_the_seat_acknowledges(inbox_host) -> None:
    host, room, cookies = inbox_host
    from baton import messages

    while messages.unread(connect(host.config.db_path), "operator") < messages.MAX_UNREAD:
        agent_says(host, room, "filler")
    with pytest.raises(ConversationFull):
        agent_says(host, room, "one too many")
    for _ in range(messages.MAX_UNREAD // 50):
        page = get(host.config, "/seat/inbox", cookies=cookies)
        assert _ack(host, cookies, page).status == 303
    agent_says(host, room, "accepted again")


def test_acknowledging_approves_nothing(inbox_host) -> None:
    host, room, cookies = inbox_host
    agent_says(host, room, "Ship it?", attention=["operator"], needs_decision=True)
    conn = sqlite3.connect(host.config.db_path)
    events_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    _ack(host, cookies, get(host.config, "/seat/inbox", cookies=cookies))
    assert stored_by_seat(host) == []  # no reply was sent for the person
    assert conn.execute("SELECT COUNT(*) FROM events WHERE actor = 'operator'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before


def test_the_inbox_warns_before_the_mailbox_fills(inbox_host) -> None:
    host, room, cookies = inbox_host
    for _ in range(400):
        agent_says(host, room, "filler")
    assert b"stop accepting messages at 500 unread" in get(host.config, "/seat/", cookies=cookies).body
