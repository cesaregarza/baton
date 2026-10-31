"""The dashboard's attention projection and the seat's own Needs page.

`dashboard.attention_role` names whose attention the Needs page is about; visibility stays the viewer's. The
projection only filters what the viewer may already read: it never reads as the person, never widens anything,
and viewing moves no cursor. Everything here is synthetic: generic roles and the dummy name "Alex Rivera".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from seat_helpers import agent_says, conversation, log_in, mailbox, make_seat_host
from seat_helpers import get as seat_get
from test_dashboard import snapshot, text

from baton import config as config_module
from baton import conversations, jobs
from baton.config import Config, ConfigError
from baton.store import connect

try:
    from baton import dashboard
except ImportError:  # each test then fails on its own
    dashboard = None  # type: ignore[assignment]

PERSON = "Alex Rivera"
JOB_STATES = {"initial": "open", "states": {
    "open": {"waiting_on": "person", "next": ["done"]},
    "done": {}}}


def projection_config(tmp_path: Path, display_name: str = PERSON, **dashboard_keys: Any) -> Config:
    """`viewer` sees every conversation; `person` is the human the projection is about, in some of them."""
    return config_module.parse({
        "host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")},
        "roles": {"orchestrator": {"can_create_conversations": True, "can_manage_participants": True},
                  "assistant": {},
                  "person": {"human": True, "display_name": display_name},
                  "viewer": {"display_only": True, "can_view_all_conversations": True}},
        "conversations": {"enabled": True, "max_messages_per_hour": 1000},
        "jobs": JOB_STATES,
        "dashboard": {"enabled": True, "viewer_role": "viewer", **dashboard_keys},
    })


def get(config: Config, target: str) -> Any:
    return dashboard.respond(config, "GET", target, f"127.0.0.1:{config.dashboard.listen_port}")


def say(config: Config, conversation_id: str, sender: str, words: str, **kw: Any) -> str:
    conn = connect(config.db_path)
    try:
        return conversations.send(conn, config, sender=sender, conversation_id=conversation_id, text=words,
                                  **kw)["message_id"]
    finally:
        conn.close()


def seeded(config: Config) -> dict[str, str]:
    conn = connect(config.db_path)
    try:
        room = conversations.create(conn, config, actor="orchestrator", title="Release room",
                                    participants=["orchestrator", "assistant", "person"])["conversation_id"]
    finally:
        conn.close()
    ids = {"room": room}
    ids["needs_person"] = say(config, room, "orchestrator", "Alex, approve the release?", attention=["person"],
                              needs_decision=True)
    ids["to_assistant"] = say(config, room, "orchestrator", "assistant, run the checks", attention=["assistant"],
                              needs_decision=True)
    return ids


def section(body: str, start: str, end: str) -> str:
    """The text between two markers, so a test can tell which block an item sits in."""
    head = body.index(start)
    tail = body.find(end, head)
    return body[head:tail if tail >= 0 else len(body)]


def test_a_real_needs_person_message_shows_under_needs_that_person(tmp_path) -> None:
    config = projection_config(tmp_path, attention_role="person")
    seeded(config)
    body = text(get(config, "/needs"))
    assert f"Needs {PERSON}, read-only view, as visible to viewer" in body
    mine = section(body, f"Needs {PERSON}", 'id="decisions"')
    assert "Alex, approve the release?" in mine
    assert "assistant, run the checks" not in mine, "a decision for another role stays collapsed"
    assert "assistant, run the checks" in section(body, 'id="decisions"', 'id="jobs"')


def test_attention_naming_the_viewer_confers_nothing_when_attention_role_is_elsewhere(tmp_path) -> None:
    config = projection_config(tmp_path, attention_role="person")
    conn = connect(config.db_path)
    try:
        room = conversations.create(conn, config, actor="orchestrator", title="Side room",
                                    participants=["orchestrator", "assistant", "person", "viewer"])["conversation_id"]
    finally:
        conn.close()
    say(config, room, "orchestrator", "viewer, glance at this", attention=["viewer"])
    before = snapshot(config)
    body = text(get(config, "/needs"))
    assert "viewer, glance at this" not in section(body, f"Needs {PERSON}", 'id="decisions"')
    assert "asks for you" not in body
    assert snapshot(config) == before, "viewing moved a cursor or wrote a row"


def test_an_agent_reply_quoting_the_person_is_not_the_persons_reply(tmp_path) -> None:
    config = projection_config(tmp_path, attention_role="person")
    ids = seeded(config)
    say(config, ids["room"], "assistant", f"{PERSON} said: approved", reply_to=ids["needs_person"])
    body = text(get(config, "/needs"))
    assert "Alex, approve the release?" in section(body, f"Needs {PERSON}", 'id="decisions"')
    item = re.search(r"<li>(?:(?!</li>).)*Alex, approve the release\?(?:(?!</li>).)*</li>", body, re.S).group(0)
    assert f"{PERSON} replied" not in item, "an agent's words about the person are not the person's reply"


def test_the_persons_own_human_reply_is_labelled_replied_and_never_approved(tmp_path) -> None:
    config = projection_config(tmp_path, attention_role="person")
    ids = seeded(config)
    say(config, ids["room"], "person", "Yes, ship it.", reply_to=ids["needs_person"])
    before = snapshot(config)
    body = text(get(config, "/needs"))
    item = re.search(r"<li>(?:(?!</li>).)*Alex, approve the release\?(?:(?!</li>).)*</li>", body, re.S).group(0)
    assert f"{PERSON} replied" in item
    for word in ("approved", "resolved"):
        assert word not in item.lower()
    assert snapshot(config) == before


def test_jobs_waiting_on_the_person_are_hidden_unless_attention_jobs_is_on(tmp_path) -> None:
    config = projection_config(tmp_path, attention_role="person")
    conn = connect(config.db_path)
    try:
        jobs.create(conn, config.jobs, request_id="job-1", actor="orchestrator", title="Sign the release notes")
    finally:
        conn.close()
    body = text(get(config, "/needs"))
    assert "Sign the release notes" not in body
    assert f"Jobs waiting on {PERSON} aren't shown in this read-only view" in body
    shown = projection_config(tmp_path, attention_role="person", attention_jobs=True)
    body = text(get(shown, "/needs"))
    assert "Sign the release notes" in body
    assert f"Jobs waiting on {PERSON}" in body and "aren't shown" not in body


def test_without_attention_role_the_page_is_todays_needs_you(tmp_path) -> None:
    config = projection_config(tmp_path)
    seeded(config)
    body = text(get(config, "/needs"))
    assert "<h1>Needs you</h1>" in body and "read-only view, as visible to" not in body
    assert config.dashboard.attention_role == "viewer"


def test_the_projection_never_widens_what_the_viewer_reads(tmp_path) -> None:
    config = config_module.parse({
        "host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")},
        "roles": {"orchestrator": {"can_create_conversations": True}, "assistant": {},
                  "person": {"human": True, "display_name": PERSON}, "viewer": {"display_only": True}},
        "conversations": {"enabled": True, "max_messages_per_hour": 1000}, "jobs": JOB_STATES,
        "dashboard": {"enabled": True, "viewer_role": "viewer", "attention_role": "person"},
    })
    conn = connect(config.db_path)
    try:
        room = conversations.create(conn, config, actor="orchestrator", title="Person only",
                                    participants=["orchestrator", "person"])["conversation_id"]
    finally:
        conn.close()
    say(config, room, "orchestrator", "private ask for Alex", attention=["person"], needs_decision=True)
    body = text(get(config, "/needs"))
    assert f"Needs {PERSON}, read-only view, as visible to viewer" in body
    assert "private ask for Alex" not in body, "the viewer isn't in it, so it can't show"


@pytest.mark.parametrize(("keys", "fragment"), [
    ({"attention_role": "nobody"}, "dashboard.attention_role"),
    ({"attention_role": 7}, "dashboard.attention_role"),
    ({"attention_role": "person", "attention_jobs": "yes"}, "dashboard.attention_jobs"),
])
def test_config_refuses_a_bad_attention_setting(tmp_path, keys, fragment) -> None:
    with pytest.raises(ConfigError, match=re.escape(fragment)):
        projection_config(tmp_path, **keys)


def test_config_reads_attention_role_and_attention_jobs(tmp_path) -> None:
    config = projection_config(tmp_path, attention_role="person", attention_jobs=True)
    assert (config.dashboard.attention_role, config.dashboard.attention_jobs) == ("person", True)
    plain = projection_config(tmp_path)
    assert (plain.dashboard.attention_role, plain.dashboard.attention_jobs) == ("viewer", False)


# The seat's own Needs page: the same list, from the seat role's own membership, and the jobs waiting on it.

SEAT_JOBS = """
[jobs]
initial = "open"
[jobs.states.open]
waiting_on = "operator"
next = ["done"]
[jobs.states.done]
"""


def test_seat_needs_lists_what_asks_for_the_seat_role_and_moves_nothing(tmp_path) -> None:
    host = make_seat_host(tmp_path, seat_extra=SEAT_JOBS)
    mine = conversation(host, ("orchestrator", "reviewer", "operator"), title="Ours")
    theirs = conversation(host, ("orchestrator", "reviewer"), title="Not ours")
    agent_says(host, mine, "Alex, pick a date", attention=["operator"], needs_decision=True)
    agent_says(host, mine, "reviewer, check the copy", attention=["reviewer"], needs_decision=True)
    agent_says(host, theirs, "operator should see this?", attention=["reviewer"], needs_decision=True)
    conn = connect(host.config.db_path)
    try:
        jobs.create(conn, host.config.jobs, request_id="job-1", actor="orchestrator", title="Countersign the order")
    finally:
        conn.close()
    _, cookies = log_in(host.config, host.seat_token)
    before = mailbox(host)
    response = seat_get(host.config, "/seat/needs", cookies=cookies)
    body = response.body.decode()
    assert response.status == 200
    head = section(body, "<h1>", 'id="decisions"')
    assert "Alex, pick a date" in head
    assert "reviewer, check the copy" not in head
    assert "operator should see this?" not in body, "the seat reads only what its own role may"
    assert "Countersign the order" in body
    assert mailbox(host) == before, "viewing the seat's Needs page acknowledged something"
    assert 'href="/seat/needs"' in seat_get(host.config, "/seat/", cookies=cookies).body.decode()


def test_seat_needs_without_a_session_goes_to_login(tmp_path) -> None:
    host = make_seat_host(tmp_path)
    response = seat_get(host.config, "/seat/needs")
    assert response.status in (302, 303) and "/seat/login" in response.headers.get("Location", "")


# A display name is configured text, so every place a page shows it goes through the same escaping.

MARKUP = "Alex <b>Team</b>"
ESCAPED = "Alex &lt;b&gt;Team&lt;/b&gt;"


def test_a_display_name_with_markup_is_escaped_in_each_needs_label(tmp_path) -> None:
    config = projection_config(tmp_path, display_name=MARKUP, attention_role="person")
    ids = seeded(config)
    say(config, ids["room"], "person", "Yes, ship it.", reply_to=ids["needs_person"])
    body = text(get(config, "/needs"))
    assert "<b>Team</b>" not in body, "a display name reached the page as markup"
    assert f"asks for {ESCAPED}" in body and f"{ESCAPED} replied" in body


def test_a_display_name_with_markup_is_escaped_in_the_needs_pager(tmp_path) -> None:
    config = projection_config(tmp_path, display_name=MARKUP, attention_role="person")
    room = seeded(config)["room"]
    for n in range(dashboard.MAX_NEEDS):   # one page more than fits: the pager shows
        say(config, room, "orchestrator", f"ask {n}", attention=["person"])
    body = text(get(config, "/needs"))
    assert f"Older messages for {ESCAPED}" in body
    assert "<b>Team</b>" not in body


def test_projected_pages_say_needs_the_person_in_the_title_and_the_index_links(tmp_path) -> None:
    config = projection_config(tmp_path, display_name=MARKUP, attention_role="person")
    conn = connect(config.db_path)
    try:
        conversations.create(conn, config, actor="orchestrator", title="Topical",
                             participants=["orchestrator", "person"], topic="alpha")
    finally:
        conn.close()
    needs = text(get(config, "/needs"))
    assert f"<title>Needs {ESCAPED} · baton</title>" in needs
    index = text(get(config, "/"))
    assert f'<a href="/needs">Needs {ESCAPED}</a>' in index
    assert f"Needs {ESCAPED} here</a>" in text(get(config, "/t/alpha"))
    for page in (needs, index, text(get(config, "/t/alpha"))):
        assert "Needs you" not in page and "<b>Team</b>" not in page
    plain = projection_config(tmp_path)
    assert "<title>Needs you · baton</title>" in text(get(plain, "/needs"))
    assert '<a href="/needs">Needs you</a>' in text(get(plain, "/"))


def test_the_seat_escapes_a_display_name_with_markup(tmp_path) -> None:
    host = make_seat_host(tmp_path)
    host.reload(host.path.read_text().replace('display_name = "Alex Rivera"', f'display_name = "{MARKUP}"'))
    room = conversation(host, ("orchestrator", "operator"), title="Ours")
    agent_says(host, room, "Alex, a question", attention=["operator"], needs_decision=True)
    _, cookies = log_in(host.config, host.seat_token)
    for target in ("/seat/", "/seat/needs", f"/seat/c/{room}"):
        body = seat_get(host.config, target, cookies=cookies).body.decode()
        assert "<b>Team</b>" not in body, f"{target} shows a display name as markup"
