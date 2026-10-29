"""The dashboard read on a phone: long words wrap, every item past a page cap can be reached, and a deep link
keeps its parent's context. Synthetic and offline, with generic roles."""

from __future__ import annotations

import re
from typing import Any

import pytest
from test_dashboard import text
from test_dashboard_pages import MINE, get, made, needs_items, page_config

from baton import conversations, jobs
from baton.store import connect

try:
    from baton import dashboard
except ImportError:  # written before the module: each test then fails on its own instead of the whole run stopping
    dashboard = None  # type: ignore[assignment]

LONG = "x" * 6000  # one unbroken word, far wider than any phone


def rules(css: str) -> dict[str, str]:
    """Each selector in the stylesheet with its declarations, joined, so a test can ask what a class gets."""
    found: dict[str, str] = {}
    for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        for selector in selectors.split(","):
            found[selector.strip()] = found.get(selector.strip(), "") + body
    return found


def links(body: str) -> list[str]:
    return [href.replace("&amp;", "&") for href in re.findall(r'href="([^"]+)"', body)]


# 1. Long words wrap ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("selector", [".text", ".relayed-text", ".meta", "h1", "li", "a"])
def test_long_unbroken_text_wraps_instead_of_widening_the_page(selector: str) -> None:
    css = rules(dashboard.STYLE)
    assert selector in css, selector
    assert "overflow-wrap: anywhere" in css[selector], selector


def test_the_page_never_scrolls_sideways_and_keeps_a_phone_viewport(tmp_path) -> None:
    css = rules(dashboard.STYLE)
    assert "overflow-x: hidden" not in css.get("body", "")  # wrapping, not clipping, keeps every word readable
    assert "min-width: 0" in css["li"]
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        conversation = made(conn, config, LONG, MINE, "alpha")
        conversations.send(conn, config, sender="orchestrator", conversation_id=conversation, text=LONG,
                           attention=["operator"])
    finally:
        conn.close()
    for target in ("/", "/t/alpha", "/needs", f"/c/{conversation}"):
        body = text(get(config, target))
        assert 'name="viewport" content="width=device-width, initial-scale=1"' in body, target


# 2. Every item past a cap can be reached ----------------------------------------------------------------------


def walk(config: Any, first: str, pattern: str, more_label: str) -> list[str]:
    """Follow the page's onward link until there is none, collecting what each page lists."""
    seen: list[str] = []
    target, guard = first, 0
    while target and guard < 20:
        body = text(get(config, target))
        seen += re.findall(pattern, body)
        onward = [href for href in links(body) if more_label in href]
        target = onward[0] if onward else None
        guard += 1
    return seen


def test_every_conversation_past_the_list_cap_can_be_reached(tmp_path) -> None:
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        names = [made(conn, config, f"Conversation {index:03d}", MINE, "alpha") and f"Conversation {index:03d}"
                 for index in range(dashboard.MAX_LISTED + 6)]
    finally:
        conn.close()
    for first in ("/", "/t/alpha"):
        seen = walk(config, first, r'<a href="/c/[^"]+">([^<]+)</a>', "page=")
        assert sorted(seen) == sorted(names), first  # every one, each once
        assert "showing the newest 200 of 206 conversations" in text(get(config, first))


def test_every_signal_past_the_needs_cap_can_be_reached(tmp_path) -> None:
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        mine = made(conn, config, "Many asks", MINE)
        for index in range(dashboard.MAX_NEEDS + 11):
            conversations.send(conn, config, sender="reviewer", conversation_id=mine, text=f"ask {index:03d}",
                               attention=["operator"])
    finally:
        conn.close()
    seen = walk(config, "/needs", r'<p class="text">([^<]*)</p>', "page=")
    assert sorted(seen) == [f"ask {index:03d}" for index in range(dashboard.MAX_NEEDS + 11)]
    assert "showing the newest 100 of 111" in text(get(config, "/needs"))


def test_every_job_past_the_jobs_cap_can_be_reached(tmp_path) -> None:
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        for index in range(dashboard.MAX_NEEDS_JOBS + 2):
            jobs.create(conn, config.jobs, request_id=f"20261029T120000Z-reach-{index}", actor="orchestrator",
                        title=f"Job {index:03d}")
    finally:
        conn.close()
    seen = walk(config, "/needs", r"(Job \d{3})", "jobs_page=")
    assert sorted(set(seen)) == [f"Job {index:03d}" for index in range(dashboard.MAX_NEEDS_JOBS + 2)]
    assert len(seen) == len(set(seen))
    assert "showing 50 of 52" in text(get(config, "/needs"))


