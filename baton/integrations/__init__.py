"""Optional integrations: code for one particular kind of client, kept out of the core.

An integration is a module in this package, enabled by an
`[integrations.<name>]` table with `enabled = true`. The core imports a module
only when its table enables it. A role uses one for its wakes with
`wake = { integration = "<name>" }`.

The core keeps everything it already owns: roles and credentials, the event
log, wake coalescing, leases, backoff, attempts and `baton wakes`. An
integration only adds MCP methods to the server and takes over the last step
of delivering a content-free wake. One that has work of its own, such as
`decision_push`, may also define `prepare()`, which `baton serve` calls before
the server accepts any request, and `run(stop)`, which it runs on its own thread
until the host stops.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from typing import Any, Protocol

from mcp.server.mcpserver import MCPServer

from baton.config import Config, ConfigError


class Integration(Protocol):
    def middleware(self) -> Sequence[Any]:
        """MCP server middleware to install, such as one advertising a capability."""

    def register(self, server: MCPServer) -> None:
        """Add the integration's MCP methods to the server."""

    def deliver(self, role: str, wake_id: str, body: bytes) -> bool:
        """Deliver one wake for `role`. True only if some recipient accepted it; the core retries otherwise."""


def load_enabled(config: Config) -> dict[str, Integration]:
    """Instantiate every enabled integration. Each module exposes `load(config, settings) -> Integration`."""
    loaded: dict[str, Integration] = {}
    for name, settings in config.integrations.items():
        try:
            module = importlib.import_module(f"{__name__}.{name}")
        except ModuleNotFoundError as error:
            if error.name != f"{__name__}.{name}":
                raise
            raise ConfigError(f"integrations.{name}: no such integration in this baton") from None
        loaded[name] = module.load(config, settings)
    return loaded
