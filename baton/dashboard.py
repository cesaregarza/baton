"""An optional, read-only view of the host's conversations, for a person at the host's own machine.

Off unless `[dashboard] enabled = true`. It is served from the host's own
store by a small standard-library HTTP server beside the MCP server, on a
loopback address only, and it never sees a token.

`/` is a directory of the topics a core role has opened, each with its
description, its subtopics and how many threads in it this viewer may read;
the conversations with no topic follow in a group of their own, so none is
hidden. `/t/<project>` and `/t/<project>/<subtopic>` show one topic, headed by
its own details, with the same whole-segment filter as `list_conversations`. A
path that is not a topic is a missing page, because a topic is a container
rather than a label. A thread a guest started names the alias behind it.

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

A thread page has a Copy button. One press copies the whole thread as plain text, which `/c/<id>/text` serves:
read the way the page reads it (the same viewer and predicate, a page at a time), so it holds what the viewer
may see and no more, and it reaches past the one page of the thread that the page shows.

The threat model is the machine it runs on. The view has no login, so any
person or process that can reach the port can read it; on a shared machine,
choose a viewer role that sees little. Against other sites in a browser on
the same machine, it answers only to its own loopback Host name (so DNS
rebinding gets nothing), sends no CORS headers, can't be framed, takes no
writes, and runs no script under a CSP that allows none, with one exception:
a thread page may load the Copy button's own `/copy.js` and fetch the thread's
text, from this origin alone and with nothing inline. Reaching it
from anywhere else needs its own auth in front of it, and is the operator's
separate decision.
"""

from __future__ import annotations

import html
import re
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal
from urllib.parse import SplitResult, parse_qs, quote, unquote, urlsplit

from baton import board_read, conversations, jobs, messages, topics
from baton.config import Config, ConfigError
from baton.database import Connection
from baton.database.store import connect
from baton.errors import BatonError

HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}
# A thread page alone may also run a script, and only the dashboard's own `/copy.js`, which fetches the thread's text
# from this origin. Nothing else about the policy changes, and no other response is given either directive.
COPY_POLICY = HEADERS["Content-Security-Policy"] + "; script-src 'self'; connect-src 'self'"
READS = ("GET", "HEAD")
# Every refusal offers the way back that the not-found page does, so none of them is a dead end.
BACK = ' <a href="/">All conversations</a>'
# A thread's page, or the thread as plain text for the Copy button (the second group).
CONVERSATION_PATH = re.compile(r"/c/([A-Za-z0-9_-]{1,64})(/text)?")
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
/* tokens: one palette, so the dark one only restates the values */
:root {
  color-scheme: light dark;
  --bg: #ffffff; --surface: #f4f6f8; --text: #1b1f24; --muted: #565e68;
  --link: #0b57d0; --link-hover: #083f9c; --link-line: #8fb0ea; --focus: #0b57d0;
  --line: #e1e5ea; --line-strong: #c3c9d1;
  --accent-bg: #e6eefc; --accent-text: #0a4aa6; --accent-line: #a9c2ee; --target-bg: #e6eefc;
  --font: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171b; --surface: #1d2127; --text: #e7e9ec; --muted: #a4acb6;
    --link: #8ab4f8; --link-hover: #b9d2fc; --link-line: #4d6a9c; --focus: #8ab4f8;
    --line: #2b3038; --line-strong: #434a54;
    --accent-bg: #1d2e4a; --accent-text: #b4cdfa; --accent-line: #3f5a8a; --target-bg: #1d2e4a;
  }
}
/* base: 16px text, a 42rem column, and a type scale of 13, 14, 16, 18, 24 and 28px */
*, ::before, ::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
body { font: 1rem/1.55 var(--font); margin: 0 auto; max-width: 42rem; padding: 1rem 1rem 3rem;
       background: var(--bg); color: var(--text); }
h1, h2, h3 { line-height: 1.25; text-wrap: balance; }
h1 { font-size: 1.5rem; font-weight: 700; letter-spacing: -.01em; margin: .25rem 0 .5rem; }
h2 { font-size: 1.125rem; font-weight: 600; margin: 2rem 0 .25rem; }
h3 { font-size: 1rem; font-weight: 600; margin: 1.25rem 0 .25rem; }
p { margin: .6rem 0; }
a { color: var(--link); text-decoration: underline; text-decoration-color: var(--link-line);
    text-decoration-thickness: 1px; text-underline-offset: .2em; }
a:hover { color: var(--link-hover); text-decoration-color: currentColor; }
a:focus-visible, summary:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 2px; }
h2 a { color: inherit; text-decoration-color: var(--line-strong); }
h1, h2, h3, a, li, p, summary, .text, .relayed-text, .meta, .reply-to { overflow-wrap: anywhere; }
/* lists and messages */
ul { list-style: none; margin: 0; padding: 0; }
li { border-bottom: 1px solid var(--line); padding: .85rem 0; }
li:last-child, li li, li.muted { border-bottom: 0; }
li { min-width: 0; }
.meta, .notice, .source, .muted, .reply-to { color: var(--muted); }
.meta, .notice, .source, .reply-to { font-size: .8125rem; }
.muted { font-size: .875rem; }
.role { font-weight: 600; color: var(--text); }
.badge { display: inline-block; border: 1px solid var(--line-strong); border-radius: 999px; padding: 0 .45rem;
         margin-left: .35rem; font-size: .75rem; line-height: 1.5; color: var(--muted); vertical-align: 1px; }
.badge.human { background: var(--accent-bg); border-color: var(--accent-line); color: var(--accent-text); }
.text { white-space: pre-wrap; margin: .25rem 0 .1rem; }
.relayed { border-left: 3px solid var(--line-strong); background: var(--surface); border-radius: 0 .375rem .375rem 0;
           margin: .6rem 0; padding: .5rem .8rem; }
