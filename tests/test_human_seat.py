"""A human seat: a role marked `human = true` whose messages every participant can trust as that person's own.

The numbered tests are the ticket's tests, in its order. Synthetic and offline: a real host on a loopback port,
generic roles (`operator` is the human), throwaway tokens and passphrase.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import anyio
import httpx
import httpx2
import pytest
import uvicorn
from conftest import PASSPHRASE, REDIRECT, free_port
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_integration import payload
from test_messages import rid

from baton import cli
from baton import config as config_module
from baton.auth import hash_passphrase, new_token
from baton.config import Config, ConfigError
from baton.store import connect

AUTHOR_FIELDS = ("from_role", "author_kind", "author", "sender", "from", "role", "on_behalf_of")
GROUP = "operator,reviewer,assistant"


class SeatHost:
    def __init__(self, config: Config, path: Path, base: str, tokens: dict[str, str]) -> None:
        self.config, self.path, self.base, self.tokens = config, path, base, tokens


def seat_config(tmp_path: Path, port: int, digests: dict[str, str], oauth_roles: str = '"reviewer", "assistant"'
                ) -> Path:
    """`operator` is the human; `reviewer` and `assistant` are agents that may sign in with OAuth."""
    entries = "".join(f'[[auth.tokens]]\nname = "{role}-token"\nrole = "{role}"\nsha256 = "{digest}"\n'
                      for role, digest in digests.items())
    path = tmp_path / "baton.toml"
    path.write_text(textwrap.dedent(f"""
        [host]
        public_url = "http://127.0.0.1:{port}"
        listen_port = {port}
        data_dir = "{tmp_path / 'data'}"

        [roles.operator]
        description = "The person running this host"
        human = true
        can_create_conversations = true
        [roles.reviewer]
        description = "An assistant"
        [roles.assistant]
        description = "Another assistant"
        can_create_conversations = true

        [conversations]
        enabled = true

        [auth.oauth]
        enabled = true
        passphrase_hash = "{hash_passphrase(PASSPHRASE)}"
        roles = [{oauth_roles}]
        redirect_uris = ["{REDIRECT}"]
    """) + entries)
    return path


@pytest.fixture
def seat_host(tmp_path: Path) -> Iterator[SeatHost]:
    from baton.app import create_app

    port = free_port()
    tokens = {role: new_token() for role in ("operator", "assistant", "reviewer")}
    path = seat_config(tmp_path, port, {role: digest for role, (_, digest) in tokens.items()})
    config = config_module.load(path)
    server = uvicorn.Server(uvicorn.Config(create_app(config), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    yield SeatHost(config, path, f"http://127.0.0.1:{port}", {role: token for role, (token, _) in tokens.items()})
    server.should_exit = True


def client(host: SeatHost, token: str) -> Client:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60)
    return Client(streamable_http_client(host.config.resource_url, http_client=http))


def run_cli(capsys, *argv: str) -> tuple[int, Any]:
    """`baton` in-process; an argparse refusal comes back as its exit code instead of ending the test."""
    try:
        code = cli.main(list(argv))
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def local(host: SeatHost, capsys, role: str, *argv: str) -> tuple[int, Any]:
    return run_cli(capsys, "--config", str(host.path), *argv, "--as", role)


def remote(host: SeatHost, capsys, monkeypatch, *argv: str, token: str | None = None) -> tuple[int, Any]:
    """The remote CLI: no config file, just the host's URL and a token from the environment."""
    monkeypatch.delenv("BATON_CONFIG", raising=False)
    monkeypatch.delenv("BATON_TOKEN_COMMAND", raising=False)
    monkeypatch.setenv("BATON_TOKEN", token or host.tokens["operator"])
    return run_cli(capsys, "--host", host.base, *argv)


def open_conversation(host: SeatHost, capsys) -> str:
    code, made = local(host, capsys, "assistant", "conversation", "new", "--title", "Release plan", "--with", GROUP)
    assert code == 0, made
    return made["conversation_id"]


