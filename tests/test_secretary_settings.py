"""Explicit settings and timeout relationships for installed secretary workers."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from secretary_support import explicit_policy

from baton.config import ConfigError
from baton.config.secretary import IntegrationConfig, load_policy, validate_decision_budget


def _config():
    return SimpleNamespace(roles={"planner": SimpleNamespace(human=False, display_only=False)})


def _policy_file(path: Path, *, engine_timeout: float = 3) -> Path:
    value = explicit_policy()
    value["decision_engine"]["timeout_seconds"] = engine_timeout
    value["summary_bytes"] = 4000
    path.write_text(json.dumps(value))
    return path


def _integration(**overrides) -> dict:
    return {
        "version": 1,
        "product": "claude-code",
        "project": "/tmp/project",
        "config": "/tmp/config.toml",
        "policy": "/tmp/policy.json",
        "state": "/tmp/instance/attention.sqlite",
        "poll_seconds": 1,
        "budget_seconds": 10,
        "delivery_timeout_seconds": 7,
        **overrides,
    }


def test_delivery_timeout_is_explicit_and_independent_of_assessment_budget() -> None:
    integration = IntegrationConfig.from_dict(_integration())

    assert integration.budget_seconds == 10
    assert integration.delivery_timeout_seconds == 7
    assert integration.as_dict()["delivery_timeout_seconds"] == 7
    with pytest.raises(ConfigError, match="delivery_timeout_seconds"):
        IntegrationConfig.from_dict({k: v for k, v in _integration().items() if k != "delivery_timeout_seconds"})


@pytest.mark.parametrize("budget", [3, 2])
def test_decision_engine_timeout_must_leave_assessment_time(tmp_path, budget: int) -> None:
    policy = load_policy(_config(), _policy_file(tmp_path / "policy.json"))

    with pytest.raises(ConfigError, match="less than the assessment budget"):
        validate_decision_budget(policy, budget)


def test_decision_engine_timeout_below_assessment_budget_is_valid(tmp_path) -> None:
    policy = load_policy(_config(), _policy_file(tmp_path / "policy.json"))

    validate_decision_budget(policy, 10)


def test_install_rejects_engine_timeout_not_shorter_than_budget(tmp_path, monkeypatch) -> None:
    install_module = importlib.import_module("baton.integrations.secretary.install")
    project = tmp_path / "project"
    project.mkdir()
    config_path = tmp_path / "baton.toml"
    config_path.write_text("stub")
    policy_path = _policy_file(tmp_path / "policy.json")
    destination = tmp_path / "instance"
    destination.mkdir(mode=0o700)
    activate_called = False

    def unexpected_activate(*args, **kwargs):
        nonlocal activate_called
        activate_called = True

    monkeypatch.setattr(install_module, "load", lambda _: _config())
    monkeypatch.setattr(install_module, "activate", unexpected_activate)

    with pytest.raises(ConfigError, match="less than the assessment budget"):
        install_module.install(
            destination=destination,
            project=project,
            config=config_path,
            policy=policy_path,
            product="claude-code",
            python=Path(sys.executable),
            poll_seconds=1,
            budget_seconds=3,
            delivery_timeout_seconds=7,
        )

    assert not activate_called
    assert list(destination.iterdir()) == []


def test_install_accepts_shorter_engine_timeout_and_writes_delivery_setting(tmp_path, monkeypatch) -> None:
    install_module = importlib.import_module("baton.integrations.secretary.install")
    project = tmp_path / "project"
    project.mkdir()
    config_path = tmp_path / "baton.toml"
    config_path.write_text("stub")
    policy_path = _policy_file(tmp_path / "policy.json")
    destination = tmp_path / "instance"
    destination.mkdir(mode=0o700)
    activated = []

    monkeypatch.setattr(install_module, "load", lambda _: _config())
    monkeypatch.setattr(install_module, "activate", lambda secretary: activated.append(secretary))

    result = install_module.install(
        destination=destination,
        project=project,
        config=config_path,
        policy=policy_path,
        product="claude-code",
        python=Path(sys.executable),
        poll_seconds=1,
        budget_seconds=10,
        delivery_timeout_seconds=7,
    )

    stored = json.loads(result["instance"].read_text())
    assert stored["budget_seconds"] == 10
    assert stored["delivery_timeout_seconds"] == 7
    assert len(activated) == 1


def test_instance_load_rejects_engine_timeout_before_secretary_startup(tmp_path, monkeypatch) -> None:
    instance_module = importlib.import_module("baton.integrations.secretary.instance")
    policy_path = _policy_file(tmp_path / "policy.json")
    integration = IntegrationConfig.from_dict(_integration(policy=str(policy_path), budget_seconds=3))
    secretary_constructed = False

    def unexpected_secretary(*args, **kwargs):
        nonlocal secretary_constructed
        secretary_constructed = True
        raise AssertionError("secretary startup must be rejected before construction")

    monkeypatch.setattr(instance_module.datadir, "canonical", lambda path: path)
    monkeypatch.setattr(instance_module.IntegrationConfig, "load", lambda _: integration)
    monkeypatch.setattr(instance_module.config, "load", lambda _: _config())
    monkeypatch.setattr(instance_module, "Secretary", unexpected_secretary)

    with pytest.raises(ConfigError, match="less than the assessment budget"):
        instance_module.Instance.load(tmp_path / "integration.json")

    assert not secretary_constructed


def test_standalone_decide_rejects_budget_before_activation_or_model(tmp_path, monkeypatch) -> None:
    service_module = importlib.import_module("baton.agents.secretary.service")
    queue_module = importlib.import_module("baton.agents.secretary.attention.queue")
    policy_path = _policy_file(tmp_path / "policy.json")
    calls = []

    class StubDatabaseWorker:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def call(self, function, *args, **kwargs):
            calls.append(function.__name__)
            return function(*args, **kwargs)

    def unexpected_activation(*args, **kwargs):
        raise AssertionError("budget validation must precede state activation")

    def unexpected_assessment(*args, **kwargs):
        raise AssertionError("budget validation must precede model assessment")

    monkeypatch.setattr(service_module, "DatabaseWorker", StubDatabaseWorker)
    monkeypatch.setattr(queue_module.InboxWindow, "head", unexpected_activation)
    monkeypatch.setattr(queue_module.AttentionQueue, "assess", unexpected_assessment)
    secretary = service_module.Secretary(_config(), tmp_path / "state.sqlite", policy_path)

    with pytest.raises(ConfigError, match="less than the assessment budget"):
        asyncio.run(secretary.decide(budget_seconds=3))

    assert calls == ["load_policy"]
