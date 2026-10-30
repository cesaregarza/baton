"""Review findings on the seat: every message reachable by paging, bad input refused cleanly, limits that hold."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from seat_helpers import (
    SEAT_HOST,
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

try:
    from baton import seat
except ImportError:  # written before the module: each test then fails on its own instead of the whole run stopping
    seat = None  # type: ignore[assignment]


@pytest.fixture
def room(tmp_path: Path):
    host = make_seat_host(tmp_path)
    cid = conversation(host)
    _, cookies = log_in(host.config, host.seat_token)
    return host, cid, cookies


def _form(host, cid, cookies) -> dict[str, str]:
    page = get(host.config, f"/seat/c/{cid}", cookies=cookies)
    return {"csrf": hidden(page.body, "csrf"), "request_id": hidden(page.body, "request_id")}


def test_a_byte_limited_history_page_links_to_the_rest(room) -> None:
    host, cid, cookies = room
    for n in range(12):
        agent_says(host, cid, f"long {n:02d} " + "x" * 30_000)
    seen, target = set(), f"/seat/c/{cid}"
    for _ in range(12):
        body = get(host.config, target, cookies=cookies).body.decode()
        seen |= {n for n in range(12) if f"long {n:02d} " in body}
        marker = f'href="/seat/c/{cid}?after_seq='
        newer = [part.split('"')[0] for part in body.split(marker)[1:] if part.split("</a>")[0].endswith('">Newer')]
        if not newer:
            break
        target = f"/seat/c/{cid}?after_seq={newer[0]}"
    assert seen == set(range(12))


@pytest.mark.parametrize("value", ["é", "ñ" * 64, " "])
def test_a_non_ascii_csrf_value_is_refused_not_raised(room, value: str) -> None:
    host, cid, cookies = room
    fields = _form(host, cid, cookies) | {"text": "hi", "csrf": value}
    assert post(host.config, f"/seat/c/{cid}/send", fields, cookies=cookies).status == 403
    assert stored_by_seat(host) == []


def test_a_non_ascii_pre_login_value_is_refused_not_raised(room) -> None:
    host, _, _ = room
    form = get(host.config, "/seat/login")
    pre = [c.split(";", 1)[0].split("=", 1)[1] for c in form.cookies if c.startswith(seat.PRE_COOKIE + "=")][0]
    response = post(host.config, "/seat/login", {"token": host.seat_token, "pre": "é" + pre},
                    cookies={seat.PRE_COOKIE: pre})
    assert response.status == 403


@pytest.mark.parametrize("after", [str(2**63), "9" * 19])
def test_a_cursor_past_sqlite_range_is_a_client_error(room, after: str) -> None:
    host, cid, cookies = room
    assert get(host.config, f"/seat/c/{cid}?after_seq={after}", cookies=cookies).status == 400


def test_the_hourly_quota_holds_when_two_sends_overlap(room, monkeypatch) -> None:
    """At 59 sends, a second send that arrives while the first is being stored is refused: 60, never 61."""
    host, cid, cookies = room
    monkeypatch.setattr(seat, "SENDS_PER_HOUR", 3)
    for n in range(2):
        post(host.config, f"/seat/c/{cid}/send", _form(host, cid, cookies) | {"text": f"early {n}"}, cookies=cookies)
    first, second = _form(host, cid, cookies), _form(host, cid, cookies)
    real_send = seat.conversations.send
    overlapped = []

    def send_with_a_second_arriving(*args, **kwargs):
        if not overlapped:  # the second request lands after the first passed its check, before it is stored
            overlapped.append(None)
            overlapped[0] = post(host.config, f"/seat/c/{cid}/send", second | {"text": "second"}, cookies=cookies)
        return real_send(*args, **kwargs)

    monkeypatch.setattr(seat.conversations, "send", send_with_a_second_arriving)
    accepted = post(host.config, f"/seat/c/{cid}/send", first | {"text": "first"}, cookies=cookies)
    assert accepted.status == 303
    [refused] = overlapped
    assert refused.status == 429 and hidden(refused.body, "request_id") == second["request_id"]
    assert b"second" in refused.body
    assert [row[2] for row in stored_by_seat(host)] == ["early 0", "early 1", "first"]


def test_a_refused_send_frees_its_place_in_the_quota(room, monkeypatch) -> None:
    host, cid, cookies = room
    monkeypatch.setattr(seat, "SENDS_PER_HOUR", 1)
    bad = _form(host, cid, cookies) | {"text": "to nowhere", "reply_to": "msg_000000000000000000000000"}
    assert post(host.config, f"/seat/c/{cid}/send", bad, cookies=cookies).status == 400
    good = _form(host, cid, cookies) | {"text": "counted"}
    assert post(host.config, f"/seat/c/{cid}/send", good, cookies=cookies).status == 303


def test_a_resubmitted_form_is_not_counted_twice(room, monkeypatch) -> None:
    host, cid, cookies = room
    monkeypatch.setattr(seat, "SENDS_PER_HOUR", 1)
    fields = _form(host, cid, cookies) | {"text": "once"}
    assert post(host.config, f"/seat/c/{cid}/send", fields, cookies=cookies).status == 303
    assert post(host.config, f"/seat/c/{cid}/send", fields, cookies=cookies).status == 303
    assert len(stored_by_seat(host)) == 1


def _rows(host, session: str | None = None) -> int:
    conn = sqlite3.connect(host.config.db_path)
    if session is None:
        return conn.execute("SELECT COUNT(*) FROM seat_requests").fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM seat_requests WHERE session = ?", (session,)).fetchone()[0]


def test_form_ids_kept_per_session_never_pass_the_bound(room, monkeypatch) -> None:
    host, cid, cookies = room
    monkeypatch.setattr(seat, "MAX_REQUESTS", 10)
    monkeypatch.setattr(seat, "SENDS_PER_HOUR", 6)
    for n in range(7):
        post(host.config, f"/seat/c/{cid}/send", _form(host, cid, cookies) | {"text": f"m{n}"}, cookies=cookies)
    for _ in range(10):
        get(host.config, f"/seat/c/{cid}", cookies=cookies)
    assert _rows(host) <= 10
    assert len(stored_by_seat(host)) == 6
    # the latest form still works after the trimming
    assert post(host.config, f"/seat/c/{cid}/send", _form(host, cid, cookies) | {"text": "late"},
                cookies=cookies).status == 429  # the quota, not a lost binding


def test_form_ids_in_all_never_pass_the_bound(tmp_path: Path, monkeypatch) -> None:
    host = make_seat_host(tmp_path)
    cid = conversation(host)
    monkeypatch.setattr(seat, "MAX_REQUESTS", 5)
    monkeypatch.setattr(seat, "MAX_SESSIONS", 3)
    for _ in range(5):
        _, cookies = log_in(host.config, host.seat_token)
        for _ in range(8):
            get(host.config, f"/seat/c/{cid}", cookies=cookies)
    assert _rows(host) <= 3 * 5


def test_a_huge_cursor_is_refused_before_any_query(room) -> None:
    host, cid, cookies = room
    headers = [("Host", SEAT_HOST), ("Cookie", f"{SESSION_COOKIE}={cookies[SESSION_COOKIE]}")]
    assert seat.respond(host.config, "GET", f"/seat/c/{cid}?after_seq={2**64}", headers, b"").status == 400


def test_pages_keep_the_origin_on_same_origin_form_posts(room) -> None:
    """Under `no-referrer` Chromium sends `Origin: null` on a form post, which the exact Origin check refuses."""
    host, cid, cookies = room
    for page in (get(host.config, "/seat/login"), get(host.config, f"/seat/c/{cid}", cookies=cookies)):
        assert page.headers["Referrer-Policy"] == "same-origin"


@pytest.mark.parametrize("origin", ["null", "https://elsewhere.example.test"])
def test_a_null_or_foreign_origin_is_still_refused(room, origin: str) -> None:
    host, cid, cookies = room
    fields = _form(host, cid, cookies) | {"text": "hi"}
    assert post(host.config, f"/seat/c/{cid}/send", fields, cookies=cookies, origin=origin).status == 403
    assert stored_by_seat(host) == []


def test_each_inbox_item_names_its_conversation(room) -> None:
    host, cid, cookies = room
    other = conversation(host, title="Weekly notes")
    agent_says(host, cid, "from the first")
    agent_says(host, other, "from the second")
    body = get(host.config, "/seat/inbox", cookies=cookies).body.decode()
    first, second = body.index("from the first"), body.index("from the second")
    assert body.rfind("Example", 0, first) > body.rfind("</li>", 0, first)
    assert body.rfind("Weekly notes", 0, second) > body.rfind("</li>", 0, second)


def test_sends_older_than_the_hour_are_trimmed_too(room, monkeypatch) -> None:
    host, cid, cookies = room
    monkeypatch.setattr(seat, "MAX_REQUESTS", 10)
    for n in range(6):
        post(host.config, f"/seat/c/{cid}/send", _form(host, cid, cookies) | {"text": f"m{n}"}, cookies=cookies)
    start = seat._now()
    for minutes in (50, 100):  # the same session, kept alive, until those sends are more than an hour old
        monkeypatch.setattr(seat, "_now", lambda m=minutes: start + m * 60)
        assert get(host.config, "/seat/", cookies=cookies).status == 200
    for _ in range(12):
        get(host.config, f"/seat/c/{cid}", cookies=cookies)
    assert _rows(host) <= 10
