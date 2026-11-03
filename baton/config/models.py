"""Configuration values and their derived properties.

Each configuration class is defined once here and re-exported by baton.config.
Parsing and file loading depend on these values, never the other way around."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class ConfigError(ValueError):
    """The configuration file is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class Wake:
    """How a role is told something is waiting for it: at most one of a webhook, a command or an integration.

    `only_when_addressed` narrows *when*: the role still receives every message, but a conversation
    message wakes it only if it singles the role out for attention or asks for a decision. Anything else
    addressed to the role alone -- a direct message, a job change, an escalation -- wakes it either way.
    """

    webhook: str | None = None
    secret_env: str | None = None
    command: tuple[str, ...] = ()
    integration: str | None = None
    timeout_seconds: float = 10.0
    only_when_addressed: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.webhook or self.command or self.integration)


@dataclass(frozen=True)
class Role:
    name: str
    description: str = ""
    wake: Wake = field(default_factory=Wake)
    can_create_conversations: bool = False
    can_manage_participants: bool = False
    # Lets this role open, edit and rename a topic. Starting a thread inside one needs no such flag:
    # any identified caller may do that, which is what makes a topic a container rather than a label.
    can_create_topics: bool = False
    # Lets the dashboard's viewer see every conversation, not only the ones this role is in.
    can_view_all_conversations: bool = False
    # A person, not an agent: acts only with its own bearer token, never through OAuth or `--as`.
    human: bool = False
    # An identity that only displays, like the dashboard's viewer: it never holds a credential of any kind.
    display_only: bool = False
    # How pages name the role, such as a person's name; the role name when unset.
    display_name: str = ""
    # Whether this role may create or move jobs. Defaults on to preserve existing fixed-role behavior.
    job_authority: bool = True

    @property
    def label(self) -> str:
        return self.display_name or self.name


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
class Conversations:
    """Group conversations, off unless enabled. The limits apply to each conversation."""

    enabled: bool = False
    max_participants: int = 8
    max_messages_per_hour: int = 30
    # Woken once an hour at most while a conversation is over its limit.
    escalation_role: str | None = None


@dataclass(frozen=True)
class Aliases:
    """Guest alias storage and hourly admission bounds; continuity never grants role authority."""

    binding_ttl_seconds: int = 86400
    # For an alias mapped to a role: re-arming costs the owner a go, so at most weekly by default.
    mapped_binding_ttl_seconds: int = 604800
    max_bindings_per_alias: int = 20
    max_posts_per_alias_per_hour: int = 30
    max_posts_per_credential_per_hour: int = 300
    max_claims_per_credential_per_hour: int = 20
    max_claims_per_alias_per_hour: int = 5
    max_claims_per_hour: int = 100
    # New threads a guest may start in a rolling hour, per alias and across every alias together.
    max_threads_per_alias_per_hour: int = 5
    max_threads_per_guest_per_hour: int = 50
    # Who hears that the guest-wide thread cap filled, at most once an hour. Unset means nobody is told.
    guest_cap_alert_role: str | None = None


@dataclass(frozen=True)
class Search:
    """Keyword search over messages for roles, off unless enabled. Each role finds only what it may read."""

    enabled: bool = False
    # Off at 0. Otherwise a producer may also write `summary_recent`, over at most this many latest messages.
    rolling_window_messages: int = 0
    # The largest vector a generation's embedding profile may declare.
    max_vector_dim: int = 1024
    # Off unless set: generations may embed, commits then carry vectors, and search takes a query vector.
    embeddings: bool = False


@dataclass(frozen=True)
class Indexer:
    """The host owner's local indexing worker: the producer names its writer commands may act as, and the bounds
    every batch and commit is held to. Empty `producers` turns the writer off; the read-only commands need none."""

    producers: tuple[str, ...] = ()
    max_artifact_bytes: int = 16384
    max_citations: int = 64
    max_manifest_items: int = 500
    max_manifest_segments: int = 64
    page_items: int = 100
    max_open_batches: int = 4
    batch_ttl_seconds: int = 900
    max_input_segments: int = 256
    max_commits_per_hour: int = 60
    receipt_retention_days: int = 30
    max_generations: int = 3
    generation_retention_days: int = 30
    max_activation_reconcile: int = 10000
    max_vectors_per_generation: int = 1000000


@dataclass(frozen=True)
class Dashboard:
    """The optional read-only view of conversations, off unless enabled. It shows what `viewer_role` may see."""

    enabled: bool = False
    viewer_role: str | None = None
    listen_host: str = "127.0.0.1"
    listen_port: int = 8766
    # One more Host the view answers to, exactly, for an authenticating proxy in front of it. Off by default.
    proxy_host: str | None = None
    # Whose attention the Needs page is about: `viewer_role` unless set. Visibility stays `viewer_role`'s; this
    # only chooses which of the messages the viewer may already read are listed as needing that role.
    attention_role: str | None = None
    # Whether the Needs page lists the jobs waiting on `attention_role` when that isn't the viewer. Off: showing
    # them widens what the view's audience sees, so it is a decision of its own.
    attention_jobs: bool = False


@dataclass(frozen=True)
class Seat:
    """The optional browser seat for one human role, off unless enabled; see `baton.seat`."""

    enabled: bool = False
    role: str | None = None
    listen_host: str = "127.0.0.1"
    listen_port: int = 8767
    # The one exact https origin the seat is reached at, through a proxy that terminates TLS.
    origin: str = ""
    # A seat on a host name other services share hands them its cookie: allowed only when written down here.
    same_host_trust_accepted: bool = False
    idle_seconds: int = 3600
    absolute_seconds: int = 12 * 3600
    # The one zone the pages show times in; they run no scripts, so they can't ask the phone for its own.
    timezone: str = "UTC"

    @property
    def host(self) -> str:
        return urlparse(self.origin).netloc


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
    conversations: Conversations = field(default_factory=Conversations)
    dashboard: Dashboard = field(default_factory=Dashboard)
    seat: Seat = field(default_factory=Seat)
    search: Search = field(default_factory=Search)
    indexer: Indexer = field(default_factory=Indexer)
    aliases: Aliases = field(default_factory=Aliases)

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

    @property
    def revoked_tokens_path(self) -> Path:
        return self.data_dir / "revoked_tokens.json"

    @property
    def human_roles(self) -> frozenset[str]:
        return frozenset(name for name, role in self.roles.items() if role.human)

    def author_kind(self, role: str) -> str:
        """What a message from `role` is recorded as when sent: `human` for a human role, otherwise `agent`."""
        entry = self.roles.get(role)
        return "human" if entry is not None and entry.human else "agent"

    def require_role(self, name: str) -> Role:
        role = self.roles.get(name)
        if role is None:
            raise ConfigError(f"unknown role {name!r}; configured roles: {', '.join(sorted(self.roles))}")
        return role
