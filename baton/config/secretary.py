"""Own secretary policy normalization/provider settings and installed worker configuration."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from baton import datadir
from baton.config import Config, ConfigError

POLICY_VERSION = 3
MAX_POLICY_BYTES = 16 * 1024
INTEGRATION_VERSION = 1
MAX_BUDGET_SECONDS = 60
MAX_SCHEDULE_SECONDS = 24 * 60 * 60
HOOK_SUMMARY_BYTES = 6000
MAX_MODEL_RESPONSE_BYTES = 1024 * 1024
MAX_COMMAND_RESPONSE_BYTES = 64 * 1024


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Policy:
    principal: str
    secretary_id: str
    model: dict
    decision_engine: dict
    instructions: str
    knowledge: str
    max_tool_calls: int
    max_events: int
    context_bytes: int
    summary_bytes: int
    wake_threshold: float
    retry_initial_seconds: float
    retry_max_seconds: float
    max_attempts: int
    digest_seconds: float
    raw: dict
    digest: str

    @property
    def decision_timeout(self) -> float:
        return self.decision_engine["timeout_seconds"]


def load_policy(config: Config, path: Path) -> Policy:
    try:
        data = path.read_bytes()
        if len(data) > MAX_POLICY_BYTES:
            raise ConfigError("secretary policy exceeds 16 KiB")
        raw = json.loads(data)
    except (OSError, ValueError) as error:
        raise ConfigError("cannot read secretary policy JSON") from error
    if not isinstance(raw, dict) or type(raw.get("version")) is not int:
        raise ConfigError("secretary policy must name version 3")
    if raw["version"] != POLICY_VERSION:
        raise ConfigError("unsupported secretary policy version; use version 3 with explicit model configuration")
    fields = {
        "version",
        "principal",
        "secretary_id",
        "model",
        "decision_engine",
        "instructions",
        "knowledge",
        "max_tool_calls",
        "max_events",
        "context_bytes",
        "summary_bytes",
        "wake_threshold",
        "retry_initial_seconds",
        "retry_max_seconds",
        "max_attempts",
        "digest_seconds",
    }
    if "principal" not in raw or raw.keys() - fields:
        raise ConfigError("secretary policy fields are invalid")
    principal = raw["principal"]
    role = config.roles.get(principal) if isinstance(principal, str) else None
    if role is None or principal == "guest" or role.human or role.display_only:
        raise ConfigError("secretary principal must be an existing non-human, non-display agent role")
    secretary_id = raw.get("secretary_id")
    if not isinstance(secretary_id, str) or not secretary_id.strip() or len(secretary_id) > 200:
        raise ConfigError("secretary_id must be a nonempty stable identifier of at most 200 characters")
    instructions = _text(raw.get("instructions", ""), "instructions")
    knowledge = _text(raw.get("knowledge", ""), "knowledge")
    if "model" not in raw or "decision_engine" not in raw:
        raise ConfigError("secretary policy requires explicit model and decision_engine configuration")
    model = _model(raw["model"])
    engine = _engine(raw["decision_engine"])
    max_tool_calls = _integer(raw.get("max_tool_calls"), "max_tool_calls", 1, 8)
    max_events = _integer(raw.get("max_events"), "max_events", 1, 10000)
    context_bytes = _integer(raw.get("context_bytes"), "context_bytes", 1024, 8388608)
    summary_bytes = _integer(raw.get("summary_bytes"), "summary_bytes", 256, 65536)
    wake_threshold = _probability(raw.get("wake_threshold"), "wake_threshold")
    retry_initial_seconds = _duration(raw.get("retry_initial_seconds"), "retry_initial_seconds")
    retry_max_seconds = _duration(raw.get("retry_max_seconds"), "retry_max_seconds")
    if retry_max_seconds < retry_initial_seconds:
        raise ConfigError("retry_max_seconds must be greater than or equal to retry_initial_seconds")
    max_attempts = _integer(raw.get("max_attempts"), "max_attempts", 1, 100)
    digest_seconds = _duration(raw.get("digest_seconds"), "digest_seconds")
    normalized = {
        "version": POLICY_VERSION,
        "principal": principal,
        "secretary_id": secretary_id,
        "model": model,
        "decision_engine": engine,
        "instructions": instructions,
        "knowledge": knowledge,
        "max_tool_calls": max_tool_calls,
        "max_events": max_events,
        "context_bytes": context_bytes,
        "summary_bytes": summary_bytes,
        "wake_threshold": wake_threshold,
        "retry_initial_seconds": retry_initial_seconds,
        "retry_max_seconds": retry_max_seconds,
        "max_attempts": max_attempts,
        "digest_seconds": digest_seconds,
    }
    return Policy(
        principal=principal,
        secretary_id=secretary_id,
        model=model,
        decision_engine=engine,
        instructions=instructions,
        knowledge=knowledge,
        max_tool_calls=max_tool_calls,
        max_events=max_events,
        context_bytes=context_bytes,
        summary_bytes=summary_bytes,
        wake_threshold=wake_threshold,
        retry_initial_seconds=retry_initial_seconds,
        retry_max_seconds=retry_max_seconds,
        max_attempts=max_attempts,
        digest_seconds=digest_seconds,
        raw=normalized,
        digest=fingerprint(normalized),
    )


def require_hook_summary(policy: Policy) -> None:
    if policy.summary_bytes > HOOK_SUMMARY_BYTES:
        raise ConfigError("hook integrations require summary_bytes <= 6000; update the policy")


def validate_decision_budget(policy: Policy, assessment_budget_seconds: float) -> None:
    """Ensure a decision engine timeout leaves time for assessment work to finish."""
    budget = _timeout(assessment_budget_seconds, "budget_seconds")
    if policy.decision_timeout >= budget:
        raise ConfigError("decision_engine.timeout_seconds must be less than the assessment budget_seconds")


@dataclass(frozen=True)
class IntegrationConfig:
    """Validated version 1 JSON settings for an installed secretary worker."""

    product: str
    project: Path
    config: Path
    policy: Path
    state: Path
    poll_seconds: float
    budget_seconds: float
    delivery_timeout_seconds: float
    codex: str | None = None
    version: int = INTEGRATION_VERSION

    @classmethod
    def load(cls, path: Path) -> IntegrationConfig:
        path = datadir.canonical(path)
        with datadir.opened(path.parent) as directory:
            fd = datadir.open_file(directory, path.name, os.O_RDONLY, directory=path.parent)
            with os.fdopen(fd) as source:
                value = json.load(source)
        return cls.from_dict(value, instance_path=path)

    @classmethod
    def from_dict(cls, value: object, *, instance_path: Path | None = None) -> IntegrationConfig:
        if not isinstance(value, dict):
            raise ConfigError("unsupported secretary integration")
        if value.get("version") != INTEGRATION_VERSION or value.get("product") not in {"codex", "claude-code"}:
            raise ConfigError("unsupported secretary integration")
        values = {}
        for name in ("project", "config", "policy", "state"):
            candidate = value.get(name)
            if not isinstance(candidate, str) or not Path(candidate).is_absolute():
                raise ConfigError(f"integration {name} must be an absolute path")
            values[name] = Path(candidate)
        if instance_path is not None and values["state"].parent != instance_path.parent:
            raise ConfigError("integration state must be in its private instance directory")
        poll_seconds = _timeout(value.get("poll_seconds"), "poll_seconds")
        budget_seconds = _timeout(value.get("budget_seconds"), "budget_seconds")
        delivery_timeout_seconds = _timeout(value.get("delivery_timeout_seconds"), "delivery_timeout_seconds")
        if 2 * budget_seconds + poll_seconds > MAX_BUDGET_SECONDS:
            raise ConfigError("two assessment passes plus the poll interval must fit within 60 seconds")
        product = value["product"]
        executable = value.get("codex")
        if product == "codex" and (
            not isinstance(executable, str) or not Path(executable).is_absolute() or not os.access(executable, os.X_OK)
        ):
            raise ConfigError("codex must name an absolute executable path")
        return cls(
            product=product,
            **values,
            codex=executable,
            poll_seconds=poll_seconds,
            budget_seconds=budget_seconds,
            delivery_timeout_seconds=delivery_timeout_seconds,
            version=INTEGRATION_VERSION,
        )

    def as_dict(self) -> dict:
        value = {
            "version": self.version,
            "product": self.product,
            "project": str(self.project),
            "config": str(self.config),
            "policy": str(self.policy),
            "state": str(self.state),
            "poll_seconds": self.poll_seconds,
            "budget_seconds": self.budget_seconds,
            "delivery_timeout_seconds": self.delivery_timeout_seconds,
        }
        if self.codex is not None:
            value["codex"] = self.codex
        return value


def _model(value: object) -> dict:
    if not isinstance(value, dict):
        raise ConfigError("model must be an object")
    provider = value.get("provider")
    common = {"provider", "timeout_seconds"}
    if provider == "command":
        if value.keys() != common | {"command", "env", "max_output_tokens"}:
            raise ConfigError("command model fields are invalid")
        return {
            "provider": provider,
            "command": _command(value.get("command")),
            "env": _environment_names(value.get("env"), "model.env"),
            "timeout_seconds": _timeout(value.get("timeout_seconds")),
            "max_output_tokens": _integer(value.get("max_output_tokens"), "model.max_output_tokens", 256, 32768),
        }
    required = common | {
        "model",
        "base_url",
        "api_key_env",
        "reasoning_effort",
        "max_output_tokens",
        "cache_mode",
    }
    if provider != "openai" or value.keys() != required:
        raise ConfigError("model provider or fields are invalid")
    settings = dict(value)
    if settings["cache_mode"] not in ("implicit", "layered"):
        raise ConfigError("model.cache_mode must be implicit or layered")
    if (
        not isinstance(settings["reasoning_effort"], str)
        or not settings["reasoning_effort"].strip()
        or len(settings["reasoning_effort"]) > 100
    ):
        raise ConfigError("model.reasoning_effort must be a nonempty string of at most 100 characters")
    settings["max_output_tokens"] = _integer(settings["max_output_tokens"], "max_output_tokens", 256, 32768)
    return _http_settings(settings, "model")


def _http_settings(settings: dict, label: str) -> dict:
    if not isinstance(settings["model"], str) or not settings["model"].strip() or len(settings["model"]) > 200:
        raise ConfigError(f"{label}.model must be a nonempty model ID")
    if not isinstance(settings["api_key_env"], str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", settings["api_key_env"]
    ):
        raise ConfigError(f"{label}.api_key_env must name an environment variable")
    try:
        url = urlsplit(settings["base_url"])
        valid = (
            url.scheme in ("http", "https")
            and url.hostname
            and not (url.username or url.password or url.query or url.fragment)
        )
        # HTTP is useful for a local model server; credentials must not cross plaintext remote networks.
        valid = valid and (url.scheme == "https" or url.hostname in ("localhost", "127.0.0.1", "::1"))
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ConfigError(f"{label}.base_url must be HTTPS, or HTTP on localhost, without credentials/query/fragment")
    settings["base_url"] = settings["base_url"].rstrip("/")
    settings["timeout_seconds"] = _timeout(settings["timeout_seconds"])
    return settings


def _engine(value: object) -> dict:
    if not isinstance(value, dict):
        raise ConfigError("decision_engine must be an explicitly configured object")
    provider = value.get("provider")
    if provider == "command":
        if value.keys() != {"provider", "command", "env", "timeout_seconds", "max_output_tokens"}:
            raise ConfigError("command decision_engine fields are invalid")
        return {
            "provider": provider,
            "command": _command(value.get("command")),
            "env": _environment_names(value.get("env"), "decision_engine.env"),
            "timeout_seconds": _timeout(value.get("timeout_seconds")),
            "max_output_tokens": _integer(
                value.get("max_output_tokens"), "decision_engine.max_output_tokens", 256, 32768
            ),
        }
    expected = {
        "provider",
        "model",
        "base_url",
        "api_key_env",
        "timeout_seconds",
    }
    if provider != "jev" or value.keys() != expected:
        raise ConfigError("decision_engine must explicitly name provider jev or command and all provider settings")
    return _http_settings(dict(value), "decision_engine")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value.encode()) > 8192:
        raise ConfigError(f"secretary {field} must be text of at most 8192 bytes")
    return value


def _command(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 32
        or not all(isinstance(part, str) and 0 < len(part) <= 4096 and "\0" not in part for part in value)
        or not Path(value[0]).is_absolute()
    ):
        raise ConfigError("command must be an argv list starting with an absolute executable path")
    return value


def _environment_names(value: object, field: str) -> list[str]:
    """Validate an explicit allowlist of host environment names for commands."""
    if (
        not isinstance(value, list)
        or len(value) > 32
        or not all(isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in value)
        or len(set(value)) != len(value)
    ):
        raise ConfigError(f"{field} must be a list of at most 32 unique environment variable names")
    return value


def _timeout(value: object, field: str = "timeout_seconds") -> float:
    if (
        not (type(value) is int or type(value) is float)
        or not math.isfinite(value)
        or not 0 < value <= MAX_BUDGET_SECONDS
    ):
        raise ConfigError(f"{field} must be greater than zero and at most {MAX_BUDGET_SECONDS}")
    return float(value)


def _integer(value: object, field: str, least: int, most: int) -> int:
    if type(value) is not int or not least <= value <= most:
        raise ConfigError(f"{field} must be an integer from {least} to {most}")
    return value


def _probability(value: object, field: str) -> float:
    if not (type(value) is int or type(value) is float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ConfigError(f"{field} must be a finite number from 0 to 1")
    return float(value)


def _duration(value: object, field: str) -> float:
    if (
        not (type(value) is int or type(value) is float)
        or not math.isfinite(value)
        or not 0 < value <= MAX_SCHEDULE_SECONDS
    ):
        raise ConfigError(f"{field} must be greater than zero and at most {MAX_SCHEDULE_SECONDS}")
    return float(value)
