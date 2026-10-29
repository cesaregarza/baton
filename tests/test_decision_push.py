"""The optional decision push: a content-free notice to one role's channel when a decision is waiting for it.

Synthetic and offline: generic roles, a temporary data directory, a recording stand-in for the channel, and
loopback HTTP servers for the failing endpoints. The push carries a fixed title and, when configured, a
link to the dashboard item; never a message's words.
"""

from __future__ import annotations

import http.server
import json
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from test_conversations import GROUP, say, start
from test_messages import rid

from baton import app as app_module
from baton import config as config_module
from baton.config import Config, ConfigError
from baton.integrations import load_enabled
from baton.store import connect

try:
    from baton.integrations import decision_push
except ImportError:  # written before the module: each test then fails on its own instead of the whole run stopping
    decision_push = None  # type: ignore[assignment]

ROLE = "assistant"
TITLE = "A decision is waiting in Baton"
BODY = "Waiting for your decision."
# Built at run time so no secret scanner mistakes this file for a leak; it only has to look like a token.
# The URL with credentials below is assembled the same way.
SECRET_SHAPED = "ghp_" + "Zq7" * 12
TOKEN_ENV = "BATON_TEST_PUSH_TOKEN"
TOKEN_VALUE = "push-token-" + "x9" * 8


def push_config(tmp_path: Path, *, table: dict[str, Any] | None = None, roles: dict[str, Any] | None = None) -> Config:
    """Lane A's generic roles with the push on for `assistant`, unless `table` says otherwise."""
    settings = {"enabled": True, "role": ROLE, "url": "https://push.example.test/decisions",
                "link_base": "https://dash.example.test"} if table is None else table
    return config_module.parse({
        "host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")},
        "roles": roles or {"orchestrator": {"can_create_conversations": True, "can_manage_participants": True},
                           "reviewer": {}, "assistant": {}, "outsider": {}},
        "conversations": {"enabled": True},
        "integrations": {"decision_push": settings},
    })


class Channel:
    """Stands in for the push endpoint: records each request, answers with `status` or raises `error`."""

    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.status, self.error, self.calls = status, error, []

    def __call__(self, url: str, headers: dict[str, str], body: bytes) -> int:
        self.calls.append({"url": url, "headers": dict(headers), "body": body})
        if self.error is not None:
            raise self.error
        return self.status


def pusher(config: Config, channel: Channel | None = None, **kwargs: Any) -> Any:
    return decision_push.load(config, config.integrations["decision_push"]).with_transport(
        channel or Channel(), **kwargs)


def everything_sent(channel: Channel) -> str:
    return json.dumps([{**call, "body": call["body"].decode()} for call in channel.calls])


# Fires only on needs_decision, once per message -----------------------------------------------------------


def test_it_pushes_only_for_needs_decision_and_once_per_message(tmp_path) -> None:
    config = push_config(tmp_path)
    db = connect(config.db_path)
    channel = Channel()
    push = pusher(config, channel)
    push.poll_once()  # the first pass starts at the end of the mailbox: enabling it sends no backlog
    cid = start(db, config)
    say(db, config, "orchestrator", cid, "plain chatter")
    say(db, config, "orchestrator", cid, "look at this", attention=[ROLE])
    decision = say(db, config, "orchestrator", cid, "please decide", needs_decision=True,
                   request_id=rid("decide-once"))
    push.poll_once()
    assert len(channel.calls) == 1
    # A retried send replays without a new copy, and later passes find nothing new.
    again = say(db, config, "orchestrator", cid, "please decide", needs_decision=True, request_id=rid("decide-once"))
    assert again["message_id"] == decision["message_id"] and again["created"] is False
    push.poll_once()
    pusher(config, channel).poll_once()  # a restarted host keeps what it already pushed
    assert len(channel.calls) == 1


def test_it_pushes_only_what_the_role_received(tmp_path) -> None:
    config = push_config(tmp_path)
    db = connect(config.db_path)
    channel = Channel()
    push = pusher(config, channel)
    push.poll_once()
    others = start(db, config, ["orchestrator", "reviewer"])
    say(db, config, "orchestrator", others, "decide, but not for the role", needs_decision=True)
    mine = start(db, config, GROUP)
    say(db, config, ROLE, mine, "the role's own question", needs_decision=True)
    push.poll_once()
    assert channel.calls == []


def test_a_backlog_from_before_it_was_enabled_is_not_pushed(tmp_path) -> None:
    config = push_config(tmp_path)
    db = connect(config.db_path)
    cid = start(db, config)
    say(db, config, "orchestrator", cid, "an old decision", needs_decision=True)
    channel = Channel()
    push = pusher(config, channel)
    push.poll_once()
    push.poll_once()  # and not on a later pass either: the first pass starts after it
    assert channel.calls == []


