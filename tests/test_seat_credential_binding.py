"""A seat session is bound to the exact token it logged in with, and checked again on every request."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from seat_helpers import SESSION_COOKIE, get, log_in, make_seat_host, seat_config_text

from baton import auth
from baton.auth import new_token


@pytest.fixture
def seat_host(tmp_path: Path):
    return make_seat_host(tmp_path)


def test_the_seat_token_opens_a_session(seat_host) -> None:
    response, cookies = log_in(seat_host.config, seat_host.seat_token)
    assert response.status == 303 and cookies
    assert get(seat_host.config, "/seat/", cookies=cookies).status == 200


@pytest.mark.parametrize("which", ["agent", "garbage", "empty"])
def test_any_other_token_is_refused_the_same_way(seat_host, which: str) -> None:
    token = {"agent": seat_host.agent_token, "garbage": "baton_not-a-real-token", "empty": ""}[which]
    response, cookies = log_in(seat_host.config, token)
    assert (response.status, cookies) == (401, {})
    assert b"not accepted" in response.body
    if token:
        assert token.encode() not in response.body


def test_a_revoked_token_cannot_log_in(seat_host) -> None:
    auth.revoke_token(seat_host.config, "operator-phone")
    assert log_in(seat_host.config, seat_host.seat_token)[0].status == 401


def test_only_the_binding_is_stored_never_the_token(seat_host) -> None:
    _, cookies = log_in(seat_host.config, seat_host.seat_token)
    session = cookies[SESSION_COOKIE]
    stored = b"".join(path.read_bytes() for path in seat_host.config.data_dir.iterdir() if path.is_file())
    assert seat_host.seat_token.encode() not in stored
    assert session.encode() not in stored  # the session id is kept only as a digest
    conn = sqlite3.connect(seat_host.config.db_path)
    [row] = conn.execute("SELECT authority FROM seat_sessions").fetchall()
    assert seat_host.seat_digest in row[0] and "operator-phone" in row[0]


def test_the_session_is_not_an_mcp_bearer(seat_host) -> None:
    _, cookies = log_in(seat_host.config, seat_host.seat_token)
    assert auth.static_token(seat_host.config, cookies[SESSION_COOKIE]) is None


def _rewrite(seat_host, tmp_path: Path, **overrides) -> None:
    _, agent_digest = new_token()
    seat_host.reload(seat_config_text(tmp_path, overrides.pop("seat_digest", seat_host.seat_digest), agent_digest,
                                      **overrides))


@pytest.mark.parametrize("change", ["revoked", "removed", "rotated", "renamed", "reassigned"])
def test_a_change_to_the_credential_ends_its_sessions(seat_host, tmp_path: Path, change: str) -> None:
    _, cookies = log_in(seat_host.config, seat_host.seat_token)
    if change == "revoked":
        auth.revoke_token(seat_host.config, "operator-phone")
    elif change == "removed":
        _rewrite(seat_host, tmp_path, include_seat_token=False)
    elif change == "rotated":
        _rewrite(seat_host, tmp_path, seat_digest=new_token()[1])
    elif change == "renamed":
        _rewrite(seat_host, tmp_path, seat_token_name="operator-laptop")
    elif change == "reassigned":  # the same digest now belongs to an agent role
        _rewrite(seat_host, tmp_path, seat_token_role="reviewer")
    response = get(seat_host.config, "/seat/", cookies=cookies)
    assert response.status in (303, 401)
    assert b"/seat/login" in response.body or response.headers.get("Location") == "/seat/login"
    conn = sqlite3.connect(seat_host.config.db_path)
    assert conn.execute("SELECT COUNT(*) FROM seat_sessions").fetchone()[0] == 0


def test_a_session_cookie_sent_before_login_is_never_adopted(seat_host) -> None:
    planted = {SESSION_COOKIE: "seat_planted-by-someone-else"}
    from seat_helpers import PRE_COOKIE, cookie_value, hidden, post

    form = get(seat_host.config, "/seat/login", cookies=planted)
    pre = cookie_value(form, PRE_COOKIE)
    response = post(seat_host.config, "/seat/login", {"token": seat_host.seat_token, "pre": hidden(form.body, "pre")},
                    cookies={**planted, PRE_COOKIE: pre})
    issued = cookie_value(response, SESSION_COOKIE)
    assert issued and issued != planted[SESSION_COOKIE]
    assert get(seat_host.config, "/seat/", cookies=planted).status != 200


def test_the_session_cookie_is_host_only_and_strict(seat_host) -> None:
    from seat_helpers import cookies_set

    response, _ = log_in(seat_host.config, seat_host.seat_token)
    attributes = [part.strip().lower() for part in cookies_set(response)[SESSION_COOKIE].split(";")[1:]]
    assert {"secure", "httponly", "samesite=strict", "path=/"} <= set(attributes)
    assert not any(part.startswith("domain") for part in attributes)