.relayed .source { margin: 0 0 .2rem; }
.relayed-text { white-space: pre-wrap; margin: 0; }
.notice { font-style: italic; }
p.notice { background: var(--surface); border-radius: .375rem; padding: .5rem .8rem; margin: .75rem 0; }
.replies { margin: .6rem 0 0 .25rem; border-left: 2px solid var(--line); padding-left: .85rem; }
.replies > li { padding: .6rem 0 0; }
.reply-to { margin: .1rem 0; }
.message { scroll-margin-top: 1rem; }
.message:target > .meta { background: var(--target-bg);
                          box-shadow: -.5rem 0 0 var(--target-bg), .5rem 0 0 var(--target-bg); }
.id { font-family: var(--mono); font-size: .8125rem; box-sizing: content-box; background: var(--surface);
      border-radius: .25rem; padding: 0 .3rem; }
details { margin: 1.75rem 0 .5rem; border: 1px solid var(--line); border-radius: .5rem; padding: 0 1rem; }
summary { cursor: pointer; padding: .75rem 0; font-weight: 600; }
details[open] > summary { border-bottom: 1px solid var(--line); margin-bottom: .25rem; }
.pager { margin: .5rem 0; }
.gap-bar { display: none; }
/* the bar and its hidden measuring copy share their box; only the bar is fixed to the viewport's bottom */
.gap-bar, .gap-measure { margin: 0; padding: .5rem 1rem; background: var(--bg);
  border-top: 1px solid var(--line-strong); color: var(--muted); font-size: .85rem; text-align: center; }
.gap-measure { display: none; }
.gap-dismiss { position: fixed; bottom: 0; left: 0; width: 1px; height: 1px; margin: 0; opacity: 0;
  visibility: hidden; }
body:has(li:target) .gap-dismiss:not(:checked) + .gap-bar { display: block; position: fixed; bottom: 0; left: 0;
  right: 0; }
body:has(li:target):has(.gap-dismiss:not(:checked)) { padding-bottom: 4rem; }
.gap-bar label { margin-left: .6rem; color: var(--link); text-decoration: underline; cursor: pointer; }
.gap-dismiss:focus-visible + .gap-bar label { outline: 2px solid var(--focus); outline-offset: 2px; }
/* without scroll-driven animations or anchor positioning the bar shows on arrival, as it always has (acceptance E) */
@supports not ((animation-timeline: view()) and (anchor-name: --a)) {
  body:has(li:target) .gap-dismiss:not(:checked) { visibility: visible; }
}
/* with both, the bar appears once the end of the anchored message's own content comes into view. The marker closes
   that content (before the replies), and the hidden copy hangs its full height below the marker, so the copy's bottom
   edge reaches the viewport's bottom exactly when the bar's top reaches the message's last line. */
@supports (animation-timeline: view()) and (anchor-name: --a) {
  li:target > .gap-end { display: block; height: 0; anchor-name: --gap-end; }
  .gap-measure { display: block; position: absolute; position-anchor: --gap-end; top: anchor(bottom); left: 0; right: 0;
    visibility: hidden; pointer-events: none; view-timeline: --gap-reveal block; }
  body { timeline-scope: --gap-reveal; }
  body:has(li:target) .gap-dismiss:not(:checked),
  body:has(li:target) .gap-dismiss:not(:checked) + .gap-bar {
    visibility: hidden; animation: gap-reveal linear both; animation-timeline: --gap-reveal;
    animation-range: entry-crossing calc(100% - 2px) entry-crossing calc(100% - 2px); }
  @keyframes gap-reveal { from { visibility: hidden; } to { visibility: visible; } }
}
/* primary links are at least 44px tall: an item that opens with its link lets the link carry the item's top space */
h2 a { display: inline-block; padding: .7rem 0; }
h2:has(> a) { margin: 1rem 0 0; }
li > a:first-child, .meta > a:first-child { display: flex; align-items: flex-end; min-height: 2.75rem;
                                           padding-bottom: .15rem; font-size: 1rem; font-weight: 600;
                                           text-decoration: none; }
li > a:first-child:hover, .meta > a:first-child:hover { text-decoration: underline; }
li:has(> a:first-child), li:has(> .meta > a:first-child) { padding-top: 0; }
.sep { display: none; }
.pager a, .crumbs a { display: inline-block; padding: .7rem .15rem; margin-left: -.15rem; }
.crumbs { margin: 0; font-size: .875rem; }
.lead { font-size: 1.125rem; margin: 2.5rem 0; }
.lead a { display: inline-block; padding: .5rem 0; }
/* narrow screens shorten ids and tighten threads; wide ones get a larger title and deeper threads */
@media (max-width: 480px) {
  .id { display: inline-block; max-width: 9ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        vertical-align: bottom; }
  .replies { margin-left: 0; padding-left: .6rem; }
  /* on narrow screens the bar's action row is a row of its own: two 44 CSS px targets under the bar's text. A control
     keeps its width and its words, so a word never breaks; the row wraps when both controls don't fit side by side.
     The space between the controls is the row's column gap, not the label's margin, so a control on a line of its own
     is centred */
  .gap-sep { display: none; }
  .gap-actions { display: flex; flex-wrap: wrap; justify-content: center; align-items: center; row-gap: 4px;
    column-gap: .6rem; min-height: 44px; margin-top: .3rem; }
  .gap-actions a, .gap-actions label { display: inline-flex; align-items: center; flex-shrink: 0; overflow-wrap: normal;
    min-height: 44px; min-width: 44px; padding: 0 .6rem; margin-left: 0; }
  body:has(li:target):has(.gap-dismiss:not(:checked)) { padding-bottom: 7rem; }
}
@media (min-width: 40rem) {
  body { padding: 1.5rem 1.5rem 4rem; }
  h1 { font-size: 1.75rem; }
  .replies { margin-left: .5rem; padding-left: 1rem; }
}
/* a thread page's top row: the way back, then Copy at the right, and below them a line saying how the copy went */
.top { display: flex; flex-wrap: wrap; align-items: center; column-gap: .75rem; }
.top .crumbs { margin-right: auto; }
.copy { font: inherit; font-weight: 600; min-height: 2.75rem; padding: 0 1.1rem; color: var(--link);
        background: var(--surface); border: 1px solid var(--line-strong); border-radius: .5rem; cursor: pointer; }