def test_a_needs_page_keeps_its_topic_filter_on_the_onward_link(tmp_path, monkeypatch) -> None:
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        mine = made(conn, config, "Topic asks", MINE, "alpha")
        for index in range(5):
            conversations.send(conn, config, sender="reviewer", conversation_id=mine, text=f"alpha ask {index}",
                               attention=["operator"])
    finally:
        conn.close()
    monkeypatch.setattr(dashboard, "MAX_NEEDS", 2)
    onward = [href for href in links(text(get(config, "/needs?topic=alpha"))) if "page=" in href]
    assert onward and all("topic=alpha" in href for href in onward)


@pytest.mark.parametrize("query", ["page=0", "page=-1", "page=abc", "page=1e3", "page=99999999999999999999",
                                   "page=2&page=3", "jobs_page=0", "jobs_page=x", "jobs_page=1&jobs_page=2"])
def test_a_bad_page_number_is_a_bad_request_with_the_standard_headers(tmp_path, query: str) -> None:
    config = page_config(tmp_path)
    for path in ("/", "/needs"):
        if path == "/" and query.startswith("jobs_page"):
            continue
        response = get(config, f"{path}?{query}")
        assert response.status == 400, (path, query)
        assert {key: response.headers[key] for key in dashboard.HEADERS} == dashboard.HEADERS


def test_a_page_past_the_end_says_so_and_links_back(tmp_path) -> None:
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        made(conn, config, "Only one", MINE)
    finally:
        conn.close()
    body = text(get(config, "/?page=5"))
    assert get(config, "/?page=5").status == 200
    assert "Only one" not in body and 'href="/"' in body


# 3. Deep links keep their context ----------------------------------------------------------------------------


def threaded(config: Any) -> tuple[str, int, int]:
    """A conversation whose 30th message replies to its first and asks for the viewer."""
    conn = connect(config.db_path)
    try:
        conversation = made(conn, config, "Deep thread", MINE)
        parent = conversations.send(conn, config, sender="reviewer", conversation_id=conversation,
                                    text="the original question")["message_id"]
        for index in range(28):
            conversations.send(conn, config, sender="reviewer", conversation_id=conversation, text=f"chatter {index}")
        conversations.send(conn, config, sender="orchestrator", conversation_id=conversation,
                           text="replying to the original", reply_to=parent, attention=["operator"])
        history = conversations.history(conn, "operator", conversation)["messages"]
    finally:
        conn.close()
    return conversation, history[0]["seq"], history[-1]["seq"]


def test_a_signals_deep_link_lands_on_the_message_with_its_parent_in_view(tmp_path) -> None:
    config = page_config(tmp_path)
    conversation, parent_seq, reply_seq = threaded(config)
    needs = text(get(config, "/needs"))
    target = next(href for href in links(needs) if f"#m-{reply_seq}" in href)
    page = text(get(config, target.split("#")[0]))
    assert f'id="m-{reply_seq}"' in page
    assert "the original question" in page  # the parent's words are on the page the link opens
    assert f'href="/c/{conversation}?after_seq={parent_seq - 1}#m-{parent_seq}"' in page


def test_in_reply_to_is_a_working_link_to_the_parent(tmp_path) -> None:
    config = page_config(tmp_path)
    conversation, parent_seq, reply_seq = threaded(config)
    page = text(get(config, f"/c/{conversation}?after_seq={reply_seq - 1}"))
    marker = re.search(r'<p class="reply-to">(.*?)</p>', page).group(1)
    assert f'href="/c/{conversation}?after_seq={parent_seq - 1}#m-{parent_seq}"' in marker
    assert f"#{parent_seq}" in marker
    landing = text(get(config, f"/c/{conversation}?after_seq={parent_seq - 1}"))
    assert f'id="m-{parent_seq}"' in landing


def test_a_page_that_starts_mid_thread_links_to_earlier_messages(tmp_path) -> None:
    config = page_config(tmp_path)
    conversation, _, reply_seq = threaded(config)
    page = text(get(config, f"/c/{conversation}?after_seq={reply_seq - 1}"))
    earlier = [href for href in links(page) if href.startswith(f"/c/{conversation}?after_seq=") and "#" not in href]
    assert earlier, "a page that starts mid-thread offers a way back"
    assert "Earlier messages" in page


def test_a_parent_the_viewer_cannot_see_stays_unavailable_and_shows_nothing_of_it(tmp_path) -> None:
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        late = made(conn, config, "Joined later", ["orchestrator", "reviewer"])
        hidden = conversations.send(conn, config, sender="reviewer", conversation_id=late,
                                    text="words from before the join")["message_id"]
        conversations.add(conn, config, late, "operator", actor="orchestrator")
        conversations.send(conn, config, sender="orchestrator", conversation_id=late, text="a reply after the join",
                           reply_to=hidden, attention=["operator"])
    finally:
        conn.close()
    needs = text(get(config, "/needs"))
    target = next(href for href in links(needs) if "#m-" in href and late in href)
    page = text(get(config, target.split("#")[0]))
    assert "a reply after the join" in page
    assert "words from before the join" not in page
    assert "parent unavailable" in page
    assert "a reply after the join" in needs_items(needs)[0]


