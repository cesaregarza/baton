"""The dashboard's topic pages, its "Needs you" page and the opt-in proxy host.

Everything here is synthetic: generic roles, a temporary data directory and example host names. Each page
shows only what the configured viewer role may already see, and viewing writes nothing.
"""

from __future__ import annotations

import http.client
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import free_port
from test_dashboard import PAYLOAD, snapshot, text

from baton import config as config_module
from baton import conversations, jobs, messages
from baton.config import Config, ConfigError
from baton.store import connect

try:
    from baton import dashboard
except ImportError:  # written before the module: each test then fails on its own instead of the whole run stopping
    dashboard = None  # type: ignore[assignment]

MINE = ["orchestrator", "reviewer", "operator"]
THEIRS = ["orchestrator", "assistant"]
PROXY = "dash.example.test"
JOB_STATES = {"initial": "open", "states": {
    "open": {"waiting_on": "operator", "next": ["working"]},
    "working": {"waiting_on": "reviewer", "next": ["done"]},
    "done": {}}}


def page_config(tmp_path: Path, *, view_all: bool = False, port: int | None = None, **dashboard_keys: Any) -> Config:
    """Generic roles; `operator` is the viewer and sees every conversation only with the flag."""
    return config_module.parse({
        "host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")},
        "roles": {"orchestrator": {"can_create_conversations": True, "can_manage_participants": True},
                  "reviewer": {}, "assistant": {}, "operator": {"can_view_all_conversations": view_all}},
        "conversations": {"enabled": True},
        "jobs": JOB_STATES,
        "dashboard": {"enabled": True, "viewer_role": "operator", "listen_port": port or 8766, **dashboard_keys},
    })


def get(config: Config, target: str, *, method: str = "GET", host: str | None = None) -> Any:
    own = f"127.0.0.1:{config.dashboard.listen_port}"
    return dashboard.respond(config, method, target, own if host is None else host)


def made(conn: Any, config: Config, title: str, participants: list[str], topic: str | None = None) -> str:
    return conversations.create(conn, config, actor="orchestrator", title=title, participants=participants,
                                topic=topic)["conversation_id"]


def topics_seeded(config: Config) -> dict[str, str]:
    """Conversations under alpha, alpha/x, alphabet and no topic, plus one under alpha the viewer isn't in."""
    conn = connect(config.db_path)
    try:
        return {"alpha": made(conn, config, "Alpha root", MINE, "alpha"),
                "alpha/x": made(conn, config, "Alpha x", MINE, "alpha/x"),
                "alphabet": made(conn, config, "Alphabet", MINE, "alphabet"),
                "untagged": made(conn, config, "Untagged", MINE),
                "hidden": made(conn, config, "Hidden alpha", THEIRS, "alpha/hidden")}
    finally:
        conn.close()


def titles(body: str) -> list[str]:
    return re.findall(r'<a href="/c/[^"]+">([^<]+)</a>', body)


# 1. Topic pages ----------------------------------------------------------------------------------------------


def test_a_topic_page_matches_whole_segments_only(tmp_path) -> None:
    config = page_config(tmp_path)
    topics_seeded(config)
    alpha = get(config, "/t/alpha")
    assert alpha.status == 200
    assert sorted(titles(text(alpha))) == ["Alpha root", "Alpha x"]  # never alphabet, never the untagged
    sub = get(config, "/t/alpha/x")
    assert sub.status == 200 and titles(text(sub)) == ["Alpha x"]
    assert "alpha › x" in text(sub)  # the breadcrumb
    assert titles(text(get(config, "/t/alphabet"))) == ["Alphabet"]


def test_a_topic_page_never_widens_what_the_viewer_sees(tmp_path) -> None:
    config = page_config(tmp_path)
    topics_seeded(config)
    assert "Hidden alpha" not in text(get(config, "/t/alpha"))
    assert "Hidden alpha" not in text(get(config, "/t/alpha/hidden"))
    flagged = page_config(tmp_path, view_all=True)
    assert "Hidden alpha" in text(get(flagged, "/t/alpha"))


@pytest.mark.parametrize("target", ["/t/Alpha", "/t/alpha/x/y", "/t/", "/t/alpha/", "/t/al%20pha", "/t/-alpha",
                                    "/t/alpha//x", "/t/" + "a" * 200, "/t/alpha%2Fx", "/t/alpha%252Fx",
                                    "/t/alpha%00", "/t/%E2%80%AEalpha"])
