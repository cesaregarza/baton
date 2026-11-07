"""Dashboard pagination, deep-link context, and generated mobile viewport declarations.

Synthetic and offline, with generic roles.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from test_dashboard import text
from test_dashboard_pages import MINE, get, made, needs_items, page_config

from baton import conversations, dashboard, jobs
from baton.database.store import connect


def links(body: str) -> list[str]:
    return [href.replace("&amp;", "&") for href in re.findall(r'href="([^"]+)"', body)]


def test_dashboard_pages_declare_a_mobile_viewport(tmp_path) -> None:
    """The generated HTML opts into the device-width viewport on each main route."""
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        conversation = made(conn, config, "y" * 190, MINE, "alpha")
    finally:
        conn.close()
    for target in ("/", "/t/alpha", "/needs", f"/c/{conversation}"):
        body = text(get(config, target))
        assert 'name="viewport" content="width=device-width, initial-scale=1"' in body, target


# 2. Every item past a cap can be reached ----------------------------------------------------------------------


def walk(config: Any, first: str, pattern: str, more_label: str) -> tuple[list[str], str]:
    """Follow the page's onward link until there is none, collecting what each page lists."""
    seen: list[str] = []
    first_body = ""
    target, guard = first, 0
    while target and guard < 20:
        body = text(get(config, target))
        if not first_body:
            first_body = body
        seen += re.findall(pattern, body)
        onward = [href for href in links(body) if more_label in href]
        target = onward[0] if onward else None
        guard += 1
    return seen, first_body


def test_conversations_past_the_first_page_are_reachable_once_on_both_lists(tmp_path, monkeypatch) -> None:
    config = page_config(tmp_path)
    monkeypatch.setattr(dashboard, "MAX_LISTED", 2)
    conn = connect(config.db_path)
    try:
        names = [
            made(conn, config, f"Conversation {index:03d}", MINE, "alpha") and f"Conversation {index:03d}"
            for index in range(5)
        ]
    finally:
        conn.close()
    for first in ("/", "/t/alpha"):
        seen, first_body = walk(config, first, r'<a href="/c/[^"]+">([^<]+)</a>', "page=")
        assert sorted(seen) == names and len(seen) == len(set(seen)), first
        assert len(re.findall(r'<a href="/c/[^"]+">([^<]+)</a>', first_body)) == 2, first
        assert "showing the newest 2 of 5 conversations" in first_body


def test_signals_past_the_first_page_are_reachable_once_and_counted(tmp_path, monkeypatch) -> None:
    config = page_config(tmp_path)
    monkeypatch.setattr(dashboard, "MAX_NEEDS", 2)
    conn = connect(config.db_path)
    try:
        mine = made(conn, config, "Many asks", MINE)
        for index in range(4):
            conversations.send(
                conn, config, sender="reviewer", conversation_id=mine, text=f"ask {index:03d}", attention=["operator"]
            )
            if index < 2:
                conversations.send(conn, config, sender="reviewer", conversation_id=mine, text=f"noise {index:03d}")
    finally:
        conn.close()
    seen, first = walk(config, "/needs", r'<p class="text">([^<]*)</p>', "page=")
    assert sorted(seen) == [f"ask {index:03d}" for index in range(4)]
    assert len(seen) == len(set(seen))
    assert needs_items(first) == ["ask 003", "ask 002"]
    assert "4 messages asked for you" in first and "showing the newest 2 of 4" in first


