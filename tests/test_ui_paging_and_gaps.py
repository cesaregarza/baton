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
from baton.database.store import connect


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
    ids = {
        f"Thread {n:03d}": conversation(
            host, participants=("orchestrator", "reviewer", "operator"), title=f"Thread {n:03d}"
        )
        for n in range(conversations.MAX_LISTED + 1)
    }
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
    body = seat_get(
        host.config, "/seat/?before_at=2000-01-01T00:00:00%2B00:00&before_id=conv_000", cookies=cookies
    ).body.decode()
    assert "No older conversations" in body and "not in any conversation yet" not in body


def test_the_seat_pager_refuses_a_cursor_it_did_not_make(tmp_path: Path) -> None:
    host = make_seat_host(tmp_path)
    _, cookies = log_in(host.config, host.seat_token)
    for query in (
        "before_at=2026-10-31T00:00:00%2B00:00",
        "before_id=conv_abc",
        "before_at=yesterday&before_id=c",
        "before_at=2026-10-31T00:00:00%2B00:00&before_id=conv_abc&before_id=conv_def",
        "before_at=2026-10-31T00:00:00%2B00:00&before_id=%3Cscript%3E",
    ):
        assert seat_get(host.config, f"/seat/?{query}", cookies=cookies).status == 400, query


def _gap_between(body: str, before: str, after: str) -> str:
    """The text between two messages' words on a page, where a gap notice belongs."""
    return body[body.index(before) + len(before) : body.index(after)]


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
        conversations.send(
            conn, config, sender="reviewer", conversation_id=cid, text=word, reply_to=parent["message_id"]
        )
    conn.execute("DELETE FROM messages WHERE conversation_id = ? AND conversation_seq = 3", (cid,))


def _thread_with(config, steps: list[tuple[str, int | None]], missing: set[int]) -> str:
    """Send `steps` as (words, the step it replies to or None) in order, so step n is seq n; drop the `missing`
    seqs; and return the dashboard's page for the conversation."""
    conn = connect(config.db_path)
    cid = made(conn, config, "Threads with gaps", MINE)
    ids = {}
    for seq, (words, parent) in enumerate(steps, 1):
        sent = conversations.send(
            conn, config, sender="reviewer", conversation_id=cid, text=words, reply_to=ids[parent] if parent else None
        )
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
    assert "no longer kept" not in body[body.index("<ul>") : body.index("</ul>")]  # no inline wording among them


def test_the_dashboard_summarises_a_gap_above_an_older_top_level_message(tmp_path: Path) -> None:
    """The reviewer's case: #1 has reply #4, #2 is top level, #3 is gone."""
    steps = [("one root", None), ("two top level", None), ("three gone", None), ("four reply to one", 1)]
    _summary_only(_thread_with(page_config(tmp_path), steps, {3}), "#3")


def test_the_dashboard_summarises_a_gap_between_two_branches(tmp_path: Path) -> None:
    steps = [
        ("one root", None),
        ("two reply to one", 1),
        ("three gone", None),
        ("four root", None),
        ("five reply to four", 4),
    ]
    _summary_only(_thread_with(page_config(tmp_path), steps, {3}), "#3")


def test_the_dashboard_summarises_a_gap_inside_a_reply_branch(tmp_path: Path) -> None:
    steps = [
        ("one root", None),
        ("two reply to one", 1),
        ("three gone", 1),
        ("four reply to one", 1),
        ("five root", None),
    ]
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
INTERLEAVED = [
    ("one root", None),
    ("two reply to one", 1),
    ("three gone", 1),
    ("four root", None),
    ("five reply to one", 1),
    ("six reply to four", 4),
]


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
    assert "no longer kept" not in body[body.index("<ul>") : body.index("</ul>")]  # still no inline notice among them
    for seq, _ in enumerate(steps, 1):  # every retained message keeps its anchor
        assert (seq == 3) != (f'id="m-{seq}"' in body)


