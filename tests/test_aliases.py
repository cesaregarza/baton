"""Synthetic schema/claim/binding checks; dispatch runs in-process with no listener."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from conftest import make_config

from baton import app as app_module
from baton import auth, store
from baton.config import Role, StaticToken

PROTOCOL = "2026-07-28"


@pytest.fixture
def guest_host(tmp_path):
    host = make_config(tmp_path)
    raw, digest = auth.new_token()
    host.tokens["guest"] = raw
    host.config = replace(host.config, roles={**host.config.roles, "guest": Role(name="guest")},
                          tokens=(*host.config.tokens, StaticToken("guest-test", "guest", digest)))
    return host


async def rpc(client, host, method, params, *, role="guest"):
    headers = {"Authorization": f"Bearer {host.tokens[role]}", "Accept": "application/json, text/event-stream",
               "Mcp-Protocol-Version": PROTOCOL, "Mcp-Method": method}
    if method == "tools/call":
        headers["Mcp-Name"] = params["name"]
    meta = {"io.modelcontextprotocol/protocolVersion": PROTOCOL,
            "io.modelcontextprotocol/clientInfo": {"name": "alias-test", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}
    # Escape lone surrogates on the wire so malformed strings reach MCP, not the client's UTF-8 encoder.
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": meta}}
    response = await client.post(host.config.resource_url, headers={**headers, "Content-Type": "application/json"},
                                 content=json.dumps(body).encode("ascii"))
    return response.json()


def payload(response):
    result = response["result"]
    if "structuredContent" in result:
        value = result["structuredContent"]
        return value.get("result", value)
    return json.loads(result["content"][0]["text"])


def dispatch(host, scenario, integrations=None):
    app = app_module.create_app(host.config, integrations or {})

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=host.config.public_url) as client:
                await scenario(client)
    asyncio.run(run())


def test_claim_tools_are_registered_and_dispatch_to_claim_behavior(guest_host):
    async def scenario(client):
        listed = await rpc(client, guest_host, "tools/list", {})
        tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
        assert {"alias_exists", "claim_alias"} <= tools.keys()
        assert tools["alias_exists"]["annotations"]["readOnlyHint"] is True
        assert tools["claim_alias"]["annotations"]["readOnlyHint"] is False
        assert payload(await rpc(client, guest_host, "tools/call", {
            "name": "alias_exists", "arguments": {"name": "lane-b"}})) == {"exists": False}
        claimed = payload(await rpc(client, guest_host, "tools/call", {
            "name": "claim_alias", "arguments": {"name": "Lane-B", "passphrase": "synthetic secret"}}))
        assert claimed["alias"] == "lane-b"
        assert claimed["alias_id"] and claimed["code"] and claimed["binding_token"]
        assert payload(await rpc(client, guest_host, "tools/call", {
            "name": "alias_exists", "arguments": {"name": "LANE-B"}})) == {"exists": True}
    dispatch(guest_host, scenario)


def test_schema_11_migrates_to_alias_storage_without_changing_existing_rows(guest_host):
    # Build the actual pre-slice schema, rather than relabeling a schema-12 database.
    database = guest_host.config.db_path
    database.parent.mkdir(mode=0o700)
    raw = sqlite3.connect(database)
    raw.executescript((Path(__file__).parent / "fixtures" / "schema_v11.sql").read_text())
    raw.execute("INSERT INTO mailboxes VALUES ('worker', 7, 3)")
    raw.commit()
    raw.close()
    database.chmod(0o600)
    conn = store.connect(database)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == store.SCHEMA_VERSION
        assert tuple(conn.execute("SELECT * FROM mailboxes").fetchone()) == ("worker", 7, 3)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"aliases", "alias_bindings"} <= tables
    finally:
        conn.close()
    store.connect(database).close()


@pytest.fixture
def alias_store(guest_host):
    conn = store.connect(guest_host.config.db_path)
    principal = auth.static_token(guest_host.config, guest_host.tokens["guest"])
    config = replace(guest_host.config, aliases=replace(guest_host.config.aliases, binding_ttl_seconds=60))
    yield conn, config, principal
    conn.close()


def claim(alias_store, name="lane-b", passphrase="synthetic secret", **kwargs):
    from baton import aliases
    conn, config, principal = alias_store
    return aliases.claim(conn, config, principal, name, passphrase, **kwargs)


def with_limits(alias_store, **limits):
    conn, config, principal = alias_store
    return conn, replace(config, aliases=replace(config.aliases, **limits)), principal


def snapshot(conn):
    return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in ("aliases", "alias_bindings")}


def test_new_claim_resume_wrong_passphrase_and_persistent_random_identity(alias_store, monkeypatch):
    from baton import aliases
    conn, config, principal = alias_store
    calls = []
    original = auth.hash_passphrase

    def counted(passphrase, salt=None):
        calls.append(salt)
        return original(passphrase, salt)
    monkeypatch.setattr(auth, "hash_passphrase", counted)
    first = claim(alias_store)
    before = snapshot(conn)
    from baton.errors import Forbidden
    with pytest.raises(Forbidden, match="passphrase did not match"):
        claim(alias_store, passphrase="synthetic wrong secret")
    assert snapshot(conn) == before
    resumed = claim(alias_store, name="LANE-B")
    assert (first["alias_id"], first["alias"], first["code"], first["badge"]) == (
        resumed["alias_id"], "lane-b", resumed["code"], "guest")
    assert first["binding_token"] != resumed["binding_token"]
    assert len(calls) == 3  # exactly one scrypt for each admitted new/resume/wrong attempt
    assert calls[0] is None and calls[1] is not None and calls[2] is not None
    calls.clear()
    assert aliases.resolve_binding(conn, principal, resumed["binding_token"])["alias_id"] == first["alias_id"]
    assert calls == []
    assert aliases.exists(conn, "ＬＡＮＥ-Ｂ") is True
    conn.close()
    reopened = store.connect(config.db_path)
    try:
        assert aliases.resolve_binding(reopened, principal, first["binding_token"])["code"] == first["code"]
    finally:
        reopened.close()


@pytest.mark.parametrize("name", ["hilary", "HILARY", "hi1ary", "hіlary", "system", "planner", "worker", "guest",
                                 "", "white space", "foo/bar", "x" * 65])
def test_reservations_and_invalid_names_are_refused(alias_store, name):
    from baton.errors import BatonError
    conn, _, _ = alias_store
    with pytest.raises(BatonError):
        claim(alias_store, name=name)
    assert snapshot(conn) == {"aliases": [], "alias_bindings": []}


def test_human_seat_names_and_normalized_display_are_reserved(alias_store):
    from baton import aliases
    from baton.errors import Forbidden
    conn, config, principal = alias_store
    config = replace(config, roles={**config.roles, "seat": Role(name="seat", human=True, display_name="Cesar")})
    for name in ("seat", "CESAR", "cesar"):
        with pytest.raises(Forbidden, match="reserved"):
            aliases.claim(conn, config, principal, name, "synthetic")
    normalized = claim(alias_store, name="hiIary")
    assert normalized["alias"] == "hiiary"
    assert claim(alias_store, name="ｌａｎｅ-ｂ")["alias"] == "lane-b"


@pytest.mark.parametrize(("first", "confusable"), [("moss", "rnoss"), ("wood", "vvood"),
                                                   ("solo", "s010"), ("lark", "1ark")])
def test_existing_alias_skeleton_collisions_are_refused(alias_store, first, confusable):
    from baton.errors import Forbidden
    claim(alias_store, name=first)
    before = snapshot(alias_store[0])
    with pytest.raises(Forbidden, match="conflicts"):
        claim(alias_store, name=confusable)
    assert snapshot(alias_store[0]) == before


def test_concurrent_claims_have_one_owner_and_database_constraints(alias_store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from baton import aliases
    from baton.errors import Forbidden
    conn, config, principal = alias_store
    barrier = Barrier(2)

    def contender(secret):
        own = store.connect(config.db_path)
        try:
            barrier.wait(timeout=5)
            try:
                return aliases.claim(own, config, principal, "contested", secret)
            except Forbidden as error:
                return error.code
        finally:
            own.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, ["synthetic first", "synthetic second"]))
    assert len([result for result in results if isinstance(result, dict)]) == 1
    assert results.count("forbidden") == 1
    assert conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM alias_bindings").fetchone()[0] == 1
    row = conn.execute("SELECT * FROM aliases").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO aliases VALUES ('different-id', ?, 'different', 'unused', 0)", (row["name"],))


def test_proofs_are_digest_only_expire_and_require_authenticated_guest(alias_store, monkeypatch):
    from baton import aliases
    from baton.errors import Forbidden
    conn, _, principal = alias_store
    monkeypatch.setattr(aliases.time, "time", lambda: 1000)
    result = claim(alias_store)
    row = conn.execute("SELECT * FROM alias_bindings").fetchone()
    assert row["digest"] == auth.token_digest(result["binding_token"])
    assert row["expires_at"] == result["expires_at"] == 1060
    assert len(result["binding_token"]) >= 40
    assert result["binding_token"] not in repr(snapshot(conn))
    for invalid in (None, "", "unrecognized", "x" * 257):
        with pytest.raises(Forbidden, match="missing, invalid or expired"):
            aliases.resolve_binding(conn, principal, invalid)
    for invalid_principal in (None, replace_principal(principal, "planner")):
        with pytest.raises(Forbidden, match="authenticated guest"):
            aliases.resolve_binding(conn, invalid_principal, result["binding_token"])
        with pytest.raises(Forbidden, match="authenticated guest"):
            aliases.claim(conn, alias_store[1], invalid_principal, "new-name", "synthetic")
    monkeypatch.setattr(aliases.time, "time", lambda: 1060)
    with pytest.raises(Forbidden, match="missing, invalid or expired"):
        aliases.resolve_binding(conn, principal, result["binding_token"])
    resumed = claim(alias_store)
    assert resumed["alias_id"] == result["alias_id"]
    assert conn.execute("SELECT COUNT(*) FROM alias_bindings").fetchone()[0] == 1


def replace_principal(principal, role):
    return principal.model_copy(update={"claims": {"role": role}, "subject": role})


def test_binding_capacity_refuses_without_evicting_and_prunes_expired(alias_store, monkeypatch):
    from baton import aliases
    from baton.errors import LimitExceeded
    alias_store = with_limits(alias_store, max_bindings_per_alias=1)
    monkeypatch.setattr(aliases.time, "time", lambda: 1000)
    first = claim(alias_store)
    before = snapshot(alias_store[0])
    with pytest.raises(LimitExceeded) as refused:
        claim(alias_store)
    assert refused.value.extra == {"limit": "bindings_per_alias"}
    assert snapshot(alias_store[0]) == before
    monkeypatch.setattr(aliases.time, "time", lambda: 1061)
    resumed = claim(alias_store)
    assert resumed["alias_id"] == first["alias_id"]
    assert len(snapshot(alias_store[0])["alias_bindings"]) == 1


@pytest.mark.parametrize(("limit", "same_alias", "other_credential"), [
    ("max_claims_per_credential_per_hour", False, False), ("max_claims_per_alias_per_hour", True, True),
    ("max_claims_per_hour", False, True)])
def test_claim_limits_admit_before_scrypt_and_bound_tracking(alias_store, monkeypatch, limit, same_alias,
                                                            other_credential):
    from baton import aliases
    from baton.errors import LimitExceeded
    alias_store = with_limits(alias_store, **{limit: 1})
    conn, config, principal = alias_store
    monkeypatch.setattr(aliases.time, "time", lambda: 1000)
    claim(alias_store)
    before = snapshot(conn)
    if other_credential:
        principal = principal.model_copy(update={"token": "synthetic other credential"})

    def no_scrypt(*args, **kwargs):
        raise AssertionError("rate-limited call reached scrypt")
    with monkeypatch.context() as patch:
        patch.setattr(auth, "hash_passphrase", no_scrypt)
        with pytest.raises(LimitExceeded) as refused:
            aliases.claim(conn, config, principal, "lane-b" if same_alias else "second", "synthetic")
        assert refused.value.extra == {"limit": "alias_claims"}
    assert snapshot(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM alias_claims").fetchone()[0] == 1
    monkeypatch.setattr(aliases.time, "time", lambda: 4601)
    claim(alias_store, name="third")
    assert conn.execute("SELECT COUNT(*) FROM alias_claims").fetchone()[0] == 1


def test_wrong_passphrase_consumes_alias_claim_quota(alias_store):
    from baton.errors import Forbidden, LimitExceeded
    alias_store = with_limits(alias_store, max_claims_per_alias_per_hour=2)
    claim(alias_store)
    with pytest.raises(Forbidden, match="passphrase did not match"):
        claim(alias_store, passphrase="synthetic wrong")
    with pytest.raises(LimitExceeded):
        claim(alias_store)
    assert alias_store[0].execute("SELECT COUNT(*) FROM alias_claims").fetchone()[0] == 2


def test_passphrase_salted_verifier_and_code_independence(alias_store):
    import hashlib

    from baton import aliases
    first = claim(alias_store)
    second = claim(alias_store, name="second")
    rows = list(alias_store[0].execute("SELECT * FROM aliases"))
    assert rows[0]["verifier"].startswith("scrypt$")
    assert rows[0]["verifier"] != rows[1]["verifier"]
    assert "synthetic secret" not in repr(snapshot(alias_store[0]))
    assert first["alias_id"] != second["alias_id"]
    assert first["code"] == hashlib.sha256(b"baton-alias-code-v1\x00" + first["alias_id"].encode()).hexdigest()[:4]
    alias_store[0].execute("UPDATE aliases SET verifier = ? WHERE id = ?",
                          (auth.hash_passphrase("synthetic changed"), first["alias_id"]))
    assert aliases.resolve_binding(alias_store[0], alias_store[2], first["binding_token"])["code"] == first["code"]


def test_exists_is_read_only_and_does_not_disclose_identity(alias_store):
    from baton import aliases
    conn, _, _ = alias_store
    first = claim(alias_store)
    before = snapshot(conn)
    attempts = conn.execute("SELECT COUNT(*) FROM alias_claims").fetchone()[0]
    assert aliases.exists(conn, "LANE-B") is True
    assert aliases.exists(conn, "missing") is False
    assert snapshot(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM alias_claims").fetchone()[0] == attempts
    assert set(first) == {"alias_id", "alias", "badge", "code", "binding_token", "expires_at"}


@pytest.mark.parametrize("passphrase", ["", "synthetic secret" * 100])
def test_invalid_passphrase_error_never_echoes_secret_or_hashes(alias_store, monkeypatch, passphrase):
    from baton.errors import InvalidRequest
    before = snapshot(alias_store[0])

    def no_scrypt(*args, **kwargs):
        raise AssertionError("invalid input reached scrypt")
    monkeypatch.setattr(auth, "hash_passphrase", no_scrypt)
    with pytest.raises(InvalidRequest) as refused:
        claim(alias_store, passphrase=passphrase)
    assert "synthetic secret" not in str(refused.value)
    assert snapshot(alias_store[0]) == before
    assert alias_store[0].execute("SELECT COUNT(*) FROM alias_claims").fetchone()[0] == 0


def test_raw_claim_validation_never_echoes_passphrase_in_sdk_errors(guest_host, caplog):
    secret = "synthetic secret which must never appear"

    async def scenario(client):
        for arguments in ({"passphrase": secret}, {"name": "new", "passphrase": [secret]},
                          {"name": 5, "passphrase": secret}, {"name": "new", "passphrase": secret * 100},
                          {"name": "new", "passphrase": ""}):
            response = await rpc(client, guest_host, "tools/call", {"name": "claim_alias", "arguments": arguments})
            assert payload(response)["error"] == "invalid_request"
            assert secret not in json.dumps(response)
    dispatch(guest_host, scenario)
    assert secret not in guest_host.config.audit_path.read_text()
    assert secret not in caplog.text


@pytest.mark.parametrize("ttl", [0, -1, True, 1.5])
def test_claim_cannot_issue_a_nonexpiring_or_invalid_binding(alias_store, ttl):
    from baton import aliases
    from baton.errors import InvalidRequest
    with pytest.raises(InvalidRequest, match="binding lifetime"):
        aliases.claim(*with_limits(alias_store, binding_ttl_seconds=ttl), "lane-b", "synthetic")
    assert snapshot(alias_store[0]) == {"aliases": [], "alias_bindings": []}


def test_claim_tools_errors_audit_and_replay_paths_are_secret_safe(guest_host, monkeypatch, caplog):
    from baton import events
    secret = "synthetic passphrase which must stay private"

    def no_replay_digest(*args, **kwargs):
        raise AssertionError("a claim must not digest its passphrase for replay")
    monkeypatch.setattr(events, "request_digest", no_replay_digest)

    async def scenario(client):
        first = payload(await rpc(client, guest_host, "tools/call", {
            "name": "claim_alias", "arguments": {"name": "lane-b", "passphrase": secret}}))
        assert first["binding_token"]
        wrong = await rpc(client, guest_host, "tools/call", {
            "name": "claim_alias", "arguments": {"name": "lane-b", "passphrase": secret + " wrong"}})
        assert payload(wrong)["error"] == "forbidden"
        assert secret not in json.dumps(wrong)
        assert payload(await rpc(client, guest_host, "tools/call", {
            "name": "claim_alias", "arguments": {"name": "another", "passphrase": secret}},
            role="planner"))["error"] == "forbidden"
        raw = await rpc(client, guest_host, "tools/call", {"name": "claim_alias", "arguments": {
            "name": "another", "passphrase": secret, "alias_id": "forged", "request_id": secret}})
        assert payload(raw)["error"] == "forbidden_field"
        assert secret not in json.dumps(raw)
        assert payload(await rpc(client, guest_host, "tools/call", {
            "name": "send_message", "arguments": {"text": "hello", "to": "planner",
                                                     "token": first["binding_token"]}}))["error"] == "forbidden"
    dispatch(guest_host, scenario)
    assert secret not in caplog.text
    assert secret not in guest_host.config.audit_path.read_text()
    conn = store.connect(guest_host.config.db_path)
    try:
        assert secret not in repr(snapshot(conn))
        assert conn.execute("SELECT COUNT(*) FROM message_requests").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM alias_bindings").fetchone()[0] == 1
    finally:
        conn.close()


def test_claim_tools_keep_registered_guest_restrictions_in_force_in_real_dispatch(guest_host):
    config = guest_host.config
    guest_host.config = replace(config, conversations=replace(config.conversations, enabled=True),
                                search=replace(config.search, enabled=True))

    async def scenario(client):
        listed = (await rpc(client, guest_host, "tools/list", {}))["result"]["tools"]
        names = {tool["name"] for tool in listed}
        assert app_module.GUEST_TOOLS <= names
        for tool in listed:
            if tool["name"] in app_module.GUEST_TOOLS:
                continue
            arguments = {key: {"string": "synthetic", "integer": 0, "number": 0, "boolean": False,
                               "array": [], "object": {}}.get(spec.get("type"), "synthetic")
                         for key, spec in tool["inputSchema"].get("properties", {}).items()
                         if key in tool["inputSchema"].get("required", [])}
            assert payload(await rpc(client, guest_host, "tools/call", {
                "name": tool["name"], "arguments": arguments}))["error"] == "forbidden"
    dispatch(guest_host, scenario)


def test_alias_limits_are_loaded_from_typed_config():
    from baton.config import parse
    configured = parse({"host": {"public_url": "https://alias.example.test"}, "roles": {"guest": {}},
                        "aliases": {"max_bindings_per_alias": 4, "max_posts_per_alias_per_hour": 8,
                                    "max_posts_per_credential_per_hour": 9,
                                    "max_claims_per_credential_per_hour": 5,
                                    "max_claims_per_alias_per_hour": 6, "max_claims_per_hour": 7}})
    assert configured.aliases.max_posts_per_alias_per_hour == 8
    assert configured.aliases.max_posts_per_credential_per_hour == 9
    assert configured.aliases.max_bindings_per_alias == 4
    assert configured.aliases.max_claims_per_credential_per_hour == 5
    assert configured.aliases.max_claims_per_alias_per_hour == 6
    assert configured.aliases.max_claims_per_hour == 7


@pytest.mark.parametrize("key", ["max_posts_per_alias_per_hour", "max_posts_per_credential_per_hour",
                               "max_bindings_per_alias",
                               "max_claims_per_credential_per_hour", "max_claims_per_alias_per_hour",
                               "max_claims_per_hour"])
@pytest.mark.parametrize("value", [True, "1", 0, -1, 1.5, 10001])
def test_alias_limits_refuse_wrong_types_or_bounds(key, value):
    from baton.config import ConfigError, parse
    with pytest.raises(ConfigError, match=f"aliases.{key}"):
        parse({"host": {"public_url": "https://alias.example.test"}, "roles": {"guest": {}},
               "aliases": {key: value}})


def test_binding_ttl_defaults_to_ratified_24_hours_and_can_be_shortened_in_config():
    from baton.config import parse
    base = {"host": {"public_url": "https://alias.example.test"}, "roles": {"guest": {}}}
    assert parse(base).aliases.binding_ttl_seconds == 86400
    assert parse({**base, "aliases": {"binding_ttl_seconds": 86400}}).aliases.binding_ttl_seconds == 86400
    assert parse({**base, "aliases": {"binding_ttl_seconds": 120}}).aliases.binding_ttl_seconds == 120


@pytest.mark.parametrize("value", [True, "86400", 0, -1, 86400.5, 86401])
def test_binding_ttl_config_refuses_invalid_or_unbounded_lifetimes(value):
    from baton.config import ConfigError, parse
    with pytest.raises(ConfigError, match="aliases.binding_ttl_seconds"):
        parse({"host": {"public_url": "https://alias.example.test"}, "roles": {"guest": {}},
               "aliases": {"binding_ttl_seconds": value}})


def test_24_hour_boundary_refuses_expired_proof_and_resumes_permanent_identity(alias_store, monkeypatch):
    from baton import aliases
    from baton.errors import Forbidden
    alias_store = with_limits(alias_store, binding_ttl_seconds=86400)
    conn, _, principal = alias_store
    monkeypatch.setattr(aliases.time, "time", lambda: 1000)
    first = claim(alias_store)
    assert first["expires_at"] == 87400
    original_alias = tuple(conn.execute("SELECT * FROM aliases").fetchone())
    monkeypatch.setattr(aliases.time, "time", lambda: 87399.999)
    assert aliases.resolve_binding(conn, principal, first["binding_token"])["alias_id"] == first["alias_id"]
    monkeypatch.setattr(aliases.time, "time", lambda: 87400)
    with pytest.raises(Forbidden, match="missing, invalid or expired"):
        aliases.resolve_binding(conn, principal, first["binding_token"])
    assert aliases.exists(conn, "lane-b") is True
    before = snapshot(conn)
    with pytest.raises(Forbidden, match="passphrase did not match"):
        claim(alias_store, passphrase="synthetic wrong secret")
    assert snapshot(conn) == before
    resumed = claim(alias_store)
    assert resumed["expires_at"] == 173800
    assert resumed["binding_token"] != first["binding_token"]
    assert (resumed["alias_id"], resumed["alias"], resumed["code"]) == (
        first["alias_id"], first["alias"], first["code"])
    assert tuple(conn.execute("SELECT * FROM aliases").fetchone()) == original_alias
    assert conn.execute("SELECT COUNT(*) FROM alias_bindings").fetchone()[0] == 1


def test_claim_uses_configured_shorter_binding_ttl(alias_store, monkeypatch):
    from baton import aliases
    monkeypatch.setattr(aliases.time, "time", lambda: 1000)
    result = claim(with_limits(alias_store, binding_ttl_seconds=120))
    assert result["expires_at"] == 1120


def test_more_than_twenty_aliases_keep_permanent_ownership_across_claim_windows(alias_store, monkeypatch):
    from baton import aliases
    conn, _, _ = alias_store
    monkeypatch.setattr(aliases.time, "time", lambda: 1000)
    first_twenty = [claim(alias_store, name=f"ephemeral-{index}") for index in range(20)]
    # The existing hourly admission limit still applies; advancing its window is not ownership eviction.
    monkeypatch.setattr(aliases.time, "time", lambda: 4601)
    extra = [claim(alias_store, name=f"ephemeral-{index}") for index in range(20, 26)]
    assert conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0] == 26
    expected = {(entry["alias_id"], entry["alias"]) for entry in first_twenty + extra}
    assert {(row["id"], row["name"]) for row in conn.execute("SELECT * FROM aliases")} == expected
    resumed = claim(alias_store, name="ephemeral-0")
    assert (resumed["alias_id"], resumed["code"]) == (first_twenty[0]["alias_id"], first_twenty[0]["code"])


def test_binding_reuse_and_resumption_work_across_authenticated_guest_credentials(guest_host):
    from baton import aliases
    raw, digest = auth.new_token()
    guest_host.config = replace(guest_host.config, tokens=(*guest_host.config.tokens,
                                StaticToken("other-guest", "guest", digest)))
    guest_host.tokens["other-guest"] = raw
    proofs = []

    async def scenario(client):
        for role in ("guest", "other-guest"):
            result = payload(await rpc(client, guest_host, "tools/call", {
                "name": "claim_alias", "arguments": {"name": "portable", "passphrase": "synthetic"}}, role=role))
            assert result["binding_token"]
            proofs.append(result)
    dispatch(guest_host, scenario)
    assert proofs[0]["alias_id"] == proofs[1]["alias_id"]
    assert proofs[0]["code"] == proofs[1]["code"]
    assert proofs[0]["binding_token"] != proofs[1]["binding_token"]
    conn = store.connect(guest_host.config.db_path)
    try:
        for raw_credential in (guest_host.tokens["guest"], raw):
            principal = auth.static_token(guest_host.config, raw_credential)
            assert principal is not None
            for issued in proofs:
                resolved = aliases.resolve_binding(conn, principal, issued["binding_token"])
                assert resolved["alias_id"] == proofs[0]["alias_id"]
    finally:
        conn.close()


def test_oauth_refresh_preserves_claim_rate_identity_without_storing_access_tokens(alias_store):
    from mcp.server.auth.provider import AccessToken

    from baton import aliases
    from baton.errors import LimitExceeded
    conn, config, _ = with_limits(alias_store, max_claims_per_credential_per_hour=1)
    principal = AccessToken(token="synthetic access", client_id="synthetic-client", scopes=auth.ALL_SCOPES,
                            resource=config.resource_url, subject="guest", claims={"role": "guest", "grant": "grant-a"})
    aliases.claim(conn, config, principal, "oauth-alias", "synthetic")
    refreshed = principal.model_copy(update={"token": "synthetic refreshed access"})
    with pytest.raises(LimitExceeded) as refused:
        aliases.claim(conn, config, refreshed, "another", "synthetic")
    assert refused.value.extra == {"limit": "alias_claims"}
    attempts = repr([tuple(row) for row in conn.execute("SELECT * FROM alias_claims")])
    assert "synthetic access" not in attempts and "synthetic refreshed access" not in attempts


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"], ids=["high-surrogate", "low-surrogate"])
def test_raw_surrogate_passphrase_is_safely_refused_before_admission(guest_host, caplog, surrogate):
    secret = "synthetic secret must stay private"

    async def scenario(client):
        response = await rpc(client, guest_host, "tools/call", {"name": "claim_alias", "arguments": {
            "name": "malformed", "passphrase": secret + surrogate}})
        text = response["result"]["content"][0]["text"]
        assert '"invalid_request"' in text, "claim must return a structured invalid_request refusal"
        assert json.loads(text)["error"] == "invalid_request"
        assert secret not in json.dumps(response)
        assert "Traceback" not in json.dumps(response)
    dispatch(guest_host, scenario)
    conn = store.connect(guest_host.config.db_path)
    try:
        assert snapshot(conn) == {"aliases": [], "alias_bindings": []}
        assert conn.execute("SELECT COUNT(*) FROM alias_claims").fetchone()[0] == 0
    finally:
        conn.close()
    assert secret not in guest_host.config.audit_path.read_text()
    assert secret not in caplog.text
    assert "Traceback" not in caplog.text
    assert not any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"], ids=["high-surrogate", "low-surrogate"])
def test_storage_surrogate_passphrase_is_refused_before_admission_or_hashing(alias_store, monkeypatch, surrogate):
    from baton.errors import InvalidRequest
    secret = "synthetic secret must stay private"
    original = auth.hash_passphrase
    hashed = []

    def counted(*args, **kwargs):
        hashed.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(auth, "hash_passphrase", counted)
    before = snapshot(alias_store[0])
    with pytest.raises(InvalidRequest) as refused:
        claim(alias_store, passphrase=secret + surrogate)
    assert secret not in str(refused.value)
    assert hashed == []
    assert snapshot(alias_store[0]) == before
    assert alias_store[0].execute("SELECT COUNT(*) FROM alias_claims").fetchone()[0] == 0


def test_valid_unicode_passphrase_claim_and_resumption_work_in_real_dispatch(guest_host):
    secret = "café 漢字 🧪 e\u0301"

    async def scenario(client):
        responses = [payload(await rpc(client, guest_host, "tools/call", {"name": "claim_alias", "arguments": {
            "name": "unicode-secret", "passphrase": secret}})) for _ in range(2)]
        assert responses[0]["alias_id"] == responses[1]["alias_id"]
        assert responses[0]["code"] == responses[1]["code"]
        assert responses[0]["binding_token"] != responses[1]["binding_token"]
    dispatch(guest_host, scenario)
    conn = store.connect(guest_host.config.db_path)
    try:
        row = conn.execute("SELECT verifier FROM aliases WHERE name = 'unicode-secret'").fetchone()
        assert auth.check_passphrase(secret, row["verifier"]) is True
    finally:
        conn.close()
