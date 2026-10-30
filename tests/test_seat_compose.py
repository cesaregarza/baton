"""Compose: the seat sends as its own role, once, only into conversations it is in, bound to its own session."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from seat_helpers import (
    SESSION_COOKIE,
    agent_says,
    conversation,
    get,
    hidden,
    log_in,
    make_seat_host,
    post,
    stored_by_seat,
)

from baton import conversations
from baton.store import connect

try:
    from baton import seat
except ImportError:  # written before the module: each test then fails on its own instead of the whole run stopping
    seat = None  # type: ignore[assignment]


@pytest.fixture
def room(tmp_path: Path):
    host = make_seat_host(tmp_path)
    room = conversation(host)
    _, cookies = log_in(host.config, host.seat_token)
    return host, room, cookies


def _form(host, room, cookies, query: str = "") -> dict[str, str]:
    page = get(host.config, f"/seat/c/{room}{query}", cookies=cookies)
    assert page.status == 200
    fields = {"csrf": hidden(page.body, "csrf"), "request_id": hidden(page.body, "request_id")}
    if "reply=" in query:
        fields["reply_to"] = hidden(page.body, "reply_to")
    return fields


def test_a_send_is_stored_once_as_the_person_and_redirects_to_it(room) -> None:
    host, cid, cookies = room
    response = post(host.config, f"/seat/c/{cid}/send", _form(host, cid, cookies) | {"text": "Ship it."},
                    cookies=cookies)
    [(message_id, conversation_id, text, kind)] = stored_by_seat(host)
    assert (conversation_id, text, kind) == (cid, "Ship it.", "human")
    assert response.status == 303
    assert response.headers["Location"] == f"/seat/c/{cid}?after_seq=0#m-1"


def test_the_page_says_how_a_message_is_sent_and_never_calls_it_signed(room) -> None:
    host, cid, cookies = room
    page = get(host.config, f"/seat/c/{cid}", cookies=cookies)
    assert b"sent through authenticated seat" in page.body.lower()
    assert b"signed" not in page.body.lower()


def test_a_resubmitted_form_stores_one_copy(room) -> None:
    host, cid, cookies = room
    fields = _form(host, cid, cookies) | {"text": "Once only."}
    first = post(host.config, f"/seat/c/{cid}/send", fields, cookies=cookies)
    again = post(host.config, f"/seat/c/{cid}/send", fields, cookies=cookies)
    assert first.status == again.status == 303 and first.headers["Location"] == again.headers["Location"]
    assert len(stored_by_seat(host)) == 1


def test_the_same_request_id_with_other_text_is_a_conflict_that_keeps_the_text(room) -> None:
    host, cid, cookies = room
    fields = _form(host, cid, cookies)
    post(host.config, f"/seat/c/{cid}/send", fields | {"text": "First."}, cookies=cookies)
    response = post(host.config, f"/seat/c/{cid}/send", fields | {"text": "Second thoughts."}, cookies=cookies)
    assert response.status == 409 and b"Second thoughts." in response.body
    assert hidden(response.body, "request_id") != fields["request_id"]
    assert [row[2] for row in stored_by_seat(host)] == ["First."]


def test_a_refused_send_keeps_its_request_id_for_the_retry(tmp_path: Path) -> None:
    host = make_seat_host(tmp_path, max_per_hour=5)
    cid = conversation(host)
    _, cookies = log_in(host.config, host.seat_token)
    fields = _form(host, cid, cookies) | {"text": "Try again."}
    for _ in range(host.config.conversations.max_messages_per_hour):
        agent_says(host, cid, "noise")
    response = post(host.config, f"/seat/c/{cid}/send", fields, cookies=cookies)
    assert response.status == 429 and hidden(response.body, "request_id") == fields["request_id"]
    assert b"Try again." in response.body and stored_by_seat(host) == []


def test_a_request_id_from_another_session_is_refused(room) -> None:
    host, cid, cookies = room
    fields = _form(host, cid, cookies) | {"text": "Mine."}
    _, other = log_in(host.config, host.seat_token)
    other_fields = _form(host, cid, other)
    response = post(host.config, f"/seat/c/{cid}/send", fields | {"csrf": other_fields["csrf"]}, cookies=other)
    assert response.status == 403 and stored_by_seat(host) == []


def test_a_request_id_from_another_conversation_is_refused(room) -> None:
    host, cid, cookies = room
    elsewhere = conversation(host, title="Elsewhere")
    fields = _form(host, elsewhere, cookies) | {"text": "Wrong room."}
    response = post(host.config, f"/seat/c/{cid}/send", fields, cookies=cookies)
    assert response.status == 403 and stored_by_seat(host) == []


def test_a_removed_seat_gets_no_replay_of_an_earlier_send(room) -> None:
    host, _, cookies = room
    cid = conversation(host, participants=("orchestrator", "reviewer", "operator"), title="Three")
    fields = _form(host, cid, cookies) | {"text": "Before removal."}
    assert post(host.config, f"/seat/c/{cid}/send", fields, cookies=cookies).status == 303
    conn = connect(host.config.db_path)
    conversations.remove(conn, host.config, cid, "operator", actor="orchestrator")
    conn.close()
    response = post(host.config, f"/seat/c/{cid}/send", fields, cookies=cookies)
    assert response.status == 403 and b"no longer in" in response.body


def test_a_conversation_the_seat_is_not_in_shows_nothing(room) -> None:
    host, _, cookies = room
    private = conversation(host, participants=("orchestrator", "reviewer"), title="Private")
    agent_says(host, private, "not for the seat")
    page = get(host.config, f"/seat/c/{private}", cookies=cookies)
    assert page.status == 403 and b"not for the seat" not in page.body and b"Private" not in page.body


def test_a_reply_names_its_parent_and_an_unseen_parent_is_refused(room) -> None:
    host, cid, cookies = room
    parent = agent_says(host, cid, "Which layout first?")["message_id"]
    fields = _form(host, cid, cookies, f"?reply={parent}") | {"text": "Layout A."}
    assert fields["reply_to"] == parent
    assert post(host.config, f"/seat/c/{cid}/send", fields, cookies=cookies).status == 303
    fields = _form(host, cid, cookies) | {"text": "To nothing.", "reply_to": "msg_000000000000000000000000"}
    assert post(host.config, f"/seat/c/{cid}/send", fields, cookies=cookies).status == 400


def test_logout_ends_the_session(room) -> None:
    host, cid, cookies = room
    fields = _form(host, cid, cookies)
    assert post(host.config, "/seat/logout", {"csrf": fields["csrf"]}, cookies=cookies).status == 303
    assert get(host.config, "/seat/", cookies=cookies).status == 303
    assert post(host.config, f"/seat/c/{cid}/send", fields | {"text": "after logout"}, cookies=cookies).status == 303
    assert stored_by_seat(host) == []


@pytest.mark.parametrize("limit", ["idle", "absolute"])
def test_an_expired_session_is_refused_and_deleted(room, monkeypatch, limit: str) -> None:
    import sqlite3

    host, _, cookies = room
    start = seat._now()
    if limit == "absolute":  # keep it active, so only the absolute limit can end it
        step = host.config.seat.idle_seconds - 60
        moment = start
        while moment < start + host.config.seat.absolute_seconds:
            moment += step
            monkeypatch.setattr(seat, "_now", lambda m=moment: m)
            get(host.config, "/seat/", cookies=cookies)
    else:
        monkeypatch.setattr(seat, "_now", lambda: start + host.config.seat.idle_seconds + 1)
    assert get(host.config, "/seat/", cookies=cookies).status == 303
    assert sqlite3.connect(host.config.db_path).execute("SELECT COUNT(*) FROM seat_sessions").fetchone()[0] == 0


def test_no_token_session_csrf_or_text_reaches_a_log_record(room, caplog) -> None:
    host, cid, cookies = room
    caplog.set_level(logging.DEBUG)
    fields = _form(host, cid, cookies) | {"text": "a private sentence"}
    post(host.config, f"/seat/c/{cid}/send", fields, cookies=cookies)
    log_in(host.config, "baton_wrong-token-value")
    for secret in (host.seat_token, cookies[SESSION_COOKIE], fields["csrf"], "a private sentence",
                   "baton_wrong-token-value"):
        assert secret not in caplog.text