def test_a_page_without_a_gap_has_no_bar(tmp_path: Path) -> None:
    body = _thread_with(page_config(tmp_path), CROSS_BRANCH, set())
    assert "gap-bar" not in body.split("<body>", 1)[1] and BAR not in body and 'id="missing"' not in body


def test_the_marker_closes_a_message_before_its_replies(tmp_path: Path) -> None:
    body = _thread_with(page_config(tmp_path), [("parent words", None), ("reply words", 1)], set())
    parent = body[body.index('id="m-1"') :]
    marker = parent.index('<span class="gap-end"></span>')
    assert parent.index('<p class="text">parent words</p>') < marker < parent.index('<ul class="replies">')


def test_a_gap_page_has_one_hidden_copy_after_the_thread_and_a_clean_page_has_none(tmp_path: Path) -> None:
    body = _thread_with(page_config(tmp_path), CROSS_BRANCH, {3})
    copies = re.findall(r'<p class="gap-bar gap-measure"[^>]*>.*?</p>', body, re.S)
    assert len(copies) == 1, "one measuring copy of the bar per page with gaps"
    assert copies[0].startswith('<p class="gap-bar gap-measure" aria-hidden="true">')
    assert " id=" not in copies[0] and " for=" not in copies[0] and "<input" not in copies[0]
    assert body.index(copies[0]) > body.index("</ul>"), "the copy sits after the thread, so no marker follows it"
    assert "gap-measure" not in _thread_with(page_config(tmp_path / "clean"), CROSS_BRANCH, set())


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


def _narrow_block() -> str:
    """The stylesheet's rules for narrow screens: the block that opens at 480px and closes at its own brace."""
    from baton import dashboard

    found = re.search(r"@media \(max-width: 480px\) \{(.*?)\n\}", dashboard.STYLE, re.S)
    assert found, "no narrow-screen block"
    return found.group(1)


TAP_RULE = r"\.gap-actions a, \.gap-actions label \{[^}]*min-height: 44px;[^}]*min-width: 44px;"
ACTION_ROW = r"\.gap-actions \{[^}]*min-height: 44px;"


def test_the_bar_targets_are_44px_tall_on_narrow_screens() -> None:
    """The action row is a row of its own under the bar's text, 44 CSS px tall, and its two targets fill it at
    the same height and at least that wide, so the targets never reach over the text."""
    narrow = _narrow_block()
    assert re.search(TAP_RULE, narrow), "no 44px target rule at narrow widths"
    assert re.search(ACTION_ROW, narrow), "the action row is not 44px tall at narrow widths"
    assert ".gap-sep { display: none; }" in narrow, "the separator would dangle at the end of the text row"


def test_the_gap_controls_are_44_css_px_and_never_shrink_or_break_a_word() -> None:
    """The narrow controls are 44 CSS px, not rem, so they don't grow with the root font. They keep their width and
    their words: a control never shrinks and a word never breaks, and the action row wraps when both don't fit."""
    narrow = _narrow_block()
    (controls,) = re.findall(r"\.gap-actions a, \.gap-actions label \{([^}]*)\}", narrow)
    for declaration in ("min-height: 44px", "min-width: 44px", "flex-shrink: 0", "overflow-wrap: normal"):
        assert declaration in controls, declaration
    (row,) = re.findall(r"\.gap-actions \{([^}]*)\}", narrow)
    for declaration in ("min-height: 44px", "flex-wrap: wrap", "row-gap:"):
        assert declaration in row, declaration
    assert "2.75rem" not in controls and "2.75rem" not in row


def test_a_wrapped_gap_control_is_centred_on_its_own_line() -> None:
    """The space between the controls is the row's column gap, not the label's margin, so a control that wraps
    is centred on its line."""
    narrow = _narrow_block()
    (row,) = re.findall(r"\.gap-actions \{([^}]*)\}", narrow)
    (controls,) = re.findall(r"\.gap-actions a, \.gap-actions label \{([^}]*)\}", narrow)
    assert "column-gap: .6rem;" in row, "the space between the controls is not the row's column gap"
    assert "margin-left: 0;" in controls, "a control on its own line keeps the label's left margin"


