"""Every readable conversation is reachable from the seat, and both views say where messages are no longer kept."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from seat_helpers import agent_says, conversation, log_in, make_seat_host
from seat_helpers import get as seat_get
from test_dashboard_pages import MINE, made, page_config
from test_dashboard_pages import get as dashboard_get

from baton import conversations
from baton.store import connect


def _older(body: str) -> str | None:
    found = re.search(r'href="(/seat/\?[^"]*)">Older conversations<', body)
    return found.group(1).replace("&amp;", "&") if found else None


def _walk(host, cookies, between=None) -> list[str]:
    """Every title the seat lists, page by page through its Older links, calling `between` after each page."""
    seen, target = [], "/seat/"
    while target:
        body = seat_get(host.config, target, cookies=cookies).body.decode()
        seen += re.findall(r">(Thread \d{3})</a>", body)
        if between:
            between()
        target = _older(body)
    return seen


def test_every_readable_conversation_is_reachable_through_the_seat_pager(tmp_path: Path) -> None:
    host = make_seat_host(tmp_path)
    titles = [f"Thread {n:03d}" for n in range(conversations.MAX_LISTED + 7)]
    for title in titles:
        conversation(host, title=title)
    _, cookies = log_in(host.config, host.seat_token)
    seen = _walk(host, cookies)
    assert sorted(seen) == titles, f"{len(set(titles) - set(seen))} unreachable, {len(seen) - len(set(seen))} twice"


def test_a_removal_between_pages_neither_hides_nor_repeats_a_conversation(tmp_path: Path) -> None:
    host = make_seat_host(tmp_path)
    ids = {f"Thread {n:03d}": conversation(host, participants=("orchestrator", "reviewer", "operator"),
                                           title=f"Thread {n:03d}") for n in range(conversations.MAX_LISTED + 1)}
    _, cookies = log_in(host.config, host.seat_token)
    pages = []

    def remove_one_from_the_first_page() -> None:
        if not pages:
            pages.append(1)
            conn = connect(host.config.db_path)
            conversations.remove(conn, host.config, ids["Thread 150"], "operator", actor="orchestrator")
            conn.close()

    seen = _walk(host, cookies, remove_one_from_the_first_page)
    assert sorted(seen) == sorted(ids), f"missing {set(ids) - set(seen)}, repeated {len(seen) - len(set(seen))}"


def test_an_insertion_between_pages_repeats_nothing(tmp_path: Path) -> None:
    host = make_seat_host(tmp_path)
    titles = [f"Thread {n:03d}" for n in range(conversations.MAX_LISTED + 1)]
    for title in titles:
        conversation(host, title=title)
    _, cookies = log_in(host.config, host.seat_token)
    added = []

    def add_a_newer_one() -> None:
        if not added:
            added.append(conversation(host, title="Thread 999"))

    seen = _walk(host, cookies, add_a_newer_one)
    assert sorted(seen) == titles, f"repeated {len(seen) - len(set(seen))}"


def test_a_page_past_the_oldest_says_so(tmp_path: Path) -> None:
    host = make_seat_host(tmp_path)
    conversation(host, title="Thread 001")
    _, cookies = log_in(host.config, host.seat_token)
    body = seat_get(host.config, "/seat/?before_at=2000-01-01T00:00:00%2B00:00&before_id=conv_000",
                    cookies=cookies).body.decode()
    assert "No older conversations" in body and "not in any conversation yet" not in body


def test_the_seat_pager_refuses_a_cursor_it_did_not_make(tmp_path: Path) -> None:
    host = make_seat_host(tmp_path)
    _, cookies = log_in(host.config, host.seat_token)
    for query in ("before_at=2026-10-31T00:00:00%2B00:00", "before_id=conv_abc", "before_at=yesterday&before_id=c",
                  "before_at=2026-10-31T00:00:00%2B00:00&before_id=conv_abc&before_id=conv_def",
                  "before_at=2026-10-31T00:00:00%2B00:00&before_id=%3Cscript%3E"):
        assert seat_get(host.config, f"/seat/?{query}", cookies=cookies).status == 400, query


def _gap_between(body: str, before: str, after: str) -> str:
    """The text between two messages' words on a page, where a gap notice belongs."""
    return body[body.index(before) + len(before):body.index(after)]


