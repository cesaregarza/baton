"""OAuth end to end against a real host, plus the pieces that need no server."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import anyio
import httpx
import httpx2
from conftest import PASSPHRASE, REDIRECT, Host
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_integration import payload

from baton import auth
from baton.config import parse


def register(host: Host, redirect: str = REDIRECT) -> httpx.Response:
    return httpx.post(host.base + "/register", json={"redirect_uris": [redirect], "client_name": "Example client",
                                                     "token_endpoint_auth_method": "none"})


def authorize(host: Host, *, role: str = "planner", passphrase: str = PASSPHRASE) -> dict:
    client_id = register(host).json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    started = httpx.get(host.base + "/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": "s1", "resource": host.mcp_url})
    login_id = parse_qs(urlparse(started.headers["location"]).query)["id"][0]
    page = httpx.get(host.base + "/login", params={"id": login_id})
    assert "Example client" in page.text and "<option>planner</option>" in page.text
    approved = httpx.post(host.base + "/login", data={"id": login_id, "passphrase": passphrase, "role": role})
    if approved.status_code != 302:
        return {"refused": approved.status_code}
    query = parse_qs(urlparse(approved.headers["location"]).query)
    assert query["state"] == ["s1"]
    tokens = httpx.post(host.base + "/token", data={
        "grant_type": "authorization_code", "code": query["code"][0], "client_id": client_id,
        "redirect_uri": REDIRECT, "code_verifier": verifier}).json()
    return {**tokens, "client_id": client_id}


def whoami(host: Host, token: str) -> dict:
    async def run() -> dict:
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
        async with Client(streamable_http_client(host.mcp_url, http_client=http)) as client:
            return payload(await client.call_tool("whoami", {}))

    return anyio.run(run)


def test_metadata_points_clients_at_the_host(start_host) -> None:
    host = start_host(oauth=True)
    server = httpx.get(host.base + "/.well-known/oauth-authorization-server").json()
    resource = httpx.get(host.base + "/.well-known/oauth-protected-resource/mcp").json()
    assert server["issuer"] == host.config.public_url
    assert server["registration_endpoint"] == host.base + "/register"
    assert server["scopes_supported"] == auth.ALL_SCOPES
    assert resource["resource"] == host.mcp_url
    assert resource["authorization_servers"] == [host.config.public_url]
    challenge = httpx.post(host.mcp_url, json={}).headers["www-authenticate"]
    assert "/.well-known/oauth-protected-resource/mcp" in challenge


def test_a_client_authorizes_with_the_passphrase_and_acts_as_the_chosen_role(start_host) -> None:
    host = start_host(oauth=True)
    tokens = authorize(host)
    assert whoami(host, tokens["access_token"])["role"] == "planner"
    # Bearer tokens keep working beside OAuth.
    assert whoami(host, host.tokens["worker"])["role"] == "worker"

    refreshed = httpx.post(host.base + "/token", data={"grant_type": "refresh_token", "client_id": tokens["client_id"],
                                                       "refresh_token": tokens["refresh_token"]}).json()
    assert whoami(host, refreshed["access_token"])["role"] == "planner"
    reused = httpx.post(host.base + "/token", data={"grant_type": "refresh_token", "client_id": tokens["client_id"],
                                                    "refresh_token": tokens["refresh_token"]})
    assert reused.status_code == 400  # rotation spent the first refresh token

    # Both access tokens (refreshing does not end the old one early) and the live refresh token.
    assert auth.revoke_all(host.config.oauth_state_path) == 3
    assert httpx.post(host.mcp_url, json={}, headers={"Authorization": f"Bearer {refreshed['access_token']}"}
                      ).status_code == 401


def test_a_wrong_passphrase_or_a_role_not_offered_is_refused(start_host) -> None:
    host = start_host(oauth=True)
    assert authorize(host, passphrase="wrong passphrase") == {"refused": 200}
    assert authorize(host, role="worker") == {"refused": 200}


def test_registration_needs_an_allowed_redirect(start_host) -> None:
    host = start_host(oauth=True)
    assert register(host, "https://elsewhere.example.test/cb").status_code == 400
    assert register(host, "http://127.0.0.1:43123/callback").status_code == 201


def test_redirect_rules() -> None:
    config = parse({"host": {"public_url": "https://baton.example.test"}, "roles": {"planner": {}},
                    "auth": {"oauth": {"enabled": True, "passphrase_hash": "scrypt$00$00", "roles": ["planner"],
                                       "redirect_uris": [REDIRECT], "allow_loopback_redirects": False}}})
    assert auth.valid_redirect_uri(REDIRECT, config)
    assert not auth.valid_redirect_uri(REDIRECT + "/extra", config)
    assert not auth.valid_redirect_uri("http://127.0.0.1:5000/cb", config)


def test_tokens_and_passphrases() -> None:
    token, digest = auth.new_token()
    assert token.startswith("baton_") and digest == auth.token_digest(token) and token not in digest
    stored = auth.hash_passphrase("a long example passphrase")
    assert auth.check_passphrase("a long example passphrase", stored)
    assert not auth.check_passphrase("something else", stored)
