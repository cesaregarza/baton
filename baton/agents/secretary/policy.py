"""Configuration for the principal's lightweight model and optional decision engine."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from baton.config import Config, ConfigError
from baton.database import Connection
from baton.database.store import connect_read_only

POLICY_VERSION = 2
MAX_POLICY_BYTES = 16 * 1024


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Policy:
    principal: str
    model: dict
    decision_engine: dict | None
    instructions: str
    max_events: int
    context_bytes: int
    summary_bytes: int
    raw: dict
    digest: str


def read_policy(config: Config, path: Path):
    with connect_read_only(config.db_path) as baton:
        return load_policy(config, baton, path)


def load_policy(config: Config, baton: Connection, path: Path):
    try:
        data = path.read_bytes()
        if len(data) > MAX_POLICY_BYTES:
            raise ConfigError("secretary policy exceeds 16 KiB")
        raw = json.loads(data)
    except (OSError, ValueError) as error:
        raise ConfigError("cannot read secretary policy JSON") from error
    if not isinstance(raw, dict) or type(raw.get("version")) is not int:
        raise ConfigError("secretary policy must name version 2 (or legacy version 1)")
    if raw["version"] == 1:
        from .legacy.policy import load_policy as load_legacy
        return load_legacy(config, baton, path)
    if raw["version"] != POLICY_VERSION:
        raise ConfigError("unsupported secretary policy version")
    fields = {"version", "principal", "model", "decision_engine", "instructions", "max_events",
              "context_bytes", "summary_bytes"}
    if "principal" not in raw or raw.keys() - fields:
        raise ConfigError("secretary policy fields are invalid")
    principal = raw["principal"]
    role = config.roles.get(principal) if isinstance(principal, str) else None
    if role is None or principal == "guest" or role.human or role.display_only:
        raise ConfigError("secretary principal must be an existing non-human, non-display agent role")
    instructions = raw.get("instructions", "")
    if not isinstance(instructions, str) or len(instructions.encode()) > 8192:
        raise ConfigError("secretary instructions must be text of at most 8192 bytes")
    model = _model(raw.get("model", {}))
    engine = raw.get("decision_engine")
    if engine is not None:
        if not isinstance(engine, dict) or engine.keys() - {"command", "timeout_seconds"}:
            raise ConfigError("decision_engine needs command and optional timeout_seconds")
        engine = {"command": _command(engine.get("command")),
                  "timeout_seconds": _timeout(engine.get("timeout_seconds", 3))}
    normalized = {"version": POLICY_VERSION, "principal": principal, "model": model,
                  "decision_engine": engine, "instructions": instructions,
                  "max_events": _integer(raw.get("max_events", 1000), "max_events", 1, 10000),
                  "context_bytes": _integer(raw.get("context_bytes", 1024 * 1024), "context_bytes", 1024, 8388608),
                  "summary_bytes": _integer(raw.get("summary_bytes", 8192), "summary_bytes", 256, 65536)}
    return Policy(**{key: value for key, value in normalized.items() if key != "version"},
                  raw=normalized, digest=fingerprint(normalized))


def _model(value: object) -> dict:
    if not isinstance(value, dict):
        raise ConfigError("model must be an object")
    provider = value.get("provider", "openai")
    common = {"provider", "timeout_seconds"}
    if provider == "command":
        if value.keys() - common - {"command"}:
            raise ConfigError("command model fields are invalid")
        return {"provider": provider, "command": _command(value.get("command")),
                "timeout_seconds": _timeout(value.get("timeout_seconds", 20))}
    if provider != "openai" or value.keys() - common - {
            "model", "base_url", "api_key_env", "reasoning_effort", "max_output_tokens"}:
        raise ConfigError("model provider or fields are invalid")
    settings = {"provider": "openai", "model": "gpt-6-luna", "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY", "reasoning_effort": "low", "max_output_tokens": 4096,
                "timeout_seconds": 20, **value}
    if not isinstance(settings["model"], str) or not 1 <= len(settings["model"]) <= 200:
        raise ConfigError("model.model must be a nonempty model ID")
    if not isinstance(settings["api_key_env"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",
                                                                      settings["api_key_env"]):
        raise ConfigError("model.api_key_env must name an environment variable")
    if settings["reasoning_effort"] not in ("none", "low", "medium", "high", "xhigh", "max"):
        raise ConfigError("model.reasoning_effort is invalid")
    try:
        url = urlsplit(settings["base_url"])
        valid = url.scheme in ("http", "https") and url.hostname and not (
            url.username or url.password or url.query or url.fragment)
        # HTTP is useful for a local model server; credentials must not cross plaintext remote networks.
        valid = valid and (url.scheme == "https" or url.hostname in ("localhost", "127.0.0.1", "::1"))
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ConfigError("model.base_url must be HTTPS, or HTTP on localhost, without credentials/query/fragment")
    settings["base_url"] = settings["base_url"].rstrip("/")
    settings["timeout_seconds"] = _timeout(settings["timeout_seconds"])
    settings["max_output_tokens"] = _integer(settings["max_output_tokens"], "max_output_tokens", 256, 32768)
    return settings


def _command(value: object) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 32 or not all(
            isinstance(part, str) and 0 < len(part) <= 4096 and "\0" not in part for part in value) \
            or not Path(value[0]).is_absolute():
        raise ConfigError("command must be an argv list starting with an absolute executable path")
    return value


def _timeout(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 60:
        raise ConfigError("timeout_seconds must be greater than zero and at most 60")
    return float(value)


def _integer(value: object, field: str, least: int, most: int) -> int:
    if type(value) is not int or not least <= value <= most:
        raise ConfigError(f"{field} must be an integer from {least} to {most}")
    return value
