"""Per-call identity on a shared guest login: an enrolled alias's proof makes a call run as its mapped role.

Everything goes through in-process MCP dispatch or `baton.cli.main`, with synthetic roles and tokens.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from conftest import make_config
from test_aliases import dispatch, payload, rpc
from test_messages import rid

from baton import app as app_module
from baton import auth, cli, store

SECRET = "synthetic enrol secret"
OTHER = "synthetic other secret"
ROLES = """
[roles.hilary]
description = "Designs"
job_authority = false
[roles.guest]
description = "Shared login"
can_view_all_conversations = true
[roles.person]
description = "A person"
human = true
[aliases]
max_claims_per_alias_per_hour = 100
"""


def build_host(tmp_path):
    """A host with an agent role to enrol (hilary, no job authority), a shared guest login and a person."""
    made = {role: auth.new_token() for role in ("hilary", "guest", "person")}
    entries = "".join(f'[[auth.tokens]]\nname = "{role}-token"\nrole = "{role}"\nsha256 = "{digest}"\n'
                      for role, (_, digest) in made.items())
    built = make_config(tmp_path, extra=ROLES + entries)
    built.tokens.update({role: token for role, (token, _) in made.items()})
    built.config_path = tmp_path / "baton.toml"
    return built


@pytest.fixture
def host(tmp_path):
    return build_host(tmp_path)


def owner(host, *argv):
    """Run an owner command; argparse's own exit (an unknown command) counts as its exit code."""
    try:
        return cli.main(["--config", str(host.config_path), *argv])
    except SystemExit as error:
        return error.code


async def call(client, host, name, arguments=None, role="guest"):
    return payload(await rpc(client, host, "tools/call", {"name": name, "arguments": arguments or {}}, role=role))


async def claim(client, host, name="hilary", passphrase=SECRET, role="guest"):
    return await call(client, host, "claim_alias", {"name": name, "passphrase": passphrase}, role=role)


async def enrol(client, host, role="hilary"):
    assert owner(host, "alias", "arm-enrol", role) == 0
    enrolled = await claim(client, host, role, role=role)
    assert enrolled.get("effective_role") == role, enrolled
    return enrolled


def db(host):
    conn = sqlite3.connect(host.config.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def audit(host):
    return [json.loads(line) for line in host.config.audit_path.read_text().splitlines()]


def test_schema_adds_the_enrolment_and_arm_tables(host):
    store.connect(host.config.db_path).close()
    with db(host) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"alias_arms", "core_enrolment"} <= tables
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == store.SCHEMA_VERSION


def test_a_fixed_role_enrols_once_with_an_owner_arm(host):
    async def scenario(client):
        refused = await claim(client, host, role="hilary")
        assert refused.get("error") == "forbidden", "a fixed role can't enrol without an arm"
        enrolled = await enrol(client, host)
        assert enrolled["alias"] == "hilary" and enrolled["binding_token"]
        retried = await claim(client, host, role="hilary")
        assert retried.get("effective_role") == "hilary", "the same passphrase again is a renewal"
        with db(host) as conn:
            verifier = conn.execute("SELECT verifier FROM aliases WHERE name = 'hilary'").fetchone()[0]
        changed = await claim(client, host, passphrase=OTHER, role="hilary")
        assert changed.get("error") == "forbidden"
        with db(host) as conn:
            assert conn.execute("SELECT verifier FROM aliases WHERE name = 'hilary'").fetchone()[0] == verifier
            assert [tuple(row) for row in conn.execute("SELECT role FROM core_enrolment")] == [("hilary",)]
        assert owner(host, "alias", "arm-enrol", "hilary") == 1, "a role enrols once"
    dispatch(host, scenario)


def test_enrolment_names_only_the_credentials_own_role_and_the_arm_is_single_use(host):
    async def scenario(client):
        assert owner(host, "alias", "arm-enrol", "hilary") == 0
        other = await claim(client, host, "planner", role="hilary")
        assert other.get("error") == "forbidden", "an arm for hilary never enrols another name"
        guest = await claim(client, host, "hilary", role="guest")
        assert guest.get("error") == "forbidden", "a guest can't use a role's enrol arm"
        assert (await claim(client, host, role="hilary")).get("effective_role") == "hilary"
        with db(host) as conn:
            assert conn.execute("SELECT COUNT(*) FROM alias_arms").fetchone()[0] == 0
    dispatch(host, scenario)