.copy:hover { color: var(--link-hover); border-color: var(--link-line); }
.copy:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.copy-note { flex-basis: 100%; margin: 0; color: var(--muted); font-size: .875rem; }
.copy-text { display: block; width: 100%; height: 16rem; margin: .5rem 0 1rem; padding: .6rem; color: var(--text);
             background: var(--surface); border: 1px solid var(--line-strong); border-radius: .375rem;
             font: .8125rem/1.5 var(--mono); }
"""
# The Copy button's script, served beside the stylesheet. The write to the clipboard starts inside the tap and is given
# the text as a promise, which is how Safari lets a write wait for the network; if the browser refuses it, the thread
# is shown selected instead, so an ordinary copy works. It sets only text and a value, never markup.
SCRIPT = """\
(function () {
  "use strict";
  var button = document.getElementById("copy");
  var note = document.getElementById("copy-note");
  if (!button || !note) { return; }

  function show(words) {
    var area = document.getElementById("copy-text") || document.createElement("textarea");
    area.id = "copy-text";
    area.className = "copy-text";
    area.readOnly = true;
    area.setAttribute("aria-label", "The thread as plain text");
    area.value = words;
    button.parentNode.after(area);
    area.focus();
    area.select();
    area.setSelectionRange(0, words.length);
    area.scrollTop = 0;
    note.textContent = "Copying was blocked. The thread is selected below: copy it as you usually do.";
  }

  function write(words) {
    if (!(navigator.clipboard && window.ClipboardItem)) { return Promise.reject(new Error("no clipboard")); }
    try {
      var blob = words.then(function (text) { return new Blob([text], { type: "text/plain" }); });
      return navigator.clipboard.write([new ClipboardItem({ "text/plain": blob })]);
    } catch (error) {
      return Promise.reject(error);
    }
  }

  button.addEventListener("click", function () {
    var words = fetch(button.dataset.src).then(function (response) {
      if (!response.ok) { throw new Error("status " + response.status); }
      return response.text();
    });
    note.textContent = "";
    write(words).then(function () {
      note.textContent = "Copied";
    }, function () {
      words.then(show, function () { note.textContent = "The thread could not be read."; });
    });
  });
})();
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
        return Response(
            405, b"read-only\n", {**HEADERS, "Allow": "GET, HEAD", "Content-Type": "text/plain; charset=utf-8"}
        )
    url = urlsplit(target)
    if url.path == "/style.css":
        return Response(200, STYLE.encode(), {**HEADERS, "Content-Type": "text/css; charset=utf-8"})
    if url.path == "/copy.js":
        return Response(200, SCRIPT.encode(), {**HEADERS, "Content-Type": "text/javascript; charset=utf-8"})
    match = CONVERSATION_PATH.fullmatch(url.path)
    topic = _topic_route(url.path) if url.path.startswith("/t/") else None
    if (url.path.startswith("/t/") and topic is None) or (url.path == "/needs" and _needs_topic(url.query) is False):
        return _page(
            400,
            "Bad request",
            "<p>A topic is one or two lowercase slug segments, such as project or "
            "project/subtopic, given once." + BACK + "</p>",
        )
    pages = _pages(url.query)
    if pages is None:
        return _page(400, "Bad request", "<p>A page number is a whole number from 1, given once." + BACK + "</p>")
    if url.path not in ("/", "/needs") and not match and topic is None:
        return _not_found()
    return _respond_page(config, url, match, topic, pages)


def _respond_page(
    config: Config, url: SplitResult, match: re.Match[str] | None, topic: str | None, pages: dict[str, int]
) -> Response:
    conn = connect(config.db_path)
    try:
        return _page_route(config, conn, url, match, topic, pages)
    finally:
        conn.close()


def _page_route(
    config: Config,
    conn: Connection,
    url: SplitResult,
    match: re.Match[str] | None,
    topic: str | None,
    pages: dict[str, int],
) -> Response:
    if url.path == "/needs":
        topic_filter = _needs_topic(url.query)
        assert topic_filter is not False  # respond rejected an invalid filter before opening the database.
        body = _needs(config, conn, topic_filter, pages)
        return _page(200, f"Needs {_attention_label(config)}", body)
    if topic is not None:
        return _topic_page(config, conn, topic, pages["page"])
    if match is None:
        return _page(200, "Conversations", _index(config, conn, None, pages["page"]))
    if match.group(2):
        return _thread_text(config, conn, match.group(1))
    after = _after_seq(parse_qs(url.query, keep_blank_values=True).get("after_seq", ["0"])[-1])
    if after is None:
        return _page(400, "Bad request", "<p>after_seq must be a whole number of at most 19 digits." + BACK + "</p>")
    return _conversation(config, conn, match.group(1), after)


def _topic_page(config: Config, conn: Connection, topic: str, page: int) -> Response:
    # Topic details are visible only to viewers who can open that topic.
    if not topics.exists(conn, topic) or not topics.may_open(conn, config, _viewer(config), topic):
        return _not_found()
    return _page(200, topic, _index(config, conn, topic, page))


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
        segments = [unquote(segment, errors="strict") for segment in path[len("/t/") :].split("/")]
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
    return items[(page - 1) * size : page * size]