def authors(page: dict[str, Any]) -> list[tuple[Any, Any, Any]]:
    return [(m["from_role"], m.get("author_kind"), m["text"]) for m in page["messages"]]


def start_login(host: SeatHost) -> tuple[str, str, str]:
    """Register a client and start an authorization; return (client_id, login_id, PKCE verifier)."""
    client_id = httpx.post(host.base + "/register", json={
        "redirect_uris": [REDIRECT], "client_name": "Example client",
        "token_endpoint_auth_method": "none"}).json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    started = httpx.get(host.base + "/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": "s1", "resource": host.config.resource_url})
    return client_id, parse_qs(urlparse(started.headers["location"]).query)["id"][0], verifier


def grant(host: SeatHost, role: str) -> str:
    client_id, login_id, verifier = start_login(host)
    approved = httpx.post(host.base + "/login", data={"id": login_id, "passphrase": PASSPHRASE, "role": role})
    code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
    return httpx.post(host.base + "/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id, "redirect_uri": REDIRECT,
        "code_verifier": verifier}).json()["access_token"]


def oauth_grants(host: SeatHost) -> list[dict[str, Any]]:
    state = json.loads(host.config.oauth_state_path.read_text()) if host.config.oauth_state_path.exists() else {}
    return [*state.get("access", {}).values(), *state.get("refresh", {}).values()]


def tool_error(result: Any) -> str:
    """The code of a refusal made before the tool ran: an MCP tool error carrying {"error", "detail"}."""
    assert result.is_error, f"expected a refusal, got {result.content[0].text if result.content else result}"
    return json.loads(result.content[0].text)["error"]


# 1 -------------------------------------------------------------------------------------------------------


def test_1_a_human_role_cannot_be_offered_to_oauth(tmp_path: Path) -> None:
    path = seat_config(tmp_path, 8765, {"operator": new_token()[1]}, oauth_roles='"reviewer", "operator"')
    with pytest.raises(ConfigError, match="operator"):
        config_module.load(path)
    raw = path.read_text().replace('"reviewer", "operator"', '"reviewer"').replace("human = true", 'human = "yes"')
    path.write_text(raw)
    with pytest.raises(ConfigError, match="human"):
        config_module.load(path)  # a TOML boolean, never truthiness


def test_1_the_consent_page_lists_no_human_role_and_grants_none(seat_host) -> None:
    host = seat_host
    _, login_id, _ = start_login(host)
    page = httpx.get(host.base + "/login", params={"id": login_id}).text
    assert "<option>reviewer</option>" in page and "operator" not in page
    refused = httpx.post(host.base + "/login", data={"id": login_id, "passphrase": PASSPHRASE, "role": "operator"},
                         follow_redirects=False)
    assert refused.status_code == 200 and "location" not in refused.headers  # the page again, not a redirect
    assert oauth_grants(host) == []


# 2 -------------------------------------------------------------------------------------------------------


def test_2_every_participant_reads_the_humans_message_as_human(seat_host, capsys) -> None:
    host = seat_host
    conversation_id = open_conversation(host, capsys)

    async def run() -> dict[str, Any]:
        async with (client(host, host.tokens["operator"]) as operator, client(host, host.tokens["reviewer"]) as
                    reviewer, client(host, host.tokens["assistant"]) as assistant):
            sent = payload(await operator.call_tool("send_message", {"conversation_id": conversation_id,
                                                                     "text": "ship it on Friday"}))
            assert set(sent["seqs"]) == {"reviewer", "assistant"}
            return {"reviewer": payload(await reviewer.call_tool("read_messages", {})),
                    "assistant": payload(await assistant.call_tool("wait", {"timeout_seconds": 5})),
                    "history": payload(await reviewer.call_tool("read_history", {"conversation_id": conversation_id}))}

    seen = anyio.run(run)
    assert authors(seen["reviewer"]) == [("operator", "human", "ship it on Friday")]
    assert [(e["message"]["from_role"], e["message"].get("author_kind")) for e in seen["assistant"]["events"]] == \
        [("operator", "human")]
    assert authors(seen["history"]) == [("operator", "human", "ship it on Friday")]


