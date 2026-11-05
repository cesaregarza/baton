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

**Starting.** One host serves the seat over a database. A send holds its place in the hourly quota while it is in
flight, so a crash can leave a place held, and startup frees it. Only the host holding `seat.lock` may: it is
taken without waiting and kept until the process ends, because a second host, on any port, would reset a live
send's place. A host that can't take it refuses to start the seat, by the name `seat_already_running`.

Parsing and rendering live in their own modules, with shared values in models.
Stateful workflows and the HTTP lifecycle stay here.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
from collections import deque
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote
from zoneinfo import ZoneInfo

from baton import auth, board_read, conversations, datadir, jobs, messages
from baton.config import Config, ConfigError
from baton.database import Connection, Row
from baton.database.store import connect
from baton.errors import (
    BatonError,
    ConversationFull,
    ConversationRateLimited,
    IdempotencyConflict,
    MailboxFull,
    NotAParticipant,
)

from .models import Response as Response
from .models import _Refusal as _Refusal
from .models import _Request as _Request
from .models import _Session as _Session
from .parsing import FIELDS as FIELDS
from .parsing import FORM as FORM
from .parsing import MAX_BODY as MAX_BODY
from .parsing import PRE_COOKIE as PRE_COOKIE
from .parsing import SEND_PATH as SEND_PATH
from .parsing import SESSION_COOKIE as SESSION_COOKIE
from .parsing import SINGLE_HEADERS as SINGLE_HEADERS
from .parsing import _cookies as _cookies
from .parsing import _form as _form
from .parsing import _parse as _parse
from .rendering import HEADERS as HEADERS
from .rendering import STYLE as STYLE
from .rendering import WARN_UNREAD as WARN_UNREAD
from .rendering import _banner as _banner
from .rendering import _csrf as _csrf
from .rendering import _e as _e
from .rendering import _gap_notice as _gap_notice
from .rendering import _history_item as _history_item
from .rendering import _inbox_item as _inbox_item
from .rendering import _link as _link
from .rendering import _logout_form as _logout_form
from .rendering import _message as _message
from .rendering import _meta as _meta
from .rendering import _nav as _nav
from .rendering import _not_found as _not_found
from .rendering import _not_in as _not_in
from .rendering import _page as _page
from .rendering import _redirect as _redirect
from .rendering import _short as _short
from .rendering import _topic as _topic
from .rendering import _when as _when
from .rendering import _with_gaps as _with_gaps

COOKIE_ATTRIBUTES = "Secure; HttpOnly; SameSite=Strict; Path=/"
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
REQUEST_TIMEOUT = 10
LOCK_FILE = "seat.lock"
CONVERSATION_PATH = re.compile(r"/seat/c/([A-Za-z0-9_-]{1,64})")
SEQ = re.compile(r"[0-9]{1,19}")
# Where the conversation list's next page starts: after a conversation's creation time and id, as the store writes them.
CURSOR_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]{8,15}(\+00:00|Z)")
CONVERSATION_ID = re.compile(r"conv_[0-9a-f]{1,64}")
MAX_SEQ = 2**63 - 1  # SQLite's largest integer
MESSAGE_ID = re.compile(r"msg_[0-9a-f]{1,64}")

_failures: dict[str, deque[float]] = {}
_failures_lock = threading.Lock()


def _now() -> float:
    return time.time()


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


# Routing ---------------------------------------------------------------------------------------------------


def _route(config: Config, conn: Connection, request: _Request) -> Response:
    if request.path == "/seat/style.css" and request.method != "POST":
        return Response(200, STYLE.encode(), {**HEADERS, "Content-Type": "text/css; charset=utf-8"})
    if request.path == "/seat/login":
        return _login(config, conn, request) if request.method == "POST" else _login_form(conn)
    session = _session(config, conn, request.cookies.get(SESSION_COOKIE))
    if session is None:
        return _to_login()
    if request.method == "POST" and not _same(request.form.get("csrf", ""), session.csrf):
        return _page(403, "Refused", '<p>This form is from another session. <a href="/seat/">Start again</a>.</p>')
    if request.method == "POST":
        return _route_post(config, conn, session, request)
    return _route_get(config, conn, session, request)


def _route_post(config: Config, conn: Connection, session: _Session, request: _Request) -> Response:
    if request.path == "/seat/logout":
        return _logout(conn, session)
    if request.path == "/seat/ack":
        return _ack(config, conn, session, request.form["receipt"])
    return _send(config, conn, session, SEND_PATH.fullmatch(request.path).group(1), request.form)


