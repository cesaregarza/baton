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

What it shows is what one configured `viewer_role` may see under the shared
board-read predicate. A role with `can_view_all_conversations` sees every
retained conversation, including history from before it joined. Reading is bookmark-neutral:
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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from baton import board_read, conversations, jobs, messages
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
# A page number: 1-6 ASCII digits, no leading zero.
PAGE = re.compile(r"[1-9][0-9]{0,5}")
PAGE_PARAMS = ("page", "others_page", "jobs_page")
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
h1, a, li, .text, .relayed-text, .meta, .reply-to { overflow-wrap: anywhere; }
li { min-width: 0; }
.id { font-family: ui-monospace, monospace; }
details { margin: 1.2rem 0 .2rem; }
summary { cursor: pointer; color: #5f6368; font-weight: 600; }
.pager { margin: .6rem 0; }
@media (max-width: 480px) {
  .id { display: inline-block; max-width: 9ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        vertical-align: bottom; }
  body { padding: .6rem; }
}
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
    pages = _pages(url.query)
    if pages is None:
        return _page(400, "Bad request", "<p>A page number is a whole number from 1, given once.</p>")
    if url.path not in ("/", "/needs") and not match and topic is None:
        return _not_found()
    conn = connect(config.db_path)
    try:
        if url.path == "/needs":
            return _page(200, "Needs you", _needs(config, conn, _needs_topic(url.query), pages))
        if topic is not None:
            return _page(200, topic, _index(config, conn, topic, pages["page"]))
        if match is None:
            return _page(200, "Conversations", _index(config, conn, None, pages["page"]))
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


def _pages(query: str) -> dict[str, int] | None:
    """Each paging parameter's page number, 1 when absent, or None when any is malformed or given twice."""
    given = parse_qs(query, keep_blank_values=True)
    pages = {}
    for name in PAGE_PARAMS:
        values = given.get(name, ["1"])
        if len(values) != 1 or not PAGE.fullmatch(values[0]):
            return None
        pages[name] = int(values[0])
    return pages


def _slice(items: list[Any], page: int, size: int) -> list[Any]:
    return items[(page - 1) * size:page * size]


def _span(shown: int, total: int, page: int, size: int, noun: str, newest: bool = True) -> str:
    """"showing the newest N of M" on the first page, "showing A–B of M" after it, or nothing when all fit."""
    if total <= size and page == 1:
        return ""
    if page == 1:
        return f"showing {'the newest ' if newest else ''}{shown} of {total}{noun}"
    if not shown:
        return f"nothing on page {page} of {total}{noun}"
    first = (page - 1) * size + 1
    return f"showing {first}–{first + shown - 1} of {total}{noun}"


def _pager(path: str, params: dict[str, str], name: str, page: int, size: int, total: int, label: str,
           anchor: str = "") -> str:
    """A link to the next page of one list, and one back to its first. Each keeps the page's other parameters,
    so the other lists stay where they are, and lands on its own list's `anchor`."""
    def href(number: int) -> str:
        query = {key: value for key, value in params.items() if key != name}
        query.update({name: str(number)} if number > 1 else {})
        return (path + ("?" + "&amp;".join(f"{key}={_e(value)}" for key, value in query.items()) if query else "")
                + (f"#{anchor}" if anchor else ""))
    links = []
    if page * size < total:
        links.append(f'<a href="{href(page + 1)}">{label}</a>')
    if page > 1:
        links.append(f'<a href="{href(1)}">Back to the first page</a>')
    return f'<p class="pager">{" · ".join(links)}</p>' if links else ""


def _time(value: Any) -> str:
    """A compact time for the eye, with the full stamp kept in the element for anyone who needs it."""
    full = str(value)
    try:
        shown = datetime.fromisoformat(full).strftime("%b %d %H:%M")
    except ValueError:
        shown = full[:12]
    return f'<time datetime="{_e(full)}" title="{_e(full)}">{_e(shown)}</time>'


def _id(value: Any) -> str:
    return f'<span class="id" title="{_e(value)}">{_e(value)}</span>'


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
    every = board_read.sees_all(config, viewer)
    found: list[dict[str, Any]] = []
    while True:
        before = conversations.page_cursor(found[-1]) if found else None
        page = conversations.list_readable(conn, config, viewer, topic, before=before)
        found += page
        if len(page) < conversations.MAX_LISTED:
            break
    return found, "every conversation" if every else f"the conversations {viewer} is in", every


def _index(config: Config, conn: Any, topic: str | None = None, page: int = 1) -> str:
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
    shown = _slice(listed, page, MAX_LISTED)
    span = _span(len(shown), len(listed), page, MAX_LISTED, " conversations")
    more = f'<p class="muted">More than one page holds: {span}.</p>' if span else ""
    path = f"/t/{topic}" if topic else "/"
    pager = _pager(path, {}, "page", page, MAX_LISTED, len(listed), "Older conversations")
    groups = "".join(f'<h2>{_topic_link(group)}</h2><ul>' + "".join(_listing(item) for item in items) + "</ul>"
                     for group, items in _by_topic(shown))
    if not shown:
        groups = f'<p class="muted">Nothing on this page. <a href="{_e(path)}">Back to the newest</a></p>'
    return heading + more + groups + pager


def _topic_link(topic: str | None) -> str:
    return f'<a href="/t/{_e(topic)}">{_e(topic)}</a>' if topic else "No topic"


def _needs(config: Config, conn: Any, topic: str | None, pages: dict[str, int]) -> str:
    """What singles out the viewer, from what it could read, then the jobs waiting on it.

    By default it lists the messages whose `attention` names the viewer. Decisions asked of other roles, or of
    the whole conversation, sit in a collapsed section of their own. "you replied" means only that the viewer
    sent a reply to that message; nothing records a resolution.
    """
    viewer = config.dashboard.viewer_role
    found, replied = _asking(config, conn, topic)
    mine = [pair for pair in found if viewer in (pair[1].get("attention") or [])]
    others = [pair for pair in found if viewer not in (pair[1].get("attention") or [])]
    # Each list's links keep the topic and where the other lists are, so paging one leaves the others in place.
    params = {**({"topic": topic} if topic else {}),
              **{name: str(number) for name, number in pages.items() if number > 1}}
    shown = _slice(mine, pages["page"], MAX_NEEDS)
    span = _span(len(shown), len(mine), pages["page"], MAX_NEEDS, "")
    count = f'{len(mine)} message{"" if len(mine) == 1 else "s"} asked for you' + (f", {span}" if span else "")
    other_count = f' · {len(others)} decision{"" if len(others) == 1 else "s"} not addressed to you'
    where = f" under {_e(topic)}" if topic else ""
    items = "".join(_signal(viewer, conversation, item, replied) for conversation, item in shown)
    pager = _pager("/needs", params, "page", pages["page"], MAX_NEEDS, len(mine), "Older messages for you",
                   "for-you")
    other_shown = _slice(others, pages["others_page"], MAX_NEEDS)
    other_span = _span(len(other_shown), len(others), pages["others_page"], MAX_NEEDS, "")
    other_items = "".join(_signal(viewer, conversation, item, replied) for conversation, item in other_shown)
    other_pager = _pager("/needs", params, "others_page", pages["others_page"], MAX_NEEDS, len(others),
                         "Older decisions", "decisions")
    # Collapsed until the reader pages through it; then it reloads open, where the new items start.
    opened = " open" if pages["others_page"] > 1 else ""
    other_block = (f'<details id="decisions"{opened}>'
                   f'<summary>Decisions not addressed to you ({len(others)})</summary>'
                   f'<p class="muted">Decision requests that name another role, or nobody, in the conversations '
                   f'you can read.{" " + other_span.capitalize() + "." if other_span else ""}</p>'
                   f'<ul>{other_items or "<li class=muted>None.</li>"}</ul>{other_pager}</details>')
    if topic is not None:
        job_count, job_block = "", ('<h2 id="jobs">Jobs</h2><p class="muted">Jobs have no topic, so none are '
                                    'shown under a topic filter. <a href="/needs">See every job waiting on you</a>.'
                                    '</p>')
    else:
        total = jobs.count_jobs(conn, waiting_on=viewer)
        waiting = jobs.list_jobs(conn, waiting_on=viewer, limit=MAX_NEEDS_JOBS,
                                 offset=(pages["jobs_page"] - 1) * MAX_NEEDS_JOBS)
        job_span = _span(len(waiting), total, pages["jobs_page"], MAX_NEEDS_JOBS, "", newest=False)
        job_count = f' · {total} job{" is" if total == 1 else "s are"} waiting on you' + (
            f", {job_span}" if job_span else "")
        job_pager = _pager("/needs", params, "jobs_page", pages["jobs_page"], MAX_NEEDS_JOBS, total, "More jobs",
                           "jobs")
        job_block = ('<h2 id="jobs">Jobs waiting on you</h2><ul>' + ("".join(_job(job) for job in waiting)
                                                           or "<li class=muted>None.</li>") + "</ul>" + job_pager)
    crumbs = '<p><a href="/">All conversations</a>' + (' · <a href="/needs">Needs you</a>' if topic else "") + "</p>"
    return (f'{crumbs}<h1>Needs you{where}</h1><p class="muted">As {_e(viewer)}. {count}{other_count}{job_count}.</p>'
            '<p class="muted">Each message is a retained signal: it asked for you, or for a decision, when it was '
            'sent. "you replied" only means you sent a reply to it, and viewing changes nothing.</p>'
            '<p class="muted">Jump to <a href="#for-you">For you</a> · <a href="#decisions">Decisions not addressed '
            'to you</a> · <a href="#jobs">Jobs</a></p>'
            f'<h2 id="for-you">For you</h2><ul>{items or "<li class=muted>None.</li>"}</ul>{pager}'
            f'{other_block}{job_block}')


def _asking(config: Config, conn: Any, topic: str | None) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]],
                                                                     set[str]]:
    """Every message the viewer could read that names it in `attention` or asks for a decision, newest first,
    and the ids of the messages the viewer itself replied to.

    It reads each visible conversation through the same `history` path as its page, under the viewer's
    shared board-read predicate and within retention. Each message
    comes once however many copies it has, and one the viewer sent itself isn't waiting on it.
    """
    viewer = config.dashboard.viewer_role
    listed, _, _ = _visible(config, conn, topic)
    found, replied = [], set()
    for conversation in listed:
        after = 0
        while True:
            page = conversations.history(conn, viewer, conversation["conversation_id"], after, MAX_NEEDS,
                                         config=config)
            if not page["messages"]:
                break
            found += [(conversation, item) for item in page["messages"]
                      if item["from_role"] not in (None, viewer)
                      and (item.get("needs_decision") or viewer in (item.get("attention") or []))]
            replied |= {item["reply_to"]["message_id"] for item in page["messages"]
                        if item["from_role"] == viewer and item.get("reply_to")}
            after = page["next_seq"]
    return sorted(found, key=lambda pair: (pair[1]["at"], pair[1]["seq"]), reverse=True), replied