def test_an_invalid_topic_is_a_bad_request_with_the_standard_headers(tmp_path, target: str) -> None:
    config = page_config(tmp_path)
    topics_seeded(config)
    response = get(config, target)
    assert response.status == 400, target
    assert {key: response.headers[key] for key in dashboard.HEADERS} == dashboard.HEADERS


def test_a_topic_route_is_decoded_once_per_segment(tmp_path) -> None:
    """Each path segment is percent-decoded exactly once, so an encoded letter is that letter, while an encoded
    slash stays inside its segment and makes the topic invalid instead of acting as a separator."""
    config = page_config(tmp_path)
    topics_seeded(config)
    assert titles(text(get(config, "/t/%61lpha"))) == titles(text(get(config, "/t/alpha")))
    assert titles(text(get(config, "/t/alpha/%78"))) == ["Alpha x"]
    assert get(config, "/t/alpha%2Fx").status == 400


def test_the_index_keeps_the_untagged_and_links_to_topics_and_needs(tmp_path) -> None:
    config = page_config(tmp_path)
    topics_seeded(config)
    body = text(get(config, "/"))
    assert "Untagged" in body
    for link in ('href="/needs"', 'href="/t/alpha"', 'href="/t/alpha/x"', 'href="/t/alphabet"'):
        assert link in body, link
    assert 'href="/t/alpha/hidden"' not in body  # a topic only the hidden conversation has isn't offered


# 2. Needs you: messages -------------------------------------------------------------------------------------


def needs_seeded(config: Config) -> dict[str, str]:
    """Messages that do and don't ask for the viewer, in a conversation it is in and one it isn't."""
    conn = connect(config.db_path)
    try:
        mine = made(conn, config, "Release plan", MINE, "alpha/x")
        other_topic = made(conn, config, "Other work", MINE, "beta")
        theirs = made(conn, config, "Private side thread", THEIRS, "alpha")
        ids = {"mine": mine, "other_topic": other_topic, "theirs": theirs}

        def say(sender: str, conversation: str, words: str, **kwargs: Any) -> str:
            return conversations.send(conn, config, sender=sender, conversation_id=conversation, text=words,
                                      **kwargs)["message_id"]

        ids["plain"] = say("orchestrator", mine, "plain progress note")
        ids["attention"] = say("orchestrator", mine, "operator, please look", attention=["operator"])
        ids["other_attention"] = say("orchestrator", mine, "reviewer, please look", attention=["reviewer"])
        ids["at_text"] = say("reviewer", mine, "@operator this only says it in words")
        ids["decision"] = say("reviewer", mine, "pick option A or B", needs_decision=True)
        ids["beta_attention"] = say("orchestrator", other_topic, "beta needs operator", attention=["operator"])
        ids["invisible"] = say("assistant", theirs, "hidden decision", needs_decision=True)
    finally:
        conn.close()
    return ids


def needs_items(body: str) -> list[str]:
    return re.findall(r'<p class="text">([^<]*)</p>', body)


def test_needs_lists_attention_to_the_viewer_and_decisions_newest_first(tmp_path) -> None:
    config = page_config(tmp_path)
    needs_seeded(config)
    response = get(config, "/needs")
    assert response.status == 200
    assert needs_items(text(response)) == ["beta needs operator", "pick option A or B", "operator, please look"]


def test_needs_excludes_unflagged_messages_other_roles_attention_and_at_mentions_in_text(tmp_path) -> None:
    config = page_config(tmp_path)
    needs_seeded(config)
    body = text(get(config, "/needs"))
    for words in ("plain progress note", "reviewer, please look", "@operator this only says it in words"):
        assert words not in body, words


def test_needs_never_shows_an_invisible_conversation_until_the_flag_is_set(tmp_path) -> None:
    config = page_config(tmp_path)
    needs_seeded(config)
    assert "hidden decision" not in text(get(config, "/needs"))
    assert "Private side thread" not in text(get(config, "/needs"))
    flagged = page_config(tmp_path, view_all=True)
    assert "hidden decision" in text(get(flagged, "/needs"))