def _route_get(config: Config, conn: Connection, session: _Session, request: _Request) -> Response:
    if request.path in ("/seat", "/seat/"):
        query = parse_qs(request.query, keep_blank_values=True)
        at, ident = query.get("before_at", []), query.get("before_id", [])
        if (at or ident) and not (
            len(at) == len(ident) == 1 and CURSOR_AT.fullmatch(at[0]) and CONVERSATION_ID.fullmatch(ident[0])
        ):
            return _page(400, "Bad request", "<p>That link isn't one the seat makes.</p>")
        return _home(config, conn, session, (at[0], ident[0]) if at else None)
    if request.path == "/seat/inbox":
        return _inbox(config, conn, session)
    if request.path == "/seat/needs":
        return _needs(config, conn, session)
    match = CONVERSATION_PATH.fullmatch(request.path)
    if match:
        query = parse_qs(request.query, keep_blank_values=True)
        after = query.get("after_seq", ["0"])[-1]
        reply = query.get("reply", [""])[-1]
        if not SEQ.fullmatch(after) or int(after) > MAX_SEQ or (reply and not MESSAGE_ID.fullmatch(reply)):
            return _page(400, "Bad request", "<p>That link isn't one the seat makes.</p>")
        return _conversation(config, conn, session, match.group(1), int(after), reply or None)
    return _not_found()


# Login and sessions ----------------------------------------------------------------------------------------


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _login_form(conn: Connection, status: int = 200, notice: str = "", *, paused: bool = False) -> Response:
    """A fresh login form, or a refusal once MAX_PRE forms, used or not, were issued in the last PRE_TTL.

    Forms are kept once used or expired, so refusing is what bounds the table: no form is evicted to make room.
    The refusal adds its own notice under the caller's, unless the caller is `paused`: that notice already says how
    long to wait, and a second wait under it would contradict the first.
    """
    pre = secrets.token_urlsafe(24)
    now = _now()
    with conn.transaction():
        issued = conn.execute("SELECT COUNT(*) FROM seat_prelogin WHERE expires_at > ?", (now,)).fetchone()[0]
        if issued < MAX_PRE:
            conn.execute("INSERT INTO seat_prelogin (digest, expires_at) VALUES (?, ?)", (_digest(pre), now + PRE_TTL))
    if issued >= MAX_PRE:
        cap = "" if paused else '<p class="notice-box">Too many login forms are open. Try again in a few minutes.</p>'
        return _page(429, "Log in", f"<h1>Log in to your seat</h1>{notice}{cap}")
    body = (
        f'<h1>Log in to your seat</h1>{notice}<form class="login" method="post" action="/seat/login">'
        '<label for="token">Seat token</label>'
        '<input id="token" type="password" name="token" autocomplete="current-password" autocapitalize="off" '
        'spellcheck="false" required>'
        f'<input type="hidden" name="pre" value="{_e(pre)}"><p><button type="submit">Log in</button></p></form>'
        '<p class="note">Paste the token from your password manager. It is checked once and not kept; your '
        "session ends after an hour without use, or 12 hours in all.</p>"
    )
    response = _page(status, "Log in", body)
    response.cookies.append(f"{PRE_COOKIE}={pre}; Max-Age={PRE_TTL}; {COOKIE_ATTRIBUTES}")
    return response