def _signal(viewer: str, conversation: dict[str, Any], item: dict[str, Any], replied: set[str]) -> str:
    """One message on /needs, linked to its place: the page that starts with it, so it is always on it.

    "asks for you" only when `attention` names the viewer. A decision request asks the conversation, so it
    shows for every reader; when its `attention` names other roles, the label says whom it was asked of.
    """
    seq = int(item["seq"])
    attention = item.get("attention") or []
    labels = ["asks for you"] if viewer in attention else []
    if item.get("needs_decision"):
        others = [role for role in attention if role != viewer]
        if viewer in attention:
            labels.append("a decision is needed")
        else:
            labels.append(f"a decision is needed (asked of {', '.join(others) or 'the whole conversation'})")
    if item["message_id"] in replied:
        labels.append("you replied")
    why = " · ".join(labels)
    return (f'<li><div class="meta"><a href="{_e(message_path(conversation["conversation_id"], seq))}">'
            f'{_e(conversation["title"])}</a> · <span class="role">{_e(item["from_role"])}</span> · '
            f'{_time(item["at"])} · #{seq} · {why}</div><p class="text">{_e(item["text"])}</p></li>')


def _job(job: dict[str, Any]) -> str:
    return (f'<li>{_e(job["title"])}<div class="meta">{_id(job["id"])} · {_e(job["state"])} · '
            f'updated {_time(job["updated_at"])}</div></li>')


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
            f'{_time(item["created_at"])}</div></li>')


