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

POLICY_VERSION = 2
MAX_POLICY_BYTES = 16 * 1024
INTEGRATION_VERSION = 1
DEFAULT_MODEL_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_MODEL_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL_API_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_REASONING_EFFORT = "low"
DEFAULT_MAX_OUTPUT_TOKENS = 4096
DEFAULT_MODEL_TIMEOUT_SECONDS = 20
DEFAULT_MODEL_CACHE_MODE = "layered"
DEFAULT_DECISION_ENGINE_PROVIDER = "jev"
DEFAULT_DECISION_ENGINE_MODEL = "jev-latest"
DEFAULT_DECISION_ENGINE_BASE_URL = "https://api.typesafe.ai/v1"
DEFAULT_DECISION_ENGINE_API_KEY_ENV = "TYPESAFE_API_KEY"
DEFAULT_DECISION_TIMEOUT_SECONDS = 3
DEFAULT_INTEGRATION_POLL_SECONDS = 2
DEFAULT_INTEGRATION_BUDGET_SECONDS = 30
MAX_BUDGET_SECONDS = 60
HOOK_SUMMARY_BYTES = 6000
MAX_MODEL_RESPONSE_BYTES = 1024 * 1024
MAX_COMMAND_RESPONSE_BYTES = 64 * 1024


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Policy:
    principal: str
    model: dict
    decision_engine: dict | None
    instructions: str
    knowledge: str
    max_tool_calls: int
    max_events: int
    context_bytes: int
    summary_bytes: int
    raw: dict
    digest: str

    @property
    def decision_timeout(self) -> float:
        if self.decision_engine is None:
            return DEFAULT_DECISION_TIMEOUT_SECONDS
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
        raise ConfigError("secretary policy must name version 2")
    if raw["version"] != POLICY_VERSION:
        raise ConfigError("unsupported secretary policy version; use version 2 with a model configuration")
    fields = {"version", "principal", "model", "decision_engine", "instructions", "knowledge", "max_tool_calls",
              "max_events", "context_bytes", "summary_bytes"}
    if "principal" not in raw or raw.keys() - fields:
        raise ConfigError("secretary policy fields are invalid")
    principal = raw["principal"]
    role = config.roles.get(principal) if isinstance(principal, str) else None
    if role is None or principal == "guest" or role.human or role.display_only:
        raise ConfigError("secretary principal must be an existing non-human, non-display agent role")
    instructions = _text(raw.get("instructions", ""), "instructions")
    knowledge = _text(raw.get("knowledge", ""), "knowledge")
    model = _model(raw.get("model", {}))
    engine = _engine(raw.get("decision_engine"))
    normalized = {"version": POLICY_VERSION, "principal": principal, "model": model,
                  "decision_engine": engine, "instructions": instructions, "knowledge": knowledge,
                  "max_tool_calls": _integer(raw.get("max_tool_calls", 2), "max_tool_calls", 1, 8),
                  "max_events": _integer(raw.get("max_events", 1000), "max_events", 1, 10000),
                  "context_bytes": _integer(raw.get("context_bytes", 1024 * 1024), "context_bytes", 1024, 8388608),
                  "summary_bytes": _integer(raw.get("summary_bytes", 8192), "summary_bytes", 256, 65536)}
    return Policy(**{key: value for key, value in normalized.items() if key != "version"},
                  raw=normalized, digest=fingerprint(normalized))


def require_hook_summary(policy: Policy) -> None:
    if policy.summary_bytes > HOOK_SUMMARY_BYTES:
        raise ConfigError("hook integrations require summary_bytes <= 6000; update the policy")


@dataclass(frozen=True)
class IntegrationConfig:
    """Validated version 1 JSON settings for an installed secretary worker."""

    product: str
    project: Path
    config: Path
    policy: Path
    state: Path
    codex: str | None = None
    poll_seconds: float = DEFAULT_INTEGRATION_POLL_SECONDS
    budget_seconds: float = DEFAULT_INTEGRATION_BUDGET_SECONDS
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
        if value.get("version") != INTEGRATION_VERSION or value.get("product") not in {
                "codex", "claude-code"}:
            raise ConfigError("unsupported secretary integration")
        values = {}
        for name in ("project", "config", "policy", "state"):
            candidate = value.get(name)
            if not isinstance(candidate, str) or not Path(candidate).is_absolute():
                raise ConfigError(f"integration {name} must be an absolute path")
            values[name] = Path(candidate)
        if instance_path is not None and values["state"].parent != instance_path.parent:
            raise ConfigError("integration state must be in its private instance directory")
        poll_seconds = value.get("poll_seconds")
        budget_seconds = value.get("budget_seconds")
        for name, candidate in (("poll_seconds", poll_seconds), ("budget_seconds", budget_seconds)):
            if type(candidate) not in (int, float) or not 0 < candidate <= MAX_BUDGET_SECONDS:
                raise ConfigError(f"{name} must be greater than zero and at most {MAX_BUDGET_SECONDS}")
        product = value["product"]
        executable = value.get("codex")
        if product == "codex" and (
                not isinstance(executable, str) or not Path(executable).is_absolute()
                or not os.access(executable, os.X_OK)):
            raise ConfigError("codex must name an absolute executable path")
        return cls(product=product, **values, codex=executable, poll_seconds=poll_seconds,
                   budget_seconds=budget_seconds, version=INTEGRATION_VERSION)

    def as_dict(self) -> dict:
        value = {"version": self.version, "product": self.product, "project": str(self.project),
                 "config": str(self.config), "policy": str(self.policy), "state": str(self.state),
                 "poll_seconds": self.poll_seconds, "budget_seconds": self.budget_seconds}
        if self.codex is not None:
            value["codex"] = self.codex
        return value


