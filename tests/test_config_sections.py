"""Malformed section shapes are config refusals, without changing accepted section behavior."""

from __future__ import annotations

import json

import pytest

from baton import cli, config

BASE = {"host": {"public_url": "https://baton.example.test"}, "roles": {"planner": {}}}
SENSITIVE = "private-section-value"


@pytest.mark.parametrize(
    ("section", "value", "detail"),
    [
        ("host", f'"{SENSITIVE}"', "host must be a table"),
        ("roles", "[]", "roles must be a table"),
        ("auth", "false", "auth must be a table"),
        ("integrations", "[false]", "integrations must be a table"),
        ("indexer", "[]", "indexer must be a table"),
        ("conversations", "true", "conversations must be a table"),
        ("dashboard", "false", "dashboard must be a table"),
        ("seat", "false", "seat must be a table"),
        ("search", "false", "search must be a table"),
        ("jobs", "1", "jobs must be a table"),
        ("roles", f'{{planner = "{SENSITIVE}"}}', "roles.planner must be a table"),
        ("roles", "{planner = {wake = false}}", "roles.planner.wake must be a table"),
        ("auth", f'{{oauth = "{SENSITIVE}"}}', "auth.oauth must be a table"),
        ("auth", "{tokens = 42}", "auth.tokens must be an array of tables"),
        ("auth", f'{{tokens = ["{SENSITIVE}"]}}', "auth.tokens[0] must be a table"),
        ("jobs", f'{{states = "{SENSITIVE}"}}', "jobs.states must be a table"),
        ("jobs", f'{{states = {{open = "{SENSITIVE}"}}}}', "jobs.states.open must be a table"),
    ],
)
def test_malformed_toml_sections_produce_config_json(tmp_path, monkeypatch, capsys, section, value, detail):
    fields = {"host": '{public_url = "https://baton.example.test"}', "roles": "{planner = {}}"}
    fields[section] = value
    path = tmp_path / "baton.toml"
    path.write_text("\n".join(f"{key} = {literal}" for key, literal in fields.items()))
    with pytest.raises(config.ConfigError) as raised:
        config.load(path)
    assert str(raised.value) == detail

    monkeypatch.delenv("BATON_HOST", raising=False)
    assert cli.main(["--config", str(path), "wakes"]) == 2
    output = capsys.readouterr().out
    assert json.loads(output) == {"error": "config", "detail": detail}
    assert SENSITIVE not in output


@pytest.mark.parametrize("section", ["jobs", "states"])
def test_empty_job_tables_select_the_default_graph(section):
    jobs = {} if section == "jobs" else {"states": {}}
    assert config.parse({**BASE, "jobs": jobs}).jobs == config.parse(BASE).jobs


@pytest.mark.parametrize("value", [None, False, 0, "", []])
def test_a_falsey_job_state_is_not_treated_as_an_empty_table(value):
    with pytest.raises(config.ConfigError, match=r"^jobs.states.open must be a table$"):
        config.parse({**BASE, "jobs": {"states": {"open": value}}})


def test_empty_token_list_is_accepted():
    assert config.parse({**BASE, "auth": {"tokens": []}}).tokens == ()


def test_disabled_features_still_ignore_their_other_settings():
    parsed = config.parse(
        {
            **BASE,
            "dashboard": {"enabled": False, "viewer_role": [], "attention_role": {}},
            "seat": {"enabled": False, "role": [], "origin": False, "timezone": {}},
            "auth": {"oauth": {"enabled": False, "roles": False, "redirect_uris": 42, "passphrase_hash_env": []}},
        }
    )
    assert not parsed.dashboard.enabled and not parsed.seat.enabled and not parsed.oauth.enabled


def test_existing_specialized_table_refusals_keep_their_messages():
    with pytest.raises(config.ConfigError, match="^aliases must be a table of limits$"):
        config.parse({**BASE, "aliases": False})
    with pytest.raises(config.ConfigError) as raised:
        config.parse({**BASE, "integrations": {"example": False}})
    assert "integrations.example must be a table" in str(raised.value)
