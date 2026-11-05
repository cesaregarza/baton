# ruff: noqa: F811 - the receiver and events_host fixtures are imported from test_openai_mcp_events
"""Credentials and the records about them are kept by state instead of deleted.

Each test names one record that used to be deleted, expired or overwritten, and says it is still stored, marked
with how it ended, and still refused. A test whose name starts with `test_guard_` asserts a refusal that held
before, when the record was gone, and must go on holding now that it is kept.

A record kept in a table this change adds is read through `table_rows`, which finds nothing while the table does
not exist, and delivery receipts and outcomes are looked for both in the integration's state file, where they
used to be, and in their tables. So on the code before this change every other test fails by assertion.

The one record still deleted is a client registration left unused for a day. It is refused from the moment it
lapses and deleted, with its client secret, by the next write, and never moves to the ended record.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from conftest import REDIRECT, make_config
from mcp.server.auth.provider import RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from seat_helpers import (
    PRE_COOKIE,
    SESSION_COOKIE,
    agent_says,
    conversation,
    cookie_value,
    get,
    hidden,
    log_in,
    mailbox,
    make_seat_host,
    post,
    stored_by_seat,
)
from test_aliases import dispatch as call_tools
from test_auth import authorize, register
from test_index_writer import artifacts_for, begin, converse, index_config, read_all
from test_openai_mcp_events import (
    dispatch,
    events_host,  # noqa: F401 - fixture
    new_secret,
    receiver,  # noqa: F401 - fixture
    send,
    state,
    subscribe,
)
from test_per_call_identity import OTHER, build_host, claim, owner

from baton import auth, datadir, derived, seat, wakes
from baton.database import store
from baton.database.store import connect
from baton.errors import BatonError
from baton.integrations import openai_mcp_events as integration

LAPSED = auth.UNUSED_CLIENT_TTL + 60  # how long ago a registration that has lapsed arrived
LONG_AGO = 946_684_800  # 2000-01-01, long before any file a test writes, so only a rewrite moves a file off it
FIXTURES = Path(__file__).parent / "fixtures"


def table_rows(path: Path, sql: str, *args: Any) -> list[tuple]:
    """Rows of a table this change adds; none while that table does not exist."""
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error):
            raise
        return []
    finally:
        conn.close()


def count(path: Path, sql: str, *args: Any) -> int:
    return table_rows(path, sql, *args)[0][0]


# OAuth ---------------------------------------------------------------------------------------------------------


def oauth_state(host) -> dict[str, Any]:
    path = host.config.oauth_state_path
    return json.loads(path.read_text()) if path.exists() else {}


def ended_path(host) -> Path:
    return host.config.data_dir / "oauth-ended.jsonl"


def ended_record(host) -> list[dict[str, Any]]:
    path = ended_path(host)
    return [json.loads(line) for line in path.read_text().split("\n") if line] if path.exists() else []


def age(host, change) -> None:
    """Rewrite the stored OAuth state as time passing would: no OAuth write runs, so nothing acts on it yet."""
    state = oauth_state(host)
    change(state)
    with datadir.opened(host.config.data_dir) as dir_fd:
        datadir.replace_file(dir_fd, host.config.oauth_state_path.name, json.dumps(state).encode())


def lapse(client_id: str, seconds_ago: float = LAPSED):
    return lambda state: state["unused"].update({client_id: time.time() - seconds_ago})


def stamp(host) -> tuple[str, int, int]:
    """The state file's digest, inode and modification time. A rewrite replaces the file, so it moves the inode and
    the time even when it writes the same bytes."""
    path = host.config.oauth_state_path
    info = os.stat(path)
    return hashlib.sha256(path.read_bytes()).hexdigest(), info.st_ino, info.st_mtime_ns


def pinned(host) -> tuple[str, int, int]:
    """The state file's stamp after setting a modification time long past, which only a rewrite replaces."""
    os.utime(host.config.oauth_state_path, (LONG_AGO, LONG_AGO))
    return stamp(host)


def register_with_secret(host) -> dict[str, Any]:
    """A client registered for the default method, so the host mints and stores a plaintext client secret."""
    response = httpx.post(host.base + "/register", json={"redirect_uris": [REDIRECT], "client_name": "Example client"})
    assert response.status_code == 201 and response.json()["client_secret"], response.text
    return response.json()


def authorize_status(host, client_id: str) -> int:
    challenge = hashlib.sha256(b"a synthetic code verifier for the tests").hexdigest()
    return httpx.get(host.base + "/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": "s1", "resource": host.mcp_url}).status_code


def refresh(host, tokens: dict[str, Any]) -> httpx.Response:
    return httpx.post(host.base + "/token", data={"grant_type": "refresh_token", "client_id": tokens["client_id"],
                                                  "refresh_token": tokens["refresh_token"]})


def mcp_status(host, access_token: str) -> int:
    return httpx.post(host.mcp_url, json={}, headers={"Authorization": f"Bearer {access_token}"}).status_code