def test_the_bar_targets_are_sized_only_at_narrow_widths() -> None:
    from baton import dashboard

    assert len(re.findall(TAP_RULE, _narrow_block())) == 1
    assert len(re.findall(TAP_RULE, dashboard.STYLE)) == 1, "the target rule belongs in the narrow block only"


def test_the_bar_markup_is_the_one_the_stylesheet_targets(tmp_path: Path) -> None:
    body = _thread_with(page_config(tmp_path), CROSS_BRANCH, {3})
    assert (
        '<input type="checkbox" id="gap-dismiss" class="gap-dismiss">'
        '<p class="gap-bar">Some messages on this page are no longer kept<span class="gap-sep"> · </span>'
        '<span class="gap-actions"><a href="#missing">view ranges</a>'
        '<label for="gap-dismiss">Dismiss</label></span></p>'
    ) in body


def test_the_reveal_is_timed_on_a_hidden_copy_hung_from_the_marker() -> None:
    """The copy hangs from the marker that closes the anchored message's own content, so the reveal never waits for the
    replies. The copy owns the view timeline; the bar and its checkbox animate on it, so the bar's real height decides,
    and no fixed inset stands in for it."""
    from baton import dashboard

    style = dashboard.STYLE
    assert "@supports (animation-timeline: view()) and (anchor-name: --a) {" in style
    assert "@supports not ((animation-timeline: view()) and (anchor-name: --a)) {" in style
    assert "li:target > .gap-end { display: block; height: 0; anchor-name: --gap-end; }" in style
    (copy,) = [rule for rule in _rules(".gap-measure") if "position: absolute" in rule]
    for declaration in (
        "display: block",
        "position-anchor: --gap-end",
        "top: anchor(bottom)",
        "left: 0",
        "right: 0",
        "visibility: hidden",
        "pointer-events: none",
        "view-timeline: --gap-reveal block",
    ):
        assert declaration in copy, declaration
    assert "body { timeline-scope: --gap-reveal; }" in style
    assert "--gap-inset" not in style and "view-timeline-inset" not in style
    assert "li:target { view-timeline" not in style and "view-timeline: --gap-end" not in style


def test_the_copy_and_the_bar_share_their_box_styles() -> None:
    shared = [rule for rule in _rules(".gap-measure") if rule.split("{")[0].strip().endswith(".gap-bar, .gap-measure")]
    assert len(shared) == 1, "the bar and its copy have one shared box rule"
    for declaration in (
        "padding: .5rem 1rem",
        "font-size: .85rem",
        "border-top: 1px solid var(--line-strong)",
        "text-align: center",
    ):
        assert declaration in shared[0], declaration
    assert "position" not in shared[0], "only the real bar is positioned to the viewport"


def test_the_bar_reveals_on_the_copy_and_keeps_its_targets_out_of_tab_while_hidden() -> None:
    """Where scroll-driven animations and anchor positioning exist, the bar's visibility follows the copy's timeline,
    and the checkbox and link are hidden with visibility while it is hidden. Without them, the bar shows on arrival."""
    from baton import dashboard

    style = dashboard.STYLE
    assert "animation-timeline: --gap-reveal" in style
    assert "animation-range: entry-crossing calc(100% - 2px) entry-crossing calc(100% - 2px)" in style
    assert "@keyframes gap-reveal { from { visibility: hidden; } to { visibility: visible; } }" in style
    assert "visibility: hidden; animation: gap-reveal linear both" in style, "hidden with visibility, not opacity alone"


def test_the_action_row_clears_the_focus_ring_of_the_text_on_narrow_screens() -> None:
    assert re.search(r"\.gap-actions \{[^}]*margin-top: \.3rem;", _narrow_block()), "no gap between text and row"
