"""The host's configuration: one TOML file names the roles, the auth, the job states and every path.

Nothing about a deployment is hard-coded. `load()` reads the file named by
`--config`, then `$BATON_CONFIG`, then `~/.config/baton/baton.toml`, and
refuses a file that is internally inconsistent (an unknown role, a job state
that names no state) before the host starts.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_ENV = "BATON_CONFIG"
DEFAULT_CONFIG = Path("~/.config/baton/baton.toml")
ROLE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
INTEGRATION_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")

# Used when the config has no [jobs] table: a plain open → working → done flow.
DEFAULT_JOB_STATES: dict[str, dict[str, Any]] = {
    "open": {"next": ["working", "cancelled"]},
    "working": {"next": ["done", "failed", "cancelled"]},
    "done": {},
    "failed": {},
    "cancelled": {},
}


class ConfigError(ValueError):
    """The configuration file is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class Wake:
    """How a role is told something is waiting for it: at most one of a webhook, a command or an integration."""

    webhook: str | None = None
    secret_env: str | None = None
    command: tuple[str, ...] = ()
    integration: str | None = None
    timeout_seconds: float = 10.0

    @property
    def enabled(self) -> bool:
        return bool(self.webhook or self.command or self.integration)


@dataclass(frozen=True)
class Role:
    name: str
    description: str = ""
    wake: Wake = field(default_factory=Wake)


@dataclass(frozen=True)
class StaticToken:
    """A bearer token for one role, stored only as its SHA-256 hex digest."""

    name: str
    role: str
    sha256: str


@dataclass(frozen=True)
class OAuth:
    enabled: bool = False
    passphrase_hash: str = ""
    roles: tuple[str, ...] = ()
    redirect_uris: tuple[str, ...] = ()
    allow_loopback_redirects: bool = True


@dataclass(frozen=True)
class JobState:
    name: str
    waiting_on: str | None
    next: frozenset[str]

    @property
    def terminal(self) -> bool:
        return not self.next


@dataclass(frozen=True)
class Jobs:
    initial: str
    states: dict[str, JobState]


@dataclass(frozen=True)
class Config:
    public_url: str
    listen_host: str
    listen_port: int
    data_dir: Path
    roles: dict[str, Role]
    tokens: tuple[StaticToken, ...]
    oauth: OAuth
    jobs: Jobs
    allowed_origins: tuple[str, ...] = ()
    # Raw `[integrations.<name>]` tables; each integration validates its own (see baton.integrations).
    integrations: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def resource_url(self) -> str:
        return self.public_url + "/mcp"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "baton.db"

    @property
    def oauth_state_path(self) -> Path:
        return self.data_dir / "oauth.json"

    @property
    def audit_path(self) -> Path:
        return self.data_dir / "audit.jsonl"

    def require_role(self, name: str) -> Role:
        role = self.roles.get(name)
        if role is None:
            raise ConfigError(f"unknown role {name!r}; configured roles: {', '.join(sorted(self.roles))}")
        return role


def config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    return Path(explicit or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG).expanduser()


def load(explicit: str | os.PathLike[str] | None = None) -> Config:
    path = config_path(explicit)
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"no config file at {path}; see the README quickstart") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from None
    return parse(raw, base=path.parent)


def parse(raw: dict[str, Any], *, base: Path = Path(".")) -> Config:
    host = raw.get("host", {})
    public_url = str(host.get("public_url", "")).rstrip("/")
    if not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", public_url):
        raise ConfigError("host.public_url must be the http(s) URL clients reach the host at, without /mcp")
    data_dir = Path(str(host.get("data_dir", "~/.local/share/baton"))).expanduser()
    roles = {name: _role(name, table) for name, table in raw.get("roles", {}).items()}
    if not roles:
        raise ConfigError("define at least one role under [roles.<name>]")
    integrations = _integrations(raw.get("integrations", {}), roles)
    auth = raw.get("auth", {})
    tokens = tuple(_token(index, table, roles) for index, table in enumerate(auth.get("tokens", [])))
    if len({token.sha256 for token in tokens}) != len(tokens):
        raise ConfigError("two auth.tokens entries share a digest")
    return Config(
        public_url=public_url,
        listen_host=str(host.get("listen_host", "127.0.0.1")),
        listen_port=_number(host, "listen_port", 8765, "host.listen_port", 1, 65535, integer=True),
        data_dir=data_dir if data_dir.is_absolute() else base / data_dir,
        roles=roles,
        tokens=tokens,
        oauth=_oauth(auth.get("oauth", {}), roles),
        jobs=_jobs(raw.get("jobs"), roles),
        allowed_origins=tuple(str(origin) for origin in host.get("allowed_origins", [])),
        integrations=integrations,
    )


def _boolean(table: dict[str, Any], key: str, default: bool, where: str) -> bool:
    """A TOML boolean, or `default` when the key is missing. Never truthiness: the string "false" is refused."""
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be a boolean (true or false, unquoted)")
    return value