def _login(config: Config, conn: Connection, request: _Request) -> Response:
    pre = request.form["pre"]
    now = _now()
    with conn.transaction():
        fresh = conn.execute(
            "UPDATE seat_prelogin SET ended_at = ?, end_reason = 'used' WHERE digest = ?"
            " AND expires_at > ? AND ended_at IS NULL",
            (now, _digest(pre), now),
        ).rowcount
    if not fresh or not _same(request.cookies.get(PRE_COOKIE, ""), pre):
        return _login_form(conn, 403, '<p class="notice-box">That login form had expired. Paste the token again.</p>')
    key = str(config.db_path)
    with _failures_lock:
        failures = _failures.setdefault(key, deque())
        while failures and now - failures[0] > 3600:
            failures.popleft()
        if len(failures) >= FAILURES_PER_HOUR:
            return _login_form(
                conn,
                429,
                '<p class="notice-box">Too many tries. Logging in is paused for up to an hour.</p>',
                paused=True,
            )
    token = request.form["token"].strip()
    access = auth.static_token(config, token) if token else None
    if access is None or auth.role_of(access) != config.seat.role or not _seat_role_ok(config):
        with _failures_lock:
            _failures[key].append(now)
        return _login_form(
            conn, 401, '<p class="notice-box">That token was not accepted. Check you copied the whole seat token.</p>'
        )
    authority = auth.credential_authority(access)
    raw = "seat_" + secrets.token_urlsafe(32)
    with conn.transaction():
        _end_lapsed(config, conn, now)
        for row in conn.execute(
            "SELECT digest FROM seat_sessions WHERE role = ? AND ended_at IS NULL"
            " ORDER BY last_seen DESC LIMIT -1 OFFSET ?",
            (config.seat.role, MAX_SESSIONS - 1),
        ).fetchall():
            _end(conn, row["digest"], now, "cap")
        conn.execute(
            "INSERT INTO seat_sessions (digest, role, authority, created_at, last_seen) VALUES (?, ?, ?, ?, ?)",
            (_digest(raw), config.seat.role, json.dumps(authority, sort_keys=True), now, now),
        )
    response = _redirect("/seat/")
    response.cookies += [
        f"{SESSION_COOKIE}={raw}; {COOKIE_ATTRIBUTES}",
        f"{PRE_COOKIE}=; Max-Age=0; {COOKIE_ATTRIBUTES}",
    ]
    return response


def _seat_role_ok(config: Config) -> bool:
    role = config.roles.get(config.seat.role or "")
    return config.seat.enabled and role is not None and role.human and not role.display_only


def _session(config: Config, conn: Connection, raw: str | None) -> _Session | None:
    """The live session `raw` names, or None; a session that no longer holds is ended on the spot."""
    if not raw:
        return None
    digest, now = _digest(raw), _now()
    with conn.transaction():
        row = conn.execute("SELECT * FROM seat_sessions WHERE digest = ?", (digest,)).fetchone()
        if row is None or row["ended_at"] is not None:
            return None
        authority = json.loads(row["authority"])
        live = (
            now - row["last_seen"] <= config.seat.idle_seconds
            and now - row["created_at"] <= config.seat.absolute_seconds
            and row["role"] == config.seat.role == authority.get("role")
            and _seat_role_ok(config)
            and auth.authority_live(config, authority)
        )
        if not live:
            _end(conn, digest, now, "lapsed")
            return None
        conn.execute("UPDATE seat_sessions SET last_seen = ? WHERE digest = ?", (now, digest))
    return _Session(raw, digest, row["role"], ZoneInfo(config.seat.timezone))


def _end(conn: Connection, digest: str, now: float, reason: str) -> None:
    """End a session, and the receipts and form ids still live under it, by state; every row stays."""
    conn.execute(
        "UPDATE seat_sessions SET ended_at = ?, end_reason = ? WHERE digest = ? AND ended_at IS NULL",
        (now, reason, digest),
    )
    for table in ("seat_receipts", "seat_requests"):
        conn.execute(
            f"UPDATE {table} SET ended_at = ?, end_reason = 'session' WHERE session = ? AND ended_at IS NULL",
            (now, digest),
        )


def _end_lapsed(config: Config, conn: Connection, now: float) -> None:
    """End idle or old sessions, and form ids older than REQUEST_TTL. An expired receipt needs nothing: its own
    `expires_at` refuses it."""
    for row in conn.execute(
        "SELECT digest FROM seat_sessions WHERE ended_at IS NULL AND (last_seen < ? OR created_at < ?)",
        (now - config.seat.idle_seconds, now - config.seat.absolute_seconds),
    ).fetchall():
        _end(conn, row["digest"], now, "lapsed")
    conn.execute(
        "UPDATE seat_requests SET ended_at = ?, end_reason = 'expired' WHERE ended_at IS NULL AND created_at < ?",
        (now, now - REQUEST_TTL),
    )


def _logout(conn: Connection, session: _Session) -> Response:
    with conn.transaction():
        _end(conn, session.digest, _now(), "logout")
    return _to_login()


def logout_all(config: Config) -> int:
    """End every seat session at once; `baton seat logout-all`."""
    conn = connect(config.db_path)
    try:
        with conn.transaction():
            live, now = conn.execute("SELECT digest FROM seat_sessions WHERE ended_at IS NULL").fetchall(), _now()
            for row in live:
                _end(conn, row["digest"], now, "logout_all")
        return len(live)
    finally:
        conn.close()


