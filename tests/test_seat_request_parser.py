"""Every seat POST is strictly parsed and needs an exact Origin and a CSRF token; refusals store nothing."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from seat_helpers import FORM, SEAT_HOST, SEAT_ORIGIN, SESSION_COOKIE, get, hidden, log_in, make_seat_host, post

from baton import conversations
from baton.store import connect


@pytest.fixture
def signed_in(tmp_path: Path):
    host = make_seat_host(tmp_path)
    conn = connect(host.config.db_path)
    conversation = conversations.create(conn, host.config, actor="orchestrator", title="Example",
                                        participants=["orchestrator", "operator"])["conversation_id"]
    conn.close()
    _, cookies = log_in(host.config, host.seat_token)
    page = get(host.config, f"/seat/c/{conversation}", cookies=cookies)
    fields = {"text": "hello", "csrf": hidden(page.body, "csrf"), "request_id": hidden(page.body, "request_id")}
    return host, conversation, cookies, fields


def _stored(host) -> int:
    conn = sqlite3.connect(host.config.db_path)
    return conn.execute("SELECT COUNT(*) FROM messages WHERE sender = 'operator'").fetchone()[0]


def test_a_well_formed_send_is_stored_as_the_person(signed_in) -> None:
    host, conversation, cookies, fields = signed_in
    response = post(host.config, f"/seat/c/{conversation}/send", fields, cookies=cookies)
    assert response.status == 303
    conn = sqlite3.connect(host.config.db_path)
    assert conn.execute("SELECT DISTINCT sender, author_kind FROM messages WHERE sender = 'operator'").fetchall() == [
        ("operator", "human")]


@pytest.mark.parametrize("origin", [None, "https://elsewhere.example.test", "http://127.0.0.1:8766",
                                    SEAT_ORIGIN + ":444", "null"])
def test_a_wrong_or_missing_origin_is_refused(signed_in, origin) -> None:
    host, conversation, cookies, fields = signed_in
    assert post(host.config, f"/seat/c/{conversation}/send", fields, cookies=cookies, origin=origin).status == 403
    assert _stored(host) == 0


def test_a_cross_site_fetch_is_refused_even_with_the_right_origin(signed_in) -> None:
    host, conversation, cookies, fields = signed_in
    response = post(host.config, f"/seat/c/{conversation}/send", fields, cookies=cookies,
                    extra=[("Sec-Fetch-Site", "cross-site")])
    assert response.status == 403 and _stored(host) == 0


@pytest.mark.parametrize("csrf", ["", "wrong", None])
def test_a_missing_or_wrong_csrf_token_is_refused(signed_in, csrf) -> None:
    host, conversation, cookies, fields = signed_in
    fields = {k: v for k, v in fields.items() if k != "csrf"} | ({} if csrf is None else {"csrf": csrf})
    assert post(host.config, f"/seat/c/{conversation}/send", fields, cookies=cookies).status in (400, 403)
    assert _stored(host) == 0


def test_another_sessions_csrf_token_is_refused(signed_in) -> None:
    host, conversation, cookies, fields = signed_in
    _, other = log_in(host.config, host.seat_token)
    other_page = get(host.config, f"/seat/c/{conversation}", cookies=other)
    fields = fields | {"csrf": hidden(other_page.body, "csrf")}
    assert post(host.config, f"/seat/c/{conversation}/send", fields, cookies=cookies).status == 403
    assert _stored(host) == 0


@pytest.mark.parametrize("name", ["Host", "Origin", "Cookie", "Content-Type", "Content-Length", "Sec-Fetch-Site"])
def test_a_duplicated_header_is_refused(signed_in, name: str) -> None:
    host, conversation, cookies, fields = signed_in
    values = {"Host": SEAT_HOST, "Origin": SEAT_ORIGIN, "Cookie": f"{SESSION_COOKIE}={cookies[SESSION_COOKIE]}",
              "Content-Type": FORM, "Sec-Fetch-Site": "same-origin"}
    if name == "Content-Length":
        from urllib.parse import urlencode

        values[name] = str(len(urlencode(fields)))
    response = post(host.config, f"/seat/c/{conversation}/send", fields, cookies=cookies,
                    extra=[(name, values[name])] + ([("Sec-Fetch-Site", "same-origin")]
                                                    if name == "Sec-Fetch-Site" else []))
    assert response.status in (400, 421) and _stored(host) == 0


def test_two_session_cookies_are_refused(signed_in) -> None:
    host, conversation, cookies, fields = signed_in
    response = post(host.config, f"/seat/c/{conversation}/send", fields, cookies=cookies,
                    extra=[("Cookie", f"{SESSION_COOKIE}=seat_another")])
    assert response.status == 400 and _stored(host) == 0


@pytest.mark.parametrize("field", ["text", "csrf", "request_id", "reply_to"])
def test_a_repeated_form_field_is_refused(signed_in, field: str) -> None:
    host, conversation, cookies, fields = signed_in
    pairs = list(fields.items()) + [(field, fields.get(field, "x"))]
    assert post(host.config, f"/seat/c/{conversation}/send", pairs, cookies=cookies).status == 400
    assert _stored(host) == 0


@pytest.mark.parametrize("field", ["from_role", "author_kind", "author", "sender", "from", "role", "on_behalf_of",
                                   "relayed", "attention", "needs_decision", "job_id", "unexpected"])
def test_a_field_outside_the_form_is_refused(signed_in, field: str) -> None:
    host, conversation, cookies, fields = signed_in
    assert post(host.config, f"/seat/c/{conversation}/send", fields | {field: "x"}, cookies=cookies).status == 400
    assert _stored(host) == 0


@pytest.mark.parametrize("framing", ["chunked", "chunked-and-length", "no-length", "bad-length", "short-body",
                                     "oversized"])
def test_ambiguous_or_oversized_framing_is_refused(signed_in, framing: str) -> None:
    from urllib.parse import urlencode

    from baton import seat

    host, conversation, cookies, fields = signed_in
    body = urlencode(fields | ({"text": "x" * 17 * 1024} if framing == "oversized" else {})).encode()
    headers = [("Host", SEAT_HOST), ("Origin", SEAT_ORIGIN), ("Content-Type", FORM),
               ("Cookie", f"{SESSION_COOKIE}={cookies[SESSION_COOKIE]}")]
    length = {"chunked": None, "no-length": None, "bad-length": "12abc", "short-body": str(len(body) + 10)}.get(
        framing, str(len(body)))
    if length is not None:
        headers.append(("Content-Length", length))
    if framing.startswith("chunked"):
        headers.append(("Transfer-Encoding", "chunked"))
    response = seat.respond(host.config, "POST", f"/seat/c/{conversation}/send", headers, body)
    assert response.status in (400, 411, 413) and _stored(host) == 0


@pytest.mark.parametrize("content_type", ["application/json", "multipart/form-data; boundary=x", "text/plain",
                                          FORM + "; charset=utf-8, text/plain"])
def test_only_a_plain_form_body_is_accepted(signed_in, content_type: str) -> None:
    host, conversation, cookies, fields = signed_in
    response = post(host.config, f"/seat/c/{conversation}/send", fields, cookies=cookies, content_type=content_type)
    assert response.status in (400, 415) and _stored(host) == 0


def test_invalid_utf8_is_refused(signed_in) -> None:
    from baton import seat

    host, conversation, cookies, fields = signed_in
    body = b"text=%FF%FE&csrf=" + fields["csrf"].encode() + b"&request_id=" + fields["request_id"].encode()
    headers = [("Host", SEAT_HOST), ("Origin", SEAT_ORIGIN), ("Content-Type", FORM),
               ("Content-Length", str(len(body))), ("Cookie", f"{SESSION_COOKIE}={cookies[SESSION_COOKIE]}")]
    assert seat.respond(host.config, "POST", f"/seat/c/{conversation}/send", headers, body).status == 400
    assert _stored(host) == 0


@pytest.mark.parametrize("route", ["/seat/login", "/seat/logout", "/seat/ack"])
def test_every_post_route_needs_the_origin(signed_in, route: str) -> None:
    host, _, cookies, fields = signed_in
    assert post(host.config, route, {"csrf": fields["csrf"]}, cookies=cookies, origin=None).status == 403


def test_seat_pages_cannot_be_framed_and_run_no_script(signed_in) -> None:
    host, conversation, cookies, _ = signed_in
    page = get(host.config, f"/seat/c/{conversation}", cookies=cookies)
    csp = page.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp and "default-src 'none'" in csp and "form-action 'self'" in csp
    assert page.headers["X-Frame-Options"] == "DENY" and b"<script" not in page.body.lower()


def test_a_wrong_host_is_refused(signed_in) -> None:
    from baton import seat

    host, _, cookies, _ = signed_in
    headers = [("Host", "raspberry.example.test"), ("Cookie", f"{SESSION_COOKIE}={cookies[SESSION_COOKIE]}")]
    assert seat.respond(host.config, "GET", "/seat/", headers, b"").status == 421


def test_body_rules_apply_only_to_post(signed_in) -> None:
    from baton import seat

    host, conversation, cookies, _ = signed_in
    headers = [("Host", SEAT_HOST), ("Cookie", f"{SESSION_COOKIE}={cookies[SESSION_COOKIE]}"),
               ("Content-Type", "application/json")]
    assert seat.respond(host.config, "GET", f"/seat/c/{conversation}", headers, b"").status == 200
    assert seat.respond(host.config, "HEAD", "/seat/", headers, b"").status == 200
