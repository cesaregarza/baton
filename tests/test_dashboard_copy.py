"""The copy button on a thread page: one press copies the whole thread as plain text.

Everything here is synthetic: generic roles and a temporary data directory. The text is read the way the page
reads the thread, as the same viewer and a page at a time, so it holds what that viewer may see and nothing the
page hides. The script that copies is the dashboard's own file, and only a thread page may load it.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

import pytest
from test_dashboard import COPY_SCRIPT, PAYLOAD, snapshot, text
from test_dashboard_pages import MINE, THEIRS, get, made, page_config

from baton import conversations, dashboard, messages
from baton.config import Config
from baton.store import connect, transaction

HEADER = "Thread: {title}\nId: {cid}\n\n"


def say(config: Config, cid: str, sender: str, words: str, **kwargs: Any) -> str:
    conn = connect(config.db_path)
    try:
        return conversations.send(conn, config, sender=sender, conversation_id=cid, text=words, **kwargs)["message_id"]
    finally:
        conn.close()


def open_thread(config: Config, title: str, participants: list[str] = MINE) -> str:
    conn = connect(config.db_path)
    try:
        return made(conn, config, title, participants)
    finally:
        conn.close()


def varied_thread(config: Config) -> str:
    """A thread with two replies to its first message, a quotation, and words over several lines."""
    cid = open_thread(config, "Release plan")
    first = say(config, cid, "orchestrator", "plan is drafted\nand a second line")
    say(config, cid, "reviewer", "looks good", reply_to=first)
    say(config, cid, "orchestrator", "the build says:", relayed=[
        {"source": "ci", "at": "2026-10-29T18:02:00+00:00", "text": "all passed\nin 41 seconds"}])
    say(config, cid, "reviewer", "one more thing", reply_to=first)
    return cid


class Shown(HTMLParser):
    """What a thread page shows: each message's number, sender, time, words and quotations, and each notice's
    words and time. The page nests a reply under its parent, so that is how a reply's parent is read."""

    FIELDS = {("span", "role"): "sender", ("p", "reply-to"): "marker", ("p", "text"): "words"}

    def __init__(self, body: str) -> None:
        super().__init__()
        self.messages: list[dict[str, Any]] = []
        self.notices: list[dict[str, Any]] = []
        self._lis: list[dict[str, Any]] = []
        self._into: tuple[dict[str, Any], str] | None = None
        self._quote: dict[str, Any] = {}
        self.feed(body)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        given = dict(attrs)
        classes = (given.get("class") or "").split()
        kind = (tag, classes[0] if classes else "")
        owner = self._lis[-1] if self._lis else {}
        if kind == ("li", "message"):
            entry = {"seq": int(str(given["id"])[2:]), "parent": owner.get("seq"), "at": "", "marker": "",
                     "words": "", "sender": "", "quotes": []}
            self.messages.append(entry)
            self._lis.append(entry)
        elif kind == ("li", "notice"):
            entry = {"words": "", "at": ""}
            self.notices.append(entry)
            self._lis.append(entry)
            self._into = (entry, "words")
        elif tag == "li":
            self._lis.append({})
        elif tag == "time":
            owner["at"] = owner["at"] or given["datetime"]
            self._into = None
        elif tag == "blockquote":
            self._quote = {"source": "", "quoted": ""}
            owner["quotes"].append(self._quote)
        elif kind == ("p", "source"):
            self._into = (self._quote, "source")
        elif kind == ("p", "relayed-text"):
            self._into = (self._quote, "quoted")
        elif kind in self.FIELDS:
            self._into = (owner, self.FIELDS[kind])

    def handle_endtag(self, tag: str) -> None:
        if tag == "li":
            self._lis.pop()
        elif tag in ("p", "span"):
            self._into = None

    def handle_data(self, data: str) -> None:
        if self._into is not None:
            self._into[0][self._into[1]] += data


def copy_of(config: Config, cid: str) -> str:
    response = get(config, f"/c/{cid}/text")
    assert response.status == 200
    return text(response)


def worded(entry: dict[str, Any]) -> str:
    """One message as the copy should word it, from what the page shows of it."""
    head = [f"#{entry['seq']}", entry["at"], entry["sender"]]
    if entry["parent"] is not None:
        head.append(f"in reply to #{entry['parent']}")
    quotes = "".join(f"\n\n{quote['source']}:\n{quote['quoted']}" for quote in entry["quotes"])
    return " · ".join(head) + "\n" + entry["words"] + quotes