def test_the_seat_says_where_messages_are_no_longer_kept(tmp_path: Path) -> None:
    host = make_seat_host(tmp_path)
    cid = conversation(host, participants=("orchestrator", "reviewer", "operator"))
    for word in ("first note", "second note", "third note", "fourth note", "fifth note"):
        agent_says(host, cid, word)
    conn = connect(host.config.db_path)
    conn.execute("DELETE FROM messages WHERE conversation_id = ? AND conversation_seq IN (2, 3)", (cid,))
    conn.close()
    _, cookies = log_in(host.config, host.seat_token)
    body = seat_get(host.config, f"/seat/c/{cid}", cookies=cookies).body.decode()
    assert "Messages #2–#3 are no longer kept" in _gap_between(body, "first note", "fourth note")
    assert "no longer kept" not in _gap_between(body, "fourth note", "fifth note")


def _replies_with_a_gap(conn, config, cid: str) -> None:
    """A message, three replies to it, and the middle reply (seq 3) gone."""
    parent = conversations.send(conn, config, sender="orchestrator", conversation_id=cid, text="first note")
    for word in ("second note", "third note", "fourth note"):
        conversations.send(conn, config, sender="reviewer", conversation_id=cid, text=word,
                           reply_to=parent["message_id"])
    conn.execute("DELETE FROM messages WHERE conversation_id = ? AND conversation_seq = 3", (cid,))


def _thread_with(config, steps: list[tuple[str, int | None]], missing: set[int]) -> str:
    """Send `steps` as (words, the step it replies to or None) in order, so step n is seq n; drop the `missing`
    seqs; and return the dashboard's page for the conversation."""
    conn = connect(config.db_path)
    cid = made(conn, config, "Threads with gaps", MINE)
    ids = {}
    for seq, (words, parent) in enumerate(steps, 1):
        sent = conversations.send(conn, config, sender="reviewer", conversation_id=cid, text=words,
                                  reply_to=ids[parent] if parent else None)
        ids[seq] = sent["message_id"]
    for seq in missing:
        conn.execute("DELETE FROM messages WHERE conversation_id = ? AND conversation_seq = ?", (cid, seq))
    conn.close()
    return dashboard_get(config, f"/c/{cid}").body.decode()


SUMMARY = "Not kept any more on this page: messages "


def _summary_only(body: str, ranges: str) -> None:
    """The threaded view states the missing ranges once, for the page, and puts no notice among the messages."""
    assert SUMMARY + ranges in body, f"no summary of {ranges}"
    assert body.count(SUMMARY) == 1
    assert "no longer kept" not in body[body.index("<ul>"):]  # the seat's inline wording never appears among them


def test_the_dashboard_summarises_a_gap_above_an_older_top_level_message(tmp_path: Path) -> None:
    """The reviewer's case: #1 has reply #4, #2 is top level, #3 is gone."""
    steps = [("one root", None), ("two top level", None), ("three gone", None), ("four reply to one", 1)]
    _summary_only(_thread_with(page_config(tmp_path), steps, {3}), "#3")


def test_the_dashboard_summarises_a_gap_between_two_branches(tmp_path: Path) -> None:
    steps = [("one root", None), ("two reply to one", 1), ("three gone", None), ("four root", None),
             ("five reply to four", 4)]
    _summary_only(_thread_with(page_config(tmp_path), steps, {3}), "#3")


def test_the_dashboard_summarises_a_gap_inside_a_reply_branch(tmp_path: Path) -> None:
    steps = [("one root", None), ("two reply to one", 1), ("three gone", 1), ("four reply to one", 1),
             ("five root", None)]
    _summary_only(_thread_with(page_config(tmp_path), steps, {3}), "#3")