# No message content ----------------------------------------------------------------------------------------


def test_the_push_carries_a_fixed_notice_and_a_link_and_nothing_of_the_message(tmp_path) -> None:
    config = push_config(tmp_path)
    db = connect(config.db_path)
    channel = Channel()
    push = pusher(config, channel)
    push.poll_once()
    cid = start(db, config)
    say(db, config, "orchestrator", cid, f"ship it? the key is {SECRET_SHAPED}", needs_decision=True,
        relayed=[{"source": f"someone {SECRET_SHAPED}", "at": "2026-10-29T08:00:00+00:00",
                  "text": f"relayed {SECRET_SHAPED}"}])
    push.poll_once()
    [call] = channel.calls
    sent = everything_sent(channel)
    for words in (SECRET_SHAPED, "ship it", "relayed", "someone", "Release plan", "orchestrator"):
        assert words not in sent
    link = f"https://dash.example.test/c/{cid}?after_seq=0#m-1"
    assert call["url"] == "https://push.example.test/decisions"
    assert call["headers"]["Title"] == TITLE
    assert call["headers"]["Click"] == link
    assert call["body"].decode() == f"{BODY} Open it: {link}"
    state = (config.data_dir / "decision_push.json").read_text()
    assert SECRET_SHAPED not in state and "ship it" not in state


def test_without_a_link_base_it_sends_the_notice_alone(tmp_path) -> None:
    config = push_config(tmp_path, table={"enabled": True, "role": ROLE, "url": "https://push.example.test/d"})
    db = connect(config.db_path)
    channel = Channel()
    push = pusher(config, channel)
    push.poll_once()
    say(db, config, "orchestrator", start(db, config), "decide", needs_decision=True)
    push.poll_once()
    [call] = channel.calls
    assert "Click" not in call["headers"] and call["body"].decode() == f"{BODY} Open Baton to see it."


def test_the_token_comes_only_from_the_named_environment_variable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    config = push_config(tmp_path, table={"enabled": True, "role": ROLE, "url": "https://push.example.test/d",
                                          "secret_env": TOKEN_ENV})
    db = connect(config.db_path)
    channel = Channel()
    push = pusher(config, channel)
    push.poll_once()
    say(db, config, "orchestrator", start(db, config), "decide", needs_decision=True)
    push.poll_once()
    assert channel.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN_VALUE}"


# A failing endpoint never blocks or fails the send ---------------------------------------------------------