def test_an_expired_enrol_arm_is_refused(host):
    async def scenario(client):
        assert owner(host, "alias", "arm-enrol", "hilary") == 0
        with db(host) as conn:
            conn.execute("UPDATE alias_arms SET expires_at = expires_at - 901")
        assert (await claim(client, host, role="hilary")).get("error") == "forbidden"
    dispatch(host, scenario)


@pytest.mark.parametrize("role", ["person", "guest", "nobody"])
def test_arm_enrol_refuses_human_guest_and_unknown_roles(host, role):
    store.connect(host.config.db_path).close()
    assert owner(host, "alias", "arm-enrol", role) == 1, "refused by baton itself, not by argparse"
    with db(host) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alias_arms").fetchone()[0] == 0


def test_a_guest_renews_an_enrolled_alias_without_an_arm_and_acts_as_its_role(host):
    async def scenario(client):
        await enrol(client, host)
        renewed = await claim(client, host)
        assert renewed.get("effective_role") == "hilary", "the reserved name renews; it isn't created"
        proof = renewed["binding_token"]
        me = await call(client, host, "whoami", {"token": proof})
        assert (me["role"], me["credential_role"], me["effective_role"]) == ("hilary", "guest", "hilary")
        assert (me["alias"], me["code"]) == ("hilary", renewed["code"])
        sent = await call(client, host, "send_message", {"to": "hilary", "text": "for hilary"}, role="planner")
        assert sent.get("created") is True
        read = await call(client, host, "read_messages", {"token": proof})
        assert [m["text"] for m in read["messages"]] == ["for hilary"], "the mapped role's own mailbox"
        reply = await call(client, host, "send_message", {"to": "planner", "text": "from hilary", "token": proof})
        assert reply.get("created") is True
        got = await call(client, host, "read_messages", {}, role="planner")
        assert got["messages"][-1]["from_role"] == "hilary"
        assert "alias_id" not in got["messages"][-1] or got["messages"][-1]["alias_id"] is None
        job = await call(client, host, "create_job", {"title": "nope", "token": proof})
        assert job.get("error") == "forbidden", "mapped hilary keeps hilary's job authority, which is false"
    dispatch(host, scenario)


def test_an_unmapped_proof_still_posts_as_a_guest_and_cannot_use_core_tools(host):
    async def scenario(client):
        alias = await claim(client, host, "lane-x")
        assert alias.get("effective_role") == "guest"
        me = await call(client, host, "whoami", {"token": alias["binding_token"]})
        assert (me["role"], me["effective_role"], me["alias"]) == ("guest", "guest", "lane-x")
        refused = await call(client, host, "read_messages", {"token": alias["binding_token"]})
        assert refused.get("error") == "forbidden"
    dispatch(host, scenario)


@pytest.mark.parametrize("tool,arguments", [("whoami", {}), ("alias_exists", {"name": "x"}),
                                            ("read_messages", {})])
@pytest.mark.parametrize("proof", ["", "not ascii é", "x" * 257, "unknown-proof"])
def test_a_bad_token_is_refused_on_every_tool(host, tool, arguments, proof):
    async def scenario(client):
        refused = await call(client, host, tool, {**arguments, "token": proof})
        assert refused.get("error") == "forbidden", refused
        if proof:
            assert proof not in host.config.audit_path.read_text()
    dispatch(host, scenario)


def test_an_expired_or_replaced_proof_is_refused_even_on_read_only_tools(host):
    async def scenario(client):
        await enrol(client, host)
        proof = (await claim(client, host))["binding_token"]
        with db(host) as conn:
            conn.execute("UPDATE alias_bindings SET expires_at = 1")
        assert (await call(client, host, "whoami", {"token": proof})).get("error") == "forbidden"
    dispatch(host, scenario)