def _span(shown: int, total: int, page: int, size: int, noun: str, newest: bool = True) -> str:
    """ "showing the newest N of M" on the first page, "showing A–B of M" after it, or nothing when all fit."""
    if total <= size and page == 1:
        return ""
    if page == 1:
        return f"showing {'the newest ' if newest else ''}{shown} of {total}{noun}"
    if not shown:
        return f"nothing on page {page} of {total}{noun}"
    first = (page - 1) * size + 1
    return f"showing {first}–{first + shown - 1} of {total}{noun}"


def _pager(
    path: str, params: dict[str, str], name: str, page: int, size: int, total: int, label: str, anchor: str = ""
) -> str:
    """A link to the next page of one list, and one back to its first. Each keeps the page's other parameters,
    so the other lists stay where they are, and lands on its own list's `anchor`."""

    def href(number: int) -> str:
        query = {key: value for key, value in params.items() if key != name}
        query.update({name: str(number)} if number > 1 else {})
        return (
            path
            + ("?" + "&amp;".join(f"{key}={_e(value)}" for key, value in query.items()) if query else "")
            + (f"#{anchor}" if anchor else "")
        )

    links = []
    if page * size < total:
        links.append(f'<a href="{href(page + 1)}">{_e(label)}</a>')
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


def _needs_topic(query: str) -> str | None | Literal[False]:
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


def _topic_header(config: Config, conn: Any, topic: str) -> str:
    """A topic's own details, above its threads: what it is, who opened it, and who a thread here reaches."""
    try:
        details = topics.get(conn, config, reader=_viewer(config), path=topic)
    except BatonError:
        return ""
    title = f"<h2>{_e(details['title'])}</h2>" if details["title"] != topic else ""
    description = f"<p>{_e(details['description'])}</p>" if details["description"] else ""
    members = ", ".join(details["members"]) or "none yet"
    return (
        f'{title}{description}<p class="meta">Opened by {_e(details["created_by"])} · members '
        f"{_e(members)}</p>" + _subtopics(details["subtopics"])
    )


def _subtopics(paths: list[str]) -> str:
    if not paths:
        return ""
    # Styled by the existing .meta rule rather than a new one for h3: a bare h3 falls back to the
    # browser's larger, darker default and reads as outranking the h2 above it.
    return '<h3 class="meta">Subtopics</h3><ul>' + "".join(f"<li>{_topic_link(path)}</li>" for path in paths) + "</ul>"


def _directory(config: Config, conn: Any) -> str:
    """The topics, each with its description, its subtopics and this viewer's own thread count.

    A viewer that does not read the whole board is offered only the topics it can see a thread in,
    which is the rule this page has always followed: a topic whose only thread is hidden from the
    viewer is not named here either, so the directory discloses nothing the listing would not. A
    board-reading viewer sees every topic, including one still empty, which is what makes the
    directory usable as a directory.
    """
    viewer = _viewer(config)
    every = board_read.sees_all(config, viewer)
    listed = [entry for entry in topics.listing(conn, config, reader=viewer) if every or entry["threads"]]
    children: dict[str, list[str]] = {}
    for entry in listed:
        if "/" in entry["path"]:
            children.setdefault(entry["path"].split("/")[0], []).append(entry["path"])
    # A root's count covers its subtree, so a root with a visible subtopic is itself visible and there
    # is no orphan case left to carry: the entry and its children are always rendered together.
    roots = {entry["path"]: entry for entry in listed if "/" not in entry["path"]}
    items = []
    for path, entry in sorted(roots.items()):
        shown = _subtopics(children.get(path, []))
        description = f"<p>{_e(entry['description'])}</p>" if entry["description"] else ""
        activity = f" · last {_time(entry['last_activity_at'])}" if entry["last_activity_at"] else ""
        counted = f"{entry['threads']} thread{'' if entry['threads'] == 1 else 's'}"
        # A topic titled after its own path would otherwise print that word as the link and again as
        # metadata; the path earns its place in the line only when it says something the title did not.
        named = f"{_e(path)} · " if entry["title"] != path else ""
        items.append(
            f'<li><a href="/t/{_e(path)}">{_e(entry["title"])}</a>{description}'
            f'<div class="meta">{named}{counted}{activity}</div>{shown}</li>'
        )
    return "<h2>Topics</h2><ul>" + "".join(items) + "</ul>" if items else ""


def _index(config: Config, conn: Any, topic: str | None = None, page: int = 1) -> str:
    viewer = _viewer(config)
    total, shown = conversations.list_readable_page(
        conn, config, viewer, topic, limit=MAX_LISTED, offset=(page - 1) * MAX_LISTED
    )
    scope = "every conversation" if board_read.sees_all(config, viewer) else f"the conversations {viewer} is in"
    needs = f"Needs {_e(_attention_label(config))}"
    if topic is None:
        heading = (
            f'<h1>Conversations</h1><p class="muted">Read-only. Showing {_e(scope)}, as {_e(viewer)}. '
            f'<a href="/needs">{needs}</a></p>' + _directory(config, conn)
        )
    else:
        heading = (
            f'<p><a href="/">All conversations</a> · {_e(" › ".join(topic.split("/")))}</p>'
            f'<h1>{_e(topic)}</h1><p class="muted">Read-only. Showing {_e(scope)} under this topic, as '
            f'{_e(viewer)}. <a href="/needs?topic={_e(topic)}">{needs} here</a></p>'
            + _topic_header(config, conn, topic)
        )
    if not total:
        return heading + f"<ul><li class=muted>{'None yet.' if topic is None else 'None under this topic.'}</li></ul>"
    span = _span(len(shown), total, page, MAX_LISTED, " conversations")
    more = f'<p class="muted">More than one page holds: {span}.</p>' if span else ""
    path = f"/t/{topic}" if topic else "/"
    pager = _pager(path, {}, "page", page, MAX_LISTED, total, "Older conversations")
    groups = "".join(
        f"<h2>{_topic_link(group)}</h2><ul>" + "".join(_listing(item) for item in items) + "</ul>"
        for group, items in _by_topic(shown)
    )
    if not shown:
        groups = f'<p class="muted">Nothing on this page. <a href="{_e(path)}">Back to the newest</a></p>'
    return heading + more + groups + pager