def _after_seq(value: str) -> int | None:
    if not SEQ.fullmatch(value) or int(value) > MAX_SEQ:
        return None
    return int(value)


def _conversation(config: Config, conn: Any, conversation_id: str, after: int) -> Response:
    viewer = config.dashboard.viewer_role
    def read(after_seq: int, limit: int) -> dict[str, Any]:
        return conversations.history(conn, viewer, conversation_id, after_seq, limit, config=config)
    try:
        page = read(after, messages.MAX_PAGE_MESSAGES)
        # A page ends at a byte budget as well as a row count, so ask whether anything follows it.
        later = bool(page["messages"]) and bool(read(page["next_seq"], 1)["messages"])
    except BatonError:  # unknown, or one the viewer can't see: the same answer, so neither is revealed
        return _not_found()
    visible = {item["conversation_id"]: item for item in _visible(config, conn)[0]}
    title = visible[conversation_id]["title"] if conversation_id in visible else conversation_id
    on_page = {item["seq"] for item in page["messages"]}
    # The parent of a reply that starts this page, or sits on an earlier one, read the way the page is read,
    # so a parent the viewer can't see stays unavailable (its seq is None) and is never fetched.
    parents = {}
    for item in page["messages"]:
        seq = (item.get("reply_to") or {}).get("seq")
        if seq is not None and seq not in on_page and seq not in parents:
            found = read(seq - 1, 1)["messages"]
            if found and found[0]["seq"] == seq:
                parents[seq] = found[0]
    items = _thread(page["messages"], conversation_id, parents)
    more = (f'<p><a href="/c/{_e(conversation_id)}?after_seq={page["next_seq"]}">Later messages</a></p>'
            if later else "")
    earlier = (f'<p><a href="/c/{_e(conversation_id)}?after_seq={max(0, after - messages.MAX_PAGE_MESSAGES)}">'
               'Earlier messages</a></p>' if after > 0 else "")
    return _page(200, title, f'<p><a href="/">All conversations</a></p><h1>{_e(title)}</h1>{earlier}'
                             f'<ul>{items or "<li class=muted>No messages yet.</li>"}</ul>{more}')