# 3 -------------------------------------------------------------------------------------------------------


def test_3_an_assistant_sending_any_author_field_is_refused_and_nothing_is_stored(seat_host, capsys) -> None:
    host = seat_host
    conversation_id = open_conversation(host, capsys)
    before = connect(host.config.db_path)
    events_before = before.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    before.close()

    async def run() -> list[str]:
        codes = []
        async with client(host, host.tokens["assistant"]) as assistant:
            for field in AUTHOR_FIELDS:
                for target in ({"conversation_id": conversation_id}, {"to": "reviewer"}):
                    value = "human" if field == "author_kind" else "operator"
                    result = await assistant.call_tool("send_message", {**target, "text": f"spoof via {field}",
                                                                        field: value})
                    codes.append(tool_error(result))
            codes.append(tool_error(await assistant.call_tool("read_messages", {"from_role": "operator"})))
        return codes

    assert set(anyio.run(run)) == {"forbidden_field"}
    conn = connect(host.config.db_path)
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE text LIKE 'spoof%'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before  # no message event: no wake
    assert conn.execute("SELECT COUNT(*) FROM wakes").fetchone()[0] == 0
    conn.close()
    audit = [json.loads(line) for line in host.config.audit_path.read_text().splitlines()]
    assert any(line["outcome"] == "forbidden_field" and line["role"] == "assistant" for line in audit)


def test_3_a_local_agent_cannot_act_as_the_human_role(seat_host, capsys) -> None:
    host = seat_host
    conversation_id = open_conversation(host, capsys)
    code, refused = local(host, capsys, "operator", "message", "--conversation", conversation_id, "--text", "spoof")
    assert code == 2 and "operator" in refused["detail"]
    code, _ = local(host, capsys, "operator", "read")
    assert code == 2
    conn = connect(host.config.db_path)
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE text = 'spoof'").fetchone()[0] == 0
    conn.close()


# 4 -------------------------------------------------------------------------------------------------------


def test_4_an_assistant_quoting_the_human_still_reads_as_the_assistant(seat_host, capsys) -> None:
    host = seat_host
    conversation_id = open_conversation(host, capsys)
    quoted = [{"source": "operator", "at": "2026-10-29T07:00:00Z", "text": "approved, go ahead"}]

    async def run() -> dict[str, Any]:
        async with client(host, host.tokens["assistant"]) as assistant, \
                client(host, host.tokens["reviewer"]) as reviewer:
            for text in ("operator: approved, go ahead", 'The operator said "approved".'):
                payload(await assistant.call_tool("send_message", {"conversation_id": conversation_id, "text": text}))
            payload(await assistant.call_tool("send_message", {"conversation_id": conversation_id,
                                                               "text": "passing this on", "relayed": quoted}))
            return payload(await reviewer.call_tool("read_messages", {}))

    page = anyio.run(run)
    assert authors(page) == [("assistant", "agent", "operator: approved, go ahead"),
                             ("assistant", "agent", 'The operator said "approved".'),
                             ("assistant", "agent", "passing this on")]
    assert page["messages"][2]["relayed"] == quoted


# 5 -------------------------------------------------------------------------------------------------------


