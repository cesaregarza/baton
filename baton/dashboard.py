"""An optional, read-only view of the host's conversations, for a person at the host's own machine.

Off unless `[dashboard] enabled = true`. It is served from the host's own
store by a small standard-library HTTP server beside the MCP server, on a
loopback address only, and it never sees a token.

Conversations are grouped by their topic, with the untagged in a group of
their own, so a topic organises the view without becoming a requirement.
`/t/<project>` and `/t/<project>/<subtopic>` show one topic and everything
under it, by whole segments, with the same filter as `list_conversations`.

`/needs` collects the retained signals that single the viewer out: messages
it could read whose `attention` names it or that ask for a decision, newest
first, and the jobs waiting on it. It reads only structured fields, never a
message's words, and no message has a resolved state, so nothing here says
whether a signal was answered. `?topic=` narrows the messages; jobs carry no
topic, so a filter shows none of them.

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
from urllib.parse import parse_qs, unquote, urlsplit

from baton import conversations, jobs, messages
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
# How many matching messages /needs shows, newest first; the page still counts all of them first.
MAX_NEEDS = messages.MAX_PAGE_MESSAGES
MAX_NEEDS_JOBS = 50
# How many conversations a list page shows, newest first; it says so when there are more.
MAX_LISTED = conversations.MAX_LISTED
STYLE = """\
body { font: 15px/1.45 system-ui, sans-serif; margin: 0 auto; max-width: 52rem; padding: 1rem; color: #1d1d1f; }
a { color: #0b57d0; }
ul { list-style: none; padding: 0; }
li { border-bottom: 1px solid #e3e3e3; padding: .6rem 0; }
.meta, .notice, .source, .muted { color: #5f6368; font-size: .85rem; }
.role { font-weight: 600; color: #1d1d1f; }
h2 { font-size: 1rem; margin: 1.2rem 0 .2rem; color: #5f6368; font-weight: 600; }
.badge { border: 1px solid #c4c7c5; border-radius: .6rem; padding: 0 .4rem; margin-left: .3rem; }
.text { white-space: pre-wrap; margin: .3rem 0; }
.relayed { border-left: 3px solid #c4c7c5; margin: .4rem 0 .4rem .6rem; padding-left: .6rem; }
.relayed-text { white-space: pre-wrap; margin: .2rem 0; }
.notice { font-style: italic; }
.replies { margin: .4rem 0 0 1.2rem; border-left: 2px solid #e3e3e3; padding-left: .6rem; }
.reply-to { color: #5f6368; font-size: .85rem; margin: .2rem 0; }
h2 a { color: inherit; }
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
    topic = _topic_route(url.path) if url.path.startswith("/t/") else None
    if (url.path.startswith("/t/") and topic is None) or (url.path == "/needs" and _needs_topic(url.query) is False):
        return _page(400, "Bad request", "<p>A topic is one or two lowercase slug segments, such as project or "
                                         "project/subtopic, given once.</p>")
    if url.path not in ("/", "/needs") and not match and topic is None:
        return _not_found()
    conn = connect(config.db_path)
    try:
        if url.path == "/needs":
            return _page(200, "Needs you", _needs(config, conn, _needs_topic(url.query)))
        if topic is not None:
            return _page(200, topic, _index(config, conn, topic))
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
    """The Host values the view answers to, compared whole. X-Forwarded-* and Forwarded are never read."""
    settings = config.dashboard
    address = f"[{settings.listen_host}]" if ":" in settings.listen_host else settings.listen_host
    hosts = {f"{address}:{settings.listen_port}", f"localhost:{settings.listen_port}"}
    return hosts | ({settings.proxy_host} if settings.proxy_host else set())


def _topic_route(path: str) -> str | None:
    """The topic a `/t/...` path names, each segment percent-decoded exactly once, or None if it isn't valid.

    An encoded slash stays inside its segment, so it makes the topic invalid instead of acting as a separator.
    """
    try:
        segments = [unquote(segment, errors="strict") for segment in path[len("/t/"):].split("/")]
    except UnicodeDecodeError:
        return None
    if any("/" in segment for segment in segments):
        return None
    try:
        return conversations.check_topic("/".join(segments))
    except BatonError:
        return None


def _needs_topic(query: str) -> str | None | bool:
    """`/needs`'s topic filter: None for none, the topic, or False when it is invalid or given more than once."""
    values = parse_qs(query, keep_blank_values=True).get("topic")
    if values is None:
        return None
    if len(values) != 1:
        return False
    try:
        return conversations.check_topic(values[0])
    except BatonError:
        return False


def _visible(config: Config, conn: Any, topic: str | None = None) -> tuple[list[dict[str, Any]], str, bool]:
    """Every conversation the viewer may see, newest first and optionally under one topic, read page by page so
    none is left out; a phrase saying which; and whether that is every conversation. A filter only narrows."""
    viewer = config.dashboard.viewer_role
    every = config.roles[viewer].can_view_all_conversations
    found: list[dict[str, Any]] = []
    while True:
        before = conversations.page_cursor(found[-1]) if found else None
        page = (conversations.list_all(conn, topic, before=before) if every
                else conversations.list_for(conn, viewer, topic, before=before))
        found += page
        if len(page) < conversations.MAX_LISTED:
            break
    return found, "every conversation" if every else f"the conversations {viewer} is in", every


def _index(config: Config, conn: Any, topic: str | None = None) -> str:
    viewer = config.dashboard.viewer_role
    listed, scope, _ = _visible(config, conn, topic)
    heading = (f'<h1>Conversations</h1><p class="muted">Read-only. Showing {_e(scope)}, as {_e(viewer)}. '
               '<a href="/needs">Needs you</a></p>')
    if topic is not None:
        heading = (f'<p><a href="/">All conversations</a> · {_e(" › ".join(topic.split("/")))}</p>'
                   f'<h1>{_e(topic)}</h1><p class="muted">Read-only. Showing {_e(scope)} under this topic, as '
                   f'{_e(viewer)}. <a href="/needs?topic={_e(topic)}">Needs you here</a></p>')
    if not listed:
        return heading + f'<ul><li class=muted>{"None yet." if topic is None else "None under this topic."}</li></ul>'
    shown = listed[:MAX_LISTED]
    more = (f'<p class="muted">More than one page holds: showing the newest {len(shown)} of {len(listed)} '
            'conversations.</p>' if len(shown) < len(listed) else "")
    groups = "".join(f'<h2>{_topic_link(group)}</h2><ul>' + "".join(_listing(item) for item in items) + "</ul>"
                     for group, items in _by_topic(shown))
    return heading + more + groups


def _topic_link(topic: str | None) -> str:
    return f'<a href="/t/{_e(topic)}">{_e(topic)}</a>' if topic else "No topic"


def _needs(config: Config, conn: Any, topic: str | None) -> str:
    """The retained signals that single out the viewer, from what it could read, then the jobs waiting on it."""
    viewer = config.dashboard.viewer_role
    found = _asking(config, conn, topic)
    shown = found[:MAX_NEEDS]
    count = (f'{len(found)} message{"" if len(found) == 1 else "s"} asked for you or for a decision'
             + (f", showing the newest {len(shown)} of {len(found)}" if len(shown) < len(found) else ""))
    where = f" under {_e(topic)}" if topic else ""
    items = "".join(_signal(viewer, conversation, item) for conversation, item in shown)
    if topic is not None:
        job_count, job_block = "", ('<h2>Jobs</h2><p class="muted">Jobs have no topic, so none are shown under a '
                                    'topic filter. <a href="/needs">See every job waiting on you</a>.</p>')
    else:
        total = jobs.count_jobs(conn, waiting_on=viewer)
        waiting = jobs.list_jobs(conn, waiting_on=viewer, limit=MAX_NEEDS_JOBS)
        job_count = (f' · {total} job{" is" if total == 1 else "s are"} waiting on you'
                     + (f", showing {len(waiting)} of {total}" if len(waiting) < total else ""))
        job_block = ('<h2>Jobs waiting on you</h2><ul>' + ("".join(_job(job) for job in waiting)
                                                           or "<li class=muted>None.</li>") + "</ul>")
    crumbs = '<p><a href="/">All conversations</a>' + (' · <a href="/needs">Needs you</a>' if topic else "") + "</p>"
    return (f'{crumbs}<h1>Needs you{where}</h1><p class="muted">As {_e(viewer)}. {count}{job_count}.</p>'
            '<p class="muted">Each message is a retained signal: it asked for you, or for a decision, when it was '
            'sent. Nothing records whether it was answered, and viewing changes nothing.</p>'
            f'<h2>Messages</h2><ul>{items or "<li class=muted>None.</li>"}</ul>{job_block}')


def _asking(config: Config, conn: Any, topic: str | None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Every message the viewer could read that names it in `attention` or asks for a decision, newest first.

    It reads each visible conversation through the same path as its page: `history` from the viewer's join
    point and within retention, or `full_history` for a viewer that sees every conversation. Each message
    comes once however many copies it has, and one the viewer sent itself isn't waiting on it.
    """
    viewer = config.dashboard.viewer_role
    listed, _, every = _visible(config, conn, topic)
    found = []
    for conversation in listed:
        after = 0
        while True:
            if every:
                page = conversations.full_history(conn, conversation["conversation_id"], after, MAX_NEEDS)
            else:
                page = conversations.history(conn, viewer, conversation["conversation_id"], after, MAX_NEEDS)
            if not page["messages"]:
                break
            found += [(conversation, item) for item in page["messages"]
                      if item["from_role"] not in (None, viewer)
                      and (item.get("needs_decision") or viewer in (item.get("attention") or []))]
            after = page["next_seq"]
    return sorted(found, key=lambda pair: (pair[1]["at"], pair[1]["seq"]), reverse=True)


def _signal(viewer: str, conversation: dict[str, Any], item: dict[str, Any]) -> str:
    """One message on /needs, linked to its place: the page that starts with it, so it is always on it.

    "asks for you" only when `attention` names the viewer. A decision request asks the conversation, so it
    shows for every reader; when its `attention` names other roles, the label says whom it was asked of.
    """
    seq = int(item["seq"])
    attention = item.get("attention") or []
    labels = ["asks for you"] if viewer in attention else []
    if item.get("needs_decision"):
        others = [role for role in attention if role != viewer]
        labels.append(f"a decision is needed (asked of {', '.join(others)})" if others and viewer not in attention
                      else "a decision is needed")
    why = " · ".join(labels)
    return (f'<li><div class="meta"><a href="/c/{_e(conversation["conversation_id"])}?after_seq={seq - 1}#m-{seq}">'
            f'{_e(conversation["title"])}</a> · <span class="role">{_e(item["from_role"])}</span> · '
            f'{_e(item["at"])} · #{seq} · {why}</div><p class="text">{_e(item["text"])}</p></li>')


def _job(job: dict[str, Any]) -> str:
    return (f'<li>{_e(job["title"])}<div class="meta">{_e(job["id"])} · {_e(job["state"])} · '
            f'updated {_e(job["updated_at"])}</div></li>')


def _by_topic(listed: list[dict[str, Any]]) -> list[tuple[str | None, list[dict[str, Any]]]]:
    """The conversations under each topic, topics in order, and the untagged in a group of their own last.

    Untagged conversations are shown rather than dropped: a view that hid them would make a topic a
    requirement instead of a label.
    """
    groups: dict[str | None, list[dict[str, Any]]] = {}
    for item in listed:
        groups.setdefault(item["topic"], []).append(item)
    tagged = sorted(topic for topic in groups if topic is not None)
    return [(topic, groups[topic]) for topic in tagged] + ([(None, groups[None])] if None in groups else [])


def _listing(item: dict[str, Any]) -> str:
    return (f'<li><a href="/c/{_e(item["conversation_id"])}">{_e(item["title"])}</a>'
            f'<div class="meta">{_e(", ".join(item["participants"]))} · started '
            f'{_e(item["created_at"])}</div></li>')


def _after_seq(value: str) -> int | None:
    if not SEQ.fullmatch(value) or int(value) > MAX_SEQ:
        return None
    return int(value)


def _conversation(config: Config, conn: Any, conversation_id: str, after: int) -> Response:
    viewer = config.dashboard.viewer_role
    if config.roles[viewer].can_view_all_conversations:
        def read(after_seq: int, limit: int) -> dict[str, Any]:
            return conversations.full_history(conn, conversation_id, after_seq, limit)
    else:
        def read(after_seq: int, limit: int) -> dict[str, Any]:
            return conversations.history(conn, viewer, conversation_id, after_seq, limit)
    try:
        page = read(after, messages.MAX_PAGE_MESSAGES)
        # A page ends at a byte budget as well as a row count, so ask whether anything follows it.
        later = bool(page["messages"]) and bool(read(page["next_seq"], 1)["messages"])
    except BatonError:  # unknown, or one the viewer can't see: the same answer, so neither is revealed
        return _not_found()
    visible = {item["conversation_id"]: item for item in _visible(config, conn)[0]}
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
