"""The renewal contract: ordinary renewals, the owner's single-use rekey arm and the armed reset."""
from __future__ import annotations

import time
from dataclasses import replace

import pytest
from test_aliases import dispatch
from test_per_call_identity import OTHER, SECRET, build_host, call, claim, db, enrol, owner

from baton import aliases, auth, store
from baton import config as config_module
from baton.config import ConfigError
from baton.errors import Forbidden

THIRD = "synthetic third secret"
ARM_SECONDS = 15 * 60  # the contract's arm lifetime


@pytest.fixture
def host(tmp_path):
    return build_host(tmp_path)


def arms(host):
    with db(host) as conn:
        return [tuple(row) for row in conn.execute("SELECT kind, target FROM alias_arms")]


def test_an_ordinary_renewal_leaves_a_live_arm_untouched(host):
    async def scenario(client):
        await enrol(client, host)
        assert owner(host, "alias", "arm-rekey", "hilary") == 0
        renewed = await claim(client, host)
        assert renewed.get("effective_role") == "hilary"
        assert arms(host) == [("rekey", "hilary")]
    dispatch(host, scenario)


def test_an_armed_reset_replaces_the_verifier_drops_old_proofs_and_spends_the_arm(host):
    async def scenario(client):
        await enrol(client, host)
        old = (await claim(client, host))["binding_token"]
        assert owner(host, "alias", "arm-rekey", "hilary") == 0
        reset = await claim(client, host, passphrase=OTHER)
        assert reset.get("effective_role") == "hilary" and reset["binding_token"] != old
        assert arms(host) == []
        assert (await call(client, host, "whoami", {"token": old})).get("error") == "forbidden"
        assert (await call(client, host, "whoami", {"token": reset["binding_token"]}))["effective_role"] == "hilary"
        with db(host) as conn:
            assert conn.execute("SELECT COUNT(*) FROM alias_bindings").fetchone()[0] == 1
        assert (await claim(client, host)).get("error") == "forbidden", "the old passphrase is refused"
        retry = await claim(client, host, passphrase=OTHER)
        assert retry.get("effective_role") == "hilary", "a retry after the reset renews without an arm"
        assert (await claim(client, host, passphrase=THIRD)).get("error") == "forbidden", "the arm was single use"
    dispatch(host, scenario)


def test_an_armed_reset_works_on_a_guest_alias_from_any_credential(host):
    async def scenario(client):
        assert (await claim(client, host, "lane-y")).get("effective_role") == "guest"
        assert owner(host, "alias", "arm-rekey", "lane-y") == 0
        assert (await claim(client, host, "lane-y", OTHER, role="planner")).get("effective_role") == "guest"
        assert (await claim(client, host, "lane-y", OTHER)).get("effective_role") == "guest"
    dispatch(host, scenario)


def test_a_fixed_role_renews_only_its_own_enrolled_alias(host):
    async def scenario(client):
        assert (await claim(client, host, "lane-z")).get("effective_role") == "guest"
        assert (await claim(client, host, "lane-z", role="planner")).get("error") == "forbidden"
    dispatch(host, scenario)


@pytest.mark.parametrize("arguments", [{"name": "Not Valid!", "passphrase": OTHER},
                                       {"name": "hilary", "passphrase": ""}])
def test_a_failed_validation_consumes_no_arm(host, arguments):
    async def scenario(client):
        await enrol(client, host)
        assert owner(host, "alias", "arm-rekey", "hilary") == 0
        assert "error" in await call(client, host, "claim_alias", arguments)
        assert arms(host) == [("rekey", "hilary")]
    dispatch(host, scenario)


def test_a_rekey_arm_expires(host):
    async def scenario(client):
        await enrol(client, host)
        assert owner(host, "alias", "arm-rekey", "hilary") == 0
        with db(host) as conn:
            conn.execute("UPDATE alias_arms SET expires_at = expires_at - 901")
        assert (await claim(client, host, passphrase=OTHER)).get("error") == "forbidden"
    dispatch(host, scenario)


def test_arm_rekey_refuses_an_unknown_alias(host):
    async def scenario(client):
        assert owner(host, "alias", "arm-rekey", "nobody-here") == 1
        assert arms(host) == []
    dispatch(host, scenario)


