"""The optional read-only dashboard: a page per conversation, served by the host from its own store.

Everything here is synthetic: generic roles and a temporary data directory. The dashboard is off unless the
config enables it, binds only to loopback, shows what one configured viewer role may see, and reads the way
`read_history` does, so viewing marks nothing read and moves no cursor.
"""

from __future__ import annotations

import http.client
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from conftest import free_port

from baton import app as app_module
from baton import config as config_module
from baton import conversations, messages
from baton.config import Config, ConfigError
from baton.store import connect

try:
    from baton import dashboard
except ImportError:  # written before the module: each test then fails on its own instead of the whole run stopping
    dashboard = None  # type: ignore[assignment]

GROUP = ["orchestrator", "reviewer", "operator"]
OTHERS = ["orchestrator", "assistant"]
WAKE = {"wake": {"command": ["true"]}}
PAYLOAD = '<script>alert(1)</script><img src=x onerror="alert(2)">'


def view_config(tmp_path: Path, *, view_all: bool = False, dashboard_table: dict[str, Any] | None = None,
                conversations_enabled: bool = True, port: int = 8766) -> Config:
    """Five generic roles; `operator` is the dashboard's viewer, and sees every thread only with the flag."""
    table = {"enabled": True, "viewer_role": "operator", "listen_port": port} if dashboard_table is None \
        else dashboard_table
    return config_module.parse({
        "host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")},
        "roles": {"orchestrator": {"can_create_conversations": True, "can_manage_participants": True, **WAKE},
                  "reviewer": WAKE, "assistant": WAKE, "outsider": WAKE,
                  "operator": {"can_view_all_conversations": view_all}},
        "conversations": {"enabled": conversations_enabled},
        "dashboard": table,
    })


def seeded(config: Config) -> tuple[str, str]:
    """One conversation the viewer is in and one it isn't, each with a message."""
    conn = connect(config.db_path)
    try:
        mine = conversations.create(conn, config, actor="orchestrator", title="Release plan",
                                    participants=GROUP)["conversation_id"]
        theirs = conversations.create(conn, config, actor="orchestrator", title="Private side thread",
                                      participants=OTHERS)["conversation_id"]
        conversations.send(conn, config, sender="orchestrator", conversation_id=mine, text="plan is drafted")
        conversations.send(conn, config, sender="assistant", conversation_id=theirs, text="side thread words")
    finally:
        conn.close()
    return mine, theirs


def get(config: Config, target: str, *, method: str = "GET", host: str | None = None) -> Any:
    return dashboard.respond(config, method, target, host or f"127.0.0.1:{config.dashboard.listen_port}")


def text(response: Any) -> str:
    return response.body.decode()


def snapshot(config: Config) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table, so a test can show a page view wrote nothing at all."""
    conn = sqlite3.connect(config.db_path)
    try:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {table: sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr) for table in tables}
    finally:
        conn.close()


# Off by default, loopback only ---------------------------------------------------------------------------


def test_the_dashboard_is_off_by_default_and_serves_nothing(tmp_path) -> None:
    plain = config_module.parse({"host": {"public_url": "http://127.0.0.1:8765",
                                          "data_dir": str(tmp_path / "data")}, "roles": {"worker": {}}})
    assert plain.dashboard.enabled is False
    assert dashboard.server(plain) is None
    switched_off = view_config(tmp_path, dashboard_table={"enabled": False, "viewer_role": "operator"})
    assert dashboard.server(switched_off) is None


def test_serve_starts_the_dashboard_only_when_enabled(tmp_path, monkeypatch) -> None:
    """The host's `serve` runs the dashboard beside the MCP server when, and only when, the config enables it."""
    seen: dict[str, Any] = {}

    def fake_run(app: Any, host: str, port: int, **kwargs: Any) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", seen["port"], timeout=5)
        try:
            conn.request("GET", "/")
            seen["status"] = conn.getresponse().status
        except OSError as error:
            seen["status"] = type(error).__name__
        finally:
            conn.close()

    monkeypatch.setattr(app_module.uvicorn, "run", fake_run)
    seen["port"] = free_port()
    app_module.serve(view_config(tmp_path / "on", port=seen["port"]))
    assert seen["status"] == 200
    seen["port"] = free_port()
    app_module.serve(view_config(tmp_path / "off", port=seen["port"],
                                 dashboard_table={"enabled": False, "viewer_role": "operator",
                                                  "listen_port": seen["port"]}))
    assert seen["status"] == "ConnectionRefusedError"


def test_it_binds_to_loopback_by_default_and_refuses_any_other_address(tmp_path) -> None:
    config = view_config(tmp_path, port=free_port())
    assert config.dashboard.listen_host == "127.0.0.1"
    server = dashboard.server(config)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()
    for address in ("0.0.0.0", "::", "192.0.2.10", "example.test"):
        with pytest.raises(ConfigError, match="dashboard.listen_host"):
            view_config(tmp_path, dashboard_table={"enabled": True, "viewer_role": "operator",
                                                   "listen_host": address})