def refusal(operation, *args: Any) -> str | None:
    """The OAuth error code a provider call refused with, or None when it went through.

    Any other exception comes back as its type's name. The SDK turns only an OAuth error into a refusal, and answers
    anything else with a server error, so that is the wrong refusal rather than a pass.
    """
    try:
        anyio.run(operation, *args)
    except (TokenError, RegistrationError) as error:
        return error.error
    except Exception as error:
        return type(error).__name__
    return None


def test_a_lapsed_registration_is_refused_and_not_counted(start_host, monkeypatch) -> None:
    monkeypatch.setattr(auth, "MAX_CLIENTS", 2)
    host = start_host(oauth=True)
    lapsed = register(host).json()["client_id"]
    assert register(host).status_code == 201  # the cap is now full
    age(host, lapse(lapsed))

    assert authorize_status(host, lapsed) == 400, "refused from the moment it lapses, before any write"
    assert register(host).status_code == 201, "it does not count against the cap"
    assert lapsed not in oauth_state(host)["clients"], "and that write deleted it"
    over = register(host)
    assert (over.status_code, over.json()) == (429, {"error": "too_many_clients"})
    assert len(oauth_state(host)["clients"]) == 2, "nothing is stored for the refusal"


def test_guard_a_lapsed_registration_cannot_authorize_after_a_write(start_host) -> None:
    host = start_host(oauth=True)
    lapsed = register(host).json()["client_id"]
    age(host, lapse(lapsed))
    assert register(host).status_code == 201

    assert authorize_status(host, lapsed) == 400


def test_a_registration_unused_for_a_day_is_refused_at_once_and_deleted_by_the_next_write(start_host) -> None:
    host = start_host(oauth=True)
    client = register_with_secret(host)
    age(host, lapse(client["client_id"]))

    assert authorize_status(host, client["client_id"]) == 400, "refused from the moment it lapses"
    assert client["client_id"] in oauth_state(host)["clients"], "and still stored: only a write deletes it"

    assert register(host).status_code == 201  # the next write

    stored = oauth_state(host)
    assert client["client_id"] not in stored["clients"] and client["client_id"] not in stored["unused"], "deleted"
    assert client["client_secret"] not in host.config.oauth_state_path.read_text(), "with its client secret"


def test_a_registration_that_was_used_is_never_deleted_for_going_unused(start_host, monkeypatch) -> None:
    host = start_host(oauth=True)
    used = authorize(host)["client_id"]  # authorizing is what uses a registration
    idle = register(host).json()["client_id"]
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 2 * auth.UNUSED_CLIENT_TTL)  # two days on

    assert register(host).status_code == 201  # the next write

    clients = oauth_state(host)["clients"]
    assert used in clients, "a registration that was used stays, however long ago it was made"
    assert idle not in clients, "one that was not used does not"


def test_an_authorize_request_leaves_oauth_json_untouched_when_a_registration_has_lapsed(start_host) -> None:
    host = start_host(oauth=True)
    client = register_with_secret(host)
    age(host, lapse(client["client_id"]))
    before = pinned(host)

    assert authorize_status(host, client["client_id"]) == 400, "refused from the moment it lapsed"

    assert stamp(host) == before, "the request wrote nothing, so a failed write cannot fail it"


def test_the_ended_record_takes_ended_tokens_but_never_an_unused_registration(start_host) -> None:
    host = start_host(oauth=True)
    tokens = authorize(host)
    auth.revoke_all(host.config.oauth_state_path)
    idle = register_with_secret(host)
    long_ago = time.time() - auth.REFRESH_TTL - 60

    def backdate(state: dict[str, Any]) -> None:
        for table in ("access", "refresh"):
            for entry in state[table].values():
                entry["expires_at"] = long_ago
        lapse(idle["client_id"], LAPSED + auth.REFRESH_TTL)(state)  # unused for more than thirty days as well

    age(host, backdate)

    assert register(host).status_code == 201  # the write that moves what is due

    archived = {(line["table"], line["key"]) for line in ended_record(host)}
    assert archived == {("access", auth.token_digest(tokens["access_token"])),
                        ("refresh", auth.token_digest(tokens["refresh_token"]))}, "the ended tokens, and nothing else"
    assert all(line["entry"]["ended"] == "revoked" for line in ended_record(host)), "marked as they ended"
    assert idle["client_id"] not in oauth_state(host)["clients"], "the unused registration is deleted"
    assert idle["client_secret"] not in ended_path(host).read_text()


