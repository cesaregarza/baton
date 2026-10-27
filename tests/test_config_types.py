"""Operator-supplied booleans and numbers must have the right TOML type; nothing is read by truthiness."""

from __future__ import annotations

from typing import Any

import pytest

from baton.config import ConfigError, parse

HASH = "scrypt$00$00"


def config(**sections: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {"host": {"public_url": "https://baton.example.test"}, "roles": {"planner": {}}}
    for key, value in sections.items():
        raw[key] = value
    return raw


def oauth(**fields: Any) -> dict[str, Any]:
    return {"oauth": {"passphrase_hash": HASH, "roles": ["planner"], **fields}}


def refused(raw: dict[str, Any], key: str) -> None:
    try:
        parse(raw)
    except ConfigError as error:
        assert key in str(error), f"the refusal should name {key}: {error}"
        return
    except Exception as error:  # a crash is a missing check, not a refusal
        pytest.fail(f"{key}: raised {type(error).__name__}, not ConfigError")
    pytest.fail(f"{key}: a non-boolean or out-of-range value was accepted")


@pytest.mark.parametrize("value", ["false", "true", 0, 1])
def test_oauth_enabled_must_be_a_boolean(value: Any) -> None:
    refused(config(auth=oauth(enabled=value)), "auth.oauth.enabled")


@pytest.mark.parametrize("value", ["false", "true", 0, 1])
def test_allow_loopback_redirects_must_be_a_boolean(value: Any) -> None:
    refused(config(auth=oauth(enabled=True, allow_loopback_redirects=value)), "auth.oauth.allow_loopback_redirects")


@pytest.mark.parametrize("value", ["8765", True, 0, -1, 65536, 8765.5])
def test_listen_port_must_be_a_port_number(value: Any) -> None:
    refused({**config(), "host": {"public_url": "https://baton.example.test", "listen_port": value}},
            "host.listen_port")


@pytest.mark.parametrize("value", ["10", True, 0, -1, 10**6])
def test_wake_timeout_must_be_a_bounded_number(value: Any) -> None:
    refused(config(roles={"planner": {"wake": {"command": ["true"], "timeout_seconds": value}}}),
            "roles.planner.wake.timeout_seconds")


def test_valid_booleans_and_numbers_behave_as_before() -> None:
    assert not parse(config(auth=oauth(enabled=False))).oauth.enabled
    assert not parse(config()).oauth.enabled  # missing: off, as before
    on = parse(config(auth=oauth(enabled=True)))
    assert on.oauth.enabled and on.oauth.allow_loopback_redirects  # missing: allowed, as before
    assert not parse(config(auth=oauth(enabled=True, allow_loopback_redirects=False))).oauth.allow_loopback_redirects
    assert parse(config(auth=oauth(enabled=True, allow_loopback_redirects=True))).oauth.allow_loopback_redirects
    assert parse(config()).listen_port == 8765
    assert parse({**config(), "host": {"public_url": "https://baton.example.test", "listen_port": 9000}}).listen_port \
        == 9000
    assert parse(config(roles={"planner": {"wake": {"command": ["true"]}}})).roles["planner"].wake.timeout_seconds == 10
    assert parse(config(roles={"planner": {"wake": {"command": ["true"], "timeout_seconds": 2.5}}})).roles[
        "planner"].wake.timeout_seconds == 2.5
    assert parse(config(roles={"planner": {"wake": {"command": ["true"], "timeout_seconds": 3}}})).roles[
        "planner"].wake.timeout_seconds == 3