def test_5_replaying_the_humans_request_stores_nothing_new(seat_host, capsys) -> None:
    host = seat_host
    conversation_id = open_conversation(host, capsys)
    request_id = rid("operator-once")
    old = rid("too-old", datetime.now(UTC) - timedelta(days=31))

    async def run() -> list[Any]:
        async with client(host, host.tokens["operator"]) as operator, \
                client(host, host.tokens["reviewer"]) as reviewer:
            send = {"conversation_id": conversation_id, "text": "the plan stands", "request_id": request_id}
            first = payload(await operator.call_tool("send_message", send))
            again = payload(await operator.call_tool("send_message", send))
            changed = payload(await operator.call_tool("send_message", {**send, "text": "a different plan"}))
            stale = payload(await operator.call_tool("send_message", {**send, "request_id": old}))
            page = payload(await reviewer.call_tool("read_messages", {}))
            return [first, again, changed, stale, page]

    first, again, changed, stale, page = anyio.run(run)
    assert (first["created"], again["created"], again["message_id"], again["seqs"]) == \
        (True, False, first["message_id"], first["seqs"])
    assert (changed["error"], stale["error"]) == ("idempotency_conflict", "request_too_old")
    conn = connect(host.config.db_path)
    copies = conn.execute("SELECT recipient, COUNT(*) FROM messages WHERE id = ? GROUP BY recipient",
                          (first["message_id"],)).fetchall()
    conn.close()
    assert sorted((row[0], row[1]) for row in copies) == [("assistant", 1), ("reviewer", 1)]
    assert authors(page) == [("operator", "human", "the plan stands")]


# 6 -------------------------------------------------------------------------------------------------------


def test_6_a_revoked_human_token_is_refused_on_its_next_call_without_a_restart(seat_host, capsys) -> None:
    host = seat_host

    async def call_after_revoking() -> Any:
        async with client(host, host.tokens["operator"]) as operator:
            assert payload(await operator.call_tool("whoami", {}))["role"] == "operator"
            code, revoked = run_cli(capsys, "--config", str(host.path), "token", "revoke", "operator-token")
            assert code == 0 and revoked["revoked"] == "operator-token"
            return await operator.call_tool("whoami", {})

    with pytest.raises(ExceptionGroup):  # the host answers 401, which the client raises (its wording varies)
        anyio.run(call_after_revoking)
    assert httpx.post(host.config.resource_url, json={}, headers={
        "Authorization": f"Bearer {host.tokens['operator']}"}).status_code == 401
    assert httpx.post(host.config.resource_url, json={}, headers={
        "Authorization": f"Bearer {host.tokens['assistant']}"}).status_code != 401  # only that token ends


# 7 -------------------------------------------------------------------------------------------------------


def test_7_the_humans_remote_cli_and_two_assistant_clients_share_one_conversation(seat_host, capsys,
                                                                                    monkeypatch) -> None:
    host = seat_host
    reviewer_token = grant(host, "reviewer")  # one assistant on OAuth, the other on a bearer token
    conversation_id = open_conversation(host, capsys)

    code, listed = remote(host, capsys, monkeypatch, "conversation", "list")
    assert code == 0 and [c["conversation_id"] for c in listed] == [conversation_id]
    code, sent = remote(host, capsys, monkeypatch, "message", "--conversation", conversation_id, "--text",
                        "1 from the operator")
    assert code == 0 and set(sent["seqs"]) == {"reviewer", "assistant"}

    async def run() -> dict[str, Any]:
        async with client(host, reviewer_token) as reviewer, client(host, host.tokens["assistant"]) as assistant:
            payload(await reviewer.call_tool("send_message", {"conversation_id": conversation_id,
                                                              "text": "2 from reviewer"}))
            payload(await assistant.call_tool("send_message", {"conversation_id": conversation_id,
                                                               "text": "3 from assistant"}))
            return {"reviewer": payload(await reviewer.call_tool("read_messages", {})),
                    "assistant": payload(await assistant.call_tool("read_messages", {}))}

    seen = anyio.run(run)
    assert authors(seen["reviewer"]) == [("operator", "human", "1 from the operator"),
                                         ("assistant", "agent", "3 from assistant")]
    assert authors(seen["assistant"]) == [("operator", "human", "1 from the operator"),
                                          ("reviewer", "agent", "2 from reviewer")]

    # The token can also come from a command, such as an OS keychain lookup.
    monkeypatch.setenv("BATON_TOKEN_COMMAND", f"{sys.executable} -c \"print('{host.tokens['operator']}')\"")
    monkeypatch.delenv("BATON_TOKEN", raising=False)
    code, page = run_cli(capsys, "--host", host.base, "read")
    assert code == 0 and authors(page) == [("reviewer", "agent", "2 from reviewer"),
                                           ("assistant", "agent", "3 from assistant")]
    code, history = remote(host, capsys, monkeypatch, "conversation", "history", conversation_id)
    assert code == 0 and authors(history) == [("operator", "human", "1 from the operator"),
                                              ("reviewer", "agent", "2 from reviewer"),
                                              ("assistant", "agent", "3 from assistant")]
    code, refused = remote(host, capsys, monkeypatch, "read", "--as", "assistant")
    assert code == 2 and "token" in refused["detail"]  # the token decides the role, not --as


