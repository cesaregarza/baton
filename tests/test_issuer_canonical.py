"""The issuer is exactly what the OAuth metadata publishes, so every iss matches it character for character.

`public_url` is compared with the SDK's own serialization of it (the metadata's `issuer`). A form the SDK
would rewrite is refused, naming the form to write; any other form is used as written, and then the AS
metadata, the protected-resource metadata and every `iss` agree. A percent-encoded path is refused too,
for another reason: the protected-resource metadata route can't be matched under it. Synthetic and
offline: in-process requests.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import PASSPHRASE, REDIRECT
from mcp.server.auth.settings import AuthSettings
from starlette.testclient import TestClient
from test_issuer_identification import authorize_params

from baton import auth
from baton import config as config_module
from baton.config import ConfigError, parse

FORMS = [
    "https://münich.example.test",                                # a Unicode host
    "https://BATON.example.test",                                 # an upper-case host
    "https://baton.example.test:443",                             # a default port
    "http://baton.example.test:80/baton",
    "https://baton.example.test:0443",                            # a padded port
    "https://baton.example.test:08443/baton",
    "https://[2001:0db8:0000:0000:0000:0000:0000:0001]:8765",     # an uncompressed IPv6 host
    "https://[2001:DB8::1]:8765",
    "https://baton.example.test/a/../baton",                      # ".." segments
    "https://baton.example.test/./baton",
    "https://baton.example.test/a%20b",                           # percent-encoding
    "https://baton.example.test/café",                       # a non-ASCII path
    "https://baton.example.test/",                                # a trailing slash
    "https://baton.example.test/baton/",
    "https://baton.example.test",                                 # already canonical
    "https://xn--mnich-kva.example.test",
    "https://[2001:db8::1]:8765",
    "https://baton.example.test:8443/baton",
]


def sdk_issuer(public_url: str) -> str:
    """The issuer exactly as the MCP SDK will publish it for this public_url."""
    return str(AuthSettings(issuer_url=public_url, resource_server_url=public_url + "/mcp",
                            validate_token_resource=True).issuer_url)


def raw(public_url: str, tmp_path: Path) -> dict:
    return {"host": {"public_url": public_url, "data_dir": str(tmp_path / "data")}, "roles": {"planner": {}},
            "auth": {"oauth": {"enabled": True, "passphrase_hash": auth.hash_passphrase(PASSPHRASE),
                               "roles": ["planner"], "redirect_uris": [REDIRECT]}}}


@pytest.mark.parametrize("public_url", FORMS)
def test_each_form_is_refused_naming_the_canonical_one_or_agrees_everywhere(public_url: str,
                                                                           tmp_path: Path) -> None:
    written = public_url.rstrip("/")  # the config strips one trailing slash, as before
    canonical = sdk_issuer(written)
    if "%" in urlparse(canonical).path:
        with pytest.raises(ConfigError, match="percent"):
            parse(raw(public_url, tmp_path))
        return
    if canonical != written:
        with pytest.raises(ConfigError) as refused:
            parse(raw(public_url, tmp_path))
        assert f"public_url = {canonical!r}" in str(refused.value) or f"'{canonical}'" in str(refused.value)
        return
    config = parse(raw(public_url, tmp_path))
    assert config.public_url == canonical
    assert observed_issuers(config) == {canonical}


def observed_issuers(config) -> set[str]:
    """Every issuer a client can see: AS metadata, PRM authorization_servers, an error iss and a success iss."""
    from baton.app import create_app

    config.data_dir.mkdir(mode=0o700, exist_ok=True)
    origin = "{0.scheme}://{0.netloc}".format(urlparse(config.public_url))
    with TestClient(create_app(config), base_url=origin, follow_redirects=False) as http:
        server = http.get("/.well-known/oauth-authorization-server").json()
        resource = http.get("/.well-known/oauth-protected-resource" + urlparse(config.resource_url).path).json()
        client_id = http.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Example",
                                                 "token_endpoint_auth_method": "none"}).json()["client_id"]

        class Host:  # the shape authorize_params expects
            pass

        host = Host()
        host.config = config  # type: ignore[attr-defined]
        refused = http.get("/authorize", params=authorize_params(host, client_id, response_type="token"))
        started = http.get("/authorize", params=authorize_params(host, client_id))
        login_id = parse_qs(urlparse(started.headers["location"]).query)["id"][0]
        approved = http.post("/login", data={"id": login_id, "passphrase": PASSPHRASE, "role": "planner"})
    error_iss = parse_qs(urlparse(refused.headers["location"]).query)["iss"]
    success_iss = parse_qs(urlparse(approved.headers["location"]).query)["iss"]
    return {server["issuer"], *resource["authorization_servers"], *error_iss, *success_iss}


@pytest.mark.parametrize("public_url", FORMS)
def test_the_config_computes_the_same_issuer_as_the_sdk(public_url: str) -> None:
    written = public_url.rstrip("/")
    computed = getattr(config_module, "canonical_issuer", lambda url: url)(written)
    assert computed == sdk_issuer(written)