def test_needs_links_each_message_to_its_place_and_counts_both_blocks(tmp_path) -> None:
    config = page_config(tmp_path)
    ids = needs_seeded(config)
    conn = connect(config.db_path)
    try:
        jobs.create(conn, config.jobs, request_id="20261029T120000Z-job-one", actor="orchestrator", title="Sign off")
        history = conversations.history(conn, "operator", ids["mine"])["messages"]
    finally:
        conn.close()
    decision = next(m for m in history if m["text"] == "pick option A or B")
    body = text(get(config, "/needs"))
    assert f'/c/{ids["mine"]}?after_seq={decision["seq"] - 1}#m-{decision["seq"]}' in body
    assert "3 messages asked for you or for a decision" in body and "1 job is waiting on you" in body
    target = re.search(rf'href="(/c/{ids["mine"]}\?after_seq={decision["seq"] - 1})#m-{decision["seq"]}"', body)
    assert f'id="m-{decision["seq"]}"' in text(get(config, target.group(1).replace("&amp;", "&")))


def test_needs_filters_by_topic_and_refuses_an_invalid_one(tmp_path) -> None:
    config = page_config(tmp_path, view_all=True)
    needs_seeded(config)
    alpha = needs_items(text(get(config, "/needs?topic=alpha")))
    assert alpha == ["hidden decision", "pick option A or B", "operator, please look"]
    assert needs_items(text(get(config, "/needs?topic=beta"))) == ["beta needs operator"]
    assert needs_items(text(get(config, "/needs?topic=alpha/x"))) == ["pick option A or B", "operator, please look"]
    assert needs_items(text(get(config, "/needs?topic=alpha%2Fx"))) == needs_items(text(get(config,
                                                                                         "/needs?topic=alpha/x")))
    for bad in ("Alpha", "alpha/x/y", "", "al%20pha", "alpha%252Fx", "a" * 5000, "alpha&topic=beta",
                "alpha&topic=alpha"):
        response = get(config, f"/needs?topic={bad}")
        assert response.status == 400, bad
        assert {key: response.headers[key] for key in dashboard.HEADERS} == dashboard.HEADERS


def test_needs_says_so_when_nothing_needs_the_viewer(tmp_path) -> None:
    config = page_config(tmp_path)
    body = text(get(config, "/needs"))
    assert "0 messages asked for you or for a decision" in body and "0 jobs are waiting on you" in body


def test_needs_labels_messages_as_retained_signals_not_unresolved_or_unread(tmp_path) -> None:
    config = page_config(tmp_path)
    needs_seeded(config)
    body = text(get(config, "/needs")).lower()
    assert "retained" in body
    for word in ("unresolved", "unread", "resolved", "pending"):
        assert word not in body, word


def test_a_message_the_viewer_sent_itself_is_not_listed(tmp_path) -> None:
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        mine = made(conn, config, "Own question", MINE)
        conversations.send(conn, config, sender="operator", conversation_id=mine, text="my own decision request",
                           needs_decision=True)
        conversations.send(conn, config, sender="reviewer", conversation_id=mine, text="a decision for anyone",
                           needs_decision=True)
    finally:
        conn.close()
    assert needs_items(text(get(config, "/needs"))) == ["a decision for anyone"]


def test_needs_follows_what_the_viewer_could_read_from_its_join_point(tmp_path) -> None:
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        late = made(conn, config, "Joined late", ["orchestrator", "reviewer"])
        conversations.send(conn, config, sender="reviewer", conversation_id=late, text="before the join",
                           needs_decision=True)
        conversations.add(conn, config, late, "operator", actor="orchestrator")
        conversations.send(conn, config, sender="reviewer", conversation_id=late, text="after the join",
                           needs_decision=True)
    finally:
        conn.close()
    assert needs_items(text(get(config, "/needs"))) == ["after the join"]
    flagged = page_config(tmp_path, view_all=True)  # full_history: the explicit every-conversation branch
    assert needs_items(text(get(flagged, "/needs"))) == ["after the join", "before the join"]


def test_needs_drops_a_message_once_retention_deletes_the_viewers_copy(tmp_path) -> None:
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        mine = made(conn, config, "Old asks", MINE)
        conversations.send(conn, config, sender="reviewer", conversation_id=mine, text="old ask", needs_decision=True)
        conversations.send(conn, config, sender="reviewer", conversation_id=mine, text="new ask", needs_decision=True)
        messages.read(conn, "operator", limit=1)
        stale = (datetime.now(UTC) - messages.READ_RETENTION - timedelta(days=1)).isoformat()
        conn.execute("UPDATE messages SET at = ? WHERE recipient = 'operator' AND seq = 1", (stale,))
        messages.sweep(conn)
    finally:
        conn.close()
    assert needs_items(text(get(config, "/needs"))) == ["new ask"]


