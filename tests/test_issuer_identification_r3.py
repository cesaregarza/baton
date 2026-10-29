"""Second review of issuer identification: one canonical issuer, no stored callback with its own iss, no path in logs.

Synthetic and offline, reusing the real-host helpers from test_issuer_identification.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anyio
import httpx
import pytest
from conftest import PASSPHRASE
from test_issuer_identification import IssuerHost, authorize_params, query_of, register, start

from baton import auth
from baton.config import ConfigError, parse


@pytest.fixture(params=["", "/baton"], ids=["root", "under-a-path"])
def issuer_host(tmp_path: Path, request) -> Iterator[IssuerHost]:
    host, server = start(tmp_path, request.param)
    yield host
    server.should_exit = True


def config_with(public_url: str):
    return parse({"host": {"public_url": public_url}, "roles": {"planner": {}},
                  "auth": {"oauth": {"enabled": True, "passphrase_hash": "scrypt$00$00", "roles": ["planner"]}}})


# 1. One canonical issuer -----------------------------------------------------------------------------------


@pytest.mark.parametrize("public_url", [
    "https://BATON.example.test",            # a host in upper case: metadata would lowercase it
    "https://Baton.Example.Test/baton",
    "https://baton.example.test:443",        # a default port: metadata would drop it
    "http://baton.example.test:80/baton",
    "https://baton.example.test/café",  # a non-ASCII path: metadata would percent-encode it
    "https://baton.example.test/a%20b",      # an already-encoded path
])
def test_a_public_url_that_metadata_would_rewrite_is_refused(public_url: str) -> None:
    with pytest.raises(ConfigError, match="public_url"):
        config_with(public_url)


@pytest.mark.parametrize("public_url, kept", [
    ("https://baton.example.test", "https://baton.example.test"),
    ("https://baton.example.test/", "https://baton.example.test"),  # the trailing slash is stripped, as before
    ("https://baton.example.test:8443/baton", "https://baton.example.test:8443/baton"),
    ("http://127.0.0.1:8765", "http://127.0.0.1:8765"),
])
def test_a_canonical_public_url_is_accepted(public_url: str, kept: str) -> None:
    assert config_with(public_url).public_url == kept


def test_the_issuer_is_identical_in_metadata_and_every_redirect(issuer_host: IssuerHost) -> None:
    host = issuer_host
    server = httpx.get(host.base + "/.well-known/oauth-authorization-server").json()
    resource = httpx.get(host.base + "/.well-known/oauth-protected-resource"
                         + urlparse(host.config.resource_url).path).json()
    client_id = register(host).json()["client_id"]
    refused = httpx.get(host.base + "/authorize", params=authorize_params(host, client_id, response_type="token"))
    started = httpx.get(host.base + "/authorize", params=authorize_params(host, client_id))
    approved = httpx.post(host.base + "/login", data={"id": query_of(started)["id"][0], "passphrase": PASSPHRASE,
                                                      "role": "planner"})
    seen = {server["issuer"], *resource["authorization_servers"], *query_of(refused)["iss"],
            *query_of(approved)["iss"]}
    assert seen == {host.config.public_url}


# 2. A client stored before the guard can't add a second iss -------------------------------------------------


STORED_CALLBACK = "http://127.0.0.1:43123/callback?iss=https://attacker.example.test"


def persist_client(host: IssuerHost, client_id: str, redirect: str) -> None:
    """Write a client into the OAuth state file, as an older baton would have stored it."""
    state = {"clients": {client_id: {"client_id": client_id, "redirect_uris": [redirect],
                                     "token_endpoint_auth_method": "none", "grant_types": ["authorization_code",
                                                                                          "refresh_token"],
                                     "response_types": ["code"], "client_name": "Stored before the guard"}},
             "access": {}, "refresh": {}, "unused": {}}
    path = host.config.oauth_state_path
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(state, out)


def iss_counts(response: httpx.Response) -> int:
    location = response.headers.get("location", "")
    return len(parse_qs(urlparse(location).query).get("iss", []))


def test_a_stored_client_whose_callback_carries_iss_is_unusable(issuer_host: IssuerHost) -> None:
    host = issuer_host
    persist_client(host, "stored-client", STORED_CALLBACK)
    for overrides in ({}, {"resource": "https://elsewhere.example.test/mcp"}, {"response_type": "token"}):
        response = httpx.get(host.base + "/authorize", params=authorize_params(host, "stored-client", STORED_CALLBACK,
                                                                               **overrides))
        assert response.status_code == 400 and "location" not in response.headers, overrides
        assert iss_counts(response) == 0


def test_the_consent_page_never_redirects_to_a_callback_carrying_iss(tmp_path: Path) -> None:
    config = parse({"host": {"public_url": "https://baton.example.test", "data_dir": str(tmp_path / "data")},
                    "roles": {"planner": {}},
                    "auth": {"oauth": {"enabled": True, "passphrase_hash": auth.hash_passphrase(PASSPHRASE),
                                       "roles": ["planner"]}}})
    config.data_dir.mkdir(mode=0o700)
    provider = auth.PassphraseProvider(config)
    params = auth.AuthorizationParams(state="s1", scopes=None, code_challenge="x" * 43,
                                      redirect_uri=STORED_CALLBACK, redirect_uri_provided_explicitly=True)
    provider.pending["login-1"] = auth.PendingLogin("stored-client", params, ["baton.read"], 9e18)
    assert provider.complete_login("login-1", PASSPHRASE, "planner") is None


def test_no_redirect_leaves_carrying_a_second_iss() -> None:
    sent: list[dict] = []

    async def app(scope, receive, send) -> None:  # an SDK error redirect to a callback that already has iss
        await send({"type": "http.response.start", "status": 302,
                    "headers": [(b"location", (STORED_CALLBACK + "&error=invalid_request").encode())]})
        await send({"type": "http.response.body", "body": b""})

    class Provider:
        issuer = "https://baton.example.test"

        def is_consent_redirect(self, location: str) -> bool:
            return False

    async def capture(message: dict) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "http.request", "body": b""}

    middleware = auth.IssuerIdentification(app, Provider())  # type: ignore[arg-type]
    anyio.run(middleware, {"type": "http", "path": "/authorize", "method": "GET"}, receive, capture)
    start_message = sent[0]
    assert start_message["status"] == 400
    assert all(name.lower() != b"location" for name, _ in start_message.get("headers", []))


# 3. The refusal log carries no path characters at all ------------------------------------------------------


@pytest.mark.parametrize("uri", [
    "https://client.example.test/reset/abc123def456",
    "https://client.example.test/connector/oauth/abc_123-XYZ",
    "https://client.example.test/abc123def456",
])
def test_no_path_characters_are_logged(uri: str) -> None:
    shown = auth.loggable_uri(uri)
    path = urlparse(uri).path
    assert shown.startswith("https://client.example.test") and "[path redacted]" in shown
    assert all(segment not in shown for segment in path.split("/") if segment)


def test_the_refusal_line_for_an_alphanumeric_secret_path(issuer_host: IssuerHost, caplog) -> None:
    host = issuer_host
    with caplog.at_level(logging.WARNING, logger="baton.auth"):
        assert register(host, "https://client.example.test/reset/abc123def456").status_code == 400
    [line] = [r.getMessage() for r in caplog.records if "redirect" in r.getMessage()]
    assert "abc123def456" not in line and "reset" not in line and "client.example.test" in line
    assert len(line) <= 300
