"""The live-binding cap: a claim that proves the alias replaces its oldest binding instead of being refused.

A client that lost its token, such as a chat that lost its context, gets back in with its passphrase alone.
A claim that proves nothing -- a wrong passphrase, a fixed role renewing an alias it isn't enrolled to, a
claim over its hourly limit -- is refused as before and replaces nothing.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_aliases import dispatch
from test_per_call_identity import OTHER, SECRET, build_host, call, claim, db

from baton import auth
from baton.database import store

CAP = 3
ALIAS = "lane-c"
FIXTURES = Path(__file__).parent / "fixtures"


def capped_host(tmp_path, **limits):
    host = build_host(tmp_path)
    host.config = replace(host.config, aliases=replace(host.config.aliases, max_bindings_per_alias=CAP, **limits))
    return host


@pytest.fixture
def host(tmp_path):
    return capped_host(tmp_path)


async def fill(client, host):
    """Claim ALIAS up to the cap; the claims in issuance order."""
    return [await claim(client, host, ALIAS) for _ in range(CAP)]


async def works(client, host, token):
    return "error" not in await call(client, host, "whoami", {"token": token})


def bindings(host):
    with db(host) as conn:
        return sorted(row[0] for row in conn.execute("SELECT digest FROM alias_bindings"))


def test_a_valid_claim_at_the_cap_replaces_only_the_oldest_binding(host):
    async def scenario(client):
        held = await fill(client, host)
        renewed = await claim(client, host, ALIAS)
        assert "binding_token" in renewed, renewed
        assert renewed.keys() == held[0].keys(), "a replacing claim answers like any other"
        assert not await works(client, host, held[0]["binding_token"]), "the oldest binding is replaced"
        for kept in (*held[1:], renewed):
            assert await works(client, host, kept["binding_token"])
        assert len(bindings(host)) == CAP
    dispatch(host, scenario)


def test_the_replaced_binding_is_the_first_issued_whatever_its_expiry_or_rowid(host):
    async def scenario(client):
        held = [entry["binding_token"] for entry in await fill(client, host)]
        with db(host) as conn:
            # The oldest now expires last and holds the highest rowid, so only the issuance order still names it.
            conn.execute("CREATE TEMP TABLE moved AS SELECT * FROM alias_bindings WHERE digest = ?",
                         (auth.token_digest(held[0]),))
            conn.execute("DELETE FROM alias_bindings WHERE digest = ?", (auth.token_digest(held[0]),))
            conn.execute("UPDATE moved SET expires_at = expires_at + 3600")
            conn.execute("INSERT INTO alias_bindings SELECT * FROM moved")
        assert "binding_token" in await claim(client, host, ALIAS)
        assert not await works(client, host, held[0])
        assert [await works(client, host, token) for token in held[1:]] == [True, True]
    dispatch(host, scenario)


def test_a_wrong_passphrase_at_the_cap_gets_the_usual_forbidden_and_replaces_nothing(host):
    async def scenario(client):
        await fill(client, host)
        await claim(client, host, "lane-d")
        before = bindings(host)
        refused = await claim(client, host, ALIAS, OTHER)
        assert refused == await claim(client, host, "lane-d", OTHER), "the same refusal as below the cap"
        assert refused.get("error") == "forbidden", refused
        assert bindings(host) == before
    dispatch(host, scenario)


def test_a_fixed_role_renewing_an_alias_it_isnt_enrolled_to_at_the_cap_replaces_nothing(host):
    async def scenario(client):
        await fill(client, host)
        before = bindings(host)
        refused = await claim(client, host, ALIAS, role="planner")
        assert refused.get("error") == "forbidden", refused
        assert bindings(host) == before
    dispatch(host, scenario)


def test_the_hourly_claim_limit_still_refuses_after_a_replacement(tmp_path):
    host = capped_host(tmp_path, max_claims_per_alias_per_hour=CAP + 1)

    async def scenario(client):
        await fill(client, host)
        assert "binding_token" in await claim(client, host, ALIAS), "the last admitted claim replaces one"
        before = bindings(host)
        refused = await claim(client, host, ALIAS)
        assert (refused.get("error"), refused.get("limit")) == ("limit_exceeded", "alias_claims"), refused
        assert bindings(host) == before
    dispatch(host, scenario)


def test_an_expired_binding_does_not_count_toward_the_cap(host):
    # A guard, true before and after the change: only a live binding is ever replaced.
    async def scenario(client):
        held = [entry["binding_token"] for entry in await fill(client, host)]
        with db(host) as conn:
            conn.execute("UPDATE alias_bindings SET expires_at = 0 WHERE digest = ?", (auth.token_digest(held[0]),))
        assert "binding_token" in await claim(client, host, ALIAS)
        assert [await works(client, host, token) for token in held[1:]] == [True, True]
        assert len(bindings(host)) == CAP
    dispatch(host, scenario)


# The upgrade ---------------------------------------------------------------------------------------------------

def schema_19_database(host, proofs: list[tuple[str, float]]) -> None:
    """A schema-19 database holding one alias with these proofs, inserted in this (rowid) order."""
    host.config.data_dir.mkdir(mode=0o700)
    raw = sqlite3.connect(host.config.db_path)
    raw.executescript((FIXTURES / "schema_v19.sql").read_text())
    with raw:
        raw.execute("INSERT INTO aliases VALUES ('alias-m', ?, ?, ?, 1)",
                    (ALIAS, ALIAS, auth.hash_passphrase(SECRET)))
        raw.executemany("INSERT INTO alias_bindings VALUES (?, 'alias-m', ?)",
                        [(auth.token_digest(proof), expires_at) for proof, expires_at in proofs])
    raw.close()
    os.chmod(host.config.db_path, 0o600)


def binding_shape(conn: sqlite3.Connection) -> tuple[list[tuple], list[tuple]]:
    columns = [tuple(row) for row in conn.execute("PRAGMA table_info(alias_bindings)")]
    indexes = sorted((row["name"], row["unique"], tuple(column["name"] for column in conn.execute(
        f"PRAGMA index_info({row['name']})"))) for row in conn.execute("PRAGMA index_list(alias_bindings)"))
    return columns, indexes


def test_an_upgraded_database_orders_its_bindings_by_expiry_then_rowid_and_replaces_the_first(tmp_path):
    host = capped_host(tmp_path)
    later = time.time() + 3600
    schema_19_database(host, [("proof-late", later + 1), ("proof-tie-first", later), ("proof-tie-second", later)])

    conn = store.connect(host.config.db_path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == store.SCHEMA_VERSION
        assert "issued" in {row["name"] for row in conn.execute("PRAGMA table_info(alias_bindings)")}
        ordered = [row[0] for row in conn.execute("SELECT digest FROM alias_bindings ORDER BY issued")]
    finally:
        conn.close()
    assert ordered == [auth.token_digest(proof) for proof in ("proof-tie-first", "proof-tie-second", "proof-late")]

    async def scenario(client):
        assert "binding_token" in await claim(client, host, ALIAS)
        assert not await works(client, host, "proof-tie-first")
        assert [await works(client, host, proof) for proof in ("proof-tie-second", "proof-late")] == [True, True]
    dispatch(host, scenario)


def test_a_fresh_database_has_the_binding_order_an_upgraded_one_gets(tmp_path):
    shapes = []
    for kind in ("fresh", "upgraded"):
        (tmp_path / kind).mkdir(mode=0o700)
        host = capped_host(tmp_path / kind)
        if kind == "upgraded":
            schema_19_database(host, [])
        else:
            host.config.data_dir.mkdir(mode=0o700)
        conn = store.connect(host.config.db_path)
        try:
            shapes.append(binding_shape(conn))
        finally:
            conn.close()
    fresh, upgraded = shapes
    assert "issued" in {column[1] for column in fresh[0]}
    assert fresh == upgraded
