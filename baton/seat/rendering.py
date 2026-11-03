"""Stateless seat HTML, CSS and response formatting; no database or session lifecycle."""

from __future__ import annotations

import html
from datetime import datetime, tzinfo
from typing import Any

from baton import conversations, messages

from .models import Response, _Session

HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; base-uri 'none'; form-action 'self'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    # Not no-referrer: under it Chromium sends `Origin: null` on a same-origin form post, which the exact Origin
    # check (rightly) refuses. same-origin keeps the real Origin there and sends nothing to other sites.
    "Referrer-Policy": "same-origin",
}
WARN_UNREAD = 400
# One column, the phone's own type, 16px so iOS never zooms into a field. The one loud thing is a decision asked
# of the seat; everything else stays quiet. Buttons are at least 44px tall.
STYLE = """\
:root { color-scheme: light dark; --ink: #1d2125; --muted: #5d6670; --rule: #e3e5e8; --paper: #fff;
        --action: #0d5c63; --on-action: #fff; --ask: #c98a14; --ask-bg: #fdf3dc; --target: #eef6f6; --box: #f3f4f6;
        --field: #b9bec4; }
@media (prefers-color-scheme: dark) {
  :root { --ink: #e4e7ea; --muted: #9ba4ae; --rule: #2d3237; --paper: #15181b; --action: #6cc5cc; --on-action: #0c1416;
          --ask: #d9a441; --ask-bg: #2b2414; --target: #12292b; --box: #22262a; --field: #4b525a; }
}
body { font: 16px/1.5 system-ui, -apple-system, sans-serif; margin: 0 auto; max-width: 40rem; padding: .75rem 1rem 6rem;
       color: var(--ink); background: var(--paper); }
a { color: var(--action); }
a:focus-visible, button:focus-visible, input:focus-visible, textarea:focus-visible {
  outline: 2px solid var(--action); outline-offset: 2px; }
nav { font-size: .9rem; }
nav a { margin-right: 1rem; }
h1 { font-size: 1.25rem; line-height: 1.3; margin: .9rem 0 .2rem; overflow-wrap: anywhere; }
h2 { font-size: 1rem; margin: 1.4rem 0 .2rem; color: var(--muted); font-weight: 600; }
ul { list-style: none; padding: 0; margin: .4rem 0; }
li { border-bottom: 1px solid var(--rule); padding: .7rem 0; min-width: 0; }
.muted, .meta time, .reply-to, .note { color: var(--muted); font-size: .875rem; }
.meta { display: flex; gap: .5rem; align-items: baseline; flex-wrap: wrap; margin: 0; }
.meta .role { font-weight: 600; min-width: 0; overflow-wrap: anywhere; }
.meta time { margin-left: auto; }
.reply-to { overflow-wrap: anywhere; }
.person { font-size: .75rem; border: 1px solid var(--rule); border-radius: 1rem; padding: 0 .45rem;
          color: var(--muted); }
.text { white-space: pre-wrap; overflow-wrap: anywhere; margin: .25rem 0 0; }
.notice { color: var(--muted); font-style: italic; margin: 0; overflow-wrap: anywhere; }
.relayed { border-left: 3px solid var(--rule); margin: .4rem 0 0; padding: .1rem 0 .1rem .7rem; }
.relayed p { margin: 0; overflow-wrap: anywhere; white-space: pre-wrap; }
.actions { margin: .3rem 0 0; font-size: .9rem; }
li.ask { border-left: 4px solid var(--ask); background: var(--ask-bg); padding-left: .7rem; padding-right: .5rem; }
.ask-label { font-weight: 600; font-size: .875rem; margin: 0 0 .2rem; }
.placeholder { color: var(--muted); font-style: italic; }
.in { color: var(--muted); font-size: .8rem; margin: 0 0 .15rem; overflow-wrap: anywhere; }
.sent { display: none; font-weight: 600; color: var(--action); margin: 0 0 .2rem; }
li:target { background: var(--target); padding-left: .5rem; padding-right: .5rem; }
li:target .sent { display: block; }
.banner { border-left: 4px solid var(--ask); background: var(--ask-bg); padding: .6rem .8rem; margin: .8rem 0; }
.bar { position: sticky; bottom: 0; background: var(--paper); border-top: 1px solid var(--rule); padding: .6rem 0; }
button { font: inherit; font-weight: 600; min-height: 44px; padding: .5rem 1.1rem; border: 0; border-radius: .5rem;
         background: var(--action); color: var(--on-action); }
button.quiet { background: none; color: var(--action); border: 1px solid var(--rule); }
.bar button, form.login button, form.compose button { width: 100%; }
label { display: block; font-weight: 600; margin: .8rem 0 .3rem; }
input, textarea { font: inherit; width: 100%; box-sizing: border-box; border: 1px solid var(--field);
                  border-radius: .5rem; padding: .6rem .7rem; color: var(--ink); background: var(--paper); }
textarea { min-height: 7rem; resize: vertical; }
form.inline { display: inline; }
nav button.quiet { min-height: 0; padding: .2rem .6rem; font-weight: 400; font-size: .9rem; }
.notice-box { border-left: 4px solid var(--muted); padding: .5rem .8rem; margin: .8rem 0; background: var(--box); }
"""


def _banner(unread: int) -> str:
    if unread < WARN_UNREAD:
        return ""
    return (f'<p class="banner">{unread} unread. Conversations you\'re in stop accepting messages at '
            f'{messages.MAX_UNREAD} unread. <a href="/seat/inbox">Mark messages as seen</a> to keep them open.</p>')