def test_the_dashboard_summary_lists_every_range_exactly(tmp_path: Path) -> None:
    steps = [(f"note {n}", None) for n in range(1, 9)]
    _summary_only(_thread_with(page_config(tmp_path), steps, {3, 5, 6}), "#3, #5–#6")


def test_the_seat_places_a_gap_among_replies_in_order(tmp_path: Path) -> None:
    host = make_seat_host(tmp_path)
    cid = conversation(host, participants=("orchestrator", "reviewer", "operator"))
    conn = connect(host.config.db_path)
    _replies_with_a_gap(conn, host.config, cid)
    conn.close()
    _, cookies = log_in(host.config, host.seat_token)
    body = seat_get(host.config, f"/seat/c/{cid}", cookies=cookies).body.decode()
    assert "Message #3 is no longer kept" in _gap_between(body, "second note", "fourth note")


# Arriving by a message link on a page with a gap: a bar at the bottom says so and links to the summary --------

BAR = "Some messages on this page are no longer kept"
CROSS_BRANCH = [("one root", None), ("two top level", None), ("three gone", None), ("four reply to one", 1)]
INTERLEAVED = [("one root", None), ("two reply to one", 1), ("three gone", 1), ("four root", None),
               ("five reply to one", 1), ("six reply to four", 4)]


@pytest.mark.parametrize("steps", [CROSS_BRANCH, INTERLEAVED], ids=["cross-branch", "interleaved"])
def test_a_page_with_a_gap_carries_the_arrival_bar(tmp_path: Path, steps) -> None:
    body = _thread_with(page_config(tmp_path), steps, {3})
    bar = re.search(r'<p class="gap-bar"[^>]*>(.*?)</p>', body, re.S)
    assert bar, "no arrival bar on a page with a gap"
    assert BAR in bar.group(1) and re.search(r'<a href="#missing">view ranges</a>', bar.group(1))
    assert re.search(r'<p class="notice" id="missing">Not kept any more on this page: messages #3</p>', body)
    dismiss = re.search(r'<input type="checkbox" id="([\w-]+)" class="gap-dismiss"', body)
    assert dismiss and f'<label for="{dismiss.group(1)}">' in bar.group(1)
    assert body.index(dismiss.group(0)) < body.index(bar.group(0))  # the CSS reaches the bar from the checkbox
    assert "no longer kept" not in body[body.index("<ul>"):]  # still no inline notice among the messages
    for seq, _ in enumerate(steps, 1):  # every retained message keeps its anchor
        assert (seq == 3) != (f'id="m-{seq}"' in body)


def test_a_page_without_a_gap_has_no_bar(tmp_path: Path) -> None:
    body = _thread_with(page_config(tmp_path), CROSS_BRANCH, set())
    assert "gap-bar" not in body.split("<body>", 1)[1] and BAR not in body and 'id="missing"' not in body


def _rules(selector_part: str) -> list[str]:
    from baton import dashboard

    return [rule for rule in re.findall(r"([^{}]+\{[^}]*\})", dashboard.STYLE) if selector_part in rule.split("{")[0]]


def test_the_bar_shows_only_on_arrival_at_a_message_and_never_covers_the_top() -> None:
    assert any(rule.split("{")[0].strip() == ".gap-bar" and "display: none" in rule for rule in _rules(".gap-bar"))
    shown = [rule for rule in _rules(".gap-bar") if re.search(r"display: (?!none)", rule)]
    assert len(shown) == 1, "the bar has more than one way to show"
    selector, declarations = shown[0].split("{", 1)
    assert "li:target" in selector and ".gap-dismiss:not(:checked)" in selector  # arrival, and not dismissed
    assert "position: fixed" in declarations and "bottom: 0" in declarations
    assert not re.search(r"(^|[;\s])top:", declarations), "pinned to the top, where it covers the anchored message"


def test_the_dismiss_control_stays_reachable_by_keyboard() -> None:
    rules = _rules(".gap-dismiss")
    assert rules and not any("display: none" in rule for rule in rules), "a display:none checkbox can't be focused"