def _to_login() -> Response:
    response = _redirect("/seat/login")
    response.cookies.append(f"{SESSION_COOKIE}=; Max-Age=0; {COOKIE_ATTRIBUTES}")
    return response


# Pages -----------------------------------------------------------------------------------------------------


def _home(config: Config, conn: Connection, session: _Session, before: tuple[str, str] | None = None) -> Response:
    """One page of the conversations the seat may read, newest first. "Older" carries the cursor of the last row
    shown, as the dashboard's list does, so a conversation added or left between pages never shifts another
    out of view or onto two pages."""
    unread = messages.unread(conn, session.role)
    size = conversations.MAX_LISTED
    found = conversations.list_readable(conn, config, session.role, limit=size, before=before, _lookahead=True)
    listed = found[:size]
    items = "".join(
        f'<li><a href="/seat/c/{_e(c["conversation_id"])}">{_e(c["title"])}</a>{_topic(c.get("topic"))}</li>'
        for c in listed
    )
    links = ['<a href="/seat/">Newest conversations</a>'] if before else []
    if len(found) > size:
        at, ident = conversations.page_cursor(listed[-1])
        links.append(f'<a href="/seat/?before_at={_e(quote(at))}&amp;before_id={_e(ident)}">Older conversations</a>')
    pager = f'<p class="pager">{" · ".join(links)}</p>' if links else ""
    empty = (
        "<li class=muted>No older conversations.</li>"
        if before
        else "<li class=muted>You are not in any conversation yet.</li>"
    )
    inbox = f"{unread} unread" if unread else "Nothing unread"
    heading = "Readable conversations" if board_read.sees_all(config, session.role) else "Your conversations"
    body = (
        f"{_nav(session)}<h1>{_e(config.roles[session.role].label)}</h1>{_banner(unread)}"
        f'<p><a href="/seat/inbox">Inbox</a> <span class="muted">{inbox}</span></p>'
        f"<h2>{heading}</h2><ul>{items or empty}</ul>{pager}"
    )
    return _page(200, "Seat", body)


NEEDS_SHOWN = 100  # the newest of each list on /seat/needs; the count says when there are more
NEEDS_JOBS = 50


