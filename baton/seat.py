"""The browser seat: one human role reads its conversations, acknowledges its mailbox and replies, from a phone.

Off unless `[seat] enabled = true`. Like the dashboard it is a small standard-library HTTP server on a loopback
address, reached only through a proxy that terminates TLS at `seat.origin`. Unlike the dashboard it has a login,
and everything it shows or does is the seat role's own: its memberships, its history, its mailbox.

**Login.** The person pastes the seat role's own bearer token (`baton token new --role <seat>`). The token is
checked once and never stored: the session keeps only which credential it was, `auth.credential_authority`, and
every later request checks that the same credential still exists with the same role (`auth.authority_live`), so
revoking, removing, rotating or reassigning it ends the session at once. A session id is not a bearer token for
the MCP server, and the consent page never grants a human role.

**Cookies.** `__Host-` cookies, Secure, HttpOnly, SameSite=Strict, Path=/, no Domain. A browser sends a host's
cookies to every port on that host name, so the config refuses a seat on a name shared with the host or the
dashboard unless `same_host_trust_accepted` records that every service there is trusted with the person's
authority. Every POST needs the exact Origin and a CSRF token derived from the session; every request is parsed
strictly, and a duplicated header or field, an unexpected field or ambiguous framing is refused.

**Mailbox.** The seat is a participant, so it receives a copy of every message, and a role with 500 unread
messages stops its conversations accepting more. Reading is neutral, as on the dashboard. The inbox shows the
mailbox from its cursor, one page at a time, and a "mark these as seen" form carries a receipt for exactly the
rendered page; acknowledging advances the cursor over that page only, if it still starts there. Messages from a
conversation the seat has left show as content-free placeholders, so they never stall it. Seen is not approved.

**Sending.** A message is sent as the seat role, so it is recorded `author_kind = human`: sent through the
authenticated seat, bound to that credential. It is not proof against a compromised host, phone or token, and
it moves no job and marks nothing answered or approved. Each form carries a request id bound to its session and
conversation, so a resubmitted form stores one copy.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
import socket
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

from baton import auth, conversations, dashboard, messages
from baton.config import Config
from baton.errors import (
    BatonError,
    ConversationFull,
    ConversationRateLimited,
    IdempotencyConflict,
    MailboxFull,
    NotAParticipant,
)
from baton.store import connect, transaction

SESSION_COOKIE = "__Host-baton_seat"
PRE_COOKIE = "__Host-baton_seat_pre"
COOKIE_ATTRIBUTES = "Secure; HttpOnly; SameSite=Strict; Path=/"
FORM = "application/x-www-form-urlencoded"
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; base-uri 'none'; form-action 'self'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}
# Headers a request may carry once at most; two of them would leave it ambiguous which one counts.
SINGLE_HEADERS = ("host", "origin", "cookie", "content-type", "content-length", "sec-fetch-site",
                  "transfer-encoding")
MAX_BODY = 16 * 1024
PRE_TTL = 300
RECEIPT_TTL = 900
MAX_PRE = 20
MAX_SESSIONS = 10
MAX_RECEIPTS = 20
MAX_REQUESTS = 100
REQUEST_TTL = 24 * 3600
SENDS_PER_HOUR = 60
FAILURES_PER_HOUR = 5
INBOX_PAGE = 50
HISTORY_PAGE = 50
WARN_UNREAD = 400
REQUEST_TIMEOUT = 10
CONVERSATION_PATH = re.compile(r"/seat/c/([A-Za-z0-9_-]{1,64})")
SEND_PATH = re.compile(r"/seat/c/([A-Za-z0-9_-]{1,64})/send")
SEQ = re.compile(r"[0-9]{1,19}")
MESSAGE_ID = re.compile(r"msg_[0-9a-f]{1,64}")
# Each POST route's fields; anything else, or a field given twice, is refused before it is read.
FIELDS = {"/seat/login": ({"token", "pre"}, {"token", "pre"}), "/seat/logout": ({"csrf"}, {"csrf"}),
          "/seat/ack": ({"csrf", "receipt"}, {"csrf", "receipt"}),
          "send": ({"text", "reply_to", "csrf", "request_id"}, {"text", "csrf", "request_id"})}
STYLE = dashboard.STYLE + """\
form.compose textarea { width: 100%; box-sizing: border-box; min-height: 6rem; font: inherit; }
form.compose button, form.inline button, form.login button { font: inherit; padding: .5rem 1rem; margin-top: .4rem; }
form.inline { display: inline; }
form.login input { width: 100%; box-sizing: border-box; font: inherit; padding: .4rem; }
.banner { border: 1px solid #c4c7c5; border-radius: .4rem; padding: .5rem .7rem; margin: .6rem 0; }
.placeholder { color: #5f6368; font-style: italic; }
"""

_failures: dict[str, deque[float]] = {}
_failures_lock = threading.Lock()


def _now() -> float:
    return time.time()


@dataclass
class Response:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    cookies: list[str] = field(default_factory=list)


class _Refusal(Exception):
    def __init__(self, response: Response) -> None:
        self.response = response


@dataclass
class _Request:
    method: str
    path: str
    query: str
    cookies: dict[str, str]
    form: dict[str, str]


@dataclass
class _Session:
    raw: str
    digest: str
    role: str

    @property
    def csrf(self) -> str:
        """Derived from the session id, so it is stored nowhere and differs for every session."""
        return hmac.new(self.raw.encode(), b"baton seat csrf", hashlib.sha256).hexdigest()


def respond(config: Config, method: str, target: str, headers: list[tuple[str, str]], body: bytes) -> Response:
    """Answer one request. `headers` keeps every (name, value) pair as received, repeats included."""
    try:
        request = _parse(config, method, target, headers, body)
        conn = connect(config.db_path)
        try:
            return _route(config, conn, request)
        finally:
            conn.close()
    except _Refusal as refusal:
        return refusal.response


# Parsing ---------------------------------------------------------------------------------------------------


def _parse(config: Config, method: str, target: str, headers: list[tuple[str, str]], body: bytes) -> _Request:
    named: dict[str, list[str]] = {}
    for name, value in headers:
        named.setdefault(name.lower(), []).append(value)
    if any(len(named.get(name, ())) > 1 for name in SINGLE_HEADERS):
        raise _Refusal(_page(400, "Bad request", "<p>A header was sent more than once.</p>"))
    one = {name: values[0] for name, values in named.items()}
    if one.get("host") != config.seat.host:
        raise _Refusal(_page(421, "Wrong address", "<p>The seat answers only at its own address.</p>"))
    if "transfer-encoding" in one:
        raise _Refusal(_page(400, "Bad request", "<p>Only a plain body with a Content-Length is accepted.</p>"))
    if method not in ("GET", "HEAD", "POST"):
        raise _Refusal(Response(405, b"not allowed\n", {**HEADERS, "Allow": "GET, HEAD, POST",
                                                        "Content-Type": "text/plain; charset=utf-8"}))
    url = urlsplit(target)
    cookies = _cookies(one.get("cookie", ""))
    form: dict[str, str] = {}
    if method == "POST":
        if one.get("origin") != config.seat.origin or one.get("sec-fetch-site", "same-origin") != "same-origin":
            raise _Refusal(_page(403, "Refused", "<p>This form can be sent only from the seat's own pages.</p>"))
        route = "send" if SEND_PATH.fullmatch(url.path) else url.path
        if route not in FIELDS:
            raise _Refusal(_not_found())
        form = _form(one, body, *FIELDS[route])
    return _Request(method, url.path, url.query, cookies, form)


def _cookies(header: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name in (SESSION_COOKIE, PRE_COOKIE):
            if name in found:
                raise _Refusal(_page(400, "Bad request", "<p>A cookie was sent more than once.</p>"))
            found[name] = value
    return found


def _form(headers: dict[str, str], body: bytes, allowed: set[str], required: set[str]) -> dict[str, str]:
    length = headers.get("content-length")
    if length is None or not re.fullmatch(r"[0-9]{1,7}", length):
        raise _Refusal(_page(411 if length is None else 400, "Bad request", "<p>A form needs a Content-Length.</p>"))
    if int(length) > MAX_BODY:
        raise _Refusal(_page(413, "Too long", f"<p>A form is at most {MAX_BODY // 1024} KB.</p>"))
    if int(length) != len(body):
        raise _Refusal(_page(400, "Bad request", "<p>The body was not the length it said.</p>"))
    if headers.get("content-type", "").strip().lower() != FORM:
        raise _Refusal(_page(415, "Bad request", "<p>Only a plain form is accepted.</p>"))
    try:
        pairs = parse_qsl(body.decode("ascii"), keep_blank_values=True, strict_parsing=bool(body),
                          encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        raise _Refusal(_page(400, "Bad request", "<p>The form could not be read.</p>")) from None
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names) or not set(names) <= allowed or not required <= set(names):
        raise _Refusal(_page(400, "Bad request", "<p>The form had a field it shouldn't, or was missing one.</p>"))
    return dict(pairs)


# Routing ---------------------------------------------------------------------------------------------------


def _route(config: Config, conn: sqlite3.Connection, request: _Request) -> Response:
    if request.path == "/seat/style.css" and request.method != "POST":
        return Response(200, STYLE.encode(), {**HEADERS, "Content-Type": "text/css; charset=utf-8"})
    if request.path == "/seat/login":
        return _login(config, conn, request) if request.method == "POST" else _login_form(conn)
    session = _session(config, conn, request.cookies.get(SESSION_COOKIE))
    if session is None:
        return _to_login()
    if request.method == "POST" and not hmac.compare_digest(request.form.get("csrf", ""), session.csrf):
        return _page(403, "Refused", "<p>This form is from another session. <a href=\"/seat/\">Start again</a>.</p>")
    if request.method == "POST":
        if request.path == "/seat/logout":
            return _logout(conn, session)
        if request.path == "/seat/ack":
            return _ack(config, conn, session, request.form["receipt"])
        return _send(config, conn, session, SEND_PATH.fullmatch(request.path).group(1), request.form)
    if request.path in ("/seat", "/seat/"):
        return _home(config, conn, session)
    if request.path == "/seat/inbox":
        return _inbox(config, conn, session)
    match = CONVERSATION_PATH.fullmatch(request.path)
    if match:
        query = parse_qs(request.query, keep_blank_values=True)
        after = query.get("after_seq", ["0"])[-1]
        reply = query.get("reply", [""])[-1]
        if not SEQ.fullmatch(after) or (reply and not MESSAGE_ID.fullmatch(reply)):
            return _page(400, "Bad request", "<p>That link isn't one the seat makes.</p>")
        return _conversation(config, conn, session, match.group(1), int(after), reply or None)
    return _not_found()


# Login and sessions ----------------------------------------------------------------------------------------


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _login_form(conn: sqlite3.Connection, status: int = 200, notice: str = "") -> Response:
    pre = secrets.token_urlsafe(24)
    now = _now()
    with transaction(conn):
        conn.execute("DELETE FROM seat_prelogin WHERE expires_at <= ?", (now,))
        conn.execute("DELETE FROM seat_prelogin WHERE digest NOT IN (SELECT digest FROM seat_prelogin"
                     " ORDER BY expires_at DESC LIMIT ?)", (MAX_PRE - 1,))
        conn.execute("INSERT INTO seat_prelogin (digest, expires_at) VALUES (?, ?)", (_digest(pre), now + PRE_TTL))
    body = (f'<h1>Log in</h1>{notice}<form class="login" method="post" action="/seat/login">'
            '<label for="token">Your token</label>'
            '<input id="token" type="password" name="token" autocomplete="current-password" required>'
            f'<input type="hidden" name="pre" value="{_e(pre)}"><button type="submit">Log in</button></form>'
            '<p class="muted">Paste the token you keep for this seat. It is checked once and not kept.</p>')
    response = _page(status, "Log in", body)
    response.cookies.append(f"{PRE_COOKIE}={pre}; Max-Age={PRE_TTL}; {COOKIE_ATTRIBUTES}")
    return response


def _login(config: Config, conn: sqlite3.Connection, request: _Request) -> Response:
    pre = request.form["pre"]
    now = _now()
    with transaction(conn):
        fresh = conn.execute("DELETE FROM seat_prelogin WHERE digest = ? AND expires_at > ?",
                             (_digest(pre), now)).rowcount
    if not fresh or not hmac.compare_digest(request.cookies.get(PRE_COOKIE, ""), pre):
        return _login_form(conn, 403, '<p class="notice">That login form had expired. Try again.</p>')
    key = str(config.db_path)
    with _failures_lock:
        failures = _failures.setdefault(key, deque())
        while failures and now - failures[0] > 3600:
            failures.popleft()
        if len(failures) >= FAILURES_PER_HOUR:
            return _login_form(conn, 429, '<p class="notice">Too many tries. Wait an hour and try again.</p>')
    token = request.form["token"].strip()
    access = auth.static_token(config, token) if token else None
    if access is None or auth.role_of(access) != config.seat.role or not _seat_role_ok(config):
        with _failures_lock:
            _failures[key].append(now)
        return _login_form(conn, 401, '<p class="notice">That token was not accepted.</p>')
    authority = auth.credential_authority(access)
    raw = "seat_" + secrets.token_urlsafe(32)
    with transaction(conn):
        _prune(config, conn, now)
        conn.execute("DELETE FROM seat_sessions WHERE digest IN (SELECT digest FROM seat_sessions WHERE role = ?"
                     " ORDER BY last_seen DESC LIMIT -1 OFFSET ?)", (config.seat.role, MAX_SESSIONS - 1))
        conn.execute("INSERT INTO seat_sessions (digest, role, authority, created_at, last_seen)"
                     " VALUES (?, ?, ?, ?, ?)",
                     (_digest(raw), config.seat.role, json.dumps(authority, sort_keys=True), now, now))
        _prune_orphans(conn)
    response = _redirect("/seat/")
    response.cookies += [f"{SESSION_COOKIE}={raw}; {COOKIE_ATTRIBUTES}",
                         f"{PRE_COOKIE}=; Max-Age=0; {COOKIE_ATTRIBUTES}"]
    return response


def _seat_role_ok(config: Config) -> bool:
    role = config.roles.get(config.seat.role or "")
    return config.seat.enabled and role is not None and role.human and not role.display_only


def _session(config: Config, conn: sqlite3.Connection, raw: str | None) -> _Session | None:
    """The live session `raw` names, or None; a session that no longer holds is deleted on the spot."""
    if not raw:
        return None
    digest, now = _digest(raw), _now()
    with transaction(conn):
        row = conn.execute("SELECT * FROM seat_sessions WHERE digest = ?", (digest,)).fetchone()
        if row is None:
            return None
        authority = json.loads(row["authority"])
        live = (now - row["last_seen"] <= config.seat.idle_seconds
                and now - row["created_at"] <= config.seat.absolute_seconds
                and row["role"] == config.seat.role == authority.get("role") and _seat_role_ok(config)
                and auth.authority_live(config, authority))
        if not live:
            _end(conn, digest)
            return None
        conn.execute("UPDATE seat_sessions SET last_seen = ? WHERE digest = ?", (now, digest))
    return _Session(raw, digest, row["role"])


def _end(conn: sqlite3.Connection, digest: str) -> None:
    for table, column in (("seat_sessions", "digest"), ("seat_receipts", "session"), ("seat_requests", "session")):
        conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (digest,))


def _prune(config: Config, conn: sqlite3.Connection, now: float) -> None:
    for row in conn.execute("SELECT digest FROM seat_sessions WHERE last_seen < ? OR created_at < ?",
                            (now - config.seat.idle_seconds, now - config.seat.absolute_seconds)).fetchall():
        _end(conn, row["digest"])
    conn.execute("DELETE FROM seat_receipts WHERE expires_at <= ?", (now,))
    conn.execute("DELETE FROM seat_requests WHERE created_at < ?", (now - REQUEST_TTL,))


def _prune_orphans(conn: sqlite3.Connection) -> None:
    """Receipts and form ids of sessions that are gone, such as one evicted at the session cap."""
    for table in ("seat_receipts", "seat_requests"):
        conn.execute(f"DELETE FROM {table} WHERE session NOT IN (SELECT digest FROM seat_sessions)")


def _logout(conn: sqlite3.Connection, session: _Session) -> Response:
    with transaction(conn):
        _end(conn, session.digest)
    return _to_login()


def logout_all(config: Config) -> int:
    """End every seat session at once; `baton seat logout-all`."""
    conn = connect(config.db_path)
    try:
        with transaction(conn):
            ended = conn.execute("DELETE FROM seat_sessions").rowcount
            conn.execute("DELETE FROM seat_receipts")
            conn.execute("DELETE FROM seat_requests")
        return ended
    finally:
        conn.close()


def _to_login() -> Response:
    response = _redirect("/seat/login")
    response.cookies.append(f"{SESSION_COOKIE}=; Max-Age=0; {COOKIE_ATTRIBUTES}")
    return response


# Pages -----------------------------------------------------------------------------------------------------


def _home(config: Config, conn: sqlite3.Connection, session: _Session) -> Response:
    unread = messages.unread(conn, session.role)
    listed = conversations.list_for(conn, session.role)
    items = "".join(f'<li><a href="/seat/c/{_e(c["conversation_id"])}">{_e(c["title"])}</a>'
                    f'{_topic(c.get("topic"))}</li>' for c in listed)
    body = (f'<h1>{_e(config.roles[session.role].label)}</h1>{_banner(unread)}'
            f'<p><a href="/seat/inbox">Inbox</a> · {unread} unread</p>'
            f'<h2>Your conversations</h2><ul>{items or "<li class=muted>None yet.</li>"}</ul>'
            f'{_logout_form(session)}')
    return _page(200, "Seat", body)


def _banner(unread: int) -> str:
    if unread < WARN_UNREAD:
        return ""
    return (f'<p class="banner">{unread} unread. Conversations you\'re in stop accepting messages at '
            f'{messages.MAX_UNREAD} unread messages in your mailbox — <a href="/seat/inbox">mark some as seen</a>.</p>')


def _inbox(config: Config, conn: sqlite3.Connection, session: _Session) -> Response:
    """The mailbox from its cursor, without moving it, and a receipt for exactly the page shown."""
    with transaction(conn):
        start = messages.read_seq(conn, session.role)
        rows = conn.execute("SELECT * FROM messages WHERE recipient = ? AND seq > ? ORDER BY seq LIMIT ?",
                            (session.role, start, INBOX_PAGE)).fetchall()
        member = {row["conversation_id"] for row in conn.execute("SELECT conversation_id FROM participants"
                                                                  " WHERE role = ?", (session.role,)).fetchall()}
        page = messages.as_page(rows, lambda row: _inbox_entry(row, member))
        receipt = ""
        if page:
            receipt = secrets.token_urlsafe(24)
            conn.execute("INSERT INTO seat_receipts (digest, session, role, start_seq, last_seq, expires_at)"
                         " VALUES (?, ?, ?, ?, ?, ?)", (_digest(receipt), session.digest, session.role, start,
                                                        page[-1]["seq"], _now() + RECEIPT_TTL))
            conn.execute("DELETE FROM seat_receipts WHERE session = ? AND digest NOT IN (SELECT digest FROM"
                         " seat_receipts WHERE session = ? ORDER BY expires_at DESC LIMIT ?)",
                         (session.digest, session.digest, MAX_RECEIPTS))
    unread = messages.unread(conn, session.role)
    items = "".join(_inbox_item(entry) for entry in page)
    ack = (f'<form class="inline" method="post" action="/seat/ack">{_csrf(session)}'
           f'<input type="hidden" name="receipt" value="{_e(receipt)}">'
           f'<button type="submit">Mark these {len(page)} as seen</button></form>'
           '<p class="muted">Seen only moves your inbox along. It is not a reply and approves nothing.</p>'
           if page else "")
    body = (f'<p><a href="/seat/">Seat</a></p><h1>Inbox</h1>{_banner(unread)}'
            f'<p class="muted">{unread} unread, oldest first. Reading here marks nothing.</p>'
            f'<ul>{items or "<li class=muted>Nothing unread.</li>"}</ul>{ack}')
    return _page(200, "Inbox", body)


def _inbox_entry(row: sqlite3.Row, member: set[str]) -> dict[str, Any]:
    if row["conversation_id"] is not None and row["conversation_id"] not in member:
        return {"seq": row["seq"], "placeholder": True}
    return {**messages.as_dict(row), "conversation_seq": row["conversation_seq"]}


def _inbox_item(entry: dict[str, Any]) -> str:
    if entry.get("placeholder"):
        return '<li class="placeholder">A message from a conversation you\'re no longer in.</li>'
    where = (f' · <a href="{_link(entry["conversation_id"], entry["conversation_seq"])}">open</a>'
             if entry["conversation_id"] else "")
    return f'<li>{_meta(entry)}{where}<p class="text">{_e(entry["text"])}</p></li>'


def _ack(config: Config, conn: sqlite3.Connection, session: _Session, receipt: str) -> Response:
    with transaction(conn):
        row = conn.execute("SELECT * FROM seat_receipts WHERE digest = ? AND session = ?",
                           (_digest(receipt), session.digest)).fetchone()
        conn.execute("DELETE FROM seat_receipts WHERE digest = ?", (_digest(receipt),))
        current = messages.read_seq(conn, session.role)
        if row is None or row["expires_at"] <= _now() or row["role"] != session.role or row["start_seq"] != current:
            stale = True
        else:
            stale = False
            messages.mark_read(conn, session.role, row["last_seq"])
    if stale:
        return _page(409, "Out of date", '<p>This page is out of date: nothing was marked. '
                                         '<a href="/seat/inbox">Open the inbox again</a>.</p>')
    return _redirect("/seat/inbox")


def _conversation(config: Config, conn: sqlite3.Connection, session: _Session, conversation_id: str, after: int,
                  reply: str | None, *, status: int = 200, notice: str = "", text: str = "",
                  request_id: str | None = None) -> Response:
    try:
        page = conversations.history(conn, session.role, conversation_id, after, HISTORY_PAGE)
    except BatonError:
        return _not_in()
    title = conn.execute("SELECT title FROM conversations WHERE id = ?", (conversation_id,)).fetchone()["title"]
    request_id = request_id or _mint_request(conn, session, conversation_id)
    shown = {int(item["seq"]) for item in page["messages"]}
    items = "".join(_history_item(conversation_id, item, shown) for item in page["messages"])
    newer = (f'<p class="pager"><a href="/seat/c/{_e(conversation_id)}?after_seq={page["next_seq"]}">Newer</a></p>'
             if len(page["messages"]) == HISTORY_PAGE else "")
    reply_field = (f'<input type="hidden" name="reply_to" value="{_e(reply)}"><p class="reply-to">Replying to a '
                   f'message. <a href="/seat/c/{_e(conversation_id)}">Cancel</a></p>' if reply else "")
    compose = (f'<form class="compose" method="post" action="/seat/c/{_e(conversation_id)}/send">{_csrf(session)}'
               f'<input type="hidden" name="request_id" value="{_e(request_id)}">{reply_field}'
               f'<label for="text">Reply as {_e(config.roles[session.role].label)}</label>'
               f'<textarea id="text" name="text" required maxlength="{messages.MAX_MESSAGE_BYTES}">{_e(text)}'
               '</textarea><button type="submit">Send</button></form>'
               '<p class="muted">Messages you send here are sent through authenticated seat as you. Viewing '
               'changes nothing.</p>')
    body = (f'<p><a href="/seat/">Seat</a> · <a href="/seat/inbox">Inbox</a></p><h1>{_e(title)}</h1>'
            f'{notice}<p class="muted">Your history starts when you were added.</p>'
            f'<ul>{items or "<li class=muted>Nothing yet.</li>"}</ul>{newer}{compose}')
    return _page(status, title, body)


def _history_item(conversation_id: str, item: dict[str, Any], shown: set[int]) -> str:
    parent = item.get("reply_to")
    marker = ""
    if parent:
        marker = ('<p class="reply-to unavailable">parent unavailable</p>' if parent.get("seq") is None else
                  "" if int(parent["seq"]) in shown else
                  f'<p class="reply-to">in reply to <a href="{_link(conversation_id, int(parent["seq"]))}">'
                  f'#{int(parent["seq"])}</a></p>')
    reply = (f' · <a href="/seat/c/{_e(conversation_id)}?reply={_e(item["message_id"])}#text">Reply</a>'
             if item.get("from_role") else "")
    words = (f'<p class="notice">{_e(item["notice"].get("text", ""))}</p>' if item.get("notice")
             else f'<p class="text">{_e(item["text"])}</p>')
    return f'<li id="m-{int(item["seq"])}">{marker}{_meta(item)}{reply}{words}</li>'


def _send(config: Config, conn: sqlite3.Connection, session: _Session, conversation_id: str,
          form: dict[str, str]) -> Response:
    """Membership and the form's own binding first, so a retry never replays past either; then one send."""
    if session.role not in _participants(conn, conversation_id):
        return _not_in(403, "You're no longer in this conversation, so nothing was sent.")
    request_id, text, reply_to = form["request_id"], form["text"], form.get("reply_to") or None
    now = _now()
    with transaction(conn):
        bound = conn.execute("SELECT * FROM seat_requests WHERE request_id = ?", (request_id,)).fetchone()
        sent = conn.execute("SELECT COUNT(*) FROM seat_requests WHERE session = ? AND sent_at > ?",
                            (session.digest, now - 3600)).fetchone()[0]
    if bound is None or bound["session"] != session.digest or bound["conversation_id"] != conversation_id:
        return _page(403, "Refused", '<p>This form belongs to another page. <a href="/seat/">Start again</a>.</p>')
    if bound["sent_at"] is None and sent >= SENDS_PER_HOUR:
        return _retry(config, conn, session, conversation_id, form, 429, "You've sent a lot this hour; try later.")
    try:
        result = conversations.send(conn, config, sender=session.role, conversation_id=conversation_id, text=text,
                                    request_id=request_id, reply_to=reply_to)
    except IdempotencyConflict:
        return _conversation(config, conn, session, conversation_id, 0, reply_to, status=409, text=text,
                             notice='<p class="notice">That form was already used for another message. Your text '
                                    'is below in a fresh form.</p>')
    except (ConversationRateLimited, ConversationFull, MailboxFull):
        return _retry(config, conn, session, conversation_id, form, 429, "The conversation isn't accepting "
                      "messages right now. Nothing was sent; try again shortly.")
    except NotAParticipant:
        return _not_in(403, "You're no longer in this conversation, so nothing was sent.")
    except BatonError:
        return _retry(config, conn, session, conversation_id, form, 400, "That couldn't be sent as written. "
                      "Nothing was sent.")
    with transaction(conn):
        conn.execute("UPDATE seat_requests SET sent_at = COALESCE(sent_at, ?) WHERE request_id = ?", (now, request_id))
        seq = conn.execute("SELECT conversation_seq FROM messages WHERE id = ? LIMIT 1",
                           (result["message_id"],)).fetchone()["conversation_seq"]
    return _redirect(_link(conversation_id, seq))


def _retry(config: Config, conn: sqlite3.Connection, session: _Session, conversation_id: str, form: dict[str, str],
           status: int, why: str) -> Response:
    """The same form again, with its text and its request id: whether or not the send happened, a repeat is safe."""
    return _conversation(config, conn, session, conversation_id, 0, form.get("reply_to") or None, status=status,
                         notice=f'<p class="notice">{_e(why)}</p>', text=form["text"],
                         request_id=form["request_id"])


def _mint_request(conn: sqlite3.Connection, session: _Session, conversation_id: str) -> str:
    request_id = messages.new_request_id()
    with transaction(conn):
        conn.execute("INSERT INTO seat_requests (request_id, session, conversation_id, created_at) VALUES"
                     " (?, ?, ?, ?)", (request_id, session.digest, conversation_id, _now()))
        conn.execute("DELETE FROM seat_requests WHERE session = ? AND sent_at IS NULL AND request_id NOT IN"
                     " (SELECT request_id FROM seat_requests WHERE session = ? AND sent_at IS NULL"
                     " ORDER BY created_at DESC LIMIT ?)", (session.digest, session.digest, MAX_REQUESTS))
    return request_id


def _participants(conn: sqlite3.Connection, conversation_id: str) -> set[str]:
    return {row["role"] for row in conn.execute("SELECT role FROM participants WHERE conversation_id = ?",
                                                (conversation_id,)).fetchall()}


# Helpers ---------------------------------------------------------------------------------------------------


def _link(conversation_id: str, seq: int) -> str:
    """A message's place in a conversation, by its conversation seq: the page that starts with it."""
    return f"/seat/c/{conversation_id}?after_seq={seq - 1}#m-{seq}"


def _meta(item: dict[str, Any]) -> str:
    who = item.get("from_role") or "the host"
    kind = " · person" if item.get("author_kind") == "human" else ""
    return f'<p class="meta"><span class="role">{_e(who)}</span>{kind} · {_e(item.get("at", ""))[:16]}</p>'


def _topic(topic: str | None) -> str:
    return f' <span class="badge">{_e(topic)}</span>' if topic else ""


def _csrf(session: _Session) -> str:
    return f'<input type="hidden" name="csrf" value="{_e(session.csrf)}">'


def _logout_form(session: _Session) -> str:
    return (f'<form class="inline" method="post" action="/seat/logout">{_csrf(session)}'
            '<button type="submit">Log out</button></form>')


def _not_in(status: int = 403, words: str = "You're not in this conversation.") -> Response:
    return _page(status, "Not available", f'<p>{_e(words)} <a href="/seat/">Your conversations</a></p>')


def _redirect(location: str) -> Response:
    return Response(303, f'<p><a href="{_e(location)}">Continue</a></p>'.encode(),
                    {**HEADERS, "Location": location, "Content-Type": "text/html; charset=utf-8"})


def _page(status: int, title: str, body: str) -> Response:
    document = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                f'<title>{_e(title)} · baton</title><link rel="stylesheet" href="/seat/style.css"></head>'
                f'<body>{body}</body></html>')
    return Response(status, document.encode(), {**HEADERS, "Content-Type": "text/html; charset=utf-8"})


def _not_found() -> Response:
    return _page(404, "Not found", '<p>Nothing here. <a href="/seat/">Seat</a></p>')


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


# Serving ---------------------------------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "baton-seat"
    sys_version = ""
    timeout = REQUEST_TIMEOUT

    def _answer(self) -> None:
        headers = list(self.headers.items())
        body = b""
        if self.command == "POST":
            self.close_connection = True  # one form per connection: nothing left over is ever read as a request
            length = self.headers.get("Content-Length", "")
            if (len(self.headers.get_all("Content-Length") or []) == 1 and "Transfer-Encoding" not in self.headers
                    and length.isascii() and length.isdigit() and int(length) <= MAX_BODY):
                body = self.rfile.read(int(length))
        response = respond(self.server.config, self.command, self.path, headers, body)
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        for cookie in response.cookies:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = _answer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
        """Paths name conversations, and nothing about a request belongs in the host's log."""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    config: Config


class _V6Server(_Server):
    address_family = socket.AF_INET6


def server(config: Config) -> ThreadingHTTPServer | None:
    """The seat's HTTP server, bound but not yet serving, or None when the config leaves it off."""
    settings = config.seat
    if not settings.enabled:
        return None
    family = _V6Server if ":" in settings.listen_host else _Server
    bound = family((settings.listen_host, settings.listen_port), _Handler)
    bound.config = config
    return bound


def start(config: Config) -> ThreadingHTTPServer | None:
    bound = server(config)
    if bound is not None:
        threading.Thread(target=bound.serve_forever, daemon=True, name="seat").start()
    return bound
