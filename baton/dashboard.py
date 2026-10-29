"""An optional, read-only view of the host's conversations, for a person at the host's own machine.

Off unless `[dashboard] enabled = true`. It is served from the host's own
store by a small standard-library HTTP server beside the MCP server, on a
loopback address only, and it never sees a token.

What it shows is what one configured `viewer_role` may see: the
conversations that role is in, from its join point, exactly as
`read_history` returns them. A role sees every conversation only when its
config sets `can_view_all_conversations`. Reading is bookmark-neutral:
viewing marks nothing read and moves no cursor.

The threat model is the machine it runs on. The view has no login, so any
person or process that can reach the port can read it; on a shared machine,
choose a viewer role that sees little. Against other sites in a browser on
the same machine, it answers only to its own loopback Host name (so DNS
rebinding gets nothing), sends no CORS headers, can't be framed, takes no
writes, and pages carry no scripts under a CSP that allows none. Reaching it
from anywhere else needs its own auth in front of it, and is the operator's
separate decision.
"""

from __future__ import annotations

import html
import re
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from baton import conversations, messages
from baton.config import Config
from baton.errors import BatonError
from baton.store import connect

HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; base-uri 'none'; form-action 'none'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}
READS = ("GET", "HEAD")
CONVERSATION_PATH = re.compile(r"/c/([A-Za-z0-9_-]{1,64})")
# A conversation seq: ASCII digits only (str.isdigit also takes superscripts), within SQLite's signed 64 bits.
SEQ = re.compile(r"[0-9]{1,19}")
MAX_SEQ = 2**63 - 1
STYLE = """\
body { font: 15px/1.45 system-ui, sans-serif; margin: 0 auto; max-width: 52rem; padding: 1rem; color: #1d1d1f; }
a { color: #0b57d0; }
ul { list-style: none; padding: 0; }
li { border-bottom: 1px solid #e3e3e3; padding: .6rem 0; }
.meta, .notice, .source, .muted { color: #5f6368; font-size: .85rem; }
.role { font-weight: 600; color: #1d1d1f; }
.badge { border: 1px solid #c4c7c5; border-radius: .6rem; padding: 0 .4rem; margin-left: .3rem; }
.text { white-space: pre-wrap; margin: .3rem 0; }
.relayed { border-left: 3px solid #c4c7c5; margin: .4rem 0 .4rem .6rem; padding-left: .6rem; }
.relayed-text { white-space: pre-wrap; margin: .2rem 0; }
.notice { font-style: italic; }
.replies { margin: .4rem 0 0 1.2rem; border-left: 2px solid #e3e3e3; padding-left: .6rem; }
.reply-to { color: #5f6368; font-size: .85rem; margin: .2rem 0; }
"""


@dataclass
class Response:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


def respond(config: Config, method: str, target: str, host: str) -> Response:
    """Answer one request. Nothing here writes: only GET and HEAD are served."""
    if host not in _own_hosts(config):
        return _page(421, "Wrong address", "<p>This view answers only at its own loopback address.</p>")
    if method not in READS:
        return Response(405, b"read-only\n", {**HEADERS, "Allow": "GET, HEAD",
                                              "Content-Type": "text/plain; charset=utf-8"})
    url = urlsplit(target)
    if url.path == "/style.css":
        return Response(200, STYLE.encode(), {**HEADERS, "Content-Type": "text/css; charset=utf-8"})
    match = CONVERSATION_PATH.fullmatch(url.path)
    if url.path != "/" and not match:
        return _not_found()
    conn = connect(config.db_path)
    try:
        if match is None:
            return _page(200, "Conversations", _index(config, conn))
        after = _after_seq(parse_qs(url.query, keep_blank_values=True).get("after_seq", ["0"])[-1])
        if after is None:
            return _page(400, "Bad request", "<p>after_seq must be a whole number of at most 19 digits.</p>")
        return _conversation(config, conn, match.group(1), after)
    finally:
        conn.close()


def server(config: Config) -> ThreadingHTTPServer | None:
    """The dashboard's HTTP server, bound but not yet serving, or None when the config leaves it off."""
    settings = config.dashboard
    if not settings.enabled:
        return None
    family = _V6Server if ":" in settings.listen_host else _Server
    bound = family((settings.listen_host, settings.listen_port), _Handler)
    bound.config = config
    return bound


def start(config: Config) -> ThreadingHTTPServer | None:
    """Serve the dashboard on a background thread when the config enables it; the caller shuts it down."""
    bound = server(config)
    if bound is not None:
        threading.Thread(target=bound.serve_forever, daemon=True, name="dashboard").start()
    return bound


def _own_hosts(config: Config) -> set[str]:
    settings = config.dashboard
    address = f"[{settings.listen_host}]" if ":" in settings.listen_host else settings.listen_host
    return {f"{address}:{settings.listen_port}", f"localhost:{settings.listen_port}"}


def _index(config: Config, conn: Any) -> str:
    viewer = config.dashboard.viewer_role
    if config.roles[viewer].can_view_all_conversations:
        listed, scope = conversations.list_all(conn), "every conversation"
    else:
        listed, scope = conversations.list_for(conn, viewer), f"the conversations {viewer} is in"
    items = "".join(
        f'<li><a href="/c/{_e(item["conversation_id"])}">{_e(item["title"])}</a>'
        f'<div class="meta">{_e(", ".join(item["participants"]))} · started {_e(item["created_at"])}</div></li>'
        for item in listed)
    return (f'<h1>Conversations</h1><p class="muted">Read-only. Showing {_e(scope)}, as {_e(viewer)}.</p>'
            f'<ul>{items or "<li class=muted>None yet.</li>"}</ul>')