def has_copy_button(body: str, cid: str) -> bool:
    found = re.search(rf'<button type="button" id="copy"[^>]*data-src="/c/{cid}/text"[^>]*>Copy</button>', body)
    return found is not None


def in_order(copied: str, parts: list[str]) -> None:
    """Every part is in the copy, and in the order given."""
    assert all(part in copied for part in parts), [part for part in parts if part not in copied]
    places = [copied.index(part) for part in parts]
    assert places == sorted(places), parts


def directives(response: Any) -> dict[str, str]:
    policy = response.headers["Content-Security-Policy"]
    return dict(part.strip().split(" ", 1) for part in policy.split(";") if part.strip())


# 1. The button, and the one page that may run a script ----------------------------------------------------


def test_a_thread_page_has_a_copy_button_at_the_top_and_loads_only_its_own_script(tmp_path) -> None:
    config = page_config(tmp_path)
    cid = varied_thread(config)
    body = text(get(config, f"/c/{cid}"))
    button = re.search(r'<button type="button" id="copy"[^>]*>Copy</button>', body)
    assert button, "no button labelled Copy"
    assert f'data-src="/c/{cid}/text"' in button.group(0)
    assert body.index(button.group(0)) < body.index("<h1>") < body.index('class="message"')
    assert body.count("<script") == 1 and COPY_SCRIPT in body  # the dashboard's own file, nothing inline
    assert not re.search(r"\son[a-z]+\s*=", body) and "javascript:" not in body  # no inline handler
    assert re.search(r'id="copy-note"[^>]*role="status"', body), "the result has to be announced"


def test_the_thread_page_alone_may_run_a_script_and_fetch_from_its_own_origin(tmp_path) -> None:
    config = page_config(tmp_path)
    cid = varied_thread(config)
    standard = directives(get(config, "/"))
    thread = directives(get(config, f"/c/{cid}"))
    assert thread.pop("script-src", None) == "'self'" and thread.pop("connect-src", None) == "'self'"
    assert thread == standard, "everything else about the policy is the standard one"
    assert "script-src" not in standard and standard["default-src"] == "'none'"
    for target in ("/", "/needs", "/style.css", "/copy.js", f"/c/{cid}/text", f"/c/{cid}?after_seq=nope", "/c/missing",
                   "/missing"):
        response = get(config, target)
        assert response.headers["Content-Security-Policy"] == dashboard.HEADERS["Content-Security-Policy"], target


def test_the_script_is_served_like_the_stylesheet_and_never_turns_text_into_markup(tmp_path) -> None:
    config = page_config(tmp_path)
    served = get(config, "/copy.js")
    assert served.status == 200 and served.headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert {key: served.headers[key] for key in dashboard.HEADERS} == dashboard.HEADERS
    source = text(served)
    # The write starts inside the tap, with the text handed over as a promise, which is the form Safari accepts.
    assert "navigator.clipboard.write(" in source and "ClipboardItem" in source
    # What a thread says is only ever set as a value or as text, so nothing in it can run.
    assert ".value =" in source and ".textContent =" in source
    for markup in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "srcdoc"):
        assert markup not in source, markup


# 2. The copied text is the visible thread -----------------------------------------------------------------


def test_the_copied_text_matches_the_visible_thread(tmp_path) -> None:
    config = page_config(tmp_path)
    cid = varied_thread(config)
    shown = Shown(text(get(config, f"/c/{cid}"))).messages
    assert [entry["seq"] for entry in shown if entry["parent"] is None] == [1, 3], "the page nests the replies"
    expected = HEADER.format(title="Release plan", cid=cid) \
        + "\n\n".join(worded(entry) for entry in sorted(shown, key=lambda entry: entry["seq"])) + "\n"
    assert copy_of(config, cid) == expected
    assert "\n\n#2 · " in expected and " · reviewer · in reply to #1\nlooks good\n" in expected
    assert "relayed from ci at 2026-10-29T18:02:00+00:00:\nall passed\nin 41 seconds\n" in expected