def _model(value: object) -> dict:
    if not isinstance(value, dict):
        raise ConfigError("model must be an object")
    provider = value.get("provider", DEFAULT_MODEL_PROVIDER)
    common = {"provider", "timeout_seconds"}
    if provider == "command":
        if value.keys() - common - {"command"}:
            raise ConfigError("command model fields are invalid")
        return {"provider": provider, "command": _command(value.get("command")),
                "timeout_seconds": _timeout(value.get("timeout_seconds", DEFAULT_MODEL_TIMEOUT_SECONDS))}
    if provider != DEFAULT_MODEL_PROVIDER or value.keys() - common - {
            "model", "base_url", "api_key_env", "reasoning_effort", "max_output_tokens", "cache_mode"}:
        raise ConfigError("model provider or fields are invalid")
    settings = {"provider": DEFAULT_MODEL_PROVIDER, "model": DEFAULT_MODEL,
                "base_url": DEFAULT_MODEL_BASE_URL, "api_key_env": DEFAULT_MODEL_API_KEY_ENV,
                "reasoning_effort": DEFAULT_REASONING_EFFORT, "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
                "timeout_seconds": DEFAULT_MODEL_TIMEOUT_SECONDS, "cache_mode": DEFAULT_MODEL_CACHE_MODE, **value}
    if settings["cache_mode"] not in ("implicit", "layered"):
        raise ConfigError("model.cache_mode must be implicit or layered")
    if settings["reasoning_effort"] not in ("none", "low", "medium", "high", "xhigh", "max"):
        raise ConfigError("model.reasoning_effort is invalid")
    settings["max_output_tokens"] = _integer(settings["max_output_tokens"], "max_output_tokens", 256, 32768)
    return _http_settings(settings, "model")


def _http_settings(settings: dict, label: str) -> dict:
    if not isinstance(settings["model"], str) or not settings["model"].strip() or len(settings["model"]) > 200:
        raise ConfigError(f"{label}.model must be a nonempty model ID")
    if not isinstance(settings["api_key_env"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",
                                                                      settings["api_key_env"]):
        raise ConfigError(f"{label}.api_key_env must name an environment variable")
    try:
        url = urlsplit(settings["base_url"])
        valid = url.scheme in ("http", "https") and url.hostname and not (
            url.username or url.password or url.query or url.fragment)
        # HTTP is useful for a local model server; credentials must not cross plaintext remote networks.
        valid = valid and (url.scheme == "https" or url.hostname in ("localhost", "127.0.0.1", "::1"))
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ConfigError(f"{label}.base_url must be HTTPS, or HTTP on localhost, without credentials/query/fragment")
    settings["base_url"] = settings["base_url"].rstrip("/")
    settings["timeout_seconds"] = _timeout(settings["timeout_seconds"])
    return settings


def _engine(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError("decision_engine must be an object")
    provider = value.get("provider")
    if provider == "command":
        if value.keys() - {"provider", "command", "timeout_seconds"}:
            raise ConfigError("command decision_engine fields are invalid")
        return {"provider": provider, "command": _command(value.get("command")),
                "timeout_seconds": _timeout(value.get("timeout_seconds", DEFAULT_DECISION_TIMEOUT_SECONDS))}
    if provider != DEFAULT_DECISION_ENGINE_PROVIDER or value.keys() - {
            "provider", "model", "base_url", "api_key_env", "timeout_seconds"}:
        raise ConfigError("decision_engine must name provider jev or command; automatic advice is retired")
    return _http_settings({"provider": DEFAULT_DECISION_ENGINE_PROVIDER, "model": DEFAULT_DECISION_ENGINE_MODEL,
                           "base_url": DEFAULT_DECISION_ENGINE_BASE_URL,
                           "api_key_env": DEFAULT_DECISION_ENGINE_API_KEY_ENV,
                           "timeout_seconds": DEFAULT_DECISION_TIMEOUT_SECONDS, **value}, "decision_engine")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value.encode()) > 8192:
        raise ConfigError(f"secretary {field} must be text of at most 8192 bytes")
    return value


def _command(value: object) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 32 or not all(
            isinstance(part, str) and 0 < len(part) <= 4096 and "\0" not in part for part in value) \
            or not Path(value[0]).is_absolute():
        raise ConfigError("command must be an argv list starting with an absolute executable path")
    return value


def _timeout(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= MAX_BUDGET_SECONDS:
        raise ConfigError("timeout_seconds must be greater than zero and at most 60")
    return float(value)


def _integer(value: object, field: str, least: int, most: int) -> int:
    if type(value) is not int or not least <= value <= most:
        raise ConfigError(f"{field} must be an integer from {least} to {most}")
    return value