@pytest.mark.parametrize("view_all", [False, True])
def test_a_fanned_out_message_is_listed_once(tmp_path, view_all: bool) -> None:
    config = page_config(tmp_path, view_all=view_all)
    conn = connect(config.db_path)
    try:
        wide = made(conn, config, "Wide", ["orchestrator", "reviewer", "assistant", "operator"])
        conversations.send(conn, config, sender="orchestrator", conversation_id=wide, text="to all four",
                           attention=["operator", "reviewer"], needs_decision=True)
    finally:
        conn.close()
    assert needs_items(text(get(config, "/needs"))) == ["to all four"]


def test_the_total_is_counted_before_the_page_bound_and_shown_apart_from_what_is_displayed(tmp_path,
                                                                                            monkeypatch) -> None:
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        mine = made(conn, config, "Many asks", MINE)
        for index in range(7):
            conversations.send(conn, config, sender="reviewer", conversation_id=mine, text=f"ask {index}",
                               needs_decision=True)
            conversations.send(conn, config, sender="reviewer", conversation_id=mine, text=f"noise {index}")
    finally:
        conn.close()
    monkeypatch.setattr(dashboard, "MAX_NEEDS", 3)
    body = text(get(config, "/needs"))
    assert needs_items(body) == ["ask 6", "ask 5", "ask 4"]
    assert "7 messages asked for you or for a decision" in body and "showing the newest 3" in body


# 3. Needs you: jobs -----------------------------------------------------------------------------------------


def test_needs_lists_exactly_the_jobs_waiting_on_the_viewer_and_moves_none(tmp_path) -> None:
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        waiting = jobs.create(conn, config.jobs, request_id="20261029T120000Z-job-a", actor="orchestrator",
                              title="Approve the plan")
        moved = jobs.create(conn, config.jobs, request_id="20261029T120000Z-job-b", actor="orchestrator",
                            title="Already with the reviewer")
        jobs.move(conn, config.jobs, moved["job_id"], "working", actor="operator")
    finally:
        conn.close()
    before = snapshot(config)
    body = text(get(config, "/needs"))
    assert "Approve the plan" in body and "Already with the reviewer" not in body
    assert waiting["job_id"] in body
    assert snapshot(config) == before


def test_jobs_have_no_topic_so_a_topic_filter_shows_none_and_says_so(tmp_path) -> None:
    config = page_config(tmp_path)
    topics_seeded(config)
    conn = connect(config.db_path)
    try:
        jobs.create(conn, config.jobs, request_id="20261029T120000Z-job-t", actor="orchestrator",
                    title="alpha is in this job's title")
    finally:
        conn.close()
    assert "alpha is in this job" in text(get(config, "/needs"))
    filtered = text(get(config, "/needs?topic=alpha"))
    assert "alpha is in this job" not in filtered  # never inferred from text
    assert "Jobs have no topic" in filtered


# 4. Viewing stays neutral -----------------------------------------------------------------------------------


def test_visiting_every_new_page_writes_nothing(tmp_path) -> None:
    config = page_config(tmp_path, view_all=True)
    topics_seeded(config)
    needs_seeded(config)
    before = snapshot(config)
    for target in ("/", "/t/alpha", "/t/alpha/x", "/t/beta", "/needs", "/needs?topic=alpha", "/needs?topic=beta"):
        for method in ("GET", "HEAD"):
            assert get(config, target, method=method).status == 200, (method, target)
    assert snapshot(config) == before


# 5. Safety on every new route -------------------------------------------------------------------------------

NEW_ROUTES = ("/t/alpha", "/t/alpha/x", "/needs", "/needs?topic=alpha")


@pytest.mark.parametrize("target", NEW_ROUTES)
def test_every_new_route_has_the_headers_refuses_foreign_hosts_and_writes(tmp_path, target: str) -> None:
    config = page_config(tmp_path)
    topics_seeded(config)
    response = get(config, target)
    assert response.status == 200
    assert {key: response.headers[key] for key in dashboard.HEADERS} == dashboard.HEADERS
    assert not any(key.lower().startswith("access-control-") for key in response.headers)
    assert get(config, target, host="attacker.example.test").status == 421
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        refused = get(config, target, method=method)
        assert refused.status == 405 and refused.headers["Allow"] == "GET, HEAD", method