def test_a_host_notice_is_copied_in_the_words_the_page_gives_it_at_its_place_in_the_thread(tmp_path) -> None:
    config = page_config(tmp_path)
    cid = open_thread(config, "Notices")
    say(config, cid, "orchestrator", "before the notices")
    conn = connect(config.db_path)
    try:
        conversations.add(conn, config, cid, "assistant", actor="orchestrator")
        conversations.remove(conn, config, cid, "assistant", actor="orchestrator")
    finally:
        conn.close()
    say(config, cid, "orchestrator", "after the notices")
    notices = Shown(text(get(config, f"/c/{cid}"))).notices
    words = [notice["words"].rstrip(" ·") for notice in notices]
    assert words == ["assistant was added by orchestrator", "assistant was removed by orchestrator"]
    lines = [f"#{seq} · {notice['at']} · notice: {line}" for seq, notice, line in zip((2, 3), notices, words,
                                                                                      strict=True)]
    copied = copy_of(config, cid)
    assert all(line in copied.split("\n") for line in lines)
    in_order(copied, ["before the notices", lines[0], lines[1], "after the notices"])


def test_a_topic_move_notice_has_the_same_words_in_the_page_and_copy(tmp_path) -> None:
    config = page_config(tmp_path)
    cid = open_thread(config, "Moved thread")
    words = f"alias {PAYLOAD} moved this thread from alpha to beta"
    conn = connect(config.db_path)
    try:
        with transaction(conn):
            conversations._fan_out(conn, cid, MINE, sender=None, text=words,
                                   notice={"event": "topic_moved", "from": "alpha", "to": "beta",
                                           "by": "orchestrator"})
    finally:
        conn.close()
    page = text(get(config, f"/c/{cid}"))
    [notice] = Shown(page).notices
    assert notice["words"].rstrip(" ·") == words
    assert PAYLOAD not in page, "the page must escape the notice's words"
    assert copy_of(config, cid) == (HEADER.format(title="Moved thread", cid=cid)
                                   + f"#1 · {notice['at']} · notice: {words}\n")


def test_a_thread_with_nothing_in_it_says_so_as_the_page_does(tmp_path) -> None:
    config = page_config(tmp_path)
    cid = open_thread(config, "Quiet")
    page = text(get(config, f"/c/{cid}"))
    assert "No messages yet." in page and has_copy_button(page, cid)
    assert copy_of(config, cid) == HEADER.format(title="Quiet", cid=cid) + "No messages yet.\n"


@pytest.mark.parametrize(("count", "size"), [(205, 0), (40, 16 * 1024)])
def test_a_thread_longer_than_a_page_is_copied_whole_from_any_of_its_pages(tmp_path, count: int, size: int) -> None:
    """A page holds 100 messages or about half a megabyte, so a copy of the page alone would stop short."""
    config = page_config(tmp_path)
    cid = open_thread(config, "Long thread")
    conn = connect(config.db_path)
    try:
        for number in range(1, count + 1):
            conversations.send(conn, config, sender="reviewer", conversation_id=cid,
                               text=f"note {number:03d} " + "x" * size)
    finally:
        conn.close()
    first = text(get(config, f"/c/{cid}"))
    assert 0 < first.count('class="message"') < count and "Later messages" in first, "the page really is shorter"
    assert first.count('class="message"') <= messages.MAX_PAGE_MESSAGES
    copied = copy_of(config, cid)
    assert [int(number) for number in re.findall(r"^#(\d+) · ", copied, re.M)] == list(range(1, count + 1))
    assert re.findall(r"^note (\d{3})\b", copied, re.M) == [f"{number:03d}" for number in range(1, count + 1)]
    assert has_copy_button(text(get(config, f"/c/{cid}?after_seq={count - 5}")), cid), "copies from any page"


def test_messages_no_longer_kept_are_named_where_they_were_and_once(tmp_path) -> None:
    config = page_config(tmp_path)
    cid = open_thread(config, "Gaps")
    for words in ("first note", "second note", "third note", "fourth note", "fifth note"):
        say(config, cid, "orchestrator", words)
    conn = connect(config.db_path)
    try:
        conn.execute("DELETE FROM messages WHERE conversation_id = ? AND conversation_seq IN (2, 3, 5)", (cid,))
    finally:
        conn.close()
    page = text(get(config, f"/c/{cid}"))
    assert "Not kept any more on this page: messages #2–#3, #5" in page and has_copy_button(page, cid)
    copied = copy_of(config, cid)
    in_order(copied, ["first note", "Messages #2–#3 are no longer kept.", "fourth note",
                      "Message #5 is no longer kept."])
    assert copied.count("no longer kept") == 2
    assert "second note" not in copied and "fifth note" not in copied