@pytest.mark.parametrize(("table", "conversations_enabled", "field"), [
    ({"enabled": True}, True, "dashboard.viewer_role"),
    ({"enabled": True, "viewer_role": "nobody"}, True, "dashboard.viewer_role"),
    ({"enabled": True, "viewer_role": "operator"}, False, "conversations.enabled"),
    ({"enabled": "true", "viewer_role": "operator"}, True, "dashboard.enabled"),
    ({"enabled": True, "viewer_role": "operator", "listen_port": 0}, True, "dashboard.listen_port"),
])
def test_an_enabled_dashboard_needs_a_configured_viewer_and_conversations(tmp_path, table, conversations_enabled,
                                                                          field) -> None:
    with pytest.raises(ConfigError, match=re.escape(field)):
        view_config(tmp_path, dashboard_table=table, conversations_enabled=conversations_enabled)


def test_the_view_all_flag_is_a_strict_boolean_and_off_by_default(tmp_path) -> None:
    assert view_config(tmp_path).roles["operator"].can_view_all_conversations is False
    with pytest.raises(ConfigError, match="roles.operator.can_view_all_conversations"):
        config_module.parse({
            "host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")},
            "roles": {"operator": {"can_view_all_conversations": "yes"}},
        })


# Visibility ----------------------------------------------------------------------------------------------


def test_a_viewer_sees_only_the_conversations_it_is_in(tmp_path) -> None:
    config = view_config(tmp_path)
    mine, theirs = seeded(config)
    index = text(get(config, "/"))
    assert "Release plan" in index and f"/c/{mine}" in index
    assert "Private side thread" not in index and theirs not in index
    page = get(config, f"/c/{mine}")
    assert page.status == 200 and "plan is drafted" in text(page)
    hidden = get(config, f"/c/{theirs}")
    assert hidden.status == 404
    assert "side thread words" not in text(hidden) and "Private side thread" not in text(hidden)


def test_only_the_explicit_flag_shows_every_conversation(tmp_path) -> None:
    config = view_config(tmp_path, view_all=True)
    mine, theirs = seeded(config)
    index = text(get(config, "/"))
    assert "Release plan" in index and "Private side thread" in index
    assert "side thread words" in text(get(config, f"/c/{theirs}"))
    assert "plan is drafted" in text(get(config, f"/c/{mine}"))


def test_a_viewer_sees_its_own_conversation_from_its_join_point_like_read_history(tmp_path) -> None:
    config = view_config(tmp_path)
    conn = connect(config.db_path)
    try:
        cid = conversations.create(conn, config, actor="orchestrator", title="Joined late",
                                   participants=["orchestrator", "reviewer"])["conversation_id"]
        conversations.send(conn, config, sender="reviewer", conversation_id=cid, text="before the viewer joined")
        conversations.add(conn, config, cid, "operator", actor="orchestrator")
        conversations.send(conn, config, sender="reviewer", conversation_id=cid, text="after the viewer joined")
    finally:
        conn.close()
    page = text(get(config, f"/c/{cid}"))
    assert "after the viewer joined" in page and "before the viewer joined" not in page


# Read-only and bookmark-neutral --------------------------------------------------------------------------


def test_viewing_moves_no_cursor_and_writes_nothing(tmp_path) -> None:
    config = view_config(tmp_path, view_all=True)
    mine, theirs = seeded(config)
    before = snapshot(config)
    for target in ("/", f"/c/{mine}", f"/c/{theirs}", f"/c/{mine}?after_seq=0", "/style.css", "/missing"):
        get(config, target)
    assert snapshot(config) == before
    conn = connect(config.db_path)
    try:
        unread = messages.read(conn, "operator")
        assert [m["text"] for m in unread["messages"]] == ["plan is drafted"]
    finally:
        conn.close()


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_there_are_no_write_endpoints(tmp_path, method) -> None:
    config = view_config(tmp_path, view_all=True)
    mine, _ = seeded(config)
    before = snapshot(config)
    for target in ("/", f"/c/{mine}", "/c", "/send", "/api/messages"):
        response = get(config, target, method=method)
        assert response.status == 405
        assert response.headers["Allow"] == "GET, HEAD"
    assert snapshot(config) == before


def test_a_request_for_another_host_name_is_refused(tmp_path) -> None:
    """DNS rebinding: a page on another site can resolve its own name to loopback, but can't fake the Host."""
    config = view_config(tmp_path)
    seeded(config)
    for host in ("attacker.example:8766", "127.0.0.1.attacker.example", "", "localhost:9999"):
        response = get(config, "/", host=host)
        assert response.status == 421
        assert "Release plan" not in text(response)
    for host in ("127.0.0.1:8766", "localhost:8766"):
        assert get(config, "/", host=host).status == 200