def test_titles_and_text_on_the_new_pages_render_inert(tmp_path) -> None:
    config = page_config(tmp_path)
    conn = connect(config.db_path)
    try:
        conversation = made(conn, config, PAYLOAD, MINE, "alpha")
        conversations.send(conn, config, sender="orchestrator", conversation_id=conversation, text=PAYLOAD,
                           attention=["operator"])
        jobs.create(conn, config.jobs, request_id="20261029T120000Z-job-x", actor="orchestrator", title=PAYLOAD)
    finally:
        conn.close()
    for target in ("/t/alpha", "/needs", "/needs?topic=alpha"):
        body = text(get(config, target))
        assert "<script" not in body and "<img" not in body, target
        assert "&lt;script&gt;" in body, target


# 6. The opt-in proxy host -----------------------------------------------------------------------------------


def test_without_proxy_host_a_proxys_host_name_is_refused(tmp_path) -> None:
    config = page_config(tmp_path)
    assert config.dashboard.proxy_host is None
    assert get(config, "/", host=PROXY).status == 421
    assert get(config, "/", host=f"{PROXY}:443").status == 421


def test_proxy_host_accepts_exactly_that_host_and_nothing_like_it(tmp_path) -> None:
    config = page_config(tmp_path, proxy_host=PROXY)
    assert config.dashboard.proxy_host == PROXY
    assert get(config, "/", host=PROXY).status == 200
    assert get(config, "/needs", host=PROXY).status == 200
    assert get(config, "/").status == 200  # its own loopback address still works
    for lookalike in (f"{PROXY}.attacker.test", f"evil-{PROXY}", f"x{PROXY}", f"sub.{PROXY}", "example.test",
                      f"{PROXY}:443", f"{PROXY}:8443", f"{PROXY}.", "DASH.EXAMPLE.TEST", f" {PROXY}", "*"):
        assert get(config, "/", host=lookalike).status == 421, lookalike


def test_a_proxy_host_with_a_port_accepts_only_that_port(tmp_path) -> None:
    config = page_config(tmp_path, proxy_host=f"{PROXY}:7443")
    assert get(config, "/", host=f"{PROXY}:7443").status == 200
    for other in (PROXY, f"{PROXY}:443", f"{PROXY}:74430", f"{PROXY}:744"):
        assert get(config, "/", host=other).status == 421, other


@pytest.mark.parametrize("value", ["", "*.example.test", "*", "https://dash.example.test", "dash.example.test/",
                                   "dash.example.test/path", "dash.example.test:0", "dash.example.test:65536",
                                   "dash.example.test:http", "dash..example.test", "-dash.example.test",
                                   "dash.example.test ", "Dash.Example.Test", "dash.example.test#x",
                                   "[::1]:7443", 7443, True, ["dash.example.test"]])
def test_config_refuses_a_malformed_or_wildcard_proxy_host(tmp_path, value: Any) -> None:
    with pytest.raises(ConfigError, match="dashboard.proxy_host"):
        page_config(tmp_path, proxy_host=value)


def test_x_forwarded_host_is_never_trusted(tmp_path) -> None:
    config = page_config(tmp_path, port=free_port(), proxy_host=PROXY)
    server = dashboard.server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def status(headers: dict[str, str]) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", config.dashboard.listen_port, timeout=5)
        try:
            conn.putrequest("GET", "/", skip_host=True)
            for name, value in headers.items():
                conn.putheader(name, value)
            conn.endheaders()
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()

    try:
        assert status({"Host": "attacker.example.test", "X-Forwarded-Host": PROXY}) == 421
        assert status({"Host": "attacker.example.test", "X-Forwarded-Host": PROXY, "Forwarded": f"host={PROXY}",
                       "X-Forwarded-For": "127.0.0.1"}) == 421
        assert status({"Host": PROXY, "X-Forwarded-Host": "attacker.example.test"}) == 200
    finally:
        server.shutdown()
        server.server_close()


# 7. Docs ----------------------------------------------------------------------------------------------------


def test_the_docs_describe_the_new_pages_and_what_proxy_host_requires() -> None:
    root = Path(__file__).resolve().parent.parent
    readme, design = (root / "README.md").read_text(), (root / "docs" / "design.md").read_text()
    for doc in (readme, design):
        assert "/needs" in doc and "/t/" in doc and "proxy_host" in doc
        assert "authenticating proxy" in doc
    security = (root / "SECURITY.md").read_text()
    assert "proxy_host" in security and "not authentication" in security
