"""Shared fixtures. Everything here is synthetic: example roles, throwaway tokens, a temporary data directory."""

from __future__ import annotations

import socket
import sqlite3
import textwrap
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn

from baton import config as config_module
from baton.auth import hash_passphrase, new_token
from baton.config import Config
from baton.store import connect

PASSPHRASE = "example passphrase for tests"
REDIRECT = "https://client.example.test/oauth/callback"

JOBS = """
[jobs]
initial = "open"
[jobs.states.open]
waiting_on = "worker"
next = ["working", "cancelled"]
[jobs.states.working]
waiting_on = "worker"
next = ["review", "cancelled"]
[jobs.states.review]
waiting_on = "planner"
next = ["done", "working"]
[jobs.states.done]
[jobs.states.cancelled]
"""


@dataclass
class Host:
    config: Config
    tokens: dict[str, str]  # role -> bearer token
    base: str = ""
    integrations: dict | None = None

    @property
    def mcp_url(self) -> str:
        return self.config.resource_url


def make_config(tmp_path: Path, *, port: int = 8765, oauth: bool = False, extra: str = "",
                data_dir: Path | None = None) -> Host:
    tokens = {role: new_token() for role in ("planner", "worker")}
    oauth_table = textwrap.dedent(f"""
        [auth.oauth]
        enabled = true
        passphrase_hash = "{hash_passphrase(PASSPHRASE)}"
        roles = ["planner"]
        redirect_uris = ["{REDIRECT}"]
    """) if oauth else ""
    entries = "".join(f'[[auth.tokens]]\nname = "{role}-token"\nrole = "{role}"\nsha256 = "{digest}"\n'
                      for role, (_, digest) in tokens.items())
    text = textwrap.dedent(f"""
        [host]
        public_url = "http://127.0.0.1:{port}"
        listen_port = {port}
        data_dir = "{data_dir or tmp_path / 'data'}"

        [roles.planner]
        description = "Plans work and reviews it"
        [roles.worker]
        description = "Does the work"
    """) + extra + "\n" + entries + oauth_table + JOBS
    path = tmp_path / "baton.toml"
    path.write_text(text)
    return Host(config_module.load(path), {role: token for role, (token, _) in tokens.items()})


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return make_config(tmp_path)


@pytest.fixture
def conn(host: Host) -> Iterator[sqlite3.Connection]:
    connection = connect(host.config.db_path)
    yield connection
    connection.close()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def start_host(tmp_path: Path) -> Iterator[Callable[..., Host]]:
    """Start a real host on a free port; it stops when the test ends."""
    from baton.app import create_app

    servers: list[uvicorn.Server] = []

    def start(integrations: Callable[[Config], dict] | None = None, **kwargs: object) -> Host:
        """`integrations`, when given, builds the integrations the app uses from the host's config."""
        port = free_port()
        host = make_config(tmp_path, port=port, **kwargs)  # type: ignore[arg-type]
        host.integrations = integrations(host.config) if integrations else None
        server = uvicorn.Server(uvicorn.Config(create_app(host.config, host.integrations), host="127.0.0.1",
                                               port=port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        while not server.started:
            time.sleep(0.02)
        servers.append(server)
        host.base = f"http://127.0.0.1:{port}"
        return host

    yield start
    for server in servers:
        server.should_exit = True