def test_a_spent_refresh_token_is_kept_marked_spent(start_host) -> None:
    host = start_host(oauth=True)
    tokens = authorize(host)
    digest = auth.token_digest(tokens["refresh_token"])
    issued = oauth_state(host)["refresh"][digest]["expires_at"]

    assert refresh(host, tokens).status_code == 200
    assert register(host).status_code == 201  # a later write

    spent = oauth_state(host)["refresh"].get(digest)
    assert spent is not None, "the spent refresh token is kept"
    assert (spent["ended"], spent["issued_expires_at"]) == ("spent", issued)
    assert spent["expires_at"] <= time.time(), "its expiry moved back to when it was spent"
    assert isinstance(spent["expires_at"], int), "in whole seconds, as issued"


@pytest.mark.parametrize("how", ["revoke_all", "revoke"])
def test_revoked_tokens_are_kept_marked_revoked(start_host, how: str) -> None:
    host = start_host(oauth=True)
    tokens = authorize(host)
    digests = {auth.token_digest(tokens["access_token"]), auth.token_digest(tokens["refresh_token"])}
    if how == "revoke_all":
        assert auth.revoke_all(host.config.oauth_state_path) == 2
    else:
        revoked = {"token": tokens["access_token"], "client_id": tokens["client_id"], "client_secret": ""}
        assert httpx.post(host.base + "/revoke", data=revoked).status_code == 200
    assert register(host).status_code == 201  # a later write

    stored = oauth_state(host)
    kept = {key: entry for table in ("access", "refresh") for key, entry in stored[table].items() if key in digests}
    assert set(kept) == digests, "both tokens are kept"
    assert all(entry["ended"] == "revoked" and entry["expires_at"] <= time.time() for entry in kept.values())


def test_an_expired_token_is_kept(start_host) -> None:
    host = start_host(oauth=True)
    tokens = authorize(host)
    digest = auth.token_digest(tokens["access_token"])
    age(host, lambda state: state["access"][digest].update(expires_at=int(time.time()) - 60))

    assert register(host).status_code == 201

    assert digest in oauth_state(host)["access"], "the expired access token is kept"
    assert mcp_status(host, tokens["access_token"]) == 401


def test_ended_oauth_entries_move_to_the_ended_record_after_thirty_days(start_host) -> None:
    host = start_host(oauth=True)
    tokens = authorize(host)
    auth.revoke_all(host.config.oauth_state_path)
    long_ago = time.time() - auth.REFRESH_TTL - 60

    def backdate(state: dict[str, Any]) -> None:
        for table in ("access", "refresh"):
            for entry in state[table].values():
                entry["expires_at"] = long_ago

    age(host, backdate)
    for _ in range(2):  # the first write moves them, the second finds nothing more to move
        assert register(host).status_code == 201

    moved = {(line["table"], line["key"]): line["entry"] for line in ended_record(host)}
    assert set(moved) == {("access", auth.token_digest(tokens["access_token"])),
                          ("refresh", auth.token_digest(tokens["refresh_token"]))}
    assert len(ended_record(host)) == 2, "each entry is written once"
    assert all(entry["ended"] == "revoked" for entry in moved.values())
    assert oauth_state(host)["access"] == {} and oauth_state(host)["refresh"] == {}
    assert stat.S_IMODE(os.stat(ended_path(host)).st_mode) == 0o600


def test_guard_ended_tokens_are_refused(start_host) -> None:
    host = start_host(oauth=True)
    first = authorize(host)
    second = refresh(host, first)
    assert second.status_code == 200

    assert refresh(host, first).status_code == 400, "the spent refresh token"
    provider = auth.PassphraseProvider(host.config)
    access_token = second.json()["access_token"]
    authority = auth.credential_authority(anyio.run(provider.load_access_token, access_token))
    assert auth.authority_live(host.config, authority)
    assert auth.revoke_all(host.config.oauth_state_path) == 3
    assert mcp_status(host, access_token) == 401
    assert not auth.authority_live(host.config, authority), "the same process sees the revocation"
    assert auth.revoke_all(host.config.oauth_state_path) == 0, "nothing is left to revoke"


def test_a_refresh_token_spent_after_it_was_loaded_is_refused(start_host) -> None:
    host = start_host(oauth=True)
    tokens = authorize(host)
    provider = auth.PassphraseProvider(host.config)
    client = anyio.run(provider.get_client, tokens["client_id"])
    loaded = anyio.run(provider.load_refresh_token, client, tokens["refresh_token"])
    anyio.run(provider.exchange_refresh_token, client, loaded, [])  # a concurrent refresh spends it first

    assert refusal(provider.exchange_refresh_token, client, loaded, []) == "invalid_grant"


def test_a_registration_that_raced_past_the_cap_is_refused(start_host, monkeypatch) -> None:
    monkeypatch.setattr(auth, "MAX_CLIENTS", 1)
    host = start_host(oauth=True)
    assert register(host).status_code == 201
    provider = auth.PassphraseProvider(host.config)
    late = OAuthClientInformationFull(client_id="late-client", redirect_uris=[REDIRECT])  # counted before the first

    assert refusal(provider.register_client, late) == "invalid_client_metadata"
    assert "late-client" not in oauth_state(host)["clients"]


