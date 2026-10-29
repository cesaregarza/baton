"""The remote CLI carries a reply's thread and attention as far as an MCP client does.

`baton --host …` builds the tool call itself, so every argument the local command accepts has to be
forwarded there too, or the command reports success and the fields never reach the recipient. Synthetic
and offline: a real host on a loopback port, generic roles (`operator` is the human), throwaway tokens,
and a recording stand-in for wake delivery.
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from conftest import free_port
from test_conversations import woken
from test_human_seat import SeatHost, local, remote

from baton import config as config_module
from baton.auth import new_token
from baton.store import connect

GROUP = "operator,reviewer,assistant"


@pytest.fixture
def quiet_host(tmp_path: Path) -> Iterator[SeatHost]:
    """A host where `operator` is the human and `assistant` is woken only when a message addresses it."""
    from baton.app import create_app

    port = free_port()
    tokens = {role: new_token() for role in ("operator", "reviewer", "assistant")}
    entries = "".join(f'[[auth.tokens]]\nname = "{role}-token"\nrole = "{role}"\nsha256 = "{digest}"\n'
                      for role, (_, digest) in tokens.items())
    path = tmp_path / "baton.toml"
    path.write_text(textwrap.dedent(f"""
        [host]
        public_url = "http://127.0.0.1:{port}"
        listen_port = {port}
        data_dir = "{tmp_path / 'data'}"

        [roles.operator]
        description = "The person running this host"
        human = true

        [roles.reviewer]
        description = "An assistant woken for everything"
        [roles.reviewer.wake]
        command = ["true"]

        [roles.assistant]
        description = "An assistant woken only when a message addresses it"
        can_create_conversations = true
        [roles.assistant.wake]
        command = ["true"]
        only_when_addressed = true

        [conversations]
        enabled = true
    """) + entries)
    config = config_module.load(path)
    server = uvicorn.Server(uvicorn.Config(create_app(config), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    yield SeatHost(config, path, f"http://127.0.0.1:{port}", {role: token for role, (token, _) in tokens.items()})
    server.should_exit = True


def stored(db: sqlite3.Connection, text: str) -> list[tuple[Any, ...]]:
    rows = db.execute("SELECT recipient, reply_to, attention, needs_decision, author_kind FROM messages"
                      " WHERE text = ? ORDER BY recipient", (text,)).fetchall()
    return [(row["recipient"], row["reply_to"], json.loads(row["attention"]) if row["attention"] else None,
             row["needs_decision"], row["author_kind"]) for row in rows]


def test_a_remote_reply_keeps_its_parent_its_attention_and_its_decision_mark(quiet_host, capsys,
                                                                            monkeypatch) -> None:
    host = quiet_host
    code, made = local(host, capsys, "assistant", "conversation", "new", "--title", "Release plan",
                       "--with", GROUP)
    assert code == 0, made
    conversation_id = made["conversation_id"]
    code, parent = local(host, capsys, "reviewer", "message", "--conversation", conversation_id,
                         "--text", "which release?")
    assert code == 0, parent
    assert woken(host.config) == []  # the quiet role is not woken by a message that names nobody

    code, sent = remote(host, capsys, monkeypatch, "message", "--conversation", conversation_id,
                        "--text", "the next one", "--reply-to", parent["message_id"],
                        "--attention", "assistant", "--needs-decision")
    assert code == 0 and sent["created"] is True, sent

    db = connect(host.config.db_path)
    try:
        assert stored(db, "the next one") == [
            ("assistant", parent["message_id"], ["assistant"], 1, "human"),
            ("reviewer", parent["message_id"], ["assistant"], 1, "human"),
        ]
    finally:
        db.close()

    # The quiet role is woken exactly once, from its first unread, and not again.
    calls = woken(host.config)
    assert sorted((call["role"], call["first_unread_seq"]) for call in calls) == \
        [("assistant", 1), ("reviewer", 1)]
    assert woken(host.config) == []

    code, page = remote(host, capsys, monkeypatch, "conversation", "history", conversation_id)
    assert code == 0, page
    [answer] = [message for message in page["messages"] if message["text"] == "the next one"]
    assert answer["reply_to"] == {"message_id": parent["message_id"], "seq": 1}
    assert (answer["attention"], answer["needs_decision"], answer["author_kind"]) == \
        (["assistant"], True, "human")


def test_a_remote_reply_names_several_roles_for_attention(quiet_host, capsys, monkeypatch) -> None:
    host = quiet_host
    code, made = local(host, capsys, "assistant", "conversation", "new", "--title", "t", "--with", GROUP)
    assert code == 0, made
    code, sent = remote(host, capsys, monkeypatch, "message", "--conversation", made["conversation_id"],
                        "--text", "both of you", "--attention", "reviewer,assistant")
    assert code == 0 and sent["created"] is True, sent
    db = connect(host.config.db_path)
    try:
        assert stored(db, "both of you") == [
            ("assistant", None, ["reviewer", "assistant"], 0, "human"),
            ("reviewer", None, ["reviewer", "assistant"], 0, "human"),
        ]
    finally:
        db.close()


def test_the_remote_cli_still_gets_the_hosts_refusals_for_the_three(quiet_host, capsys, monkeypatch) -> None:
    """Forwarding them must not slip past the host's rules: a direct send carries none of the three."""
    host = quiet_host
    code, made = local(host, capsys, "assistant", "conversation", "new", "--title", "t", "--with", GROUP)
    assert code == 0, made
    for argv in (("--reply-to", "msg_nonexistent"), ("--attention", "reviewer"), ("--needs-decision",)):
        code, refused = remote(host, capsys, monkeypatch, "message", "--to", "reviewer", "--text", "x", *argv)
        assert (code, refused["error"]) == (1, "invalid_request"), (argv, code, refused)
    code, refused = remote(host, capsys, monkeypatch, "message", "--conversation", made["conversation_id"],
                           "--text", "x", "--attention", "nobody")
    assert (code, refused["error"]) == (1, "unknown_role"), refused
    code, refused = remote(host, capsys, monkeypatch, "message", "--conversation", made["conversation_id"],
                           "--text", "x", "--reply-to", "msg_nonexistent")
    assert (code, refused["error"]) == (1, "invalid_request"), refused
    db = connect(host.config.db_path)
    try:
        assert db.execute("SELECT COUNT(*) FROM messages WHERE text = 'x'").fetchone()[0] == 0
    finally:
        db.close()