class _Hang(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - the base class's name
        time.sleep(3)

    def log_message(self, *args: Any) -> None:
        pass


class _Fail(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - the base class's name
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


def serving(handler: type) -> tuple[http.server.ThreadingHTTPServer, str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/push"


def refused_url() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/push"


@pytest.mark.parametrize("endpoint", ["timeout", "500", "refused"])
def test_a_failing_endpoint_never_blocks_or_fails_the_send_and_logs_no_content(tmp_path, monkeypatch, caplog,
                                                                               endpoint) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    server = None
    if endpoint == "timeout":
        server, url = serving(_Hang)
    elif endpoint == "500":
        server, url = serving(_Fail)
    else:
        url = refused_url()
    try:
        config = push_config(tmp_path, table={"enabled": True, "role": ROLE, "url": url, "secret_env": TOKEN_ENV,
                                              "timeout_seconds": 0.5})
        db = connect(config.db_path)
        push = decision_push.load(config, config.integrations["decision_push"])
        push.poll_once()
        stop = threading.Event()
        loop = threading.Thread(target=push.run, args=(stop,), kwargs={"poll_seconds": 0.05}, daemon=True)
        loop.start()
        cid = start(db, config)
        caplog.set_level(logging.DEBUG)
        began = time.monotonic()
        sent = say(db, config, "orchestrator", cid, f"decide {SECRET_SHAPED}", needs_decision=True)
        assert time.monotonic() - began < 0.5, "the send waited on the push"
        assert sent["created"] is True
        assert db.execute("SELECT COUNT(*) FROM messages WHERE id = ?", (sent["message_id"],)).fetchone()[0] == 2
        deadline = time.monotonic() + 10
        while push.counts()["failed"] < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        stop.set()
        loop.join(timeout=5)
        assert push.counts() == {"sent": 0, "failed": 1, "capped": 0}
        logged = caplog.text
        for secret in (SECRET_SHAPED, TOKEN_VALUE, url, "decide", "push.example"):
            assert secret not in logged
        assert "decision push" in logged
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


# Rate cap ----------------------------------------------------------------------------------------------------


def test_the_hourly_cap_holds_and_counts_what_it_held_back(tmp_path) -> None:
    config = push_config(tmp_path, table={"enabled": True, "role": ROLE, "url": "https://push.example.test/d",
                                          "max_per_hour": 2})
    db = connect(config.db_path)
    clock = [1_000_000.0]
    channel = Channel()
    push = pusher(config, channel, clock=lambda: clock[0])
    push.poll_once()
    cid = start(db, config)
    for number in range(5):
        say(db, config, "orchestrator", cid, f"decision {number}", needs_decision=True)
    push.poll_once()
    assert len(channel.calls) == 2
    assert push.counts() == {"sent": 2, "failed": 0, "capped": 3}
    clock[0] += 1800
    say(db, config, "orchestrator", cid, "still within the hour", needs_decision=True)
    push.poll_once()
    assert len(channel.calls) == 2
    clock[0] += 1801
    say(db, config, "orchestrator", cid, "a new hour", needs_decision=True)
    push.poll_once()
    assert len(channel.calls) == 3


# Off by default, and wired into serve ------------------------------------------------------------------------


def test_it_is_off_unless_enabled_and_serve_runs_it_when_it_is(tmp_path, monkeypatch) -> None:
    plain = config_module.parse({"host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "p")},
                                 "roles": {"worker": {}}})
    assert "decision_push" not in load_enabled(plain)
    switched_off = push_config(tmp_path / "off", table={"enabled": False, "role": ROLE,
                                                         "url": "https://push.example.test/d"})
    assert "decision_push" not in load_enabled(switched_off)

    names: list[list[str]] = []
    monkeypatch.setattr(app_module.uvicorn, "run", lambda *a, **k: names.append(
        sorted(thread.name for thread in threading.enumerate())))
    app_module.serve(push_config(tmp_path / "on"))
    deadline = time.monotonic() + 5
    while any(t.name == "decision_push" for t in threading.enumerate()) and time.monotonic() < deadline:
        time.sleep(0.02)
    app_module.serve(switched_off)
    assert "decision_push" in names[0] and "decision_push" not in names[1]


# Config ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("table", "field"), [
    ({"enabled": True, "role": ROLE}, "integrations.decision_push.url"),
    ({"enabled": True, "role": ROLE, "url": "ftp://push.example.test/d"}, "integrations.decision_push.url"),
    ({"enabled": True, "role": ROLE, "url": "https://" + "name" + ":" + "word" + "@push.example.test/d"},
     "integrations.decision_push.url"),
    ({"enabled": True, "url": "https://push.example.test/d"}, "integrations.decision_push.role"),
    ({"enabled": True, "role": "nobody", "url": "https://push.example.test/d"}, "integrations.decision_push.role"),
    ({"enabled": True, "role": ROLE, "url": "https://push.example.test/d", "token": "inline"},
     "integrations.decision_push.token"),
    ({"enabled": True, "role": ROLE, "url": "https://push.example.test/d", "secret_env": "not a name"},
     "integrations.decision_push.secret_env must be the name of an environment variable"),
    ({"enabled": True, "role": ROLE, "url": "https://push.example.test/d", "secret_env": "BATON_UNSET_PUSH_TOKEN"},
     "BATON_UNSET_PUSH_TOKEN"),
    ({"enabled": True, "role": ROLE, "url": "https://push.example.test/d", "max_per_hour": 0},
     "integrations.decision_push.max_per_hour"),
    ({"enabled": True, "role": ROLE, "url": "https://push.example.test/d", "max_per_hour": "5"},
     "integrations.decision_push.max_per_hour"),
    ({"enabled": True, "role": ROLE, "url": "https://push.example.test/d", "link_base": "dash.example.test"},
     "integrations.decision_push.link_base"),
    ({"enabled": True, "role": ROLE, "url": "https:///d"}, "integrations.decision_push.url"),
    ({"enabled": True, "role": ROLE, "url": "https://:443/d"}, "integrations.decision_push.url"),
    ({"enabled": True, "role": ROLE, "url": "https://push.example.test:99999/d"}, "integrations.decision_push.url"),
    ({"enabled": True, "role": ROLE, "url": "https://push.example.test:0/d"}, "integrations.decision_push.url"),
    ({"enabled": True, "role": ROLE, "url": "https://push.example.test:http/d"}, "integrations.decision_push.url"),
    ({"enabled": True, "role": ROLE, "url": "https://[::1/d"}, "integrations.decision_push.url"),
    ({"enabled": True, "role": ROLE, "url": "https://[not-an-address]/d"}, "integrations.decision_push.url"),
    ({"enabled": True, "role": ROLE, "url": "https://push.example.test/d", "link_base": "https://:8443"},
     "integrations.decision_push.link_base"),
    ({"enabled": True, "role": ROLE, "url": "https://push.example.test/d", "link_base": "https://d.test:70000"},
     "integrations.decision_push.link_base"),
    ({"enabled": True, "role": ROLE, "url": "https://push.example.test/d", "link_base": "https://[fe80::1"},
     "integrations.decision_push.link_base"),
    ({"enabled": True, "role": ROLE, "url": "https://push.example.test/d", "link_base": "https://d.test/?x=1"},
     "integrations.decision_push.link_base"),
])
def test_config_is_validated_and_names_the_field(tmp_path, monkeypatch, table, field) -> None:
    monkeypatch.delenv("BATON_UNSET_PUSH_TOKEN", raising=False)
    config = push_config(tmp_path, table=table)
    with pytest.raises(ConfigError) as raised:
        load_enabled(config)
    assert field in str(raised.value)
    assert "inline" not in str(raised.value)


def test_a_role_cannot_use_it_for_its_wakes(tmp_path) -> None:
    roles = {"orchestrator": {"can_create_conversations": True}, "reviewer": {},
             "assistant": {"wake": {"integration": "decision_push"}}, "outsider": {}}
    with pytest.raises(ConfigError, match="roles.assistant.wake.*not for wakes"):
        load_enabled(push_config(tmp_path, roles=roles))


def test_the_url_and_token_never_appear_in_a_config_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    config = push_config(tmp_path, table={"enabled": True, "role": "nobody",
                                          "url": "https://secret-host.example.test/d", "secret_env": TOKEN_ENV})
    with pytest.raises(ConfigError) as raised:
        load_enabled(config)
    assert "integrations.decision_push.role" in str(raised.value)
    assert "secret-host" not in str(raised.value) and TOKEN_VALUE not in str(raised.value)


def test_valid_hosts_ports_and_addresses_load(tmp_path) -> None:
    for url in ("https://push.example.test/d", "http://127.0.0.1:8080/d", "https://[::1]:8443/d"):
        config = push_config(tmp_path, table={"enabled": True, "role": ROLE, "url": url,
                                              "link_base": "https://[2001:db8::1]:8443"})
        assert "decision_push" in load_enabled(config)


# A decision sent as soon as the host is up is not missed (reviewer finding on 19b9e6f5) ---------------------


def test_a_decision_sent_before_the_workers_first_pass_is_still_pushed(tmp_path, monkeypatch) -> None:
    """serve takes the starting point before the server accepts a send, not on the worker's first pass."""
    channel = Channel()
    monkeypatch.setattr(decision_push, "post", lambda timeout: channel)
    released = threading.Event()
    real_load_enabled = app_module.load_enabled

    def held_back(config: Config) -> dict[str, Any]:
        loaded = real_load_enabled(config)
        push = loaded["decision_push"]
        original = push.run

        def run_after_release(stop: threading.Event, **kwargs: Any) -> None:
            released.wait(10)
            original(stop, poll_seconds=0.05)
        push.run = run_after_release
        return loaded

    def serving(app: Any, **kwargs: Any) -> None:
        db = connect(config.db_path)
        say(db, config, "orchestrator", start(db, config), "decide right away", needs_decision=True)
        released.set()
        deadline = time.monotonic() + 10
        while not channel.calls and time.monotonic() < deadline:
            time.sleep(0.05)

    config = push_config(tmp_path)
    monkeypatch.setattr(app_module, "load_enabled", held_back)
    monkeypatch.setattr(app_module.uvicorn, "run", serving)
    app_module.serve(config)
    assert len(channel.calls) == 1


# The link opens the page that holds the message (reviewer finding on 19b9e6f5) ------------------------------


def test_a_link_past_the_first_page_opens_the_page_holding_the_message(tmp_path) -> None:
    from baton import dashboard

    config = config_module.parse({
        "host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")},
        "roles": {"orchestrator": {"can_create_conversations": True}, "reviewer": {}, "assistant": {}},
        "conversations": {"enabled": True, "max_messages_per_hour": 1000},
        "dashboard": {"enabled": True, "viewer_role": ROLE, "listen_port": 8766},
        "integrations": {"decision_push": {"enabled": True, "role": ROLE, "url": "https://push.example.test/d",
                                           "link_base": "http://127.0.0.1:8766"}},
    })
    db = connect(config.db_path)
    channel = Channel()
    push = pusher(config, channel)
    push.poll_once()
    cid = start(db, config)
    for number in range(1, 105):
        say(db, config, "orchestrator", cid, f"context {number}")
    say(db, config, "orchestrator", cid, "the late decision", needs_decision=True)
    push.poll_once()
    [call] = channel.calls
    link = call["headers"]["Click"]
    assert link == f"http://127.0.0.1:8766/c/{cid}?after_seq=104#m-105"
    target = link.removeprefix("http://127.0.0.1:8766").split("#")[0]
    page = dashboard.respond(config, "GET", target, "127.0.0.1:8766")
    assert page.status == 200
    body = page.body.decode()
    assert 'id="m-105"' in body and "the late decision" in body