def _inbox_item(entry: dict[str, Any], session: _Session) -> str:
    if entry.get("placeholder"):
        return '<li class="placeholder">A message from a conversation you\'re no longer in.</li>'
    where = (f'<p class="actions"><a href="{_link(entry["conversation_id"], entry["conversation_seq"])}">'
             'Open in conversation</a></p>' if entry["conversation_id"] else "")
    title = f'<p class="in">{_e(entry["title"])}</p>' if entry.get("title") else ""
    return _message(entry, session, where, before=title)


def _with_gaps(rendered: list[tuple[int, str]], gaps: list[dict[str, int]]) -> str:
    """Rendered messages in seq order, with a notice at each gap, where the missing messages were."""
    out, pending = [], sorted(gaps, key=lambda gap: gap["from_seq"])
    for seq, item in rendered:
        while pending and pending[0]["to_seq"] < seq:
            out.append(_gap_notice(pending.pop(0)))
        out.append(item)
    return "".join(out + [_gap_notice(gap) for gap in pending])


def _gap_notice(gap: dict[str, int]) -> str:
    return f'<li class="placeholder">{_e(conversations.gap_words(gap))}</li>'


def _history_item(conversation_id: str, item: dict[str, Any], shown: set[int], session: _Session) -> str:
    parent = item.get("reply_to")
    marker = ""
    if parent:
        marker = ('<p class="reply-to">in reply to a message you can\'t see</p>' if parent.get("seq") is None else
                  "" if int(parent["seq"]) in shown else
                  f'<p class="reply-to">in reply to <a href="{_link(conversation_id, int(parent["seq"]))}">'
                  f'an earlier message</a></p>')
    actions = (f'<p class="actions"><a href="/seat/c/{_e(conversation_id)}?reply={_e(item["message_id"])}#compose">'
               'Reply</a></p>' if item.get("from_role") and item.get("from_role") != session.role else "")
    return _message(item, session, actions, anchor=f"m-{int(item['seq'])}", before=marker)


def _link(conversation_id: str, seq: int) -> str:
    """A message's place in a conversation, by its conversation seq: the page that starts with it."""
    return f"/seat/c/{conversation_id}?after_seq={seq - 1}#m-{seq}"


def _message(item: dict[str, Any], session: _Session, after: str, *, anchor: str = "", before: str = "") -> str:
    """One message: who and when, what a decision or attention asks of the seat, its words and any relayed quote."""
    role = session.role
    ident = f' id="{anchor}"' if anchor else ""
    if not item.get("from_role"):
        return f'<li{ident}><p class="notice">{_e(item["text"])}</p></li>'
    asked = role in (item.get("attention") or [])
    label = ("Asks you to decide" if asked and item.get("needs_decision") else
             "Asks for a decision" if item.get("needs_decision") else "Asks you to look" if asked else "")
    css = ' class="ask"' if asked and item.get("needs_decision") else ""
    relayed = "".join(f'<blockquote class="relayed"><p class="muted">relayed from {_e(quote.get("source", ""))}</p>'
                      f'<p>{_e(quote.get("text", ""))}</p></blockquote>' for quote in item.get("relayed") or [])
    flag = f'<p class="ask-label">{label}</p>' if label else ""
    sent = '<p class="sent">Sent</p>' if item["from_role"] == role else ""
    return (f'<li{ident}{css}>{before}{flag}{sent}{_meta(item, session)}<p class="text">{_e(item["text"])}</p>{relayed}'
            f'{after}</li>')


def _meta(item: dict[str, Any], session: _Session) -> str:
    who = "You" if item["from_role"] == session.role else messages.author_label(item)
    person = ' <span class="person">person</span>' if item.get("author_kind") == "human" else ""
    return (f'<p class="meta"><span class="role">{_e(who)}</span>{person}'
            f'<time datetime="{_e(item.get("at", ""))}">{_e(_when(item.get("at", ""), session.zone))}</time></p>')


def _when(at: str, zone: tzinfo) -> str:
    """`29 Oct, 8:14 pm CDT` from a stored ISO time, in the seat's zone; the stored text itself if it isn't one."""
    try:
        moment = datetime.fromisoformat(at).astimezone(zone)
    except ValueError:
        return at
    hour = moment.hour % 12 or 12
    return f"{moment.day} {moment:%b}, {hour}:{moment:%M} {'am' if moment.hour < 12 else 'pm'} {moment:%Z}"


def _short(text: str, limit: int = 160) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _nav(session: _Session) -> str:
    return (f'<nav><a href="/seat/">Conversations</a><a href="/seat/inbox">Inbox</a><a href="/seat/needs">Needs you</a>'
            f'{_logout_form(session)}</nav>')


def _topic(topic: str | None) -> str:
    return f' <span class="badge">{_e(topic)}</span>' if topic else ""


def _csrf(session: _Session) -> str:
    return f'<input type="hidden" name="csrf" value="{_e(session.csrf)}">'


def _logout_form(session: _Session) -> str:
    return (f'<form class="inline" method="post" action="/seat/logout">{_csrf(session)}'
            '<button class="quiet" type="submit">Log out</button></form>')


def _not_in(status: int = 403, words: str = "You're not in this conversation.") -> Response:
    return _page(status, "Not available", f'<h1>Not available</h1><p>{_e(words)}</p>'
                                          '<p><a href="/seat/">Your conversations</a></p>')


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