# 4. Needs you: the viewer's own items by default, others' decisions collapsed, "you replied" literal ---------


def mixed(config: Any) -> dict[str, str]:
    conn = connect(config.db_path)
    try:
        mine = made(conn, config, "Mixed asks", MINE)
        ids = {"mine": mine}

        def say(sender: str, words: str, **kwargs: Any) -> str:
            return conversations.send(conn, config, sender=sender, conversation_id=mine, text=words,
                                      **kwargs)["message_id"]

        ids["for_me"] = say("orchestrator", "operator, please look", attention=["operator"])
        ids["decide_for_me"] = say("reviewer", "operator, A or B?", attention=["operator"], needs_decision=True)
        ids["decide_for_other"] = say("orchestrator", "reviewer, A or B?", attention=["reviewer"],
                                      needs_decision=True)
        ids["decide_for_anyone"] = say("reviewer", "anyone, A or B?", needs_decision=True)
        say("operator", "on it", reply_to=ids["for_me"])
    finally:
        conn.close()
    return ids


def section(body: str, marker: str) -> str:
    """The part of the page from a section's opening marker to the next heading or the end."""
    start = body.index(marker)
    rest = body[start + len(marker):]
    end = min([i for i in (rest.find("<h2"), rest.find("<details")) if i != -1] or [len(rest)])
    return rest[:end]


def test_by_default_needs_lists_only_what_names_the_viewer(tmp_path) -> None:
    config = page_config(tmp_path)
    mixed(config)
    body = text(get(config, "/needs"))
    mine = section(body, "<h2>For you</h2>")
    assert needs_items(mine) == ["operator, A or B?", "operator, please look"]
    assert "2 messages asked for you" in body


def test_decisions_not_addressed_to_the_viewer_sit_in_a_collapsed_labelled_section(tmp_path) -> None:
    config = page_config(tmp_path)
    mixed(config)
    body = text(get(config, "/needs"))
    match = re.search(r"<details>\s*<summary>([^<]*)</summary>(.*?)</details>", body, re.S)
    assert match, "a collapsed section, closed by default"
    assert "open" not in body[body.index("<details"):body.index("<details") + 12]
    assert "Decisions not addressed to you (2)" in match.group(1)
    others = match.group(2)
    assert sorted(needs_items(others)) == ["anyone, A or B?", "reviewer, A or B?"]
    assert "asked of reviewer" in others and "asked of the whole conversation" in others


def test_you_replied_is_shown_literally_and_the_item_stays_inspectable(tmp_path) -> None:
    config = page_config(tmp_path)
    mixed(config)
    body = text(get(config, "/needs")).lower()
    item = next(chunk for chunk in body.split("<li>") if "operator, please look" in chunk)
    assert "you replied" in item
    for word in ("answered", "resolved", "unresolved", "done"):
        assert word not in body, word
    other = next(chunk for chunk in body.split("<li>") if "operator, a or b?" in chunk)
    assert "you replied" not in other


def test_you_replied_comes_only_from_the_viewers_own_reply_links(tmp_path) -> None:
    config = page_config(tmp_path)
    ids = mixed(config)
    conn = connect(config.db_path)
    try:
        conversations.send(conn, config, sender="reviewer", conversation_id=ids["mine"], text="reviewer replies",
                           reply_to=ids["decide_for_me"])
    finally:
        conn.close()
    body = text(get(config, "/needs")).lower()
    item = next(chunk for chunk in body.split("<li>") if "operator, a or b?" in chunk)
    assert "you replied" not in item  # someone else's reply proves nothing about the viewer


# 5. Mobile density ------------------------------------------------------------------------------------------


def test_times_are_compact_with_the_full_value_kept_in_a_title(tmp_path) -> None:
    config = page_config(tmp_path)
    mixed(config)
    for target in ("/", "/needs"):
        body = text(get(config, target))
        stamps = re.findall(r'<time datetime="([^"]+)" title="([^"]+)">([^<]+)</time>', body)
        assert stamps, target
        for machine, full, shown in stamps:
            assert machine == full and "T" in full, (target, full)
            assert len(shown) <= 12 and "T" not in shown and "+00:00" not in shown, (target, shown)


def test_job_ids_are_shortened_on_narrow_screens_with_the_full_id_kept(tmp_path) -> None:
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        job = jobs.create(conn, config.jobs, request_id="20261029T120000Z-dense", actor="orchestrator",
                          title="Dense job")
    finally:
        conn.close()
    body = text(get(config, "/needs"))
    assert f'<span class="id" title="{job["job_id"]}">{job["job_id"]}</span>' in body
    css = rules(dashboard.STYLE)
    narrow = re.search(r"@media \(max-width: 480px\) \{(.*?)\}\s*\}", dashboard.STYLE, re.S)
    assert narrow and ".id" in narrow.group(1) and "text-overflow: ellipsis" in narrow.group(1)
    assert ".id" in css
