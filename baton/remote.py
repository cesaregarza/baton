"""The CLI's remote mode: act through a host's MCP server with a bearer token, from any machine.

This is how a human role takes part: its token is the only way to speak as it.
The token comes from `$BATON_TOKEN`, or from the output of `$BATON_TOKEN_COMMAND`
(argv, no shell), such as a lookup in the OS keychain, so it never has to sit
in a shell history or a file.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Any

from baton.config import ConfigError
from baton.errors import BatonError

TOKEN_ENV = "BATON_TOKEN"
TOKEN_COMMAND_ENV = "BATON_TOKEN_COMMAND"
HOST_ENV = "BATON_HOST"


class RemoteRefusal(BatonError):
    """A refusal reported by the host, keeping the host's code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def token() -> str:
    if os.environ.get(TOKEN_ENV):
        return os.environ[TOKEN_ENV]
    command = os.environ.get(TOKEN_COMMAND_ENV)
    if not command:
        raise ConfigError(f"remote mode needs a token: set ${TOKEN_ENV}, or ${TOKEN_COMMAND_ENV} to a command "
                          "that prints it (for example an OS keychain lookup)")
    try:
        found = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise ConfigError(f"${TOKEN_COMMAND_ENV} failed: {error}") from None
    value = found.stdout.strip()
    if not value:
        raise ConfigError(f"${TOKEN_COMMAND_ENV} printed no token")
    return value


def mcp_url(host: str) -> str:
    host = host.rstrip("/")
    return host if host.endswith("/mcp") else host + "/mcp"


def call(host: str, bearer: str, tool: str, arguments: dict[str, Any]) -> Any:
    """Call one tool as the token's role; a refusal raises with the host's code."""
    import anyio  # lazily: the MCP client is slow to import for local commands
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    statuses: list[int] = []

    async def record(response: Any) -> None:
        statuses.append(response.status_code)

    async def run() -> Any:
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {bearer}"}, timeout=60,
                                  event_hooks={"response": [record]})
        async with Client(streamable_http_client(mcp_url(host), http_client=http)) as client:
            return await client.call_tool(tool, arguments)

    try:
        result = anyio.run(run)
    except Exception as error:  # the transport raises inside task groups; report it plainly
        detail = _innermost(error)
        code = "unauthorized" if 401 in statuses else "host_unreachable"
        raise RemoteRefusal(code, f"{mcp_url(host)}: {detail}") from None
    text = result.content[0].text if result.content else ""
    if result.structured_content is not None:
        value = result.structured_content.get("result", result.structured_content)
    else:
        value = json.loads(text) if text else None
    if result.is_error and not isinstance(value, dict):
        raise RemoteRefusal("tool_error", text)
    if isinstance(value, dict) and "error" in value:
        raise RemoteRefusal(str(value["error"]), str(value.get("detail", "")))
    return value


def _innermost(error: BaseException) -> str:
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return f"{type(error).__name__}: {error}"