def _topic_link(topic: str | None) -> str:
    return f'<a href="/t/{_e(topic)}">{_e(topic)}</a>' if topic else "No topic"


def _needs_jobs(
    config: Config, conn: Connection, topic: str | None, page: int, params: dict[str, str]
) -> tuple[str, str]:
    """The Needs page's jobs count and block, preserving the visibility rules for each view."""
    viewer = _viewer(config)
    subject = config.dashboard.attention_role or viewer
    you = _attention_label(config)
    if subject != viewer and not config.dashboard.attention_jobs:
        # Listing them would widen what the view's audience sees; that takes `dashboard.attention_jobs`.
        return (
            "",
            f'<h2 id="jobs">Jobs</h2><p class="muted">Jobs waiting on {_e(you)} aren\'t '
            "shown in this read-only view.</p>",
        )
    if topic is not None:
        return (
            "",
            '<h2 id="jobs">Jobs</h2><p class="muted">Jobs have no topic, so none are '
            f'shown under a topic filter. <a href="/needs">See every job waiting on '
            f"{_e(you)}</a>.</p>",
        )

    total = jobs.count_jobs(conn, waiting_on=subject)
    waiting = jobs.list_jobs(conn, waiting_on=subject, limit=MAX_NEEDS_JOBS, offset=(page - 1) * MAX_NEEDS_JOBS)
    job_span = _span(len(waiting), total, page, MAX_NEEDS_JOBS, "", newest=False)
    job_count = f" · {total} job{' is' if total == 1 else 's are'} waiting on {_e(you)}" + (
        f", {job_span}" if job_span else ""
    )
    job_pager = _pager("/needs", params, "jobs_page", page, MAX_NEEDS_JOBS, total, "More jobs", "jobs")
    job_block = (
        f'<h2 id="jobs">Jobs waiting on {_e(you)}</h2><ul>'
        + ("".join(_job(job) for job in waiting) or "<li class=muted>None.</li>")
        + "</ul>"
        + job_pager
    )
    return job_count, job_block


def _needs(config: Config, conn: Any, topic: str | None, pages: dict[str, int]) -> str:
    """What singles out the attention role, from what the viewer could read, then the jobs waiting on it.

    The attention role is `dashboard.attention_role`, the viewer by default. It lists the messages whose
    `attention` names that role; decisions asked of other roles, or of the whole conversation, sit in a
    collapsed section of their own. Visibility is always the viewer's: when the attention role is someone else,
    the page is a read-only projection of what the viewer may already read, and it never reads as that role.
    "replied" means only that the role sent a reply to the message; nothing records a resolution.
    """
    viewer = _viewer(config)
    subject = config.dashboard.attention_role or viewer
    projected = subject != viewer
    you = _attention_label(config)  # plain text: every use below escapes it
    mine, others, replied = _asking(config, conn, topic, subject)
    # Each list's links keep the topic and where the other lists are, so paging one leaves the others in place.
    params = {
        **({"topic": topic} if topic else {}),
        **{name: str(number) for name, number in pages.items() if number > 1},
    }
    shown = _slice(mine, pages["page"], MAX_NEEDS)
    span = _span(len(shown), len(mine), pages["page"], MAX_NEEDS, "")
    count = f"{len(mine)} message{'' if len(mine) == 1 else 's'} asked for {_e(you)}" + (f", {span}" if span else "")
    other_count = f" · {len(others)} decision{'' if len(others) == 1 else 's'} not addressed to {_e(you)}"
    where = f" under {_e(topic)}" if topic else ""
    items = "".join(_signal(subject, you, conversation, item, replied) for conversation, item in shown)
    pager = _pager(
        "/needs", params, "page", pages["page"], MAX_NEEDS, len(mine), f"Older messages for {you}", "for-you"
    )
    other_shown = _slice(others, pages["others_page"], MAX_NEEDS)
    other_span = _span(len(other_shown), len(others), pages["others_page"], MAX_NEEDS, "")
    other_items = "".join(_signal(subject, you, conversation, item, replied) for conversation, item in other_shown)
    other_pager = _pager(
        "/needs", params, "others_page", pages["others_page"], MAX_NEEDS, len(others), "Older decisions", "decisions"
    )
    # Collapsed until the reader pages through it; then it reloads open, where the new items start.
    opened = " open" if pages["others_page"] > 1 else ""
    other_block = (
        f'<details id="decisions"{opened}>'
        f"<summary>Decisions not addressed to {_e(you)} ({len(others)})</summary>"
        f'<p class="muted">Decision requests that name another role, or nobody, in the conversations '
        f"{'you can read' if not projected else _e(viewer) + ' can read'}."
        f"{' ' + other_span.capitalize() + '.' if other_span else ''}</p>"
        f"<ul>{other_items or '<li class=muted>None.</li>'}</ul>{other_pager}</details>"
    )
    job_count, job_block = _needs_jobs(config, conn, topic, pages["jobs_page"], params)
    needs = f"Needs {_e(you)}"
    crumbs = (
        '<p class="crumbs"><a href="/">All conversations</a>'
        + (f' · <a href="/needs">{needs}</a>' if topic else "")
        + "</p>"
    )
    if projected:
        head = (
            f'<h1>{needs}{where}</h1><p class="muted">{needs}, read-only view, as visible to {_e(viewer)}. '
            f"{count}{other_count}{job_count}.</p>"
            f'<p class="muted">Each message is a retained signal: it asked for {_e(you)}, or for a decision, when '
            f'it was sent. "{_e(you)} replied" only means {_e(you)} sent a reply to it, which approves or resolves '
            "nothing, and viewing changes nothing.</p>"
        )
    else:
        head = (
            f'<h1>{needs}{where}</h1><p class="muted">As {_e(viewer)}. {count}{other_count}{job_count}.</p>'
            '<p class="muted">Each message is a retained signal: it asked for you, or for a decision, when it was '
            'sent. "you replied" only means you sent a reply to it, and viewing changes nothing.</p>'
        )
    for_you = f"For {_e(you)}"
    return (
        f"{crumbs}{head}"
        f'<p class="muted">Jump to <a href="#for-you">{for_you}</a> · <a href="#decisions">Decisions not '
        f'addressed to {_e(you)}</a> · <a href="#jobs">Jobs</a></p>'
        f'<h2 id="for-you">{for_you}</h2><ul>{items or "<li class=muted>None.</li>"}</ul>{pager}'
        f"{other_block}{job_block}"
    )


