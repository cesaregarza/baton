"""The `[graph_worker]` section: the off-by-default background graph producer and its hard bounds.

The worker is a host service, separate from secretary inboxes and from the indexer's manual commands. It
is configured here and nowhere else; an enabled section names a producer from `[indexer] producers`, a
private state directory outside `data_dir`, an explicit conversation allowlist and one provider mode. The
`codex` mode also names the worker's own client home, which holds only the worker's login.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models import ConfigError, GraphWorker, Indexer

PROVIDERS = ("http", "codex", "claude")
CONVERSATION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
# Names a model process must never receive: Baton's own credentials, secrets and configuration locations.
FORBIDDEN_ENV_PREFIXES = ("BATON_",)
NATIVE_COMMANDS = {"codex": ("codex",), "claude": ("claude",)}
# (low, high, default, whole): durations may be fractional; counts, byte and token limits are whole numbers.
LIMITS = {
    "poll_seconds": (1, 86400, 30, False),
    "batch_size": (1, 500, 20, True),
    "max_wait_seconds": (0, 604_800, 21_600, True),  # 0: every waiting post is due at once
    "max_input_bytes": (1000, 50_000_000, 512_000, True),
    "max_response_bytes": (1000, 100_000_000, 4_000_000, True),
    "call_timeout_seconds": (1, 3600, 300, False),
    "max_output_tokens": (1, 1_000_000, 4000, True),
    "daily_call_cap": (0, 10_000, 8, True),  # 0 is explicitly unlimited; every other bound still applies
    "calls_per_cycle": (1, 100, 4, True),
    "publication_attempts": (1, 100, 3, True),
}


def parse(raw: Any, indexer: Indexer, data_dir: Path, *, base: Path = Path(".")) -> GraphWorker:
    """Validate the section in order; a disabled section is accepted whatever else it says."""
    if not isinstance(raw, Mapping):
        raise ConfigError("graph_worker must be a table")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("graph_worker.enabled must be a boolean (true or false, unquoted)")
    if not enabled:
        return GraphWorker()
    if "client_version" in raw:
        raise ConfigError(
            "graph_worker.client_version is retired: the worker follows the installed client as it updates; remove it"
        )
    producer = raw.get("producer")
    if not isinstance(producer, str) or producer not in indexer.producers:
        raise ConfigError("graph_worker.producer must name a producer listed in indexer.producers")
    state_dir = _private_dir(
        raw.get("state_dir"), "state_dir", "a private directory path for worker artifacts", data_dir, base
    )
    conversations = raw.get("conversations")
    if (
        not isinstance(conversations, list)
        or not conversations
        or not all(isinstance(c, str) and CONVERSATION_ID.fullmatch(c) for c in conversations)
        or len(set(conversations)) != len(conversations)
    ):
        raise ConfigError("graph_worker.conversations must list distinct conversation ids explicitly; it is never '*'")
    provider = raw.get("provider", "http")
    if provider not in PROVIDERS:
        raise ConfigError(f"graph_worker.provider must be one of {', '.join(PROVIDERS)}")
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError("graph_worker.model must name the exact model explicitly")
    generation = raw.get("generation")
    if generation is not None and (not isinstance(generation, str) or not generation):
        raise ConfigError("graph_worker.generation must be a generation id when given")
    limits = {key: _number(raw, key, *bounds) for key, bounds in LIMITS.items()}
    if limits["batch_size"] > indexer.max_manifest_items:
        raise ConfigError(
            "graph_worker.batch_size must be at most indexer.max_manifest_items: one publication covers a whole batch"
        )
    return GraphWorker(
        enabled=True,
        producer=producer,
        state_dir=state_dir,
        conversations=tuple(conversations),
        provider=provider,
        model=model,
        generation=generation,
        **(_http_fields(raw) if provider == "http" else _native_fields(raw, provider, data_dir, base)),
        **limits,
    )


def _private_dir(value: Any, key: str, purpose: str, data_dir: Path, base: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"graph_worker.{key} must be {purpose}")
    path = Path(value).expanduser()
    path = path if path.is_absolute() else base / path
    if ".." in path.parts:
        raise ConfigError(f"graph_worker.{key} must not contain '..'")
    resolved = Path(str(path)).resolve(strict=False)
    if resolved == data_dir.resolve(strict=False) or resolved.is_relative_to(data_dir.resolve(strict=False)):
        raise ConfigError(f"graph_worker.{key} must be outside host.data_dir")
    return path


def _client_home(raw: Mapping[str, Any], data_dir: Path, base: Path) -> Path:
    """The worker's own client home as an absolute path, a relative one taken from `base` like `state_dir`.

    The client runs in a temporary working directory, so only an absolute home names this directory there. It is
    never the operator's home, and never the operator's client home (CODEX_HOME in the environment the worker
    starts with, and the default ~/.codex), a directory inside one or a directory around one: the worker never
    reads the operator's login.
    """
    path = _private_dir(
        raw.get("client_home"), "client_home", "the worker's own client home (it has no default)", data_dir, base
    ).absolute()
    resolved, home = path.resolve(strict=False), Path.home()
    operator = (Path(os.environ.get("CODEX_HOME") or home / ".codex"), home / ".codex")
    clients = [client.expanduser().resolve(strict=False) for client in operator]
    if resolved == home.resolve(strict=False) or any(
        resolved.is_relative_to(client) or client.is_relative_to(resolved) for client in clients
    ):
        raise ConfigError(
            "graph_worker.client_home must be a home of the worker's own, not the operator's home or client home, "
            "nor inside or around the operator's client home"
        )
    return path


def _http_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    base_url, api_key_env, allow_no_key = (
        raw.get("base_url"),
        raw.get("api_key_env"),
        raw.get("allow_no_key", False),
    )
    if not isinstance(base_url, str) or not re.fullmatch(r"https?://[^\s?#]+", base_url):
        raise ConfigError("graph_worker.base_url must be an explicit http(s) API base")
    if not isinstance(allow_no_key, bool):
        raise ConfigError("graph_worker.allow_no_key must be a boolean")
    if api_key_env is not None and (not isinstance(api_key_env, str) or not ENV_NAME.fullmatch(api_key_env)):
        raise ConfigError("graph_worker.api_key_env must be an environment variable name")
    if (api_key_env is None) == (not allow_no_key):
        raise ConfigError("graph_worker needs exactly one of api_key_env or allow_no_key = true")
    if raw.get("credential_env") or raw.get("command") or raw.get("client_home") is not None:
        raise ConfigError("graph_worker.credential_env, command and client_home apply to native providers only")
    return {"base_url": base_url, "api_key_env": api_key_env, "allow_no_key": allow_no_key}


def _native_fields(raw: Mapping[str, Any], provider: str, data_dir: Path, base: Path) -> dict[str, Any]:
    for key in ("base_url", "api_key_env", "allow_no_key"):
        if raw.get(key) is not None and raw.get(key) is not False:
            raise ConfigError(f"graph_worker.{key} applies to the http provider only")
    names = raw.get("credential_env", [])
    if not isinstance(names, list) or not all(isinstance(n, str) and ENV_NAME.fullmatch(n) for n in names):
        raise ConfigError("graph_worker.credential_env must list environment variable names")
    if any(name.startswith(FORBIDDEN_ENV_PREFIXES) for name in names):
        raise ConfigError("graph_worker.credential_env must not pass Baton's own variables to a model process")
    command = raw.get("command", list(NATIVE_COMMANDS[provider]))
    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        raise ConfigError("graph_worker.command must be a non-empty argv list for the native client (no shell)")
    fields = {"credential_env": tuple(names), "command": tuple(command)}
    if provider != "codex":
        if raw.get("client_home") is not None:
            raise ConfigError("graph_worker.client_home applies to codex only")
        return fields
    if names:
        raise ConfigError("graph_worker.credential_env does not apply to codex: its credential is in client_home")
    return {**fields, "client_home": _client_home(raw, data_dir, base)}


def _number(table: Mapping[str, Any], key: str, low: float, high: float, default: float, whole: bool) -> Any:
    value = table.get(key, default)
    kinds = (int,) if whole else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds) or not low <= value <= high:
        kind = "a whole number" if whole else "a number"
        raise ConfigError(f"graph_worker.{key} must be {kind} from {low:g} to {high:g}")
    return value