def test_guard_an_ended_token_stays_refused_if_the_clock_is_set_back(start_host, monkeypatch) -> None:
    host = start_host(oauth=True)
    tokens = authorize(host)
    provider = auth.PassphraseProvider(host.config)
    client = anyio.run(provider.get_client, tokens["client_id"])
    authority = auth.credential_authority(anyio.run(provider.load_access_token, tokens["access_token"]))
    auth.revoke_all(host.config.oauth_state_path)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 600)  # the wall clock stepped back ten minutes

    assert anyio.run(provider.load_access_token, tokens["access_token"]) is None
    assert anyio.run(provider.load_refresh_token, client, tokens["refresh_token"]) is None
    assert not auth.authority_live(host.config, authority)


# Seat logins, sessions, receipts and form ids --------------------------------------------------------------------


@pytest.fixture
def room(tmp_path: Path):
    host = make_seat_host(tmp_path)
    return host, conversation(host)


def seat_count(host, table: str) -> int:
    return count(host.config.db_path, f"SELECT COUNT(*) FROM {table}")


def reasons(host, table: str) -> list[str | None]:
    """How each row ended, oldest first; read only once the rows are known to be kept."""
    return [row[0] for row in table_rows(host.config.db_path, f"SELECT end_reason FROM {table} ORDER BY rowid")]


def ending(host, table: str, column: str, value: str) -> list[str | None]:
    return [row[0] for row in table_rows(host.config.db_path,
                                         f"SELECT end_reason FROM {table} WHERE {column} = ?", value)]


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def form_fields(host, cid: str, cookies: dict[str, str]) -> dict[str, str]:
    page = get(host.config, f"/seat/c/{cid}", cookies=cookies)
    return {"csrf": hidden(page.body, "csrf"), "request_id": hidden(page.body, "request_id")}


def later(monkeypatch, seconds: float) -> None:
    start = seat._now()
    monkeypatch.setattr(seat, "_now", lambda: start + seconds)


def test_used_and_expired_login_forms_are_kept(room, monkeypatch) -> None:
    host, _ = room
    log_in(host.config, host.seat_token)  # issues a form and uses it
    assert seat_count(host, "seat_prelogin") == 1, "the used form is kept"

    get(host.config, "/seat/login")
    later(monkeypatch, seat.PRE_TTL + 1)
    get(host.config, "/seat/login")  # issuing a form used to delete every expired one

    assert seat_count(host, "seat_prelogin") == 3, "expired forms are kept"
    assert reasons(host, "seat_prelogin") == ["used", None, None]


def test_login_forms_over_the_live_cap_are_refused(room, monkeypatch) -> None:
    host, _ = room
    monkeypatch.setattr(seat, "MAX_PRE", 3)
    log_in(host.config, host.seat_token)  # a used form still counts
    assert get(host.config, "/seat/login").status == 200
    unused = get(host.config, "/seat/login")

    over = get(host.config, "/seat/login")
    assert over.status == 429, "a fourth form within five minutes is refused"
    assert b"Too many login forms are open" in over.body and b'name="pre"' not in over.body
    assert over.cookies == [] and seat_count(host, "seat_prelogin") == 3
    wrong = post(host.config, "/seat/login", {"token": "not the token", "pre": hidden(unused.body, "pre")},
                 cookies={PRE_COOKIE: cookie_value(unused, PRE_COOKIE)})
    assert (wrong.status, b'name="pre"' in wrong.body, seat_count(host, "seat_prelogin")) == (429, False, 3)

    later(monkeypatch, seat.PRE_TTL + 1)
    assert get(host.config, "/seat/login").status == 200, "forms issued over five minutes ago no longer count"


def test_the_pause_notice_stands_alone_at_the_login_form_cap(room, monkeypatch) -> None:
    host, _ = room
    monkeypatch.setattr(seat, "MAX_PRE", 3)
    monkeypatch.setattr(seat, "FAILURES_PER_HOUR", 1)
    _, second, third = (get(host.config, "/seat/login") for _ in range(3))  # three live forms: the cap
    cap = b"Too many login forms are open"

    def refused(response) -> bytes:
        """The body of a refusal at the cap, which is always a 429 with no form and no cookie."""
        assert response.status == 429 and response.cookies == []
        assert b"<form" not in response.body and b'name="pre"' not in response.body
        return response.body

    def wrong_token(form):
        return post(host.config, "/seat/login", {"token": "not the token", "pre": hidden(form.body, "pre")},
                    cookies={PRE_COOKIE: cookie_value(form, PRE_COOKIE)})

    plain = refused(get(host.config, "/seat/login"))
    assert cap in plain and plain.count(b'class="notice-box"') == 1
    expired = refused(post(host.config, "/seat/login", {"token": "not the token", "pre": "never issued"}))
    assert b"That login form had expired" in expired and cap in expired, "the other refusals keep their notices"
    rejected = refused(wrong_token(third))
    assert b"That token was not accepted" in rejected and cap in rejected, "the other refusals keep their notices"

    paused = refused(wrong_token(second))  # the one failure an hour that this test allows was just spent
    assert b"Logging in is paused for up to an hour" in paused
    assert cap not in paused and paused.count(b'class="notice-box"') == 1, "the pause notice stands alone"


