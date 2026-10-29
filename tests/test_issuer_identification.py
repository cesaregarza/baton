"""RFC 9207 issuer identification: every authorization response sent back to a client names the issuer.

A client that sees `authorization_response_iss_parameter_supported` can check which server answered,
which defends against mix-up attacks, and some clients then use one stable, exact callback URI
instead of a per-client one. Synthetic and offline: a real host on a loopback port, a throwaway passphrase.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import textwrap
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anyio
import httpx
import pytest
import uvicorn
from conftest import PASSPHRASE, REDIRECT, free_port
from mcp.shared.auth import OAuthClientInformationFull

from baton import auth
from baton import config as config_module
from baton.config import Config

STABLE = "https://assistant.example.test/connector_platform_oauth_redirect"
PER_CLIENT = "https://assistant.example.test/connector/oauth/abc_123-XYZ"


@dataclass
class IssuerHost:
    base: str  # where the app answers; a proxy in front would strip any public_url path
    config: Config

    @property
    def issuer(self) -> str:
        return self.config.public_url


def start(tmp_path: Path, path: str = "") -> tuple[IssuerHost, uvicorn.Server]:
    from baton.app import create_app

    port = free_port()
    file = tmp_path / "baton.toml"
    file.write_text(textwrap.dedent(f"""
        [host]
        public_url = "http://127.0.0.1:{port}{path}"
        listen_port = {port}
        data_dir = "{tmp_path / 'data'}"

        [roles.planner]
        description = "An example role"

        [auth.oauth]
        enabled = true
        passphrase_hash = "{auth.hash_passphrase(PASSPHRASE)}"
        roles = ["planner"]
        redirect_uris = ["{REDIRECT}", "{STABLE}"]
    """))
    config = config_module.load(file)
    server = uvicorn.Server(uvicorn.Config(create_app(config), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    return IssuerHost(f"http://127.0.0.1:{port}", config), server


@pytest.fixture(params=["", "/baton"], ids=["root", "under-a-path"])
def issuer_host(tmp_path: Path, request) -> Iterator[IssuerHost]:
    host, server = start(tmp_path, request.param)
    yield host
    server.should_exit = True


def register(host: IssuerHost, redirect: str = REDIRECT) -> httpx.Response:
    return httpx.post(host.base + "/register", json={"redirect_uris": [redirect], "client_name": "Example client",
                                                     "token_endpoint_auth_method": "none"})


def authorize_params(host: IssuerHost, client_id: str, redirect: str = REDIRECT, **overrides: str) -> dict:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return {"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "s1", "resource": host.config.resource_url, **overrides}


def query_of(response: httpx.Response) -> dict[str, list[str]]:
    return parse_qs(urlparse(response.headers["location"]).query)


# Metadata ------------------------------------------------------------------------------------------------


def test_the_metadata_advertises_issuer_identification_with_one_issuer(issuer_host: IssuerHost) -> None:
    host = issuer_host
    server = httpx.get(host.base + "/.well-known/oauth-authorization-server").json()
    resource_path = "/.well-known/oauth-protected-resource" + urlparse(host.config.resource_url).path
    resource = httpx.get(host.base + resource_path).json()
    assert server.get("authorization_response_iss_parameter_supported") is True
    assert server["issuer"] == host.issuer  # exactly, with no trailing slash added
    assert resource["authorization_servers"] == [server["issuer"]]


# iss on every redirect back to the client ----------------------------------------------------------------


def test_the_success_redirect_carries_iss(issuer_host: IssuerHost) -> None:
    host = issuer_host
    client_id = register(host).json()["client_id"]
    started = httpx.get(host.base + "/authorize", params=authorize_params(host, client_id))
    login_id = query_of(started)["id"][0]
    approved = httpx.post(host.base + "/login", data={"id": login_id, "passphrase": PASSPHRASE, "role": "planner"})
    assert approved.status_code == 302 and approved.headers["location"].startswith(REDIRECT + "?")
    query = query_of(approved)
    assert query.get("iss") == [host.issuer] and query["state"] == ["s1"] and query["code"]


@pytest.mark.parametrize("overrides, error", [
    ({"response_type": "token"}, "unsupported_response_type"),
    ({"code_challenge_method": "plain"}, "invalid_request"),
    ({"resource": "https://elsewhere.example.test/mcp"}, "invalid_target"),
    ({"scope": "not.a.scope"}, "invalid_scope"),
])
def test_every_error_redirect_to_the_client_carries_iss(issuer_host: IssuerHost, overrides: dict, error: str) -> None:
    host = issuer_host
    client_id = register(host).json()["client_id"]
    refused = httpx.get(host.base + "/authorize", params=authorize_params(host, client_id, **overrides))
    assert refused.status_code in (302, 303) and refused.headers["location"].startswith(REDIRECT + "?")
    query = query_of(refused)
    assert query["error"] == [error] and query.get("iss") == [host.issuer]


def test_iss_appears_only_on_redirects_back_to_the_client(issuer_host: IssuerHost) -> None:
    host = issuer_host
    client_id = register(host).json()["client_id"]
    started = httpx.get(host.base + "/authorize", params=authorize_params(host, client_id))
    assert started.headers["location"].startswith(host.issuer + "/login?")  # the host's own consent page
    assert "iss" not in query_of(started)
    login_id = query_of(started)["id"][0]
    assert "iss=" not in httpx.get(host.base + "/login", params={"id": login_id}).text
    wrong = httpx.post(host.base + "/login", data={"id": login_id, "passphrase": "wrong", "role": "planner"})
    assert wrong.status_code == 200 and "location" not in wrong.headers
    unknown = httpx.get(host.base + "/authorize", params=authorize_params(host, "no-such-client"))
    assert "location" not in unknown.headers  # no client to send it to, so no redirect at all
    assert "iss" not in httpx.get(host.base + "/.well-known/oauth-protected-resource"
                                  + urlparse(host.config.resource_url).path).json()


# The stable callback is an ordinary exact URI; a per-client one is refused and logged path-only --------------


def test_the_stable_callback_registers_when_listed_and_a_per_client_one_does_not(issuer_host: IssuerHost,
                                                                                  caplog) -> None:
    host = issuer_host
    assert register(host, STABLE).status_code == 201
    with caplog.at_level(logging.WARNING, logger="baton.auth"):
        refused = register(host, PER_CLIENT + "?code=secret-code&token=secret-token#frag")
    assert refused.status_code == 400
    [line] = [r.getMessage() for r in caplog.records if "redirect" in r.getMessage()]
    assert PER_CLIENT in line  # the operator sees the exact path to list, if they choose to
    assert "secret-code" not in line and "secret-token" not in line and "frag" not in line


def test_a_refusal_log_line_is_sanitized_and_capped(tmp_path: Path, caplog) -> None:
    config = config_module.parse({
        "host": {"public_url": "https://baton.example.test", "data_dir": str(tmp_path / "data")},
        "roles": {"planner": {}},
        "auth": {"oauth": {"enabled": True, "passphrase_hash": "scrypt$00$00", "roles": ["planner"]}}})
    config.data_dir.mkdir(mode=0o700)
    provider = auth.PassphraseProvider(config)
    for uri in ("https://client.example.test/cb\x1b[31m\r\nFAKE LOG LINE\x00\x7f",
                "https://someone@example.com/" + "y" * 5000):
        client = OAuthClientInformationFull.model_construct(client_id="c1", redirect_uris=[uri])
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="baton.auth"), pytest.raises(auth.RegistrationError):
            anyio.run(provider.register_client, client)
        [line] = [r.getMessage() for r in caplog.records if "redirect" in r.getMessage()]
        assert ("client.example.test" in line or "example.com" in line) and len(line) < 400
        assert not any(ord(ch) < 32 or ord(ch) == 127 for ch in line)
        assert "someone" not in line  # userinfo is never logged


# Unchanged -----------------------------------------------------------------------------------------------


def test_exact_and_loopback_callbacks_register_as_before(issuer_host: IssuerHost) -> None:
    host = issuer_host
    for uri in (REDIRECT, "http://127.0.0.1:43123/callback", "http://localhost:43123/callback"):
        assert register(host, uri).status_code == 201, uri
    for uri in (REDIRECT + "/extra", "https://elsewhere.example.test/cb"):
        assert register(host, uri).status_code == 400, uri