def _after_seq(value: str) -> int | None:
    if not SEQ.fullmatch(value) or int(value) > MAX_SEQ:
        return None
    return int(value)


def _conversation(config: Config, conn: Any, conversation_id: str, after: int) -> Response:
    viewer = config.dashboard.viewer_role
    if config.roles[viewer].can_view_all_conversations:
        def read(after_seq: int, limit: int) -> dict[str, Any]:
            return conversations.full_history(conn, conversation_id, after_seq, limit)
        listed = conversations.list_all
    else:
        def read(after_seq: int, limit: int) -> dict[str, Any]:
            return conversations.history(conn, viewer, conversation_id, after_seq, limit)
        def listed(conn: Any) -> list[dict[str, Any]]:
            return conversations.list_for(conn, viewer)
    try:
        page = read(after, messages.MAX_PAGE_MESSAGES)
        # A page ends at a byte budget as well as a row count, so ask whether anything follows it.
        later = bool(page["messages"]) and bool(read(page["next_seq"], 1)["messages"])
    except BatonError:  # unknown, or one the viewer can't see: the same answer, so neither is revealed
        return _not_found()
    visible = {item["conversation_id"]: item for item in listed(conn)}
    title = visible[conversation_id]["title"] if conversation_id in visible else conversation_id
    items = _thread(page["messages"])
    more = (f'<p><a href="/c/{_e(conversation_id)}?after_seq={page["next_seq"]}">Later messages</a></p>'
            if later else "")
    return _page(200, title, f'<p><a href="/">All conversations</a></p><h1>{_e(title)}</h1>'
                             f'<ul>{items or "<li class=muted>No messages yet.</li>"}</ul>{more}')


def _thread(items: list[dict[str, Any]]) -> str:
    """The page's messages with each reply nested under its parent when the parent is on this page."""
    on_page = {item["seq"] for item in items}
    replies: dict[int, list[dict[str, Any]]] = {}
    top = []
    for item in items:
        parent = (item.get("reply_to") or {}).get("seq")
        if parent in on_page and parent < item["seq"]:
            replies.setdefault(parent, []).append(item)
        else:
            top.append(item)

    def render(item: dict[str, Any]) -> str:
        nested = replies.get(item["seq"])
        inner = f'<ul class="replies">{"".join(render(reply) for reply in nested)}</ul>' if nested else ""
        return _message(item, inner, on_page)

    return "".join(render(item) for item in top)


def _message(item: dict[str, Any], replies: str = "", on_page: frozenset[int] | set[int] = frozenset()) -> str:
    notice = item.get("notice")
    if notice:
        verb = "added" if notice["event"] == "participant_added" else "removed"
        return (f'<li class="notice">{_e(notice["participant"])} was {verb} by {_e(notice["by"])}'
                f' · {_e(item["at"])}{replies}</li>')
    relayed = "".join(
        f'<blockquote class="relayed"><p class="source">relayed from {_e(entry["source"])} at {_e(entry["at"])}'
        f'</p><p class="relayed-text">{_e(entry["text"])}</p></blockquote>' for entry in item.get("relayed") or [])
    kind = item.get("author_kind")
    badge = f'<span class="badge {kind}">{kind}</span>' if kind in ("human", "agent") else ""
    return (f'<li class="message" id="m-{item["seq"]}"><div class="meta"><span class="role">'
            f'{_e(item["from_role"])}</span>{badge} · {_e(item["at"])} · #{item["seq"]}</div>'
            f'{_reply_marker(item.get("reply_to"), on_page)}<p class="text">{_e(item["text"])}</p>{relayed}'
            f'{replies}</li>')


def _reply_marker(reply_to: dict[str, Any] | None, on_page: frozenset[int] | set[int]) -> str:
    """Where a reply's parent is: nothing when it's nested here, a number when it's on another page, or
    'parent unavailable' when the viewer can't see it, without its words or author."""
    if not reply_to:
        return ""
    if reply_to.get("seq") is None:
        return '<p class="reply-to unavailable">parent unavailable</p>'
    if reply_to["seq"] in on_page:
        return ""
    return f'<p class="reply-to">in reply to #{int(reply_to["seq"])}</p>'



def _page(status: int, title: str, body: str) -> Response:
    document = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                f'<title>{_e(title)} · baton</title><link rel="stylesheet" href="/style.css"></head>'
                f'<body>{body}</body></html>')
    return Response(status, document.encode(), {**HEADERS, "Content-Type": "text/html; charset=utf-8"})


def _not_found() -> Response:
    return _page(404, "Not found", '<p>Nothing here. <a href="/">All conversations</a></p>')


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


class _Handler(BaseHTTPRequestHandler):
    server_version = "baton-dashboard"
    sys_version = ""

    def _answer(self) -> None:
        response = respond(self.server.config, self.command, self.path, self.headers.get("Host", ""))
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = _answer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
        """Paths name conversations; keep them out of the host's log."""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    config: Config


class _V6Server(_Server):
    address_family = socket.AF_INET6