def test_guard_a_used_or_expired_form_cannot_log_in(room, monkeypatch) -> None:
    host, _ = room
    form = get(host.config, "/seat/login")
    fields = {"token": host.seat_token, "pre": hidden(form.body, "pre")}
    cookies = {PRE_COOKIE: cookie_value(form, PRE_COOKIE)}
    assert cookie_value(post(host.config, "/seat/login", fields, cookies=cookies), SESSION_COOKIE)

    again = post(host.config, "/seat/login", fields, cookies=cookies)
    assert (again.status, cookie_value(again, SESSION_COOKIE)) == (403, None)
    stale = get(host.config, "/seat/login")
    later(monkeypatch, seat.PRE_TTL + 1)
    late = post(host.config, "/seat/login", {"token": host.seat_token, "pre": hidden(stale.body, "pre")},
                cookies={PRE_COOKIE: cookie_value(stale, PRE_COOKIE)})
    assert (late.status, cookie_value(late, SESSION_COOKIE)) == (403, None)


def test_a_session_evicted_at_the_cap_is_kept_and_ended(room, monkeypatch) -> None:
    host, cid = room
    monkeypatch.setattr(seat, "MAX_SESSIONS", 2)
    _, first = log_in(host.config, host.seat_token)
    form_fields(host, cid, first)  # a form id bound to the first session
    for _ in range(2):
        log_in(host.config, host.seat_token)

    assert seat_count(host, "seat_sessions") == 3, "the evicted session is kept"
    assert seat_count(host, "seat_requests") == 1, "and so is its form id"
    evicted = digest(first[SESSION_COOKIE])
    assert ending(host, "seat_sessions", "digest", evicted) == ["cap"]
    assert ending(host, "seat_requests", "session", evicted) == ["session"]


def test_a_receipt_evicted_at_the_cap_is_kept(room, monkeypatch) -> None:
    host, cid = room
    monkeypatch.setattr(seat, "MAX_RECEIPTS", 2)
    agent_says(host, cid, "something to see")
    _, cookies = log_in(host.config, host.seat_token)
    for _ in range(3):
        get(host.config, "/seat/inbox", cookies=cookies)

    assert seat_count(host, "seat_receipts") == 3, "the evicted receipt is kept"
    assert reasons(host, "seat_receipts") == ["cap", None, None]


def test_a_form_id_evicted_at_the_cap_is_kept(room, monkeypatch) -> None:
    host, cid = room
    monkeypatch.setattr(seat, "MAX_REQUESTS", 2)
    _, cookies = log_in(host.config, host.seat_token)
    for _ in range(3):
        form_fields(host, cid, cookies)

    assert seat_count(host, "seat_requests") == 3, "the evicted form id is kept"
    assert reasons(host, "seat_requests") == ["cap", None, None]


def test_guard_evicted_seat_rows_are_refused(room, monkeypatch) -> None:
    host, cid = room
    for name, cap in (("MAX_SESSIONS", 2), ("MAX_RECEIPTS", 1), ("MAX_REQUESTS", 1)):
        monkeypatch.setattr(seat, name, cap)
    agent_says(host, cid, "something to see")
    _, cookies = log_in(host.config, host.seat_token)
    evicted_receipt = hidden(get(host.config, "/seat/inbox", cookies=cookies).body, "receipt")
    get(host.config, "/seat/inbox", cookies=cookies)
    evicted_form = form_fields(host, cid, cookies)
    form_fields(host, cid, cookies)

    acked = post(host.config, "/seat/ack", {"csrf": evicted_form["csrf"], "receipt": evicted_receipt}, cookies=cookies)
    assert acked.status == 409 and mailbox(host)[1] == 0
    sent = post(host.config, f"/seat/c/{cid}/send", evicted_form | {"text": "from an evicted form"}, cookies=cookies)
    assert sent.status == 403 and stored_by_seat(host) == []
    for _ in range(2):
        log_in(host.config, host.seat_token)
    assert get(host.config, "/seat/", cookies=cookies).status == 303, "the evicted session is refused"


def test_logout_logout_all_and_lapse_keep_the_session(room, monkeypatch) -> None:
    host, cid = room
    _, out = log_in(host.config, host.seat_token)
    csrf = form_fields(host, cid, out)["csrf"]
    assert post(host.config, "/seat/logout", {"csrf": csrf}, cookies=out).status == 303
    assert seat_count(host, "seat_sessions") == 1, "a logged-out session is kept"

    _, idle = log_in(host.config, host.seat_token)
    later(monkeypatch, host.config.seat.idle_seconds + 1)
    assert get(host.config, "/seat/", cookies=idle).status == 303
    for _ in range(2):
        log_in(host.config, host.seat_token)
    assert seat.logout_all(host.config) == 2
    assert seat.logout_all(host.config) == 0, "an ended session is not ended twice"

    assert seat_count(host, "seat_sessions") == 4
    assert reasons(host, "seat_sessions") == ["logout", "lapsed", "logout_all", "logout_all"]
    assert ending(host, "seat_requests", "session", digest(out[SESSION_COOKIE])) == ["session"]


