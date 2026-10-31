"""Guest posting integration and the retained-history demo, through in-process MCP dispatch only."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from test_aliases import (
    dispatch,
    guest_host,  # noqa: F401 -- synthetic credential fixture
    payload,
    rpc,
)

from baton import app, messages, store
from baton.config import Role


@pytest.fixture
def posting_host(guest_host):  # noqa: F811 -- imported pytest fixture
    config = guest_host.config
    roles = {
        **config.roles,
        "planner": replace(config.roles["planner"], can_create_conversations=True),
        "guest": Role(name="guest", can_view_all_conversations=True),
    }
    guest_host.config = replace(
        config,
        roles=roles,
        conversations=replace(config.conversations, enabled=True),
        search=replace(config.search, enabled=True),
    )
    return guest_host


async def call(client, host, name, arguments=None, role="guest"):
    return payload(await rpc(client, host, "tools/call", {"name": name, "arguments": arguments or {}}, role=role))


async def board(client, host, topic="reviews"):
    return (
        await call(
            client,
            host,
            "create_conversation",
            {"title": "Reviews", "participants": ["planner", "worker"], "topic": topic},
            role="planner",
        )
    )["conversation_id"]


async def claim(client, host, name="lane-a"):
    return await call(client, host, "claim_alias", {"name": name, "passphrase": "synthetic alias secret"})


def identity(result):
    return {key: result[key] for key in ("alias_id", "alias", "badge", "code")}


@pytest.mark.parametrize("human_flag", [False, True])
def test_guest_post_uses_bound_alias_on_all_message_read_results(posting_host, human_flag):
    config = posting_host.config
    posting_host.config = replace(
        config, roles={**config.roles, "guest": replace(config.roles["guest"], human=human_flag)}
    )

    async def scenario(client):
        conv = await board(client, posting_host)
        alias = await claim(client, posting_host)
        sent = await call(
            client,
            posting_host,
            "send_message",
            {"conversation_id": conv, "text": "synthetic review", "token": alias["binding_token"]},
        )
        assert sent.get("created") is True, f"a valid bound guest post must succeed: {sent}"
        history = await call(client, posting_host, "read_history", {"conversation_id": conv})
        assert identity(history["messages"][0]) == identity(alias)
        found = await call(client, posting_host, "get_message", {"message_id": sent["message_id"]})
        assert identity(found) == identity(alias)
        hits = await call(client, posting_host, "search", {"query": "synthetic"})
        assert identity(hits["hits"][0]) == identity(alias)
        assert found["from_role"] == "guest" and found["author_kind"] == "agent"

    dispatch(posting_host, scenario)


def test_two_sessions_fresh_joiner_and_reconnect_demo(posting_host, monkeypatch):
    from baton import auth

    cursors = {"a": 0, "b": 0}

    async def scenario(client):
        conv = await board(client, posting_host)
        retained = await call(
            client, posting_host, "send_message", {"conversation_id": conv, "text": "earlier context"}, role="planner"
        )
        a = await claim(client, posting_host, "lane-a")
        b = await claim(client, posting_host, "lane-b")
        assert (await call(client, posting_host, "list_conversations", {"topic": "reviews"}))["conversations"][0][
            "conversation_id"
        ] == conv
        for session in cursors:
            page = await call(client, posting_host, "read_history", {"conversation_id": conv, "after_seq": 0})
            assert page["messages"][0]["message_id"] == retained["message_id"]
            cursors[session] = page["next_seq"]
        assert cursors == {"a": 1, "b": 1}
        a_id = messages.new_request_id()
        sent = await call(
            client,
            posting_host,
            "send_message",
            {
                "conversation_id": conv,
                "text": "alias a review",
                "reply_to": retained["message_id"],
                "token": a["binding_token"],
                "request_id": a_id,
            },
        )
        replied = await call(
            client,
            posting_host,
            "send_message",
            {
                "conversation_id": conv,
                "text": "alias b reply",
                "reply_to": sent["message_id"],
                "token": b["binding_token"],
            },
        )
        page_b = await call(client, posting_host, "read_history", {"conversation_id": conv, "after_seq": cursors["b"]})
        cursors["b"] = page_b["next_seq"]
        assert cursors == {"a": 1, "b": 3}
        assert identity(page_b["messages"][0]) == identity(a)
        assert identity(page_b["messages"][1]) == identity(b)
        assert identity(page_b["messages"][1]["reply_to"]) == identity(a)
        assert page_b["messages"][1]["message_id"] == replied["message_id"]
        page_a = await call(client, posting_host, "read_history", {"conversation_id": conv, "after_seq": cursors["a"]})
        assert [message["message_id"] for message in page_a["messages"]] == [sent["message_id"], replied["message_id"]]
        assert (await call(client, posting_host, "claim_alias", {"name": "lane-a", "passphrase": "wrong"}))[
            "error"
        ] == "forbidden"
        c = await claim(client, posting_host, "lane-c")
        late = await call(client, posting_host, "read_history", {"conversation_id": conv, "after_seq": 0})
        assert len(late["messages"]) == 3  # pre-join context came from Baton, no bespoke handoff
        assert (
            await call(
                client,
                posting_host,
                "send_message",
                {"conversation_id": conv, "text": "fresh contribution", "token": c["binding_token"]},
            )
        )["created"]
        resumed = await claim(client, posting_host, "lane-a")
        assert identity(resumed) == identity(a)
        # Once bound, posting/retry must use the proof lookup, never scrypt.
        monkeypatch.setattr(auth, "hash_passphrase", lambda *args, **kwargs: pytest.fail("posting reached scrypt"))
        retried = await call(
            client,
            posting_host,
            "send_message",
            {
                "conversation_id": conv,
                "text": "alias a review",
                "reply_to": retained["message_id"],
                "token": resumed["binding_token"],
                "request_id": a_id,
            },
        )
        assert retried["created"] is False and retried["message_id"] == sent["message_id"]
        wrong_identity = await call(
            client,
            posting_host,
            "send_message",
            {
                "conversation_id": conv,
                "text": "alias a review",
                "reply_to": retained["message_id"],
                "token": b["binding_token"],
                "request_id": a_id,
            },
        )
        assert wrong_identity["error"] == "idempotency_conflict"
        assert (await call(client, posting_host, "wait", {"timeout_seconds": 0, "token": a["binding_token"]}))[
            "error"
        ] == "forbidden"

    dispatch(posting_host, scenario)
    conn = store.connect(posting_host.config.db_path)
    try:
        for table, column in (("mailboxes", "recipient"), ("participants", "role"), ("wakes", "role")):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = 'guest'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cursors WHERE name = 'wait:guest'").fetchone()[0] == 0
    finally:
        conn.close()


def test_invalid_proofs_are_safe_before_replay_and_never_fallback(posting_host, monkeypatch, caplog):
    from baton import events

    proofs = []

    async def scenario(client):
        conv = await board(client, posting_host)
        a = await claim(client, posting_host)
        proofs.append(a["binding_token"])
        request_id = messages.new_request_id()
        assert (
            await call(
                client,
                posting_host,
                "send_message",
                {"conversation_id": conv, "text": "first", "token": a["binding_token"], "request_id": request_id},
            )
        )["created"]
        monkeypatch.setattr(events, "replayed", lambda *args, **kwargs: pytest.fail("invalid proof reached replay"))
        conn = store.connect(posting_host.config.db_path)
        try:
            conn.execute("UPDATE alias_bindings SET expires_at = 0")
        finally:
            conn.close()
        for proof in (None, "", "unrecognized", "\ud800", "\udfff", [a["binding_token"]], a["binding_token"]):
            response = await rpc(
                client,
                posting_host,
                "tools/call",
                {
                    "name": "send_message",
                    "arguments": {"conversation_id": conv, "text": "first", "request_id": request_id, "token": proof},
                },
            )
            assert payload(response)["error"] == "forbidden"
            assert a["binding_token"] not in json.dumps(response)

    dispatch(posting_host, scenario)
    assert not any(record.exc_info for record in caplog.records)
    assert proofs[0] not in caplog.text and proofs[0] not in posting_host.config.audit_path.read_text()


def test_valid_proof_does_not_allow_guest_authority_or_forged_identity(posting_host):
    async def scenario(client):
        conv = await board(client, posting_host)
        bound = await claim(client, posting_host)
        proof = bound["binding_token"]
        for fields in (
            {"to": "worker"},
            {"to": "worker", "conversation_id": conv},
            {"conversation_id": conv, "job_id": "job_fake"},
            {"conversation_id": conv, "needs_decision": True},
        ):
            assert (await call(client, posting_host, "send_message", {"text": "refused", "token": proof, **fields}))[
                "error"
            ] == "forbidden"
        for field in messages.AUTHOR_FIELDS:
            assert (
                await call(
                    client,
                    posting_host,
                    "send_message",
                    {"text": "refused", "conversation_id": conv, "token": proof, field: "forged"},
                )
            )["error"] == "forbidden_field"
        listed = (await rpc(client, posting_host, "tools/list", {}))["result"]["tools"]
        assert (
            "identity"
            not in next(tool for tool in listed if tool["name"] == "send_message")["inputSchema"]["properties"]
        )
        allowed = {
            "whoami",
            "alias_exists",
            "claim_alias",
            "list_conversations",
            "read_history",
            "get_message",
            "search",
            "send_message",
        }
        assert app.GUEST_TOOLS == allowed
        for tool in listed:
            if tool["name"] in allowed:
                continue
            arguments = {
                key: {
                    "string": "synthetic",
                    "integer": 0,
                    "number": 0,
                    "boolean": False,
                    "array": [],
                    "object": {},
                }.get(spec.get("type"), "synthetic")
                for key, spec in tool["inputSchema"].get("properties", {}).items()
                if key in tool["inputSchema"].get("required", [])
            }
            for extra in ({}, {"token": proof}):
                assert (await call(client, posting_host, tool["name"], {**arguments, **extra}))["error"] == "forbidden"

    dispatch(posting_host, scenario)


def test_post_limits_are_layered_and_never_escalate(posting_host):
    config = posting_host.config
    posting_host.config = replace(
        config,
        aliases=replace(config.aliases, max_posts_per_alias_per_hour=1, max_posts_per_credential_per_hour=3),
        conversations=replace(config.conversations, max_messages_per_hour=100, escalation_role="worker"),
    )

    async def scenario(client):
        conv = await board(client, posting_host)
        bindings = [await claim(client, posting_host, name) for name in ("lane-a", "lane-b", "lane-c", "lane-d")]

        async def post(bound):
            return await call(
                client,
                posting_host,
                "send_message",
                {"conversation_id": conv, "text": "limited", "token": bound["binding_token"]},
            )

        assert (await post(bindings[0]))["created"]
        assert (await post(bindings[0]))["limit"] == "posts_per_alias"
        assert (await post(bindings[1]))["created"]  # another alias is not blocked by the first one's ceiling
        assert (await post(bindings[2]))["created"]
        assert (await post(bindings[3]))["limit"] == "posts_per_credential"

    dispatch(posting_host, scenario)
    conn = store.connect(posting_host.config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'conversation.rate_limited'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM alias_posts").fetchone()[0] == 3
    finally:
        conn.close()


def test_guest_conversation_ceiling_never_pages_escalation_role(posting_host):
    config = posting_host.config
    posting_host.config = replace(
        config, conversations=replace(config.conversations, max_messages_per_hour=1, escalation_role="worker")
    )

    async def scenario(client):
        conv = await board(client, posting_host)
        bound = await claim(client, posting_host)
        for expected in (True, False):
            result = await call(
                client,
                posting_host,
                "send_message",
                {"conversation_id": conv, "text": "limit", "token": bound["binding_token"]},
            )
            if expected:
                assert result["created"]
            else:
                assert result["error"] == "conversation_rate_limited"

    dispatch(posting_host, scenario)
    conn = store.connect(posting_host.config.db_path)
    try:
        assert conn.execute("SELECT escalated_at FROM conversations").fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'conversation.rate_limited'").fetchone()[0] == 0
    finally:
        conn.close()


def test_repeated_posts_never_accumulate_guest_delivery_state(posting_host):
    config = posting_host.config
    posting_host.config = replace(
        config,
        aliases=replace(config.aliases, max_posts_per_alias_per_hour=100),
        conversations=replace(config.conversations, max_messages_per_hour=100),
    )
    sent = []

    async def scenario(client):
        conv = await board(client, posting_host)
        bound = await claim(client, posting_host)
        for index in range(40):
            result = await call(
                client,
                posting_host,
                "send_message",
                {"conversation_id": conv, "text": f"post {index}", "token": bound["binding_token"]},
            )
            assert result["created"]
            sent.append(result)

    dispatch(posting_host, scenario)
    conn = store.connect(posting_host.config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE recipient = 'guest'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mailboxes WHERE recipient = 'guest'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 80
        assert {tuple(row) for row in conn.execute("SELECT recipient, last_seq, read_seq FROM mailboxes")} == {
            ("planner", 40, 0),
            ("worker", 40, 0),
        }
        assert conn.execute("SELECT COUNT(*) FROM participants WHERE role='guest'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM wakes WHERE role='guest'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cursors WHERE name='wait:guest'").fetchone()[0] == 0
    finally:
        conn.close()


def test_schema_12_migrates_forward_preserving_aliases_and_existing_messages(posting_host):
    from baton import aliases, auth

    db_path = posting_host.config.db_path
    db_path.parent.mkdir(mode=0o700)
    raw = sqlite3.connect(db_path)
    raw.executescript((Path(__file__).parent / "fixtures" / "schema_v12.sql").read_text())
    proof = "synthetic-existing-proof"
    with raw:
        raw.execute(
            "INSERT INTO aliases VALUES ('immutable-alias', 'old-alias', 'old-alias', ?, 1)",
            (auth.hash_passphrase("synthetic"),),
        )
        raw.execute(
            "INSERT INTO alias_bindings VALUES (?, 'immutable-alias', 99999999999)", (auth.token_digest(proof),)
        )
        raw.execute("INSERT INTO mailboxes VALUES ('worker', 1, 0)")
        raw.execute(
            "INSERT INTO messages (id,sender,author_kind,recipient,seq,text,at)"
            " VALUES ('msg_old','planner','agent','worker',1,'legacy','2026-10-31T00:00:00+00:00')"
        )
    raw.close()
    db_path.chmod(0o600)
    conn = store.connect(db_path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 13
        assert {row["name"] for row in conn.execute("PRAGMA table_info(messages)")} >= {
            "alias_id",
            "alias",
            "alias_code",
        }
        assert tuple(conn.execute("SELECT seq,text,alias_id,alias,alias_code FROM messages").fetchone()) == (
            1,
            "legacy",
            None,
            None,
            None,
        )
        principal = auth.static_token(posting_host.config, posting_host.tokens["guest"])
        assert aliases.resolve_binding(conn, principal, proof)["alias_id"] == "immutable-alias"
        assert (
            aliases.claim(conn, posting_host.config, principal, "old-alias", "synthetic")["alias_id"]
            == "immutable-alias"
        )
    finally:
        conn.close()
    store.connect(db_path).close()


def test_guest_replies_require_retained_same_conversation_and_board_visibility(posting_host):
    async def scenario(client):
        conv = await board(client, posting_host)
        other = await board(client, posting_host, "other")
        parent = await call(
            client, posting_host, "send_message", {"conversation_id": other, "text": "elsewhere"}, role="planner"
        )
        bound = await claim(client, posting_host)
        fields = {"conversation_id": conv, "text": "reply", "token": bound["binding_token"]}
        assert (await call(client, posting_host, "send_message", {**fields, "reply_to": parent["message_id"]}))[
            "error"
        ] == "invalid_request"
        conn = store.connect(posting_host.config.db_path)
        try:
            conn.execute("DELETE FROM messages WHERE id = ?", (parent["message_id"],))
        finally:
            conn.close()
        assert (
            await call(
                client,
                posting_host,
                "send_message",
                {**fields, "conversation_id": other, "reply_to": parent["message_id"]},
            )
        )["error"] == "invalid_request"

    dispatch(posting_host, scenario)
    config = posting_host.config
    posting_host.config = replace(config, roles={**config.roles, "guest": Role(name="guest")})

    async def hidden(client):
        bound = await claim(client, posting_host, "no-board-access")
        conn = store.connect(posting_host.config.db_path)
        conv = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()[0]
        conn.close()
        assert (
            await call(
                client,
                posting_host,
                "send_message",
                {"conversation_id": conv, "text": "hidden", "token": bound["binding_token"]},
            )
        )["error"] == "forbidden"

    dispatch(posting_host, hidden)


def test_integration_methods_keep_guest_boundary_with_and_without_alias_proof(posting_host):
    from baton.integrations import openai_mcp_events

    integration = openai_mcp_events.McpEvents(
        posting_host.config,
        openai_mcp_events.parse_settings(
            {"enabled": True, "callback_prefixes": ["https://callbacks.example.test/hooks"]}
        ),
    )
    integration._validated = lambda params: pytest.fail("guest reached callback/network validation")
    registered = set()
    original = integration.register

    def capture(server):
        low = server._lowlevel_server
        add = low.add_request_handler

        def register(method, params_type, handler):
            registered.add(method)
            return add(method, params_type, handler)

        low.add_request_handler = register
        try:
            original(server)
        finally:
            low.add_request_handler = add

    integration.register = capture

    async def scenario(client):
        proof = (await claim(client, posting_host))["binding_token"]
        assert registered == {"events/list", "events/subscribe", "events/unsubscribe"}
        assert (await rpc(client, posting_host, "server/discover", {}))["result"]
        assert (await rpc(client, posting_host, "events/list", {}))["result"]["events"]
        for extra in ({}, {"token": proof}):
            for method, delivery in (
                ("events/subscribe", {"mode": "webhook", "url": "bad", "secret": "bad"}),
                ("events/unsubscribe", {"url": "bad"}),
            ):
                outcome = await rpc(
                    client,
                    posting_host,
                    method,
                    {"name": openai_mcp_events.EVENT_NAME, "arguments": extra, "delivery": delivery},
                )
                assert outcome["error"]["code"] == -32600

    dispatch(posting_host, scenario, {"openai_mcp_events": integration})
    assert not (posting_host.config.data_dir / openai_mcp_events.STATE_FILE).exists()


def test_post_audit_and_replay_use_immutable_id_without_secrets(posting_host, monkeypatch, caplog):
    from baton import events

    audited = []
    hashed = []
    original = events.request_digest

    def digest(operation, body):
        hashed.append(body)
        return original(operation, body)

    async def scenario(client):
        conv = await board(client, posting_host)
        bound = await claim(client, posting_host)
        monkeypatch.setattr(events, "request_digest", digest)
        result = await call(
            client,
            posting_host,
            "send_message",
            {"conversation_id": conv, "text": "post", "token": bound["binding_token"]},
        )
        assert result["created"]
        assert hashed[-1]["alias_id"] == bound["alias_id"]
        assert bound["binding_token"] not in json.dumps(hashed)
        # Even refused raw calls must not audit secret-bearing unvalidated arguments.
        assert (
            await call(
                client,
                posting_host,
                "send_message",
                {
                    "text": "bad",
                    "conversation_id": conv,
                    "token": bound["binding_token"],
                    "identity": "forged",
                    "request_id": bound["binding_token"],
                },
            )
        )["error"] == "forbidden_field"
        assert (
            await call(
                client,
                posting_host,
                "send_message",
                {"conversation_id": bound["binding_token"], "text": "unknown", "token": bound["binding_token"]},
            )
        )["error"] == "unknown_conversation"
        audit = posting_host.config.audit_path.read_text()
        assert bound["binding_token"] not in audit and bound["binding_token"] not in caplog.text
        audited.extend(json.loads(line) for line in audit.splitlines())
        assert any(line.get("alias_id") == bound["alias_id"] and line["outcome"] == "ok" for line in audited)

    dispatch(posting_host, scenario)


def test_portable_proof_posts_with_independent_credential_limits(posting_host):
    from baton import auth
    from baton.config import StaticToken

    raw, digest = auth.new_token()
    config = posting_host.config
    posting_host.config = replace(
        config,
        tokens=(*config.tokens, StaticToken("other-guest", "guest", digest)),
        aliases=replace(config.aliases, max_posts_per_alias_per_hour=2, max_posts_per_credential_per_hour=1),
    )
    posting_host.tokens["other"] = raw

    async def scenario(client):
        conv = await board(client, posting_host)
        bound = await claim(client, posting_host)
        fields = {"conversation_id": conv, "text": "portable", "token": bound["binding_token"]}
        assert (await call(client, posting_host, "send_message", fields))["created"]
        assert (await call(client, posting_host, "send_message", fields))["limit"] == "posts_per_credential"
        assert (await call(client, posting_host, "send_message", fields, role="other"))["created"]
        assert (await call(client, posting_host, "send_message", fields, role="other"))["limit"] == "posts_per_alias"

    dispatch(posting_host, scenario)


def test_mailbox_wait_fallback_search_and_existing_html_show_attribution(posting_host, monkeypatch):
    from datetime import UTC
    from types import SimpleNamespace

    from baton import dashboard, search, seat

    async def scenario(client):
        conv = await board(client, posting_host)
        bound = await claim(client, posting_host)
        sent = await call(
            client,
            posting_host,
            "send_message",
            {"conversation_id": conv, "text": "read projection", "token": bound["binding_token"]},
        )
        inbox = await call(client, posting_host, "read_messages", role="planner")
        assert identity(inbox["messages"][0]) == identity(bound)
        waiting = await call(client, posting_host, "wait", {"timeout_seconds": 0}, role="worker")
        assert identity(waiting["events"][0]["message"]) == identity(bound)
        shown = await call(client, posting_host, "get_message", {"message_id": sent["message_id"]})
        label = f"guest · {bound['alias']} · {bound['code']}"
        assert label in dashboard._message(shown)
        assert label in seat._meta(shown, SimpleNamespace(role="planner", zone=UTC))
        monkeypatch.setattr(search, "keyword_mode", lambda conn: "scan")
        results = await call(client, posting_host, "search", {"query": "projection"})
        assert identity(results["hits"][0]) == identity(bound)

    dispatch(posting_host, scenario)


@pytest.mark.parametrize(
    ("field", "shape"),
    [
        ("text", "list"),
        ("to", "list"),
        ("conversation_id", "list"),
        ("job_id", "list"),
        ("reply_to", "list"),
        ("request_id", "list"),
        ("attention", "dict"),
        ("attention", "nested"),
        ("relayed", "dict"),
        ("relayed", "list"),
        ("needs_decision", "dict"),
        ("needs_decision", "scalar"),
        ("token", "list"),
    ],
)
@pytest.mark.parametrize("role", ["guest", "planner"])
def test_optional_post_validation_never_echoes_binding_proof(posting_host, caplog, field, shape, role):
    async def scenario(client):
        conv = await board(client, posting_host)
        bound = await claim(client, posting_host)
        proof = bound["binding_token"]
        bad = {"list": [proof], "dict": {"secret": proof}, "nested": [[proof]], "scalar": proof}[shape]
        arguments = {"conversation_id": conv, "text": "refused", "token": proof, field: bad}
        response = await rpc(
            client, posting_host, "tools/call", {"name": "send_message", "arguments": arguments}, role=role
        )
        assert proof not in json.dumps(response), "optional-argument validation must not disclose the binding proof"
        assert payload(response)["error"] in {"invalid_request", "forbidden"}
        assert proof not in caplog.text and proof not in posting_host.config.audit_path.read_text()
        conn = store.connect(posting_host.config.db_path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM alias_posts").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM message_requests").fetchone()[0] == 0
        finally:
            conn.close()

    dispatch(posting_host, scenario)


def test_legacy_two_member_conversation_without_delivery_recipient_refuses_atomically(posting_host):
    async def scenario(client):
        conv = await board(client, posting_host)
        conn = store.connect(posting_host.config.db_path)
        tables = (
            "conversations",
            "participants",
            "messages",
            "message_requests",
            "events",
            "mailboxes",
            "change_log",
            "wakes",
        )

        def state():
            return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in tables}

        try:
            conn.execute(
                "UPDATE participants SET role = 'guest' WHERE conversation_id = ? AND role = 'worker'", (conv,)
            )
            before = state()
            request_id = messages.new_request_id()
            for _ in range(2):
                result = await call(
                    client,
                    posting_host,
                    "send_message",
                    {"conversation_id": conv, "text": "must persist or refuse", "request_id": request_id},
                    role="planner",
                )
                assert result.get("error") == "invalid_request", "zero-copy sends must not report success or replay"
                assert state() == before
        finally:
            conn.close()

    dispatch(posting_host, scenario)


@pytest.mark.parametrize("kind", ["guest", "fixed", "human"])
def test_selected_guest_reply_form_keeps_alias_and_code(posting_host, kind):
    from datetime import UTC

    from baton import seat

    if kind == "human":
        config = posting_host.config
        posting_host.config = replace(
            config, roles={**config.roles, "planner": replace(config.roles["planner"], human=True)}
        )

    async def scenario(client):
        conv = await board(client, posting_host)
        bound = await claim(client, posting_host, "selected-guest")
        posted = await call(
            client,
            posting_host,
            "send_message",
            {"conversation_id": conv, "text": "selected reply target", "token": bound["binding_token"]},
            role="guest" if kind == "guest" else "planner",
        )
        conn = store.connect(posting_host.config.db_path)
        try:
            session = seat._Session("synthetic-reply-session", "synthetic-reply-digest", "planner", UTC)
            response = seat._conversation(posting_host.config, conn, session, conv, 0, posted["message_id"])
            html = response.body.decode()
            label = f"guest · {bound['alias']} · {bound['code']}" if kind == "guest" else "planner"
            assert f"Replying to {label}" in html
        finally:
            conn.close()

    dispatch(posting_host, scenario)


@pytest.mark.parametrize("role", ["guest", "planner"])
def test_valid_optional_post_coercions_keep_sdk_compatibility(posting_host, role):
    async def scenario(client):
        conv = await board(client, posting_host)
        bound = await claim(client, posting_host)
        sent = await call(
            client,
            posting_host,
            "send_message",
            {
                "conversation_id": conv,
                "text": "valid optional fields",
                "token": bound["binding_token"],
                "attention": '["worker"]',
                "needs_decision": "false",
                "request_id": messages.new_request_id(),
                "relayed": json.dumps([{"source": "synthetic source", "at": "2026-10-31T00:00:00Z", "text": "quoted"}]),
            },
            role=role,
        )
        assert sent["created"]
        shown = await call(client, posting_host, "get_message", {"message_id": sent["message_id"]})
        assert shown["from_role"] == role
        assert shown["attention"] == ["worker"] and shown["needs_decision"] is False
        assert shown["relayed"][0]["text"] == "quoted"
        if role == "guest":
            assert identity(shown) == identity(bound)
        else:
            assert "alias_id" not in shown

    dispatch(posting_host, scenario)