def _viewer(config: Config) -> str:
    """Page reads require the viewer role that enabled-dashboard validation guarantees."""
    role = config.dashboard.viewer_role
    if role is None:
        raise ConfigError("dashboard reads require a viewer_role")
    return role


def _attention_label(config: Config) -> str:
    """Whom the Needs page is about, as plain text: "you" for the viewer itself, else the attention role's display
    name. It is configured text, so every page escapes it where it shows it."""
    viewer = _viewer(config)
    subject = config.dashboard.attention_role or viewer
    return "you" if subject == viewer else config.roles[subject].label


def _asking(
    config: Config, conn: Any, topic: str | None, subject: str
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[tuple[dict[str, Any], dict[str, Any]]], set[str]]:
    """Messages addressed to `subject`, other decision signals, and replies, all from the viewer's readable set.

    It reads each visible conversation through the same `history` path as its page, under the viewer's
    shared board-read predicate, over the copies still stored: `subject` only filters, it never reads. Each message
    comes once however many copies it has, and one the viewer or `subject` sent isn't waiting on it. A reply
    counts as `subject`'s only when `subject` sent it; when `subject` isn't the viewer it must also be stored as
    a person's, so an agent relaying or quoting the person never counts.
    """
    viewer = _viewer(config)
    return conversations.attention_signals(conn, config, viewer, subject, topic, human_replies_only=subject != viewer)


def _signal(subject: str, you: str, conversation: dict[str, Any], item: dict[str, Any], replied: set[str]) -> str:
    """One message on /needs, linked to its place: the page that starts with it, so it is always on it.

    "asks for <you>" only when `attention` names `subject`. A decision request asks the conversation, so it
    shows for every reader; when its `attention` names other roles, the label says whom it was asked of.
    """
    seq = int(item["seq"])
    attention = item.get("attention") or []
    labels = [f"asks for {you}"] if subject in attention else []
    if item.get("needs_decision"):
        others = [role for role in attention if role != subject]
        if subject in attention:
            labels.append("a decision is needed")
        else:
            labels.append(f"a decision is needed (asked of {', '.join(others) or 'the whole conversation'})")
    if item["message_id"] in replied:
        labels.append(f"{you} replied")
    why = " · ".join(labels)
    return (
        f'<li><div class="meta"><a href="{_e(message_path(conversation["conversation_id"], seq))}">'
        f'{_e(conversation["title"])}</a><span class="sep"> · </span>'
        f'<span class="role">{_e(messages.author_label(item))}</span> · '
        f'{_time(item["at"])} · #{seq} · {_e(why)}</div><p class="text">{_e(item["text"])}</p></li>'
    )


def _job(job: dict[str, Any]) -> str:
    return (
        f'<li>{_e(job["title"])}<div class="meta">{_id(job["id"])} · {_e(job["state"])} · '
        f"updated {_time(job['updated_at'])}</div></li>"
    )


def _by_topic(listed: list[dict[str, Any]]) -> list[tuple[str | None, list[dict[str, Any]]]]:
    """The conversations under each topic, topics in order, and the untopiced in a group of their own last.

    A thread has to be filed in a topic that exists, but a thread may still have none at all, and those
    are shown rather than dropped: the index is the one place every conversation is reachable from.
    """
    groups: dict[str | None, list[dict[str, Any]]] = {}
    for item in listed:
        groups.setdefault(item["topic"], []).append(item)
    tagged = sorted(topic for topic in groups if topic is not None)
    return [(topic, groups[topic]) for topic in tagged] + ([(None, groups[None])] if None in groups else [])


def _listing(item: dict[str, Any]) -> str:
    # A thread a guest started says so, with the alias and its code, exactly as a guest's posts are
    # attributed. A role's thread carries no alias, so it reads as it always did.
    alias = (
        f" · started by {_e(item['created_by_alias'])} ({_e(item['created_by_alias_code'])})"
        if item.get("created_by_alias")
        else ""
    )
    return (
        f'<li><a href="/c/{_e(item["conversation_id"])}">{_e(item["title"])}</a>'
        f'<div class="meta">{_e(", ".join(item["participants"]))} · started '
        f"{_time(item['created_at'])}{alias}</div></li>"
    )


def _after_seq(value: str) -> int | None:
    if not SEQ.fullmatch(value) or int(value) > MAX_SEQ:
        return None
    return int(value)