def test_acknowledged_expired_and_day_old_seat_rows_are_kept(tmp_path: Path, monkeypatch) -> None:
    host = make_seat_host(tmp_path, seat_extra="idle_seconds = 86400\nabsolute_seconds = 172800\n")
    cid = conversation(host)
    agent_says(host, cid, "one")
    _, cookies = log_in(host.config, host.seat_token)
    receipt = hidden(get(host.config, "/seat/inbox", cookies=cookies).body, "receipt")
    fields = form_fields(host, cid, cookies)  # a form id, left unsent
    assert post(host.config, "/seat/ack", {"csrf": fields["csrf"], "receipt": receipt}, cookies=cookies).status == 303
    agent_says(host, cid, "two")
    get(host.config, "/seat/inbox", cookies=cookies)  # a receipt left to expire
    start = seat._now()
    for hours in (12, 24):  # the session kept alive past a day
        monkeypatch.setattr(seat, "_now", lambda h=hours: start + h * 3600 + 1)
        assert get(host.config, "/seat/", cookies=cookies).status == 200

    log_in(host.config, host.seat_token)  # a login ends what has lapsed

    assert seat_count(host, "seat_receipts") == 2, "acknowledged and expired receipts are kept"
    assert seat_count(host, "seat_requests") == 1, "a day-old form id is kept"
    assert reasons(host, "seat_receipts") == ["used", None]
    assert reasons(host, "seat_requests") == ["expired"]


# Passphrase rekeys and webhook secret rotations ------------------------------------------------------------------


def test_a_rekey_is_recorded_without_the_old_verifier(tmp_path: Path) -> None:
    host = build_host(tmp_path)

    async def scenario(client) -> None:
        assert (await claim(client, host, "lane-y")).get("effective_role") == "guest"
        assert owner(host, "alias", "arm-rekey", "lane-y") == 0
        assert (await claim(client, host, "lane-y", OTHER)).get("effective_role") == "guest"  # the armed reset
        assert (await claim(client, host, "lane-y", OTHER)).get("effective_role") == "guest"  # a renewal
        assert (await claim(client, host, "lane-y")).get("error") == "forbidden", "the old passphrase is refused"

    call_tools(host, scenario)

    recorded = table_rows(host.config.db_path, "SELECT alias_id, at FROM alias_rekeys")
    assert len(recorded) == 1, "the reset is recorded, and the renewal is not"
    alias_id = table_rows(host.config.db_path, "SELECT id FROM aliases WHERE name = 'lane-y'")[0][0]
    assert recorded[0][0] == alias_id and recorded[0][1] <= time.time()
    assert "scrypt$" not in repr(recorded)


def test_a_secret_rotation_is_recorded_without_any_secret(events_host, receiver, monkeypatch) -> None:
    host = events_host()
    replaced, current = new_secret(), new_secret()
    assert "result" in subscribe(host, "worker", receiver.url(), replaced)
    assert "result" in subscribe(host, "worker", receiver.url(), current)  # a rotation
    assert "result" in subscribe(host, "worker", receiver.url(), current)  # a refresh with the same secret

    recorded = table_rows(host.config.db_path, "SELECT subscription, owner, at FROM events_rotations")
    assert len(recorded) == 1, "the rotation is recorded, and the refresh is not"
    assert recorded[0][:2] == (next(iter(state(host)["subscriptions"])), "worker")
    assert "whsec_" not in repr(recorded)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 2 * 3600)  # past the overlap
    assert "result" in subscribe(host, "worker", receiver.url(), current)
    assert replaced not in json.dumps(state(host)), "the replaced secret itself still goes"


# Delivery receipts and wake outcomes -----------------------------------------------------------------------------

SUBSCRIPTION = {"id": "sub_synthetic", "role": "worker"}


@pytest.fixture
def offline(tmp_path: Path) -> integration.McpEvents:
    """The integration with no server or receiver, for checks on what it stores."""
    table = '[integrations.openai_mcp_events]\nenabled = true\ncallback_prefixes = ["https://callback.test/hooks/"]\n'
    config = make_config(tmp_path, extra=table).config
    connect(config.db_path).close()
    return integration.McpEvents(config, integration.parse_settings(config.integrations["openai_mcp_events"]))


def state_file(config) -> dict[str, Any]:
    path = config.data_dir / integration.STATE_FILE
    return json.loads(path.read_text()) if path.exists() else {}


