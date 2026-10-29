"""Review findings on issuer identification: one `iss` only, a structural consent-page check, and a safe log line.

Synthetic and offline, reusing the real-host fixture from test_issuer_identification.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

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


# 1. A callback can't smuggle in a second issuer -----------------------------------------------------------


def test_a_callback_carrying_iss_is_refused_at_registration(issuer_host: IssuerHost) -> None:
    host = issuer_host
    for uri in ("http://127.0.0.1:43123/callback?iss=https://attacker.example.test",
                "http://127.0.0.1:43123/callback?a=1&iss=x",
                "http://localhost:43123/callback?ISS=x&iss=y"):
        assert register(host, uri).status_code == 400, uri


def test_a_configured_redirect_uri_carrying_iss_is_refused() -> None:
    with pytest.raises(ConfigError, match="iss"):
        parse({"host": {"public_url": "https://baton.example.test"}, "roles": {"planner": {}},
               "auth": {"oauth": {"enabled": True, "passphrase_hash": "scrypt$00$00", "roles": ["planner"],
                                  "redirect_uris": ["https://client.example.test/cb?iss=https://x.example.test"]}}})


def test_every_redirect_to_a_client_with_a_query_carries_exactly_one_iss(issuer_host: IssuerHost) -> None:
    host = issuer_host
    callback = "http://127.0.0.1:43123/callback?keep=1"
    client_id = register(host, callback).json()["client_id"]
    refused = httpx.get(host.base + "/authorize", params=authorize_params(
        host, client_id, callback, resource="https://elsewhere.example.test/mcp"))
    assert query_of(refused)["iss"] == [host.issuer] and query_of(refused)["keep"] == ["1"]
    started = httpx.get(host.base + "/authorize", params=authorize_params(host, client_id, callback))
    approved = httpx.post(host.base + "/login", data={"id": query_of(started)["id"][0], "passphrase": PASSPHRASE,
                                                      "role": "planner"})
    assert query_of(approved)["iss"] == [host.issuer] and query_of(approved)["keep"] == ["1"]


# 2. The host's own consent redirect is recognized structurally, not by prefix ------------------------------


def test_a_callback_shaped_like_the_consent_page_still_gets_iss(issuer_host: IssuerHost) -> None:
    host = issuer_host
    # A loopback callback on the host's own origin and path, which the loopback rule allows.
    lookalike = f"{host.config.public_url}/login?id=not-a-login"
    assert lookalike.startswith("http://127.0.0.1:")
    client_id = register(host, lookalike).json()["client_id"]
    refused = httpx.get(host.base + "/authorize", params=authorize_params(
        host, client_id, lookalike, resource="https://elsewhere.example.test/mcp"))
    assert refused.headers["location"].startswith(host.config.public_url + "/login?")
    query = query_of(refused)
    assert query["error"] == ["invalid_target"] and query.get("iss") == [host.issuer]


def test_the_real_consent_redirect_still_has_no_iss(issuer_host: IssuerHost) -> None:
    host = issuer_host
    client_id = register(host).json()["client_id"]
    started = httpx.get(host.base + "/authorize", params=authorize_params(host, client_id))
    assert set(query_of(started)) == {"id"}


# 3. The refusal log line: an unknown path shape is redacted, and the whole line is bounded ------------------


def _config(tmp_path: Path):
    config = parse({"host": {"public_url": "https://baton.example.test", "data_dir": str(tmp_path / "data")},
                    "roles": {"planner": {}},
                    "auth": {"oauth": {"enabled": True, "passphrase_hash": "scrypt$00$00", "roles": ["planner"]}}})
    config.data_dir.mkdir(mode=0o700)
    return config


@pytest.mark.parametrize("path", [
    "/reset/password=hunter2",          # a value in the path
    "/cb;session=abc123",               # a matrix parameter
    "/a/b/c/d/e/f/g/h",                 # more segments than an ordinary callback
    "/" + "z" * 65,                     # a segment long enough to be a credential
    "/café/cb",                    # outside the plain alphabet
])
def test_a_path_of_unknown_shape_is_redacted(tmp_path: Path, caplog, path: str) -> None:
    import anyio
    from mcp.shared.auth import OAuthClientInformationFull

    provider = auth.PassphraseProvider(_config(tmp_path))
    client = OAuthClientInformationFull.model_construct(client_id="c1",
                                                        redirect_uris=["https://client.example.test" + path])
    with caplog.at_level(logging.WARNING, logger="baton.auth"), pytest.raises(auth.RegistrationError):
        anyio.run(provider.register_client, client)
    [line] = [r.getMessage() for r in caplog.records if "redirect" in r.getMessage()]
    assert "https://client.example.test" in line and path not in line and "redacted" in line


def test_an_ordinary_callback_path_is_still_shown() -> None:
    assert auth.loggable_uri("https://client.example.test/connector/oauth/abc_123-XYZ") == \
        "https://client.example.test/connector/oauth/abc_123-XYZ"


@pytest.mark.parametrize("uri", [
    "https://" + "h" * 60 + "." + "i" * 60 + "." + "j" * 60 + ".example.test/cb",
    "https://client.example.test/" + "/".join(["abcdefghij"] * 6),
])
def test_the_whole_logged_value_is_bounded(uri: str) -> None:
    shown = auth.loggable_uri(uri)
    assert len(shown) <= auth.MAX_LOGGED_URI


# 4. Under a path prefix, the root metadata route carries the path issuer; a proxy forwards the path form ------


def test_the_root_metadata_route_serves_the_path_issuer(issuer_host: IssuerHost) -> None:
    host = issuer_host
    server = httpx.get(host.base + "/.well-known/oauth-authorization-server").json()
    assert server["issuer"] == host.config.public_url
    path = urlparse(host.config.public_url).path
    if path:
        # baton serves no RFC 8414 path-form route itself; a deployment under a path forwards it to the root.
        assert httpx.get(host.base + "/.well-known/oauth-authorization-server" + path).status_code == 404


def test_the_docs_say_how_to_forward_the_path_form_metadata_route() -> None:
    docs = (Path(__file__).parent.parent / "docs" / "exposing.md").read_text()
    assert "/.well-known/oauth-authorization-server<path>" in docs



def test_control_characters_in_the_host_are_not_logged() -> None:
    shown = auth.loggable_uri("https://client\x1b\x07\x00.example.test/cb")
    assert not any(ord(ch) < 32 or ord(ch) == 127 for ch in shown) and shown.startswith("https://client")