def test_an_explicit_null_token_is_the_same_as_none(host):
    async def scenario(client):
        me = await call(client, host, "whoami", {"token": None})
        assert (me.get("role"), me.get("effective_role"), me.get("alias")) == ("guest", "guest", None)
    dispatch(host, scenario)


@pytest.mark.parametrize("tool,arguments", [("whoami", {}), ("send_message", {"to": "worker", "text": "x"})])
def test_a_token_on_a_fixed_role_credential_is_refused(host, tool, arguments):
    async def scenario(client):
        await enrol(client, host)
        proof = (await claim(client, host))["binding_token"]
        refused = await call(client, host, tool, {**arguments, "token": proof}, role="planner")
        assert refused.get("error") == "forbidden", refused
    dispatch(host, scenario)


def test_tools_take_token_centrally_except_claim_alias(host):
    async def scenario(client):
        listed = (await rpc(client, host, "tools/list", {}))["result"]["tools"]
        schemas = {tool["name"]: tool for tool in listed}
        assert "token" not in schemas["claim_alias"]["inputSchema"]["properties"]
        for name, tool in schemas.items():
            if name != "claim_alias":
                assert "token" in tool["inputSchema"]["properties"], name
                assert "token" not in tool["inputSchema"].get("required", []), name
        assert "core proof" in schemas["read_messages"]["description"]
        assert "core proof" not in schemas["whoami"]["description"]
    dispatch(host, scenario)


def test_audit_lines_carry_both_identities_and_never_the_proof(host):
    async def scenario(client):
        await enrol(client, host)
        renewed = await claim(client, host)
        await call(client, host, "whoami", {"token": renewed["binding_token"]})
        line = audit(host)[-1]
        assert (line["tool"], line["role"], line["credential_role"]) == ("whoami", "hilary", "guest")
        assert line["credential_id"] == "guest-token"
        assert (line["alias_id"], line["alias_code"]) == (renewed["alias_id"], renewed["code"])
        assert renewed["binding_token"] not in host.config.audit_path.read_text()
        assert SECRET not in host.config.audit_path.read_text()
    dispatch(host, scenario)


def test_mapped_calls_use_the_effective_roles_hourly_budget(host, monkeypatch):
    monkeypatch.setattr(app_module, "HOURLY_LIMITS", {auth.SCOPE_READ: 3, auth.SCOPE_WRITE: 300})

    async def scenario(client):
        await enrol(client, host)
        proof = (await claim(client, host))["binding_token"]
        for _ in range(2):
            assert "error" not in await call(client, host, "read_messages", {}, role="hilary")
        assert "error" not in await call(client, host, "read_messages", {"token": proof})
        limited = await call(client, host, "read_messages", {"token": proof})
        assert limited.get("error") == "rate_limited", "hilary's own calls and mapped calls share one budget"
    dispatch(host, scenario)


def test_replay_keys_on_the_effective_role_and_rechecks_authorization(host):
    async def scenario(client):
        await enrol(client, host)
        proof = (await claim(client, host))["binding_token"]
        request_id = rid("once")
        body = {"to": "worker", "text": "once", "request_id": request_id}
        first = await call(client, host, "send_message", {**body, "token": proof})
        again = await call(client, host, "send_message", {**body, "token": proof})
        assert first["message_id"] == again["message_id"] and again.get("created") is False
        other = await call(client, host, "send_message", body, role="planner")
        assert other.get("error") == "idempotency_conflict", "another role can't replay hilary's request"
        assert owner(host, "alias", "arm-rekey", "hilary") == 0
        await claim(client, host, passphrase=OTHER)
        stale = await call(client, host, "send_message", {**body, "token": proof})
        assert stale.get("error") == "forbidden", "a stale proof is refused before any cached result"
    dispatch(host, scenario)


def test_reserved_names_are_still_refused_to_new_guest_aliases(host):
    async def scenario(client):
        assert (await claim(client, host, "hilary")).get("error") == "forbidden"
        assert (await claim(client, host, "p1anner")).get("error") == "forbidden"
    dispatch(host, scenario)
