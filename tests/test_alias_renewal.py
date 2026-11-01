"""The renewal contract: ordinary renewals, the owner's single-use rekey arm and the armed reset."""
from __future__ import annotations

import pytest
from test_aliases import dispatch
from test_per_call_identity import OTHER, build_host, call, claim, db, enrol, owner

THIRD = "synthetic third secret"


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
