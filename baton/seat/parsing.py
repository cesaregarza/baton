"""Strict HTTP header, cookie and form parsing before the seat opens its database."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from baton.config import Config

from .models import Response, _Refusal, _Request
from .rendering import HEADERS, _not_found, _page

SESSION_COOKIE = "__Host-baton_seat"
PRE_COOKIE = "__Host-baton_seat_pre"
FORM = "application/x-www-form-urlencoded"
# Headers a request may carry once at most; two of them would leave it ambiguous which one counts.
SINGLE_HEADERS = ("host", "origin", "cookie", "content-type", "content-length", "sec-fetch-site", "transfer-encoding")
MAX_BODY = 16 * 1024
SEND_PATH = re.compile(r"/seat/c/([A-Za-z0-9_-]{1,64})/send")
# Each POST route's fields; anything else, or a field given twice, is refused before it is read.
FIELDS = {
    "/seat/login": ({"token", "pre"}, {"token", "pre"}),
    "/seat/logout": ({"csrf"}, {"csrf"}),
    "/seat/ack": ({"csrf", "receipt"}, {"csrf", "receipt"}),
    "send": ({"text", "reply_to", "csrf", "request_id"}, {"text", "csrf", "request_id"}),
}


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
        raise _Refusal(
            Response(
                405,
                b"not allowed\n",
                {**HEADERS, "Allow": "GET, HEAD, POST", "Content-Type": "text/plain; charset=utf-8"},
            )
        )
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
        pairs = parse_qsl(
            body.decode("ascii"), keep_blank_values=True, strict_parsing=bool(body), encoding="utf-8", errors="strict"
        )
    except (UnicodeDecodeError, ValueError):
        raise _Refusal(_page(400, "Bad request", "<p>The form could not be read.</p>")) from None
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names) or not set(names) <= allowed or not required <= set(names):
        raise _Refusal(_page(400, "Bad request", "<p>The form had a field it shouldn't, or was missing one.</p>"))
    return dict(pairs)