# An unreadable push state never stops Baton (reviewer finding on eb9bd0f1) -----------------------------------


def corrupt_state(config: Config, content: str) -> None:
    connect(config.db_path).close()  # the checked data directory, as serve would have it
    path = config.data_dir / "decision_push.json"
    path.write_text(content)
    path.chmod(0o600)


def live(name: str) -> int:
    return sum(1 for thread in threading.enumerate() if thread.name == name and thread.is_alive())


@pytest.mark.parametrize("content", [f"not json {SECRET_SHAPED}",
                                     json.dumps({"after_seq": f"x{SECRET_SHAPED}", "window": [], "counts": {}})])
def test_an_unreadable_push_state_switches_the_push_off_and_baton_still_serves(tmp_path, monkeypatch, caplog,
                                                                               content) -> None:
    config = push_config(tmp_path)
    corrupt_state(config, content)
    wakes_before = live("wakes")
    seen: dict[str, Any] = {}

    def serving(app: Any, **kwargs: Any) -> None:
        seen["served"] = True
        seen["push threads"] = live("decision_push")

    monkeypatch.setattr(app_module.uvicorn, "run", serving)
    caplog.set_level(logging.INFO)
    app_module.serve(config)  # must not raise
    assert seen == {"served": True, "push threads": 0}
    deadline = time.monotonic() + 5
    while live("wakes") > wakes_before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert live("wakes") == wakes_before and live("decision_push") == 0, "a thread was left behind"
    logged = caplog.text
    assert "decision_push" in logged and "decision_push.json" in logged
    assert SECRET_SHAPED not in logged and "not json" not in logged
    # The state is left as found: nothing resets the watermark behind the operator's back.
    assert (config.data_dir / "decision_push.json").read_text() == content


