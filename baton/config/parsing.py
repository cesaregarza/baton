"""Ordered TOML validation and construction of configuration values.

parse keeps cross-section checks and construction order together so the first refusal stays
predictable. Integration tables remain raw data; this module never loads integration code."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    Aliases,
    Config,
    ConfigError,
    Conversations,
    Dashboard,
    Indexer,
    Jobs,
    JobState,
    OAuth,
    Role,
    Search,
    Seat,
    StaticToken,
    Wake,
)

ROLE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
# A DNS name of lowercase labels, then an optional port of 1-5 digits without a leading zero.
PROXY_HOST = re.compile(
    r"(?P<name>[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*)"
    r"(?::(?P<port>[1-9][0-9]{0,4}))?"
)
INTEGRATION_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


# Used when the config has no [jobs] table: a plain open → working → done flow.
DEFAULT_JOB_STATES: dict[str, dict[str, Any]] = {
    "open": {"next": ["working", "cancelled"]},
    "working": {"next": ["done", "failed", "cancelled"]},
    "done": {},
    "failed": {},
    "cancelled": {},
}


ALIAS_LIMITS = {
    "max_posts_per_alias_per_hour": (1, 10000),
    "max_posts_per_credential_per_hour": (1, 10000),
    "binding_ttl_seconds": (1, 86400),
    "mapped_binding_ttl_seconds": (1, 604800),
    "max_bindings_per_alias": (1, 1000),
    "max_claims_per_credential_per_hour": (1, 10000),
    "max_claims_per_alias_per_hour": (1, 10000),
    "max_claims_per_hour": (1, 10000),
    "max_threads_per_alias_per_hour": (1, 10000),
    "max_threads_per_guest_per_hour": (1, 10000),
}


# Each limit and its accepted range; the defaults are the dataclass's.
INDEXER_LIMITS = {
    "max_artifact_bytes": (1, 1 << 20),
    "max_citations": (1, 1000),
    "max_manifest_items": (1, 10000),
    "max_manifest_segments": (1, 1000),
    "page_items": (1, 1000),
    "max_open_batches": (1, 100),
    "batch_ttl_seconds": (60, 86400),
    "max_input_segments": (1, 10000),
    "max_commits_per_hour": (1, 10000),
    "receipt_retention_days": (1, 365),
    "max_generations": (1, 100),
    "generation_retention_days": (1, 365),
    "max_activation_reconcile": (1, 100000),
    "max_vectors_per_generation": (1, 100000000),
}


# The dashboard binds only here: it has no login, so anyone who can reach its port can read it.
LOOPBACK_HOSTS = ("127.0.0.1", "::1")
# An https origin: lowercase host name or IPv4 address, optional port, nothing after it.
SEAT_ORIGIN = re.compile(r"https://[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[1-9][0-9]{0,4})?")


def parse(raw: dict[str, Any], *, base: Path = Path(".")) -> Config:
    _refuse_reserved(raw)
    host = _table(raw.get("host", {}), "host")
    public_url = str(host.get("public_url", "")).rstrip("/")
    if not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", public_url):
        raise ConfigError("host.public_url must be the http(s) URL clients reach the host at, without /mcp")
    _require_canonical(public_url)
    data_dir = Path(str(host.get("data_dir", "~/.local/share/baton"))).expanduser()
    roles = {name: _role(name, table) for name, table in _table(raw.get("roles", {}), "roles").items()}
    if not roles:
        raise ConfigError("define at least one role under [roles.<name>]")
    integrations = _integrations(raw.get("integrations", {}), roles)
    auth = _table(raw.get("auth", {}), "auth")
    token_values = auth.get("tokens", [])
    try:
        token_tables = iter(token_values)
    except TypeError:
        raise ConfigError("auth.tokens must be an array of tables") from None
    tokens = tuple(_token(index, table, roles) for index, table in enumerate(token_tables))
    if len({token.sha256 for token in tokens}) != len(tokens):
        raise ConfigError("two auth.tokens entries share a digest")
    names = [token.name for token in tokens]
    twice = sorted({name for name in names if names.count(name) > 1})
    if twice:
        raise ConfigError(
            f"auth.tokens has more than one entry named {twice[0]!r}: a token's name must be unique, "
            "so that revoking it ends exactly that token"
        )
    conversations = _conversations(raw.get("conversations", {}), roles)
    displayed = [token for token in tokens if roles[token.role].display_only]
    if displayed:
        raise ConfigError(
            f"auth.tokens entry {displayed[0].name!r} is for {displayed[0].role!r}, a display_only "
            "role: a role that only displays never holds a credential"
        )
    dashboard = _dashboard(raw.get("dashboard", {}), roles, conversations)
    search_table = _table(raw.get("search", {}), "search")
    search = Search(
        enabled=_boolean(search_table, "enabled", False, "search.enabled"),
        rolling_window_messages=_number(
            search_table, "rolling_window_messages", 0, "search.rolling_window_messages", 0, 10000, integer=True
        ),
        max_vector_dim=_number(search_table, "max_vector_dim", 1024, "search.max_vector_dim", 1, 16384, integer=True),
        embeddings=_boolean(search_table, "embeddings", False, "search.embeddings"),
    )
    indexer = _indexer(raw.get("indexer", {}), roles)
    if search.rolling_window_messages > indexer.max_manifest_items:
        raise ConfigError(
            "search.rolling_window_messages must be at most indexer.max_manifest_items: a recent window is one batch"
        )
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
        conversations=conversations,
        dashboard=dashboard,
        seat=_seat(raw.get("seat", {}), roles, public_url, dashboard),
        search=search,
        indexer=indexer,
        aliases=_aliases(raw.get("aliases", {}), roles),
    )


def _aliases(table: dict[str, Any], roles: dict[str, Role]) -> Aliases:
    if not isinstance(table, dict):
        raise ConfigError("aliases must be a table of limits")
    alert_role = table.get("guest_cap_alert_role")
    # A role that cannot be delivered to would make the alert fail at the moment it matters, so it is
    # refused at load instead: an unset value is the way to ask for no alert.
    if alert_role is not None and (not isinstance(alert_role, str) or alert_role not in roles or alert_role == "guest"):
        raise ConfigError(f"aliases.guest_cap_alert_role must name a configured non-guest role; got {alert_role!r}")
    defaults = Aliases()
    return Aliases(
        guest_cap_alert_role=alert_role,
        **{
            key: _number(table, key, getattr(defaults, key), f"aliases.{key}", low, high, integer=True)
            for key, (low, high) in ALIAS_LIMITS.items()
        },
    )


def _indexer(table: Mapping[str, Any], roles: dict[str, Role]) -> Indexer:
    table = _table(table, "indexer")
    producers = table.get("producers", [])
    if not isinstance(producers, list) or not all(isinstance(name, str) for name in producers):
        raise ConfigError("indexer.producers must be a list of names")
    for name in producers:
        if not ROLE_NAME.fullmatch(name):
            raise ConfigError(f"indexer.producers: {name!r} must match {ROLE_NAME.pattern}, as role names do")
        if name in roles:
            # A producer acts through local commands, a role through its credential: one name for both would
            # blur which of the two did something.
            raise ConfigError(f"indexer.producers: {name!r} is also a role name, and a producer's must differ")
    if len(set(producers)) != len(producers):
        raise ConfigError("indexer.producers lists a name more than once")
    defaults = Indexer()
    limits = {
        key: _number(table, key, getattr(defaults, key), f"indexer.{key}", low, high, integer=True)
        for key, (low, high) in INDEXER_LIMITS.items()
    }
    return Indexer(producers=tuple(producers), **limits)


# Keys a later indexer slice will read. Refused until then, so a config written for that slice can't load here
# and appear to work.
RESERVED_INDEXER_KEYS = ("messaging_role", "proposal_recipients")


def _refuse_reserved(raw: dict) -> None:
    indexer = raw.get("indexer", {})
    for key in RESERVED_INDEXER_KEYS:
        if isinstance(indexer, dict) and key in indexer:
            raise ConfigError(f"indexer.{key} is not supported yet")


def canonical_issuer(public_url: str) -> str:
    """`public_url` exactly as the OAuth metadata will publish it as the issuer.

    The MCP SDK holds the issuer as a pydantic `AnyHttpUrl` (keeping an empty
    path empty) and serializes that. This builds the same value, without
    importing the SDK, which is slow to load for a one-shot CLI command; a
    test checks it against the SDK's own result.
    """
    from pydantic import AnyHttpUrl, BaseModel, ConfigDict, ValidationError

    class Issuer(BaseModel):
        model_config = ConfigDict(url_preserve_empty_path=True)
        url: AnyHttpUrl

    try:
        return str(Issuer(url=public_url).url)
    except ValidationError:
        raise ConfigError(f"host.public_url {public_url!r} is not a valid URL") from None


def _require_canonical(public_url: str) -> None:
    """Refuse a public_url the metadata would publish differently, since every `iss` must equal the published issuer.

    A percent-encoded path is refused as well, though published as written:
    the protected-resource metadata route is registered under that encoded
    path, which the router compares against the decoded one, so it can't be
    reached.
    """
    canonical = canonical_issuer(public_url)
    if "%" in urlparse(canonical).path:
        raise ConfigError(
            f"host.public_url {public_url!r} has a percent-encoded (or non-ASCII) path, under which the "
            "OAuth metadata routes can't be reached; use a plain ASCII path"
        )
    if canonical != public_url:
        raise ConfigError(
            f"host.public_url must be written exactly as the OAuth metadata publishes it, since clients "
            f"compare the issuer character for character: write public_url = {canonical!r}"
        )


def _table(value: Any, where: str) -> Mapping[str, Any]:
    """Require a section to be a table without including its potentially sensitive contents in the error."""
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} must be a table")
    return value


def _boolean(table: Mapping[str, Any], key: str, default: bool, where: str) -> bool:
    """A TOML boolean, or `default` when the key is missing. Never truthiness: the string "false" is refused."""
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be a boolean (true or false, unquoted)")
    return value


def _number(
    table: Mapping[str, Any], key: str, default: float, where: str, low: float, high: float, *, integer: bool = False
) -> Any:
    """A TOML number within [low, high], or `default` when missing. Strings and booleans are refused."""
    value = table.get(key, default)
    kinds = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds) or not low <= value <= high:
        kind = "a whole number" if integer else "a number"
        raise ConfigError(f"{where} must be {kind} from {low:g} to {high:g}")
    return value


def _role(name: str, table: Mapping[str, Any]) -> Role:
    if not ROLE_NAME.fullmatch(name):
        raise ConfigError(f"role name {name!r} must match {ROLE_NAME.pattern}")
    table = _table(table, f"roles.{name}")
    wake = _table(table.get("wake", {}), f"roles.{name}.wake")
    if sum(bool(wake.get(key)) for key in ("webhook", "command", "integration")) > 1:
        raise ConfigError(f"roles.{name}.wake may use only one of webhook, command and integration, not both")
    webhook = wake.get("webhook")
    if webhook is not None and not _valid_webhook(str(webhook)):
        # The URL may carry a token, so the message names the field and never repeats it.
        raise ConfigError(f"roles.{name}.wake.webhook must be an http(s) URL with a host and a valid port")
    command = wake.get("command", [])
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        raise ConfigError(f"roles.{name}.wake.command must be a list of strings (argv, no shell)")
    if _boolean(table, "display_only", False, f"roles.{name}.display_only") and table.get("human") is True:
        raise ConfigError(
            f"roles.{name} sets both display_only and human: a person's role holds a credential, and "
            "a display_only one never does"
        )
    display_name = table.get("display_name", "")
    if not isinstance(display_name, str) or len(display_name) > 80 or display_name != display_name.strip():
        raise ConfigError(f"roles.{name}.display_name must be text of at most 80 characters")
    integration = wake.get("integration")
    if integration is not None and not INTEGRATION_NAME.fullmatch(str(integration)):
        raise ConfigError(f"roles.{name}.wake.integration must match {INTEGRATION_NAME.pattern}")
    description = str(table.get("description", ""))
    wake_settings = Wake(
        webhook=webhook,
        secret_env=wake.get("secret_env"),
        command=tuple(command),
        integration=integration,
        timeout_seconds=_number(wake, "timeout_seconds", 10.0, f"roles.{name}.wake.timeout_seconds", 0.1, 300),
        only_when_addressed=_boolean(wake, "only_when_addressed", False, f"roles.{name}.wake.only_when_addressed"),
    )
    return Role(
        name=name,
        description=description,
        wake=wake_settings,
        can_create_conversations=_boolean(
            table, "can_create_conversations", False, f"roles.{name}.can_create_conversations"
        ),
        can_manage_participants=_boolean(
            table, "can_manage_participants", False, f"roles.{name}.can_manage_participants"
        ),
        can_create_topics=_boolean(table, "can_create_topics", False, f"roles.{name}.can_create_topics"),
        can_view_all_conversations=_boolean(
            table, "can_view_all_conversations", False, f"roles.{name}.can_view_all_conversations"
        ),
        human=_boolean(table, "human", False, f"roles.{name}.human"),
        display_only=_boolean(table, "display_only", False, f"roles.{name}.display_only"),
        display_name=str(table.get("display_name", "")),
        job_authority=_boolean(table, "job_authority", True, f"roles.{name}.job_authority"),
    )


def _valid_webhook(url: str) -> bool:
    try:
        parsed = urlparse(url)
        parsed.port  # noqa: B018 - raises ValueError for a port that doesn't parse or is out of range
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _conversations(table: Mapping[str, Any], roles: dict[str, Role]) -> Conversations:
    table = _table(table, "conversations")
    escalation_role = table.get("escalation_role")
    if escalation_role is not None and escalation_role not in roles:
        raise ConfigError(f"conversations.escalation_role names unknown role {escalation_role!r}")
    return Conversations(
        enabled=_boolean(table, "enabled", False, "conversations.enabled"),
        max_participants=_number(table, "max_participants", 8, "conversations.max_participants", 2, 1000, integer=True),
        max_messages_per_hour=_number(
            table, "max_messages_per_hour", 30, "conversations.max_messages_per_hour", 1, 100_000, integer=True
        ),
        escalation_role=escalation_role,
    )


def _dashboard(table: Mapping[str, Any], roles: dict[str, Role], conversations: Conversations) -> Dashboard:
    table = _table(table, "dashboard")
    if not _boolean(table, "enabled", False, "dashboard.enabled"):
        return Dashboard()
    viewer_role = table.get("viewer_role")
    if viewer_role not in roles:
        raise ConfigError(f"dashboard.viewer_role must name a configured role; got {viewer_role!r}")
    if not roles[viewer_role].display_only:
        raise ConfigError(
            f"dashboard.viewer_role {viewer_role!r} must set display_only = true (and not human): the "
            "view has no login, so its identity must never be one that holds a credential"
        )
    if not conversations.enabled:
        raise ConfigError("the dashboard shows conversations: set conversations.enabled = true to enable it")
    listen_host = table.get("listen_host", "127.0.0.1")
    if listen_host not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"dashboard.listen_host must be a loopback address ({' or '.join(LOOPBACK_HOSTS)}): the "
            "view has no login, so reaching it from elsewhere needs its own auth in front of it"
        )
    attention_role = table.get("attention_role", viewer_role)
    if not isinstance(attention_role, str) or attention_role not in roles:
        raise ConfigError(f"dashboard.attention_role must name a configured role; got {attention_role!r}")
    return Dashboard(
        enabled=True,
        viewer_role=viewer_role,
        listen_host=listen_host,
        listen_port=_number(table, "listen_port", 8766, "dashboard.listen_port", 1, 65535, integer=True),
        proxy_host=_proxy_host(table.get("proxy_host")),
        attention_role=attention_role,
        attention_jobs=_boolean(table, "attention_jobs", False, "dashboard.attention_jobs"),
    )


def _seat(table: Mapping[str, Any], roles: dict[str, Role], public_url: str, dashboard: Dashboard) -> Seat:
    table = _table(table, "seat")
    if not _boolean(table, "enabled", False, "seat.enabled"):
        return Seat()
    role = table.get("role")
    if role not in roles or not roles[role].human or roles[role].display_only:
        raise ConfigError(f"seat.role must name a configured human role (human = true, not display_only); got {role!r}")
    if role == dashboard.viewer_role:
        raise ConfigError("seat.role may not be the dashboard's viewer_role")
    origin = str(table.get("origin", ""))
    if not SEAT_ORIGIN.fullmatch(origin):
        raise ConfigError(
            f"seat.origin must be one exact https origin, such as https://seat.example.com, with no "
            f"path, query or trailing slash; got {origin!r}"
        )
    shared = {
        urlparse(public_url).hostname,
        urlparse(f"//{dashboard.proxy_host}").hostname if dashboard.proxy_host else None,
    } - {None}
    trusted = _boolean(table, "same_host_trust_accepted", False, "seat.same_host_trust_accepted")
    if urlparse(origin).hostname in shared and not trusted:
        raise ConfigError(
            f"seat.origin {origin!r} shares its host name with the host or the dashboard, and a browser "
            "sends the seat's cookie to every service on that name. Give the seat a host name of its "
            "own, or set seat.same_host_trust_accepted = true to record that every service there is "
            "trusted with the person's authority"
        )
    listen_host = str(table.get("listen_host", "127.0.0.1"))
    if listen_host not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"seat.listen_host must be a loopback address ({' or '.join(LOOPBACK_HOSTS)}); the seat is "
            "reached only through the proxy at seat.origin"
        )
    timezone = table.get("timezone", "UTC")
    try:
        if not isinstance(timezone, str) or timezone != timezone.strip():
            raise ValueError(timezone)
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        raise ConfigError(
            f"seat.timezone must be an IANA time zone such as America/Chicago; got {timezone!r}"
        ) from None
    return Seat(
        enabled=True,
        role=role,
        listen_host=listen_host,
        listen_port=_number(table, "listen_port", 8767, "seat.listen_port", 1, 65535, integer=True),
        origin=origin,
        same_host_trust_accepted=trusted,
        idle_seconds=_number(table, "idle_seconds", 3600, "seat.idle_seconds", 60, 24 * 3600, integer=True),
        absolute_seconds=_number(
            table, "absolute_seconds", 12 * 3600, "seat.absolute_seconds", 60, 7 * 86400, integer=True
        ),
        timezone=timezone,
    )


def _proxy_host(value: Any) -> str | None:
    """An exact Host value, `name` or `name:port`: lowercase DNS labels, no wildcard, scheme, path or userinfo.

    It is compared to the request's Host header as a whole string, so it must be written the way a client
    sends it. Validating the Host is not authentication; see the Dashboard section of docs/design.md.
    """
    if value is None:
        return None
    match = PROXY_HOST.fullmatch(value) if isinstance(value, str) else None
    port = match and match["port"]
    if match is None or len(match["name"]) > 253 or (port is not None and not 1 <= int(port) <= 65535):
        raise ConfigError(
            "dashboard.proxy_host must be one exact host name, optionally with :port, such as "
            "dash.example.com or dash.example.com:8443: lowercase, with no wildcard, scheme or path"
        )
    return value


def _integrations(tables: Mapping[str, Any], roles: dict[str, Role]) -> dict[str, dict[str, Any]]:
    """Enabled `[integrations.<name>]` tables. A role may only name an enabled integration as its wake."""
    tables = _table(tables, "integrations")
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
            raise ConfigError(
                f"roles.{role.name}.wake names integration {role.wake.integration!r}, which is not "
                f"enabled under [integrations.{role.wake.integration}]"
            )
    return enabled


def _token(index: int, table: Mapping[str, Any], roles: dict[str, Role]) -> StaticToken:
    table = _table(table, f"auth.tokens[{index}]")
    name, role, digest = table.get("name", f"token-{index}"), table.get("role"), str(table.get("sha256", ""))
    if role not in roles:
        raise ConfigError(f"auth.tokens[{index}] names unknown role {role!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ConfigError(f"auth.tokens[{index}].sha256 must be a SHA-256 hex digest (see `baton token new`)")
    return StaticToken(str(name), role, digest)


def _oauth(table: Mapping[str, Any], roles: dict[str, Role]) -> OAuth:
    table = _table(table, "auth.oauth")
    if not _boolean(table, "enabled", False, "auth.oauth.enabled"):
        return OAuth()
    passphrase_hash = table.get("passphrase_hash") or os.environ.get(table.get("passphrase_hash_env", ""), "")
    if not str(passphrase_hash).startswith("scrypt$"):
        raise ConfigError("auth.oauth needs passphrase_hash (or passphrase_hash_env); see `baton hash-passphrase`")
    oauth_roles = tuple(table.get("roles", []))
    if not oauth_roles or any(role not in roles for role in oauth_roles):
        raise ConfigError("auth.oauth.roles must list one or more configured roles an OAuth client may act as")
    displayed = [role for role in oauth_roles if roles[role].display_only]
    if displayed:
        raise ConfigError(
            f"auth.oauth.roles lists the display_only role {displayed[0]!r}, which never holds a credential"
        )
    humans = [role for role in oauth_roles if roles[role].human]
    if humans:
        raise ConfigError(
            f"auth.oauth.roles lists the human role {humans[0]!r}: a human role signs in only with "
            "its own token from `baton token new`, never through the consent page"
        )
    redirect_uris = tuple(table.get("redirect_uris", []))
    for uri in redirect_uris:
        if any(key.lower() == "iss" for key, _ in parse_qsl(urlparse(str(uri)).query, keep_blank_values=True)):
            raise ConfigError(
                f"auth.oauth.redirect_uris has {uri!r}, which carries an iss parameter: the host adds "
                "iss itself (RFC 9207), so a callback may not carry one"
            )
    return OAuth(
        enabled=True,
        passphrase_hash=str(passphrase_hash),
        roles=oauth_roles,
        redirect_uris=redirect_uris,
        allow_loopback_redirects=_boolean(
            table, "allow_loopback_redirects", True, "auth.oauth.allow_loopback_redirects"
        ),
    )


def _jobs(table: Mapping[str, Any] | None, roles: dict[str, Role]) -> Jobs:
    table = _table(table or {}, "jobs")
    raw_states = _table(table.get("states") or DEFAULT_JOB_STATES, "jobs.states")
    states: dict[str, JobState] = {}
    for name, spec in raw_states.items():
        spec = _table(spec, f"jobs.states.{name}")
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