def receipts_found(config) -> list[dict[str, Any]]:
    """Every delivery receipt stored: in the state file, where they were, and in their table."""
    stored = table_rows(config.db_path, "SELECT at, wake_id, subscription, owner, outcome FROM events_receipts")
    return [*state_file(config).get("receipts", []),
            *(dict(zip(("at", "wake_id", "subscription", "owner", "outcome"), row, strict=True)) for row in stored)]


def outcomes_found(config) -> tuple[dict[str, dict[str, str]], dict[str, float]]:
    """Every wake's outcomes by destination, and when each wake first occurred, from the state file and the tables."""
    in_file = state_file(config).get("outcomes", {})
    outcomes = {wake_id: dict(entry["destinations"]) for wake_id, entry in in_file.items()}
    occurred = {wake_id: entry["at"] for wake_id, entry in in_file.items()}
    for wake_id, subscription, outcome in table_rows(
            config.db_path, "SELECT wake_id, subscription, outcome FROM events_outcomes"):
        outcomes.setdefault(wake_id, {})[subscription] = outcome
    occurred.update(table_rows(config.db_path, "SELECT wake_id, at FROM events_occurrences"))
    return outcomes, occurred


def test_delivery_receipts_are_kept_past_their_window_and_cap(offline, monkeypatch) -> None:
    monkeypatch.setattr(integration, "MAX_RECEIPTS", 3)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 4 * 86400)
    offline._record("wake_old", SUBSCRIPTION, "accepted")
    monkeypatch.setattr(time, "time", real)
    for n in range(4):
        offline._record(f"wake_{n}", SUBSCRIPTION, "answered 503")

    kept = receipts_found(offline.config)
    assert len(kept) == 5, "every receipt is kept"
    assert {receipt["wake_id"] for receipt in kept} == {"wake_old", "wake_0", "wake_1", "wake_2", "wake_3"}
    shown = integration.status(offline.config)["receipts"]
    assert [receipt["wake_id"] for receipt in shown] == ["wake_1", "wake_2", "wake_3"], "the view is still bounded"


def test_wake_outcomes_are_kept_after_the_core_wake_finishes(events_host, receiver) -> None:
    host = events_host()
    secret = new_secret()
    sub_id = subscribe(host, "worker", receiver.url(), secret)["result"]["id"]
    send(host, "worker", "hello")
    dispatch(host)
    wake_id = json.loads(receiver.events()[0]["body"])["eventId"]
    statuses = {wakes.wake_id(role, key): status for role, key, status in
                table_rows(host.config.db_path, "SELECT role, key, status FROM wakes")}
    assert statuses[wake_id] == "delivered"

    assert "result" in subscribe(host, "worker", receiver.url(), secret)  # a refresh runs a state pass

    outcomes, occurred = outcomes_found(host.config)
    assert outcomes.get(wake_id) == {sub_id: "accepted"}, "the outcome outlives the core wake"
    assert wake_id in occurred, "and so does the time it first occurred"


def test_receipts_and_outcomes_left_in_the_state_file_move_to_their_tables(offline) -> None:
    recent = time.time() - 60
    legacy = {"subscriptions": {}, "outcomes": {"wake_left": {"at": recent - 30, "destinations": {"sub_x": "gone"}}},
              "receipts": [{"at": recent, "wake_id": "wake_left", "subscription": "sub_x", "owner": "role:worker",
                            "outcome": "gone"}]}
    with datadir.opened(offline.config.data_dir) as dir_fd:
        datadir.replace_file(dir_fd, integration.STATE_FILE, json.dumps(legacy).encode())
    assert [r["wake_id"] for r in integration.status(offline.config)["receipts"]] == ["wake_left"]

    for _ in range(2):  # the first state pass moves them; a second finds nothing left to move
        with offline._state():
            pass

    assert table_rows(offline.config.db_path, "SELECT wake_id, subscription, owner, outcome, at FROM events_receipts"
                      ) == [("wake_left", "sub_x", "role:worker", "gone", recent)]
    assert table_rows(offline.config.db_path, "SELECT wake_id, subscription, outcome FROM events_outcomes") == [
        ("wake_left", "sub_x", "gone")]
    assert table_rows(offline.config.db_path, "SELECT wake_id, at FROM events_occurrences") == [
        ("wake_left", recent - 30)]
    assert set(state_file(offline.config)) == {"subscriptions"}, "the state file no longer holds them"
    assert [r["wake_id"] for r in integration.status(offline.config)["receipts"]] == ["wake_left"]


# The indexing writer -------------------------------------------------------------------------------------------

WRITER_SCANS = (("FROM batches WHERE producer = ", "open_batches_by_producer"),
                ("FROM commit_requests WHERE producer = 'alpha' AND committed_at > ", "commits_by_producer_time"),
                ("FROM batches WHERE generation = generations.id", "open_batches_by_generation"))


