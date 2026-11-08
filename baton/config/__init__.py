"""The host's configuration: one TOML file names the roles, the auth, the job states and every path.

Nothing about a deployment is hard-coded. `load()` reads the file named by
`--config`, then `$BATON_CONFIG`, then `~/.config/baton/baton.toml`, and
refuses a file that is internally inconsistent (an unknown role, a job state
that names no state) before the host starts.

Package map: models defines configuration values; parsing validates tables in order; loading selects
and reads the TOML file. This module preserves the public imports for callers and existing pickles.
"""

from .loading import CONFIG_ENV, DEFAULT_CONFIG, config_path, load
from .models import (
    Aliases,
    Config,
    ConfigError,
    Conversations,
    Dashboard,
    GraphWorker,
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
from .parsing import (
    ALIAS_LIMITS,
    DEFAULT_JOB_STATES,
    INDEXER_LIMITS,
    INTEGRATION_NAME,
    LOOPBACK_HOSTS,
    PROXY_HOST,
    RESERVED_INDEXER_KEYS,
    ROLE_NAME,
    SEAT_ORIGIN,
    canonical_issuer,
    parse,
)

__all__ = [
    "ALIAS_LIMITS",
    "Aliases",
    "CONFIG_ENV",
    "Config",
    "ConfigError",
    "Conversations",
    "DEFAULT_CONFIG",
    "DEFAULT_JOB_STATES",
    "Dashboard",
    "GraphWorker",
    "INDEXER_LIMITS",
    "INTEGRATION_NAME",
    "Indexer",
    "JobState",
    "Jobs",
    "LOOPBACK_HOSTS",
    "OAuth",
    "PROXY_HOST",
    "RESERVED_INDEXER_KEYS",
    "ROLE_NAME",
    "Role",
    "SEAT_ORIGIN",
    "Search",
    "Seat",
    "StaticToken",
    "Wake",
    "canonical_issuer",
    "config_path",
    "load",
    "parse",
]