def _number(table: dict[str, Any], key: str, default: float, where: str, low: float, high: float, *,
            integer: bool = False) -> Any:
    """A TOML number within [low, high], or `default` when missing. Strings and booleans are refused."""
    value = table.get(key, default)
    kinds = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds) or not low <= value <= high:
        kind = "a whole number" if integer else "a number"
        raise ConfigError(f"{where} must be {kind} from {low:g} to {high:g}")
    return value


def _role(name: str, table: dict[str, Any]) -> Role:
    if not ROLE_NAME.fullmatch(name):
        raise ConfigError(f"role name {name!r} must match {ROLE_NAME.pattern}")
    wake = table.get("wake", {})
    if sum(bool(wake.get(key)) for key in ("webhook", "command", "integration")) > 1:
        raise ConfigError(f"roles.{name}.wake may use only one of webhook, command and integration, not both")
    webhook = wake.get("webhook")
    if webhook is not None and not str(webhook).startswith(("https://", "http://")):
        raise ConfigError(f"roles.{name}.wake.webhook must be an http(s) URL")
    command = wake.get("command", [])
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        raise ConfigError(f"roles.{name}.wake.command must be a list of strings (argv, no shell)")
    integration = wake.get("integration")
    if integration is not None and not INTEGRATION_NAME.fullmatch(str(integration)):
        raise ConfigError(f"roles.{name}.wake.integration must match {INTEGRATION_NAME.pattern}")
    return Role(name, str(table.get("description", "")), Wake(
        webhook=webhook, secret_env=wake.get("secret_env"), command=tuple(command), integration=integration,
        timeout_seconds=_number(wake, "timeout_seconds", 10.0, f"roles.{name}.wake.timeout_seconds", 0.1, 300),
    ))


def _integrations(tables: dict[str, Any], roles: dict[str, Role]) -> dict[str, dict[str, Any]]:
    """Enabled `[integrations.<name>]` tables. A role may only name an enabled integration as its wake."""
    enabled = {}
    for name, table in tables.items():
        if not INTEGRATION_NAME.fullmatch(name) or not isinstance(table, dict):
            raise ConfigError(f"integrations.{name} must be a table named like {INTEGRATION_NAME.pattern}")
        if not isinstance(table.get("enabled", False), bool):
            raise ConfigError(f"integrations.{name}.enabled must be a boolean (true or false, unquoted)")
        if table.get("enabled", False):
            enabled[name] = table
    for role in roles.values():
        if role.wake.integration is not None and role.wake.integration not in enabled:
            raise ConfigError(f"roles.{role.name}.wake names integration {role.wake.integration!r}, which is not "
                              f"enabled under [integrations.{role.wake.integration}]")
    return enabled


def _token(index: int, table: dict[str, Any], roles: dict[str, Role]) -> StaticToken:
    name, role, digest = table.get("name", f"token-{index}"), table.get("role"), str(table.get("sha256", ""))
    if role not in roles:
        raise ConfigError(f"auth.tokens[{index}] names unknown role {role!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ConfigError(f"auth.tokens[{index}].sha256 must be a SHA-256 hex digest (see `baton token new`)")
    return StaticToken(str(name), role, digest)


def _oauth(table: dict[str, Any], roles: dict[str, Role]) -> OAuth:
    if not _boolean(table, "enabled", False, "auth.oauth.enabled"):
        return OAuth()
    passphrase_hash = table.get("passphrase_hash") or os.environ.get(table.get("passphrase_hash_env", ""), "")
    if not str(passphrase_hash).startswith("scrypt$"):
        raise ConfigError("auth.oauth needs passphrase_hash (or passphrase_hash_env); see `baton hash-passphrase`")
    oauth_roles = tuple(table.get("roles", []))
    if not oauth_roles or any(role not in roles for role in oauth_roles):
        raise ConfigError("auth.oauth.roles must list one or more configured roles an OAuth client may act as")
    return OAuth(True, str(passphrase_hash), oauth_roles, tuple(table.get("redirect_uris", [])),
                 _boolean(table, "allow_loopback_redirects", True, "auth.oauth.allow_loopback_redirects"))


def _jobs(table: dict[str, Any] | None, roles: dict[str, Role]) -> Jobs:
    table = table or {}
    raw_states = table.get("states") or DEFAULT_JOB_STATES
    states: dict[str, JobState] = {}
    for name, spec in raw_states.items():
        waiting_on = spec.get("waiting_on")
        if waiting_on is not None and waiting_on not in roles:
            raise ConfigError(f"jobs.states.{name}.waiting_on names unknown role {waiting_on!r}")
        states[name] = JobState(name, waiting_on, frozenset(spec.get("next", [])))
    for state in states.values():
        unknown = state.next - states.keys()
        if unknown:
            raise ConfigError(f"jobs.states.{state.name}.next names unknown states {sorted(unknown)}")
    initial = table.get("initial", next(iter(states)))
    if initial not in states:
        raise ConfigError(f"jobs.initial {initial!r} is not a state")
    return Jobs(initial, states)