def _thread(items: list[dict[str, Any]], conversation_id: str = "",
            parents: dict[int, dict[str, Any]] | None = None) -> str:
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
        return _message(item, inner, on_page, conversation_id, parents or {})

    return "".join(render(item) for item in top)


def _message(item: dict[str, Any], replies: str = "", on_page: frozenset[int] | set[int] = frozenset(),
             conversation_id: str = "", parents: dict[int, dict[str, Any]] | None = None) -> str:
    notice = item.get("notice")
    if notice:
        verb = "added" if notice["event"] == "participant_added" else "removed"
        return (f'<li class="notice">{_e(notice["participant"])} was {verb} by {_e(notice["by"])}'
                f' · {_time(item["at"])}{replies}</li>')
    relayed = "".join(
        f'<blockquote class="relayed"><p class="source">relayed from {_e(entry["source"])} at {_e(entry["at"])}'
        f'</p><p class="relayed-text">{_e(entry["text"])}</p></blockquote>' for entry in item.get("relayed") or [])
    kind = item.get("author_kind")
    badge = f'<span class="badge {kind}">{kind}</span>' if kind in ("human", "agent") else ""
    return (f'<li class="message" id="m-{item["seq"]}"><div class="meta"><span class="role">'
            f'{_e(item["from_role"])}</span>{badge} · {_time(item["at"])} · #{item["seq"]}</div>'
            f'{_reply_marker(item.get("reply_to"), on_page, conversation_id, parents or {})}'
            f'<p class="text">{_e(item["text"])}</p>{relayed}{replies}</li>')


QUOTE = 160


def _reply_marker(reply_to: dict[str, Any] | None, on_page: frozenset[int] | set[int], conversation_id: str = "",
                  parents: dict[int, dict[str, Any]] | None = None) -> str:
    """Where a reply's parent is: nothing when it's nested here; on another page, a link to it with a short
    quote of what it said; or 'parent unavailable' when the viewer can't see it, without its words or author."""
    if not reply_to:
        return ""
    if reply_to.get("seq") is None:
        return '<p class="reply-to unavailable">parent unavailable</p>'
    seq = int(reply_to["seq"])
    if seq in on_page:
        return ""
    link = (f'<a href="{_e(message_path(conversation_id, seq))}">#{seq}</a>' if conversation_id
            else f"#{seq}")
    parent = (parents or {}).get(seq)
    quote = ""
    if parent is not None:
        words = parent["text"] if len(parent["text"]) <= QUOTE else parent["text"][:QUOTE] + "…"
        quote = f' · {_e(parent["from_role"] or "the host")}: “{_e(words)}”'
    return f'<p class="reply-to">in reply to {link}{quote}</p>'



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


def message_path(conversation_id: str, seq: int) -> str:
    """The path that opens a conversation at one message: the page that starts there, anchored on it.

    Every link to a message uses it: the Needs you page, a reply's link to a parent on another page, and
    the decision push. A page that starts at the message holds it however deep in the conversation it is.
    """
    return f"/c/{quote(conversation_id, safe='')}?after_seq={seq - 1}#m-{seq}"