def _read(config: Config, conn: Any, conversation_id: str, after_seq: int, limit: int) -> dict[str, Any]:
    """Read a thread page under the dashboard viewer's permissions."""
    return conversations.history(conn, _viewer(config), conversation_id, after_seq, limit, config=config)


def _history_pages(config: Config, conn: Any, conversation_id: str, limit: int) -> Iterator[dict[str, Any]]:
    """Read through short byte-limited pages, including the final empty page's retention gaps."""
    after = 0
    while True:
        page = _read(config, conn, conversation_id, after, limit)
        yield page
        if not page["messages"]:
            return
        after = page["next_seq"]


def _title(config: Config, conn: Any, conversation_id: str) -> str:
    """A thread's title as the viewer's list gives it, or its id when that list does not hold it."""
    summary = conversations.readable_summary(conn, config, _viewer(config), conversation_id)
    return summary["title"] if summary is not None else conversation_id


def _conversation(config: Config, conn: Any, conversation_id: str, after: int) -> Response:
    def read(after_seq: int, limit: int) -> dict[str, Any]:
        return _read(config, conn, conversation_id, after_seq, limit)

    try:
        page = read(after, messages.MAX_PAGE_MESSAGES)
        # A page ends at a byte budget as well as a row count, so ask whether anything follows it.
        later = bool(page["messages"]) and bool(read(page["next_seq"], 1)["messages"])
    except BatonError:  # unknown, or one the viewer can't see: the same answer, so neither is revealed
        return _not_found()
    title = _title(config, conn, conversation_id)
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
    missing = _gap_summary(page["retention_gaps"])
    measure = _gap_measure(page["retention_gaps"])
    more = (
        f'<p class="pager"><a href="/c/{_e(conversation_id)}?after_seq={page["next_seq"]}">Later messages</a></p>'
        if later
        else ""
    )
    earlier = (
        f'<p class="pager"><a href="/c/{_e(conversation_id)}'
        f'?after_seq={max(0, after - messages.MAX_PAGE_MESSAGES)}">Earlier messages</a></p>'
        if after > 0
        else ""
    )
    return _page(
        200,
        title,
        scripts=True,
        body=_thread_top(conversation_id) + f"<h1>{_e(title)}</h1>{earlier}{missing}"
        f"<ul>{items or '<li class=muted>No messages yet.</li>'}</ul>{measure}{more}",
    )


def _thread_top(conversation_id: str) -> str:
    """A thread page's top row: the way back, the Copy button, and the line that says how the copy went. The
    button is wired by `/copy.js`, the one script any page loads, and fetches the thread's text from `data-src`."""
    return (
        '<div class="top"><p class="crumbs"><a href="/">All conversations</a></p>'
        f'<button type="button" id="copy" class="copy" data-src="/c/{_e(conversation_id)}/text" '
        'title="Copy the whole thread as plain text">Copy</button>'
        '<p id="copy-note" class="copy-note" role="status"></p></div>'
        '<script src="/copy.js" defer></script>'
    )


def _thread(
    items: list[dict[str, Any]], conversation_id: str = "", parents: dict[int, dict[str, Any]] | None = None
) -> str:
    """The page's messages with each reply nested under its parent when the parent is on this page."""
    on_page = {item["seq"] for item in items}
    replies: dict[int, list[dict[str, Any]]] = {}
    top = []
    for item in items:
        parent = (item.get("reply_to") or {}).get("seq")
        if isinstance(parent, int) and parent in on_page and parent < item["seq"]:
            replies.setdefault(parent, []).append(item)
        else:
            top.append(item)

    def render(item: dict[str, Any]) -> str:
        nested = replies.get(item["seq"])
        inner = f'<ul class="replies">{"".join(render(reply) for reply in nested)}</ul>' if nested else ""
        return _message(item, inner, on_page, conversation_id, parents or {})

    return "".join(render(item) for item in top)


def _gap_summary(gaps: list[dict[str, int]]) -> str:
    """Policy: the threaded view groups replies under their parent, so it isn't in seq order; it states the page's
    missing messages once, above the thread, never among the messages, where a notice's place would imply when."""
    if not gaps:
        return ""
    ranges = ", ".join(
        f"#{gap['from_seq']}" if gap["from_seq"] == gap["to_seq"] else f"#{gap['from_seq']}–#{gap['to_seq']}"
        for gap in sorted(gaps, key=lambda gap: gap["from_seq"])
    )
    # Arriving by a message link skips the summary, so a bar at the bottom, where it can't cover the anchored message,
    # points back to it. It is CSS only (the page runs no scripts): shown once the message's end is in view, and
    # dismissed by the checkbox. The action row is a span so narrow screens can give it a row of its own.
    return (
        f'<p class="notice" id="missing">Not kept any more on this page: messages {_e(ranges)}</p>'
        '<input type="checkbox" id="gap-dismiss" class="gap-dismiss">' + _gap_bar()
    )


def _gap_measure(gaps: list[dict[str, int]]) -> str:
    """The hidden copy of the bar, after the thread, that the reveal is timed on (see the stylesheet)."""
    return _gap_bar(measure=True) if gaps else ""


def _gap_bar(measure: bool = False) -> str:
    """The bar's markup, built once for both the arrival bar and its hidden copy, so their contents can't drift apart.
    The copy has no id or for, and is hidden from assistive technology."""
    attributes = 'class="gap-bar gap-measure" aria-hidden="true"' if measure else 'class="gap-bar"'
    dismiss = "" if measure else ' for="gap-dismiss"'
    return (
        f'<p {attributes}>Some messages on this page are no longer kept<span class="gap-sep"> · </span>'
        '<span class="gap-actions"><a href="#missing">view ranges</a>'
        f"<label{dismiss}>Dismiss</label></span></p>"
    )


