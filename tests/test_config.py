from __future__ import annotations

import pytest

from baton.config import DEFAULT_JOB_STATES, ConfigError, parse

BASE = {"host": {"public_url": "https://baton.example.test/"}, "roles": {"planner": {}, "worker": {}}}


def test_a_minimal_config_gets_the_default_job_graph() -> None:
    config = parse(BASE)
    assert config.public_url == "https://baton.example.test"
    assert config.resource_url == "https://baton.example.test/mcp"
    assert set(config.jobs.states) == set(DEFAULT_JOB_STATES)
    assert config.jobs.initial == "open"
    assert config.jobs.states["done"].terminal
    assert not config.oauth.enabled and config.tokens == ()


@pytest.mark.parametrize(("change", "message"), [
    ({"host": {"public_url": "baton.example.test"}}, "public_url"),
    ({"roles": {}}, "at least one role"),
    ({"roles": {"Bad Name": {}}}, "must match"),
    ({"roles": {"planner": {"wake": {"webhook": "https://a.test", "command": ["true"]}}}}, "both"),
    ({"roles": {"planner": {"wake": {"command": "true"}}}}, "list of strings"),
    ({"auth": {"tokens": [{"role": "nobody", "sha256": "0" * 64}]}}, "unknown role"),
    ({"auth": {"tokens": [{"role": "planner", "sha256": "short"}]}}, "SHA-256"),
    ({"auth": {"oauth": {"enabled": True, "roles": ["planner"]}}}, "passphrase_hash"),
    ({"auth": {"oauth": {"enabled": True, "passphrase_hash": "scrypt$a$b", "roles": ["nobody"]}}}, "roles"),
    ({"jobs": {"states": {"open": {"waiting_on": "nobody"}}}}, "unknown role"),
    ({"jobs": {"states": {"open": {"next": ["missing"]}}}}, "unknown states"),
    ({"jobs": {"initial": "missing", "states": {"open": {}}}}, "not a state"),
])
def test_inconsistent_configs_are_refused(change: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse({**BASE, **change})


def test_the_oauth_passphrase_hash_can_come_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("EXAMPLE_HASH", "scrypt$00$00")
    config = parse({**BASE, "auth": {"oauth": {"enabled": True, "passphrase_hash_env": "EXAMPLE_HASH",
                                               "roles": ["planner"]}}})
    assert config.oauth.passphrase_hash == "scrypt$00$00"