def test_the_writer_scans_use_their_indexes(tmp_path: Path) -> None:
    config = index_config(tmp_path)
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 2)
    issued: list[str] = []
    db.set_trace_callback(issued.append)
    manifest = begin(db, config, conversation_id)
    read_all(db, config, manifest)
    derived.commit_batch(db, config, "alpha", manifest["batch_id"],
                         {"request_id": "r-1", "artifacts": artifacts_for(manifest)})
    db.set_trace_callback(None)

    for fragment, index in WRITER_SCANS:
        statements = [statement for statement in issued if fragment in statement]
        assert statements, f"no statement has {fragment!r}"
        plan = " ".join(row["detail"] for row in db.execute("EXPLAIN QUERY PLAN " + statements[0]))
        assert index in plan, f"{fragment!r}: {plan}"
    db.close()


def batch_state(db: sqlite3.Connection, batch_id: str) -> str:
    return db.execute("SELECT state FROM batches WHERE id = ?", (batch_id,)).fetchone()[0]


def closed(db, config, manifest: dict[str, Any]) -> str | None:
    """The refusal a commit into this batch meets, or None if it was accepted."""
    try:
        derived.commit_batch(db, config, "alpha", manifest["batch_id"],
                             {"request_id": "r-late", "artifacts": artifacts_for(manifest)})
    except BatonError as error:
        return error.code
    return None


def test_retiring_or_dropping_a_generation_rewrites_no_expired_batch(tmp_path: Path) -> None:
    config = index_config(tmp_path, generation_retention_days=1)
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 2)
    begin(db, config, conversation_id)  # the implicit generation, active
    retiring = derived.generation_create(db, config, "alpha", None)["generation"]
    expired = derived.begin_batch(db, config, "alpha", conversation_id, ["summary"], generation=retiring)
    db.execute("UPDATE batches SET expires_at = 0 WHERE id = ?", (expired["batch_id"],))
    two_days_ago = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    db.execute("UPDATE generations SET last_write_at = ? WHERE id = ?", (two_days_ago, retiring))

    begin(db, config, conversation_id, kinds=("decisions",))  # a writer call retires it

    assert db.execute("SELECT dropped_at FROM generations WHERE id = ?", (retiring,)).fetchone()[0] is not None
    assert batch_state(db, expired["batch_id"]) == "open", "retiring rewrites no expired batch"
    assert closed(db, config, expired) == "batch_closed"
    dropping = derived.generation_create(db, config, "alpha", None)["generation"]
    live = derived.begin_batch(db, config, "alpha", conversation_id, ["summary"], generation=dropping)
    stale = derived.begin_batch(db, config, "alpha", conversation_id, ["decisions"], generation=dropping)
    db.execute("UPDATE batches SET expires_at = 0 WHERE id = ?", (stale["batch_id"],))
    derived.generation_drop(db, config, "alpha", dropping)
    assert (batch_state(db, live["batch_id"]), batch_state(db, stale["batch_id"])) == ("discarded", "open")
    assert (closed(db, config, live), closed(db, config, stale)) == ("batch_closed", "batch_closed")
    db.close()


# The upgrade ---------------------------------------------------------------------------------------------------

SEAT_TABLES = ("seat_sessions", "seat_prelogin", "seat_receipts", "seat_requests")
NEW_TABLES = {"alias_rekeys", "events_rotations", "events_occurrences", "events_outcomes", "events_receipts"}
NEW_INDEXES = {"open_batches_by_producer", "open_batches_by_generation", "commits_by_producer_time",
               "seat_prelogin_by_expiry", "seat_receipts_by_session", "seat_requests_by_session",
               "events_receipts_by_time"}


def test_a_v18_database_upgrades_with_its_seat_rows_live(tmp_path: Path) -> None:
    config = index_config(tmp_path)
    config.data_dir.mkdir(mode=0o700)
    raw = sqlite3.connect(config.db_path)
    raw.executescript((FIXTURES / "schema_v18.sql").read_text())
    before = {table: raw.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
              for table in (*SEAT_TABLES, "generations", "batches", "commit_requests")}
    raw.close()
    os.chmod(config.db_path, 0o600)

    db = connect(config.db_path)
    for table in SEAT_TABLES:
        columns = [row["name"] for row in db.execute(f"PRAGMA table_info({table})")]
        assert columns[-2:] == ["ended_at", "end_reason"], f"{table} has no end state"
    assert db.execute("SELECT version FROM schema_version").fetchone()[0] == store.SCHEMA_VERSION
    for table, rows in before.items():
        after = [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        carried = [row + (None, None) for row in rows] if table in SEAT_TABLES else rows
        assert rows and after == carried, f"{table} did not come through live and unchanged"
    names = {row["name"] for row in db.execute("SELECT name FROM sqlite_master")}
    assert NEW_TABLES <= names and NEW_INDEXES <= names, sorted((NEW_TABLES | NEW_INDEXES) - names)
    db.close()