def test_a_copy_keeps_the_gap_when_no_messages_remain(tmp_path) -> None:
    config = page_config(tmp_path)
    cid = open_thread(config, "Expired thread")
    say(config, cid, "orchestrator", "an expired message")
    conn = connect(config.db_path)
    try:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (cid,))
    finally:
        conn.close()
    assert copy_of(config, cid) == (HEADER.format(title="Expired thread", cid=cid)
                                   + "Message #1 is no longer kept.\n")


# 3. What the viewer cannot see is never copied ------------------------------------------------------------


def test_a_message_the_viewer_cannot_see_is_never_in_the_copy(tmp_path) -> None:
    config = page_config(tmp_path)
    late = open_thread(config, "Joined later", ["orchestrator", "reviewer"])
    hidden = say(config, late, "reviewer", "words from before the join")
    conn = connect(config.db_path)
    try:
        conversations.add(conn, config, late, "operator", actor="orchestrator")
    finally:
        conn.close()
    say(config, late, "orchestrator", "a reply after the join", reply_to=hidden)
    side = open_thread(config, "Private side thread", THEIRS)
    say(config, side, "assistant", "side thread words")
    page, copied = text(get(config, f"/c/{late}")), copy_of(config, late)
    assert "words from before the join" not in page and "words from before the join" not in copied
    assert re.search(r"^#\d+ · \S+ · orchestrator · parent unavailable\na reply after the join\n", copied, re.M)
    assert "in reply to" not in copied, "a parent the viewer cannot see is not named"
    for target in (side, "nosuchthread"):  # the copy answers exactly as the page does, and says nothing of the thread
        refused = get(config, f"/c/{target}/text")
        assert refused.status == get(config, f"/c/{target}").status == 404
        assert "Private side thread" not in text(refused) and "side thread words" not in text(refused)
    flagged = page_config(tmp_path, view_all=True)  # the copy follows the page's rule, not a rule of its own
    assert "words from before the join" in copy_of(flagged, late)
    assert "side thread words" in copy_of(flagged, side)


# 4. Message words are copied as text and never run ---------------------------------------------------------


def test_html_and_script_in_a_message_are_copied_as_text_and_never_run(tmp_path) -> None:
    config = page_config(tmp_path)
    cid = open_thread(config, f"title {PAYLOAD}")
    say(config, cid, "orchestrator", f"text {PAYLOAD}",
        relayed=[{"source": f"source {PAYLOAD}", "at": "2026-10-29T08:00:00+00:00", "text": f"relayed {PAYLOAD}"}])
    page = text(get(config, f"/c/{cid}"))
    assert page.count("<script") == 1 and COPY_SCRIPT in page and "<img" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    response = get(config, f"/c/{cid}/text")
    copied = text(response)
    for words in (f"Thread: title {PAYLOAD}\n", f"\ntext {PAYLOAD}\n", f"relayed from source {PAYLOAD} at ",
                  f":\nrelayed {PAYLOAD}\n"):
        assert words in copied, words
    assert response.headers["Content-Type"] == "text/plain; charset=utf-8"  # a browser shows it, never runs it
    assert response.headers["X-Content-Type-Options"] == "nosniff"


# 5. The copy is a read like any other -----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/copy.js", "/c/{cid}/text"])
def test_the_copy_routes_carry_the_standard_headers_and_refuse_foreign_hosts_and_writes(tmp_path, path: str) -> None:
    config = page_config(tmp_path)
    target = path.format(cid=varied_thread(config))
    before = snapshot(config)
    response = get(config, target)
    assert response.status == 200
    assert {key: response.headers[key] for key in dashboard.HEADERS} == dashboard.HEADERS
    assert not any(key.lower().startswith("access-control-") for key in response.headers)
    assert get(config, target, host="attacker.example.test").status == 421
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        refused = get(config, target, method=method)
        assert refused.status == 405 and refused.headers["Allow"] == "GET, HEAD", method
    assert snapshot(config) == before, "copying marks nothing read and writes nothing"