def test_jobs_past_the_first_page_are_reachable_once_and_counted(tmp_path, monkeypatch) -> None:
    config = page_config(tmp_path)
    monkeypatch.setattr(dashboard, "MAX_NEEDS_JOBS", 2)
    conn = connect(config.db_path)
    try:
        for index in range(4):
            jobs.create(
                conn,
                config.jobs,
                request_id=f"20261029T120000Z-reach-{index}",
                actor="orchestrator",
                title=f"Job {index:03d}",
            )
        other = jobs.create(
            conn,
            config.jobs,
            request_id="20261029T120000Z-reach-other",
            actor="orchestrator",
            title="Reviewer job",
        )
        jobs.move(conn, config.jobs, other["job_id"], "working", actor="operator")
    finally:
        conn.close()
    seen, first = walk(config, "/needs", r"(Job \d{3})", "jobs_page=")
    assert sorted(seen) == [f"Job {index:03d}" for index in range(4)]
    assert len(seen) == len(set(seen))
    assert len(re.findall(r"Job \d{3}", first)) == 2
    assert "4 jobs are waiting on you" in first and "showing 2 of 4" in first
    assert "Reviewer job" not in first


def test_a_needs_page_keeps_its_topic_filter_on_the_onward_link(tmp_path, monkeypatch) -> None:
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        mine = made(conn, config, "Topic asks", MINE, "alpha")
        for index in range(3):
            conversations.send(
                conn, config, sender="reviewer", conversation_id=mine, text=f"alpha ask {index}", attention=["operator"]
            )
    finally:
        conn.close()
    monkeypatch.setattr(dashboard, "MAX_NEEDS", 1)
    onward = [href for href in links(text(get(config, "/needs?topic=alpha"))) if "page=" in href]
    assert onward and all("topic=alpha" in href for href in onward)


@pytest.mark.parametrize(
    "query",
    [
        "page=0",
        "page=-1",
        "page=abc",
        "page=1e3",
        "page=99999999999999999999",
        "page=2&page=3",
        "jobs_page=0",
        "jobs_page=x",
        "jobs_page=1&jobs_page=2",
    ],
)
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
        parent = conversations.send(
            conn, config, sender="reviewer", conversation_id=conversation, text="the original question"
        )["message_id"]
        for index in range(28):
            conversations.send(conn, config, sender="reviewer", conversation_id=conversation, text=f"chatter {index}")
        conversations.send(
            conn,
            config,
            sender="orchestrator",
            conversation_id=conversation,
            text="replying to the original",
            reply_to=parent,
            attention=["operator"],
        )
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
        hidden = conversations.send(
            conn, config, sender="reviewer", conversation_id=late, text="words from before the join"
        )["message_id"]
        conversations.add(conn, config, late, "operator", actor="orchestrator")
        conversations.send(
            conn,
            config,
            sender="orchestrator",
            conversation_id=late,
            text="a reply after the join",
            reply_to=hidden,
            attention=["operator"],
        )
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
            return conversations.send(conn, config, sender=sender, conversation_id=mine, text=words, **kwargs)[
                "message_id"
            ]

        ids["for_me"] = say("orchestrator", "operator, please look", attention=["operator"])
        ids["decide_for_me"] = say("reviewer", "operator, A or B?", attention=["operator"], needs_decision=True)
        ids["decide_for_other"] = say("orchestrator", "reviewer, A or B?", attention=["reviewer"], needs_decision=True)
        ids["decide_for_anyone"] = say("reviewer", "anyone, A or B?", needs_decision=True)
        say("operator", "on it", reply_to=ids["for_me"])
    finally:
        conn.close()
    return ids


def section(body: str, marker: str) -> str:
    """The part of the page from a section's opening marker to the next heading or the end."""
    start = body.index(marker)
    rest = body[start + len(marker) :]
    end = min([i for i in (rest.find("<h2"), rest.find("<details")) if i != -1] or [len(rest)])
    return rest[:end]


def test_by_default_needs_lists_only_what_names_the_viewer(tmp_path) -> None:
    config = page_config(tmp_path)
    mixed(config)
    body = text(get(config, "/needs"))
    mine = section(body, '<h2 id="for-you">For you</h2>')
    assert needs_items(mine) == ["operator, A or B?", "operator, please look"]
    assert "2 messages asked for you" in body