def _message(
    item: dict[str, Any],
    replies: str = "",
    on_page: frozenset[int] | set[int] = frozenset(),
    conversation_id: str = "",
    parents: dict[int, dict[str, Any]] | None = None,
) -> str:
    if item.get("notice"):
        return f'<li class="notice">{_e(_notice_line(item))} · {_time(item["at"])}{replies}</li>'
    relayed = "".join(
        f'<blockquote class="relayed"><p class="source">relayed from {_e(entry["source"])} at {_e(entry["at"])}'
        f'</p><p class="relayed-text">{_e(entry["text"])}</p></blockquote>'
        for entry in item.get("relayed") or []
    )
    kind = item.get("author_kind")
    badge = f'<span class="badge {kind}">{kind}</span>' if kind in ("human", "agent") else ""
    return (
        f'<li class="message" id="m-{item["seq"]}"><div class="meta"><span class="role">'
        f"{_e(messages.author_label(item))}</span>{badge} · {_time(item['at'])} · #{item['seq']}</div>"
        f"{_reply_marker(item.get('reply_to'), on_page, conversation_id, parents or {})}"
        f'<p class="text">{_e(item["text"])}</p>{relayed}<span class="gap-end"></span>{replies}</li>'
    )


QUOTE = 160


def _reply_marker(
    reply_to: dict[str, Any] | None,
    on_page: frozenset[int] | set[int],
    conversation_id: str = "",
    parents: dict[int, dict[str, Any]] | None = None,
) -> str:
    """Where a reply's parent is: nothing when it's nested here; on another page, a link to it with a short
    quote of what it said; or 'parent unavailable' when the viewer can't see it, without its words or author."""
    if not reply_to:
        return ""
    if reply_to.get("seq") is None:
        return '<p class="reply-to unavailable">parent unavailable</p>'
    seq = int(reply_to["seq"])
    if seq in on_page:
        return ""
    link = f'<a href="{_e(message_path(conversation_id, seq))}">#{seq}</a>' if conversation_id else f"#{seq}"
    parent = (parents or {}).get(seq)
    quote = ""
    if parent is not None:
        words = parent["text"] if len(parent["text"]) <= QUOTE else parent["text"][:QUOTE] + "…"
        quote = f" · {_e(messages.author_label(parent))}: “{_e(words)}”"
    return f'<p class="reply-to">in reply to {link}{quote}</p>'


def _thread_text(config: Config, conn: Any, conversation_id: str) -> Response:
    """The whole thread as plain text, for the Copy button: the title and id, then each message in order.

    It reads as its page does (`_read`, the viewer's own history, a page at a time until one comes back empty, since
    a page can end at its byte budget before its 100 messages), so it holds what the viewer may see and no more,
    and it refuses what the page refuses. Where that history reports messages no longer kept, a line says so.
    """
    found: dict[int, str] = {}  # by seq, and a gap by its first seq, so one that two pages both report is kept once
    try:
        for page in _history_pages(config, conn, conversation_id, messages.MAX_PAGE_MESSAGES):
            found.update({gap["from_seq"]: conversations.gap_words(gap) for gap in page["retention_gaps"]})
            found.update({item["seq"]: _plain(item) for item in page["messages"]})
    except BatonError:  # unknown, or one the viewer can't see: the same answer, so neither is revealed
        return _not_found()
    body = "\n\n".join(found[seq] for seq in sorted(found)) or "No messages yet."
    return Response(
        200,
        f"Thread: {_title(config, conn, conversation_id)}\nId: {conversation_id}\n\n{body}\n".encode(),
        {**HEADERS, "Content-Type": "text/plain; charset=utf-8"},
    )


def _plain(item: dict[str, Any]) -> str:
    """One message as plain text: its number, time and sender, where it answers, then its words and any it relays."""
    head = [f"#{item['seq']}", item["at"]]
    if item.get("notice"):
        return " · ".join([*head, f"notice: {_notice_line(item)}"])
    head.append(messages.author_label(item))
    reply = item.get("reply_to")
    if reply:
        head.append(f"in reply to #{reply['seq']}" if reply.get("seq") is not None else "parent unavailable")
    relayed = "".join(
        f"\n\nrelayed from {entry['source']} at {entry['at']}:\n{entry['text']}" for entry in item.get("relayed") or []
    )
    return " · ".join(head) + "\n" + item["text"] + relayed


def _notice_line(item: dict[str, Any]) -> str:
    """The words the page gives a host notice: who was added or removed by whom, else the host's own sentence."""
    notice = item["notice"]
    if notice["event"] in ("participant_added", "participant_removed"):
        verb = "added" if notice["event"] == "participant_added" else "removed"
        return f"{notice['participant']} was {verb} by {notice['by']}"
    return item["text"]


def _page(status: int, title: str, body: str, scripts: bool = False) -> Response:
    """An HTML page. `scripts` is for a thread page alone, whose policy also lets it run the Copy button's script."""
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{_e(title)} · baton</title><link rel="stylesheet" href="/style.css"></head>'
        f"<body>{body}</body></html>"
    )
    policy = COPY_POLICY if scripts else HEADERS["Content-Security-Policy"]
    return Response(
        status,
        document.encode(),
        {**HEADERS, "Content-Security-Policy": policy, "Content-Type": "text/html; charset=utf-8"},
    )


def _not_found() -> Response:
    return _page(404, "Not found", '<p class="lead">Nothing here. <a href="/">All conversations</a></p>')


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


class _Handler(BaseHTTPRequestHandler):
    server_version = "baton-dashboard"
    sys_version = ""

    def _answer(self) -> None:
        assert isinstance(self.server, _Server)
        response = respond(self.server.config, self.command, self.path, self.headers.get("Host", ""))
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = _answer

    def log_message(self, format: str, *args: Any) -> None:
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