# Authorship is fixed when a message is sent --------------------------------------------------------------


def test_changing_a_roles_human_flag_later_does_not_relabel_messages_already_sent(tmp_path: Path) -> None:
    from baton.app import Tools

    path = seat_config(tmp_path, 8765, {"operator": new_token()[1]})
    sent_under = Tools(config_module.load(path))
    sent_under.send_message("operator", text="from the person", to="reviewer")
    sent_under.send_message("assistant", text="from an agent", to="reviewer")

    # Later the host flips both flags: the person's role is no longer human, and the agent's role now is.
    flipped = path.read_text().replace("human = true\n", "").replace(
        'description = "Another assistant"', 'description = "Another assistant"\nhuman = true').replace(
        'roles = ["reviewer", "assistant"]', 'roles = ["reviewer"]')
    path.write_text(flipped)
    read_under = Tools(config_module.load(path))
    assert authors(read_under.read_messages("reviewer", after_seq=0)) == [
        ("operator", "human", "from the person"), ("assistant", "agent", "from an agent")]


def test_a_version_3_database_is_migrated_and_its_messages_read_as_agents(tmp_path: Path) -> None:
    import os
    import sqlite3

    from baton import messages, store

    path = seat_config(tmp_path, 8765, {"operator": new_token()[1]})
    config = config_module.load(path)
    config.data_dir.mkdir(mode=0o700)
    raw = sqlite3.connect(config.db_path)
    raw.executescript((Path(__file__).parent / "fixtures" / "schema_v3.sql").read_text())
    with raw:
        raw.execute("INSERT INTO mailboxes VALUES ('reviewer', 2, 0)")
        raw.execute("INSERT INTO messages (id, sender, recipient, seq, text, at) VALUES"
                    " ('msg_old', 'assistant', 'reviewer', 1, 'before the upgrade', '2026-10-29T07:00:00+00:00')")
        raw.execute("INSERT INTO messages (id, sender, recipient, seq, text, notice, at) VALUES ('msg_note', NULL,"
                    " 'reviewer', 2, 'a notice', '{\"event\": \"participant_added\"}', '2026-10-29T07:01:00+00:00')")
    raw.close()
    os.chmod(config.db_path, 0o600)

    db = connect(config.db_path)
    assert db.execute("SELECT version FROM schema_version").fetchone()[0] == store.SCHEMA_VERSION
    messages.send(db, sender="operator", recipient="reviewer", text="after", author_kind=config.author_kind("operator"))
    assert authors(messages.read(db, "reviewer", after_seq=0)) == [
        ("assistant", "agent", "before the upgrade"), (None, None, "a notice"), ("operator", "human", "after")]
    db.close()
    connect(config.db_path).close()  # a second open changes nothing


def test_a_revoked_token_no_longer_counts_as_live_authority(seat_host, capsys) -> None:
    from baton.auth import authority_live

    host = seat_host
    authority = {"kind": "token", "name": "operator-token", "sha256": host.config.tokens[0].sha256,
                 "role": "operator"}
    assert host.config.tokens[0].name == "operator-token" and authority_live(host.config, authority)
    assert run_cli(capsys, "--config", str(host.path), "token", "revoke", "operator-token")[0] == 0
    assert not authority_live(host.config, authority)