def _needs(config: Config, conn: Connection, session: _Session) -> Response:
    """What asks for the seat's own role, from what that role may read: the messages whose `attention` names it,
    then decisions asked of others or of nobody, collapsed, then the jobs waiting on it. Reading moves nothing:
    no cursor, no read state. "you replied" means only that you sent a reply; it approves or resolves nothing."""
    role = session.role
    mine, others, replied = conversations.attention_signals(conn, config, role, role, human_replies_only=True)

    def item(conversation: dict[str, Any], entry: dict[str, Any]) -> str:
        attention = entry.get("attention") or []
        labels = ["asks for you"] if role in attention else []
        if entry.get("needs_decision"):
            asked = [name for name in attention if name != role]
            labels.append(
                "a decision is needed"
                if role in attention
                else f"a decision is needed (asked of {', '.join(asked) or 'the whole conversation'})"
            )
        if entry["message_id"] in replied:
            labels.append("you replied")
        where = (
            f'<p class="actions"><a href="{_link(conversation["conversation_id"], int(entry["seq"]))}">'
            f"{_e(conversation['title'])}</a> · {_e(' · '.join(labels))}</p>"
        )
        return _message(entry, session, where)

    def counted(pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> tuple[str, str]:
        shown = pairs[:NEEDS_SHOWN]
        more = f"Showing the newest {len(shown)} of {len(pairs)}." if len(pairs) > len(shown) else ""
        return "".join(item(c, e) for c, e in shown) or "<li class=muted>None.</li>", more

    mine_items, mine_more = counted(mine)
    other_items, other_more = counted(others)
    waiting = jobs.list_jobs(conn, waiting_on=role, limit=NEEDS_JOBS)
    total = jobs.count_jobs(conn, waiting_on=role)
    job_items = (
        "".join(f'<li>{_e(job["title"])} <span class="muted">{_e(job["state"])}</span></li>' for job in waiting)
        or "<li class=muted>None.</li>"
    )
    job_more = f"Showing {len(waiting)} of {total}." if total > len(waiting) else ""
    body = (
        f"{_nav(session)}<h1>Needs you</h1>"
        f'<p class="muted">{len(mine)} asked for you · {len(others)} decisions not addressed to you · '
        f"{total} job{' is' if total == 1 else 's are'} waiting on you. Each message asked for you, or for a "
        'decision, when it was sent. "you replied" only means you sent a reply to it, which approves or '
        "resolves nothing, and looking here marks nothing as seen.</p>"
        f'<h2 id="for-you">For you</h2>{f"<p class=muted>{mine_more}</p>" if mine_more else ""}'
        f'<ul class="history">{mine_items}</ul>'
        f'<details id="decisions"><summary>Decisions not addressed to you ({len(others)})</summary>'
        f"{f'<p class=muted>{other_more}</p>' if other_more else ''}"
        f'<ul class="history">{other_items}</ul></details>'
        f'<h2 id="jobs">Jobs waiting on you</h2>{f"<p class=muted>{job_more}</p>" if job_more else ""}'
        f"<ul>{job_items}</ul>"
    )
    return _page(200, "Needs you", body)


def _inbox(config: Config, conn: Connection, session: _Session) -> Response:
    """The mailbox from its cursor, without moving it, and a receipt for exactly the page shown."""
    with conn.transaction():
        start = messages.read_seq(conn, session.role)
        rows = conn.execute(
            "SELECT * FROM messages WHERE recipient = ? AND seq > ? ORDER BY seq LIMIT ?",
            (session.role, start, INBOX_PAGE),
        ).fetchall()
        member = {
            row["conversation_id"]: row["title"]
            for row in conn.execute(
                "SELECT p.conversation_id, c.title FROM participants p JOIN conversations c ON c.id = p.conversation_id"
                " WHERE p.role = ?",
                (session.role,),
            ).fetchall()
        }
        page = messages.as_page(rows, lambda row: _inbox_entry(row, member))
        receipt = ""
        if page:
            receipt = secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO seat_receipts (digest, session, role, start_seq, last_seq, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (_digest(receipt), session.digest, session.role, start, page[-1]["seq"], _now() + RECEIPT_TTL),
            )
            conn.execute(
                "UPDATE seat_receipts SET ended_at = ?, end_reason = 'cap' WHERE session = ? AND ended_at IS"
                " NULL AND digest NOT IN (SELECT digest FROM seat_receipts WHERE session = ? AND ended_at IS"
                " NULL ORDER BY expires_at DESC LIMIT ?)",
                (_now(), session.digest, session.digest, MAX_RECEIPTS),
            )
    unread = messages.unread(conn, session.role)
    items = "".join(_inbox_item(entry, session) for entry in page)
    ack = (
        f'<form class="bar" method="post" action="/seat/ack">{_csrf(session)}'
        f'<input type="hidden" name="receipt" value="{_e(receipt)}">'
        f'<button type="submit">Mark these {len(page)} as seen</button>'
        '<p class="note">Seen only moves your inbox along. It is not a reply and approves nothing.</p></form>'
        if page
        else ""
    )
    more = f" Showing the oldest {len(page)}." if unread > len(page) else ""
    body = (
        f"{_nav(session)}<h1>Inbox</h1>{_banner(unread)}"
        f'<p class="muted">{unread} unread, oldest first.{more} Reading here marks nothing.</p>'
        f"<ul>{items or '<li class=muted>Nothing unread.</li>'}</ul>{ack}"
    )
    return _page(200, "Inbox", body)


def _inbox_entry(row: Row, member: dict[str, str]) -> dict[str, Any]:
    if row["conversation_id"] is not None and row["conversation_id"] not in member:
        return {"seq": row["seq"], "placeholder": True}
    return {
        **messages.as_dict(row),
        "conversation_seq": row["conversation_seq"],
        "title": member.get(row["conversation_id"] or ""),
    }


def _ack(config: Config, conn: Connection, session: _Session, receipt: str) -> Response:
    with conn.transaction():
        row = conn.execute(
            "SELECT * FROM seat_receipts WHERE digest = ? AND session = ?", (_digest(receipt), session.digest)
        ).fetchone()
        # Single use, as before: any attempt spends it, whether or not it moves the cursor.
        conn.execute(
            "UPDATE seat_receipts SET ended_at = ?, end_reason = 'used' WHERE digest = ? AND ended_at IS NULL",
            (_now(), _digest(receipt)),
        )
        current = messages.read_seq(conn, session.role)
        if (
            row is None
            or row["ended_at"] is not None
            or row["expires_at"] <= _now()
            or row["role"] != session.role
            or row["start_seq"] != current
        ):
            stale = True
        else:
            stale = False
            messages.mark_read(conn, session.role, row["last_seq"])
    if stale:
        return _page(
            409,
            "Out of date",
            "<h1>Nothing was marked</h1><p>This page is out of date: your inbox "
            "moved on since it was shown, so marking it could skip a message.</p>"
            '<p><a href="/seat/inbox">Open the inbox again</a></p>',
        )
    return _redirect("/seat/inbox")


def _conversation(
    config: Config,
    conn: Connection,
    session: _Session,
    conversation_id: str,
    after: int,
    reply: str | None,
    *,
    status: int = 200,
    notice: str = "",
    text: str = "",
    request_id: str | None = None,
) -> Response:
    try:
        page = conversations.history(conn, session.role, conversation_id, after, HISTORY_PAGE, config=config)
    except BatonError:
        return _not_in()
    title = conn.execute("SELECT title FROM conversations WHERE id = ?", (conversation_id,)).fetchone()["title"]
    request_id = request_id or _mint_request(conn, session, conversation_id)
    shown = {int(item["seq"]) for item in page["messages"]}
    items = _with_gaps(
        [(item["seq"], _history_item(conversation_id, item, shown, session)) for item in page["messages"]],
        page["retention_gaps"],
    )
    # A page can end early on its byte budget, so whether more follow is asked of the store, not the page's length.
    readable_message, values = board_read.message_condition(config, session.role)
    more = conn.execute(
        f"SELECT 1 FROM messages m WHERE m.conversation_id = :conversation_id "
        f"AND m.conversation_seq > :next_seq AND {readable_message} LIMIT 1",
        {**values, "conversation_id": conversation_id, "next_seq": page["next_seq"]},
    ).fetchone()
    newer = (
        f'<p class="pager"><a href="/seat/c/{_e(conversation_id)}?after_seq={page["next_seq"]}">Newer</a></p>'
        if page["messages"] and more
        else ""
    )
    parent = next((item for item in page["messages"] if item["message_id"] == reply), None) if reply else None
    quoted = f'<blockquote class="relayed"><p>{_e(_short(parent["text"]))}</p></blockquote>' if parent else ""
    reply_field = (
        f'<input type="hidden" name="reply_to" value="{_e(reply)}"><p class="muted reply-to">Replying to '
        f"{_e(messages.author_label(parent) if parent else 'a message')}. "
        f'<a href="/seat/c/{_e(conversation_id)}#compose">Cancel</a></p>{quoted}'
        if reply
        else ""
    )
    label = config.roles[session.role].label
    compose = (
        f'<form class="compose" id="compose" method="post" action="/seat/c/{_e(conversation_id)}/send">'
        f'{_csrf(session)}<input type="hidden" name="request_id" value="{_e(request_id)}">{reply_field}'
        f'<label for="text">Reply as {_e(label)}</label>'
        f'<textarea id="text" name="text" required maxlength="{messages.MAX_MESSAGE_BYTES}">{_e(text)}'
        '</textarea><p><button type="submit">Send</button></p>'
        '<p class="note">Sent through authenticated seat as you. Viewing changes nothing.</p></form>'
    )
    earlier = (
        f'<p class="pager"><a href="/seat/c/{_e(conversation_id)}?after_seq={max(0, after - HISTORY_PAGE)}">'
        "Earlier messages</a></p>"
        if after > 0
        else ""
    )
    body = (
        f"{_nav(session)}<h1>{_e(title)}</h1>{notice}"
        f'<p class="muted">From when you were added. <a href="#compose">Write a reply</a></p>'
        f"{earlier}<ul>{items or '<li class=muted>Nothing yet.</li>'}</ul>{newer}{compose}"
    )
    return _page(status, title, body)


def _send(config: Config, conn: Connection, session: _Session, conversation_id: str, form: dict[str, str]) -> Response:
    """Membership and the form's own binding first, so a retry never replays past either; then one send."""
    if session.role not in _participants(conn, conversation_id):
        return _not_in(403, "You're no longer in this conversation, so nothing was sent.")
    request_id = form["request_id"]
    now = _now()
    # A form's place in the hourly quota belongs to the form id, not to one request: the quota check and the
    # reservation are one transaction, every request for the id holds the place while it is in flight, and the
    # last one out gives it back only if nothing was stored under the id. So two overlapping sends can't both
    # take the last place, and a failed attempt can't free a place its own retry is about to use.
    with conn.transaction():
        bound = conn.execute("SELECT * FROM seat_requests WHERE request_id = ?", (request_id,)).fetchone()
        mine = (
            bound is not None
            and bound["ended_at"] is None
            and bound["session"] == session.digest
            and bound["conversation_id"] == conversation_id
        )
        full = False
        if mine and bound["sent_at"] is None:
            taken = conn.execute(
                "SELECT COUNT(*) FROM seat_requests WHERE session = ? AND sent_at > ?", (session.digest, now - 3600)
            ).fetchone()[0]
            full = taken >= SENDS_PER_HOUR
            if not full:
                conn.execute("UPDATE seat_requests SET sent_at = ? WHERE request_id = ?", (now, request_id))
        if mine and not full:
            conn.execute("UPDATE seat_requests SET holders = holders + 1 WHERE request_id = ?", (request_id,))
    if not mine:
        return _page(403, "Refused", '<p>This form belongs to another page. <a href="/seat/">Start again</a>.</p>')
    if full:
        return _retry(config, conn, session, conversation_id, form, 429, "You've sent a lot this hour; try later.")
    try:
        return _store(config, conn, session, conversation_id, form)
    finally:
        with conn.transaction():
            conn.execute("UPDATE seat_requests SET holders = holders - 1 WHERE request_id = ?", (request_id,))
            conn.execute(
                "UPDATE seat_requests SET sent_at = NULL WHERE request_id = ? AND holders = 0 AND NOT"
                " EXISTS (SELECT 1 FROM message_requests WHERE request_id = ?)",
                (request_id, request_id),
            )


def _store(config: Config, conn: Connection, session: _Session, conversation_id: str, form: dict[str, str]) -> Response:
    request_id, text, reply_to = form["request_id"], form["text"], form.get("reply_to") or None
    try:
        result = conversations.send(
            conn,
            config,
            sender=session.role,
            conversation_id=conversation_id,
            text=text,
            request_id=request_id,
            reply_to=reply_to,
        )
    except IdempotencyConflict:
        return _conversation(
            config,
            conn,
            session,
            conversation_id,
            0,
            reply_to,
            status=409,
            text=text,
            notice='<p class="notice-box">That form was already used for another message. Your '
            "text is below in a fresh form.</p>",
        )
    except (ConversationRateLimited, ConversationFull, MailboxFull):
        return _retry(
            config,
            conn,
            session,
            conversation_id,
            form,
            429,
            "The conversation isn't accepting messages right now. Nothing was sent; try again shortly.",
        )
    except NotAParticipant:
        return _not_in(403, "You're no longer in this conversation, so nothing was sent.")
    except BatonError:
        return _retry(
            config, conn, session, conversation_id, form, 400, "That couldn't be sent as written. Nothing was sent."
        )
    # Core checks membership before it looks a retry up, in one transaction, so it never replays to a role that was
    # already out. A role can still leave after that transaction; it is then told so, not redirected to the
    # replayed message that it can no longer open.
    if result.get("created") is False and session.role not in _participants(conn, conversation_id):
        return _not_in(403, "You're no longer in this conversation.")
    seq = conn.execute(
        "SELECT conversation_seq FROM messages WHERE id = ? LIMIT 1", (result["message_id"],)
    ).fetchone()["conversation_seq"]
    return _redirect(_link(conversation_id, seq))


def _retry(
    config: Config,
    conn: Connection,
    session: _Session,
    conversation_id: str,
    form: dict[str, str],
    status: int,
    why: str,
) -> Response:
    """The same form again, with its text and its request id: whether or not the send happened, a repeat is safe."""
    return _conversation(
        config,
        conn,
        session,
        conversation_id,
        0,
        form.get("reply_to") or None,
        status=status,
        notice=f'<p class="notice-box">{_e(why)}</p>',
        text=form["text"],
        request_id=form["request_id"],
    )


def _mint_request(conn: Connection, session: _Session, conversation_id: str) -> str:
    request_id = messages.new_request_id()
    with conn.transaction():
        conn.execute(
            "INSERT INTO seat_requests (request_id, session, conversation_id, created_at) VALUES (?, ?, ?, ?)",
            (request_id, session.digest, conversation_id, _now()),
        )
        # At most MAX_REQUESTS live per session, sent or not: this hour's sends first (the quota counts them),
        # then the newest forms. The rest end by state and stay.
        conn.execute(
            "UPDATE seat_requests SET ended_at = ?, end_reason = 'cap' WHERE session = ? AND ended_at IS NULL"
            " AND request_id NOT IN (SELECT request_id FROM seat_requests WHERE session = ? AND ended_at IS"
            " NULL ORDER BY (sent_at IS NOT NULL AND sent_at > ?) DESC, created_at DESC LIMIT ?)",
            (_now(), session.digest, session.digest, _now() - 3600, MAX_REQUESTS),
        )
    return request_id


def _participants(conn: Connection, conversation_id: str) -> set[str]:
    return {
        row["role"]
        for row in conn.execute(
            "SELECT role FROM participants WHERE conversation_id = ?", (conversation_id,)
        ).fetchall()
    }


# Constant-time comparison ----------------------------------------------------------------------------------


def _same(given: str, expected: str) -> bool:
    """A constant-time comparison that refuses, rather than raises on, a value that isn't ASCII."""
    return hmac.compare_digest(given.encode(), expected.encode())


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
            if (
                len(self.headers.get_all("Content-Length") or []) == 1
                and "Transfer-Encoding" not in self.headers
                and length.isascii()
                and length.isdigit()
                and int(length) <= MAX_BODY
            ):
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

    def log_message(self, format: str, *args: Any) -> None:
        """Paths name conversations, and nothing about a request belongs in the host's log."""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    config: Config
    lock: int  # the descriptor holding LOCK_FILE's lock, open until the process ends: see `_lock_database`


class _V6Server(_Server):
    address_family = socket.AF_INET6


class SeatAlreadyRunning(ConfigError):
    """Another host already serves the seat over this database, and only one may: it frees what a crash left."""

    code = "seat_already_running"


def server(config: Config) -> ThreadingHTTPServer | None:
    """The seat's HTTP server, bound but not yet serving, or None when the config leaves it off.

    Startup takes the database's lock, binds, then frees what cut-off sends still hold, in that order. A host that
    can't take the lock is refused by name before it touches a reservation, and one that can't bind frees nothing.
    A startup that fails gives back what it took; one that succeeds keeps the lock until the process ends.
    """
    settings = config.seat
    if not settings.enabled:
        return None
    with ExitStack() as undo:
        lock = _lock_database(config)
        undo.callback(os.close, lock)
        family = _V6Server if ":" in settings.listen_host else _Server
        bound = family((settings.listen_host, settings.listen_port), _Handler)
        undo.callback(bound.server_close)
        _free_cut_off_sends(config)
        undo.pop_all()  # started: the process keeps the listener and the lock, the lock until it ends
    bound.config = config
    bound.lock = lock
    return bound


def _lock_database(config: Config) -> int:
    """Take the database's exclusive seat lock without waiting, and return the descriptor that holds it.

    Only the host holding it may free the places that cut-off sends hold: it alone knows no other seat has a send
    in flight, where a second host, even on another port, would reset a live send's place. The file is opened as
    the data directory's other lock files are, but nothing here releases it. The kernel does when the process ends,
    however it ends; closing the server doesn't, since a request thread can outlive `server_close()`.
    """
    directory = config.data_dir
    with datadir.opened(directory) as dir_fd:
        lock = datadir.open_file(dir_fd, LOCK_FILE, os.O_RDWR | os.O_CREAT, directory=directory)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock)
            raise SeatAlreadyRunning(
                f"another baton host is already serving the seat over the database in "
                f"{directory}: it holds {directory / LOCK_FILE}. Stop it, or give this host a "
                "data_dir of its own. This startup changed nothing."
            ) from None
        except BaseException:
            os.close(lock)
            raise
    return lock


def _free_cut_off_sends(config: Config) -> None:
    """Give back the hourly places that sends cut off by a crash still hold, in one transaction.

    `_send` takes a form's place and gives it back in a `finally`; an exit between the two leaves the place held,
    and counted until its hour passes. Only the host holding the database's lock calls this, so no other seat has a
    request in flight and nothing is legitimately held: every form's holders go to 0, and a form with no stored
    message (in `message_requests`) has its place freed. A stored send stays counted.
    """
    conn = connect(config.db_path)
    try:
        with conn.transaction():
            conn.execute("UPDATE seat_requests SET holders = 0 WHERE holders != 0")
            conn.execute(
                "UPDATE seat_requests SET sent_at = NULL WHERE sent_at IS NOT NULL AND NOT EXISTS"
                " (SELECT 1 FROM message_requests WHERE request_id = seat_requests.request_id)"
            )
    finally:
        conn.close()


def start(config: Config) -> ThreadingHTTPServer | None:
    bound = server(config)
    if bound is not None:
        threading.Thread(target=bound.serve_forever, daemon=True, name="seat").start()
    return bound