def test_recovery_works_at_the_claim_and_binding_caps(host):
    config = host.config
    host.config = replace(config, aliases=replace(config.aliases, max_bindings_per_alias=2,
                                                  max_claims_per_alias_per_hour=3))

    async def scenario(client):
        await enrol(client, host)
        assert "binding_token" in await claim(client, host)
        assert (await claim(client, host)).get("error") == "limit_exceeded"
        assert (await claim(client, host)).get("error") == "limit_exceeded"
        assert owner(host, "alias", "arm-rekey", "hilary") == 0
        assert (await claim(client, host)).get("error") == "limit_exceeded", "an ordinary renewal keeps the caps"
        assert arms(host) == [("rekey", "hilary")]
        reset = await claim(client, host, passphrase=OTHER)
        assert reset.get("effective_role") == "hilary", reset
        with db(host) as conn:
            assert conn.execute("SELECT COUNT(*) FROM alias_bindings").fetchone()[0] == 1
    dispatch(host, scenario)


def test_a_reset_after_a_renewal_ends_the_renewals_proof(host):
    async def scenario(client):
        await enrol(client, host)
        assert owner(host, "alias", "arm-rekey", "hilary") == 0
        renewed = (await claim(client, host))["binding_token"]
        assert (await claim(client, host, passphrase=OTHER)).get("effective_role") == "hilary"
        assert (await call(client, host, "whoami", {"token": renewed})).get("error") == "forbidden"
    dispatch(host, scenario)


def enrolled_and_armed(host):
    async def scenario(client):
        await enrol(client, host)
    dispatch(host, scenario)
    assert owner(host, "alias", "arm-rekey", "hilary") == 0


def direct_claim(host, passphrase):
    """A claim straight through `aliases.claim`, so a patched clock or check sees only the claim's own calls."""
    conn = store.connect(host.config.db_path)
    try:
        return aliases.claim(conn, host.config, auth.static_token(host.config, host.tokens["guest"]), "hilary",
                             passphrase)
    finally:
        conn.close()


def test_an_arm_that_expires_after_the_early_check_is_not_spent(host, monkeypatch):
    enrolled_and_armed(host)
    start = time.time()
    clock = iter([start])
    monkeypatch.setattr(aliases.time, "time", lambda: next(clock, start + ARM_SECONDS + 1))
    with pytest.raises(Forbidden):
        direct_claim(host, OTHER)
    monkeypatch.undo()
    assert direct_claim(host, SECRET)["effective_role"] == "hilary", "the verifier is unchanged"


def test_an_arm_spent_by_a_concurrent_call_after_the_early_check_resets_nothing(host, monkeypatch):
    enrolled_and_armed(host)
    original = auth.check_passphrase

    def check_then_lose_the_arm(passphrase, verifier):
        matched = original(passphrase, verifier)
        with db(host) as conn:
            conn.execute("DELETE FROM alias_arms")
        return matched

    monkeypatch.setattr(auth, "check_passphrase", check_then_lose_the_arm)
    with pytest.raises(Forbidden):
        direct_claim(host, OTHER)
    monkeypatch.undo()
    assert direct_claim(host, SECRET)["effective_role"] == "hilary", "the verifier is unchanged"


def test_mapped_bindings_last_the_mapped_lifetime_and_guest_bindings_stay_short(host):
    async def scenario(client):
        await enrol(client, host)
        before = time.time()
        mapped = await claim(client, host)
        guest = await claim(client, host, "lane-g")
        assert before + 604800 <= mapped["expires_at"] <= time.time() + 604800
        assert before + 86400 <= guest["expires_at"] <= time.time() + 86400
    dispatch(host, scenario)


@pytest.mark.parametrize("value,accepted", [(604800, True), (1, True), (604801, False), (0, False)])
def test_the_mapped_lifetime_is_bounded_in_config(tmp_path, value, accepted):
    path = build_host(tmp_path).config_path
    path.write_text(path.read_text().replace("[aliases]\n", f"[aliases]\nmapped_binding_ttl_seconds = {value}\n"))
    if accepted:
        assert getattr(config_module.load(path).aliases, "mapped_binding_ttl_seconds", None) == value
    else:
        with pytest.raises(ConfigError):
            config_module.load(path)