def test_prepare_refuses_an_unreadable_state_by_naming_the_file_only(tmp_path) -> None:
    config = push_config(tmp_path)
    corrupt_state(config, f"{{broken {SECRET_SHAPED}")
    from baton.integrations import IntegrationUnavailable

    with pytest.raises(IntegrationUnavailable) as raised:
        decision_push.load(config, config.integrations["decision_push"]).prepare()
    assert "decision_push.json" in str(raised.value) and SECRET_SHAPED not in str(raised.value)


def test_serve_prepares_every_integration_before_any_worker_thread_starts(tmp_path, monkeypatch) -> None:
    started: list[str] = []
    real_start = threading.Thread.start

    def recording_start(self: threading.Thread) -> None:
        started.append(self.name)
        real_start(self)

    order: list[str] = []
    real_prepare = decision_push.DecisionPush.prepare

    def recording_prepare(self: Any) -> None:
        order.append(f"prepare after {len(started)} threads")
        real_prepare(self)

    monkeypatch.setattr(threading.Thread, "start", recording_start)
    monkeypatch.setattr(decision_push.DecisionPush, "prepare", recording_prepare)
    monkeypatch.setattr(app_module.uvicorn, "run", lambda *a, **k: None)
    app_module.serve(push_config(tmp_path))
    assert order == ["prepare after 0 threads"]
    assert "wakes" in started and "decision_push" in started


def test_any_other_prepare_failure_is_contained_and_logged_by_type_alone(tmp_path, monkeypatch, caplog) -> None:
    def failing(self: Any) -> None:
        raise RuntimeError(f"something it read: {SECRET_SHAPED}")

    seen: dict[str, Any] = {}
    monkeypatch.setattr(decision_push.DecisionPush, "prepare", failing)
    monkeypatch.setattr(app_module.uvicorn, "run", lambda *a, **k: seen.update(served=live("decision_push")))
    caplog.set_level(logging.INFO)
    app_module.serve(push_config(tmp_path))  # must not raise
    assert seen == {"served": 0}
    assert "decision_push" in caplog.text and "RuntimeError" in caplog.text
    assert SECRET_SHAPED not in caplog.text and "something it read" not in caplog.text