def test_decisions_not_addressed_to_the_viewer_sit_in_a_collapsed_labelled_section(tmp_path) -> None:
    config = page_config(tmp_path)
    mixed(config)
    body = text(get(config, "/needs"))
    match = re.search(r"<details[^>]*>\s*<summary>([^<]*)</summary>(.*?)</details>", body, re.S)
    assert match, "a collapsed section, closed by default"
    opening = body[body.index("<details") : body.index(">", body.index("<details")) + 1]
    assert "open" not in opening, opening
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
        conversations.send(
            conn,
            config,
            sender="reviewer",
            conversation_id=ids["mine"],
            text="reviewer replies",
            reply_to=ids["decide_for_me"],
        )
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


def test_job_id_is_available_in_full_in_the_html(tmp_path) -> None:
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        job = jobs.create(
            conn, config.jobs, request_id="20261029T120000Z-dense", actor="orchestrator", title="Dense job"
        )
    finally:
        conn.close()
    body = text(get(config, "/needs"))
    assert f'<span class="id" title="{job["job_id"]}">{job["job_id"]}</span>' in body


def test_advancing_one_needs_list_keeps_the_others_where_they_are(tmp_path, monkeypatch) -> None:
    config = page_config(tmp_path)
    three_lists(config)
    for name in ("MAX_NEEDS", "MAX_NEEDS_JOBS"):
        monkeypatch.setattr(dashboard, name, 1)
    body = text(get(config, "/needs?page=2&others_page=2&jobs_page=2"))
    hrefs = links(body)
    names = {"page": "#for-you", "others_page": "#decisions", "jobs_page": "#jobs"}
    for name, anchor in names.items():
        onward = [href for href in hrefs if re.search(rf"(\?|&){name}=3(&|#|$)", href)]
        assert onward and all(href.endswith(anchor) for href in onward), (name, onward)
        for href in onward:
            for other in set(names) - {name}:
                assert re.search(rf"(\?|&){other}=2(&|#|$)", href), (name, href)
    for anchor in ('id="for-you"', 'id="decisions"', 'id="jobs"'):
        assert anchor in body, anchor
    top = body[: body.index('id="for-you"')]
    assert 'href="#for-you"' in top and 'href="#jobs"' in top
    filtered = links(text(get(config, "/needs?topic=alpha&page=2&others_page=2")))
    assert all("topic=alpha" in href for href in filtered if "page=" in href)


def three_lists(config: Any) -> None:
    """Three of each list item, enough to test another page with a one-item cap."""
    conn = connect(config.db_path)
    try:
        mine = made(conn, config, "Navigation", MINE, "alpha")
        for index in range(3):
            conversations.send(
                conn, config, sender="reviewer", conversation_id=mine, text=f"for you {index}", attention=["operator"]
            )
            conversations.send(
                conn,
                config,
                sender="orchestrator",
                conversation_id=mine,
                text=f"other {index}",
                attention=["reviewer"],
                needs_decision=True,
            )
            jobs.create(
                conn,
                config.jobs,
                request_id=f"20261029T120000Z-nav-{index}",
                actor="orchestrator",
                title=f"Nav job {index}",
            )
    finally:
        conn.close()


def details_tag(body: str) -> str:
    start = body.index("<details")
    return body[start : body.index(">", start) + 1]


def test_paging_the_collapsed_decisions_reloads_with_them_open(tmp_path, monkeypatch) -> None:
    config = page_config(tmp_path)
    three_lists(config)
    monkeypatch.setattr(dashboard, "MAX_NEEDS", 1)
    first = text(get(config, "/needs"))
    assert "open" not in details_tag(first)
    older = next(href for href in links(first) if "others_page=2" in href)
    reloaded = text(get(config, older.split("#")[0]))
    assert "open" in details_tag(reloaded)  # the section the reader asked for stays open
    back = [href for href in links(reloaded) if href.endswith("#decisions") and "others_page" not in href]
    assert back, "a way back to the first page of decisions, landing on them"
