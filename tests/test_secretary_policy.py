"""Configuration contracts for explicit secretary models and attention thresholds."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from baton.config import ConfigError
from baton.config.secretary import load_policy


def _raw() -> dict:
    return {
        "version": 3,
        "principal": "planner",
        "secretary_id": "ops-secretary",
        "model": {
            "provider": "openai",
            "model": "configured-triage-model",
            "base_url": "https://models.example.invalid/v1",
            "api_key_env": "TRIAGE_KEY",
            "reasoning_effort": "none",
            "max_output_tokens": 2048,
            "timeout_seconds": 15,
            "cache_mode": "implicit",
        },
        "decision_engine": {
            "provider": "jev",
            "model": "configured-decision-model",
            "base_url": "https://decision.example.invalid/v1",
            "api_key_env": "DECISION_KEY",
            "timeout_seconds": 3,
        },
        "instructions": "",
        "knowledge": "",
        "max_tool_calls": 2,
        "max_events": 500,
        "context_bytes": 1048576,
        "summary_bytes": 8192,
        "wake_threshold": 0.8,
        "retry_initial_seconds": 2,
        "retry_max_seconds": 30,
        "max_attempts": 5,
        "digest_seconds": 10,
    }


def _config():
    return SimpleNamespace(roles={"planner": SimpleNamespace(human=False, display_only=False)})


def _load(tmp_path: Path, value: dict | None = None):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(_raw() if value is None else value))
    return load_policy(_config(), path)


def test_explicit_model_engine_and_threshold_are_normalized(tmp_path) -> None:
    policy = _load(tmp_path)
    assert policy.secretary_id == "ops-secretary"
    assert policy.model["model"] == "configured-triage-model"
    assert policy.decision_engine["model"] == "configured-decision-model"
    assert policy.wake_threshold == 0.8
    assert policy.raw["retry_initial_seconds"] == 2.0
    assert policy.raw["digest_seconds"] == 10.0


def test_reasoning_effort_accepts_bounded_nonempty_provider_values(tmp_path) -> None:
    value = _raw()
    value["model"]["reasoning_effort"] = "minimal"

    assert _load(tmp_path, value).model["reasoning_effort"] == "minimal"

    value["model"]["reasoning_effort"] = " \t "
    with pytest.raises(ConfigError, match="reasoning_effort"):
        _load(tmp_path, value)


@pytest.mark.parametrize("effort", [" minimal", "minimal ", "min\x00imal", "low\nhigh", "\tminimal"])
def test_reasoning_effort_rejects_surrounding_whitespace_and_controls(tmp_path, effort) -> None:
    value = _raw()
    value["model"]["reasoning_effort"] = effort
    with pytest.raises(ConfigError, match="reasoning_effort"):
        _load(tmp_path, value)


@pytest.mark.parametrize(
    "field",
    [
        "secretary_id",
        "model",
        "decision_engine",
        "max_tool_calls",
        "max_events",
        "context_bytes",
        "summary_bytes",
        "wake_threshold",
        "retry_initial_seconds",
        "retry_max_seconds",
        "max_attempts",
        "digest_seconds",
    ],
)
def test_policy_requires_runtime_identity_model_and_limits(tmp_path, field: str) -> None:
    value = _raw()
    value.pop(field)
    with pytest.raises(ConfigError):
        _load(tmp_path, value)


@pytest.mark.parametrize("threshold", [-0.1, 1.1, True, float("nan")])
def test_wake_threshold_must_be_finite_probability(tmp_path, threshold) -> None:
    value = _raw()
    value["wake_threshold"] = threshold
    with pytest.raises(ConfigError, match="wake_threshold"):
        _load(tmp_path, value)


@pytest.mark.parametrize(
    "field",
    [
        "provider",
        "model",
        "base_url",
        "api_key_env",
        "reasoning_effort",
        "max_output_tokens",
        "timeout_seconds",
        "cache_mode",
    ],
)
def test_http_provider_requires_every_explicit_field(tmp_path, field: str) -> None:
    value = _raw()
    del value["model"][field]
    with pytest.raises(ConfigError, match="model provider or fields"):
        _load(tmp_path, value)


@pytest.mark.parametrize("field", ["provider", "model", "base_url", "api_key_env", "timeout_seconds"])
def test_jev_provider_requires_every_explicit_field(tmp_path, field: str) -> None:
    value = _raw()
    del value["decision_engine"][field]
    with pytest.raises(ConfigError, match="decision_engine"):
        _load(tmp_path, value)


def test_command_adapters_require_output_budgets(tmp_path) -> None:
    value = _raw()
    value["model"] = {
        "provider": "command",
        "command": ["/usr/bin/true"],
        "env": [],
        "timeout_seconds": 5,
    }
    with pytest.raises(ConfigError, match="command model fields"):
        _load(tmp_path, value)
    value["model"]["max_output_tokens"] = 512
    value["decision_engine"] = {
        "provider": "command",
        "command": ["/usr/bin/true"],
        "env": [],
        "timeout_seconds": 3,
    }
    with pytest.raises(ConfigError, match="command decision_engine fields"):
        _load(tmp_path, value)