# Rendering -----------------------------------------------------------------------------------------------


def test_message_text_title_and_relayed_words_render_inert(tmp_path) -> None:
    config = view_config(tmp_path)
    conn = connect(config.db_path)
    try:
        cid = conversations.create(conn, config, actor="orchestrator", title=f"title {PAYLOAD}",
                                   participants=GROUP)["conversation_id"]
        conversations.send(conn, config, sender="orchestrator", conversation_id=cid, text=f"text {PAYLOAD}",
                           relayed=[{"source": f"source {PAYLOAD}", "at": "2026-10-29T08:00:00+00:00",
                                     "text": f"relayed {PAYLOAD}"}])
    finally:
        conn.close()
    for target in ("/", f"/c/{cid}"):
        body = text(get(config, target))
        assert "<script" not in body and "<img" not in body
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_every_response_carries_a_strict_csp_and_the_pages_load_nothing_from_elsewhere(tmp_path) -> None:
    config = view_config(tmp_path)
    mine, _ = seeded(config)
    responses = [get(config, target) for target in ("/", f"/c/{mine}", "/style.css", "/missing")]
    responses.append(get(config, "/", method="POST"))
    responses.append(get(config, "/", host="attacker.example"))
    for response in responses:
        policy = response.headers["Content-Security-Policy"]
        directives = dict(part.strip().split(" ", 1) for part in policy.split(";") if part.strip())
        assert directives["default-src"] == "'none'"
        assert directives.get("script-src", "'none'") == "'none'"
        assert directives["style-src"] == "'self'"
        assert directives["frame-ancestors"] == "'none'"
        assert directives["form-action"] == "'none'"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
    for response in responses[:2]:
        body = text(response)
        assert "<script" not in body and "<form" not in body and "<textarea" not in body
        assert not re.search(r"""(?:src|href)\s*=\s*["']?(?!/)""", body), "a page links outside the host"


def test_no_response_grants_another_origin_access(tmp_path) -> None:
    """No CORS headers anywhere, preflight included: a page on another site can't read a response."""
    config = view_config(tmp_path, view_all=True)
    mine, _ = seeded(config)
    for method in ("GET", "HEAD", "OPTIONS", "POST"):
        for target in ("/", f"/c/{mine}", "/style.css", "/missing"):
            response = get(config, target, method=method)
            granted = [name for name in response.headers if name.lower().startswith("access-control-")]
            assert granted == [], (method, target, granted)


def test_relayed_entries_and_participant_notices_render_apart_from_the_senders_text(tmp_path) -> None:
    config = view_config(tmp_path)
    conn = connect(config.db_path)
    try:
        cid = conversations.create(conn, config, actor="orchestrator", title="Relay check",
                                   participants=GROUP)["conversation_id"]
        conversations.send(conn, config, sender="orchestrator", conversation_id=cid, text="my own words",
                           relayed=[{"source": "a person", "at": "2026-10-29T08:00:00+00:00",
                                     "text": "their exact words"}])
        conversations.add(conn, config, cid, "assistant", actor="orchestrator")
    finally:
        conn.close()
    body = text(get(config, f"/c/{cid}"))
    own = re.search(r'<p class="text">(.*?)</p>', body, re.S)
    assert own and own.group(1) == "my own words"
    relayed = re.search(r'<blockquote class="relayed">(.*?)</blockquote>', body, re.S)
    assert relayed and "their exact words" in relayed.group(1) and "a person" in relayed.group(1)
    assert "my own words" not in relayed.group(1)
    assert '<span class="badge agent">agent</span>' in body and '<span class="role">orchestrator</span>' in body
    notice = re.search(r'<li class="notice">(.*?)</li>', body, re.S)
    assert notice and "assistant" in notice.group(1) and "added" in notice.group(1)
    assert "their exact words" not in notice.group(1)


def test_a_real_loopback_server_serves_the_pages(tmp_path) -> None:
    config = view_config(tmp_path, port=free_port())
    mine, _ = seeded(config)
    server = dashboard.server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for method, target, status in (("GET", f"/c/{mine}", 200), ("HEAD", "/", 200), ("POST", "/", 405)):
            conn = http.client.HTTPConnection("127.0.0.1", config.dashboard.listen_port, timeout=5)
            conn.request(method, target)
            response = conn.getresponse()
            body = response.read()
            assert response.status == status
            assert "default-src 'none'" in response.getheader("Content-Security-Policy")
            if method == "GET":
                assert b"plan is drafted" in body
            if method == "HEAD":
                assert body == b""
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
