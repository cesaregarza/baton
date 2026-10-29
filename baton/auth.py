"""Client authentication: static bearer tokens, and an optional OAuth 2.1 server for remote MCP clients.

Every credential resolves to one configured role, carried in the access
token's `claims["role"]`; tools act as that role and nothing else.

* **Bearer tokens** are listed in the config by SHA-256 digest
  (`baton token new` prints a token and its digest). Simple and enough for
  local agents and scripts, and the only credential a human role can hold.
  `baton token revoke` ends one at once, without a restart.
* **OAuth** follows the MCP authorization spec: protected-resource and
  authorization-server metadata, dynamic client registration, PKCE, refresh
  token rotation and revocation. Consent is a page on the host where the
  operator enters a passphrase (stored as an scrypt hash) and picks the role
  the client may act as. Redirect URIs must be on the configured allowlist, or
  loopback addresses (RFC 8252) if allowed. Every authorization response sent
  back to a client names the issuer (RFC 9207), so a client can tell which
  server answered; some clients then use one stable, exact callback URI.

OAuth tokens are opaque and stored only as SHA-256 digests in one 0600 JSON
file in the checked data directory (`baton.datadir`). Access tokens last an
hour; refresh tokens last 30 days and rotate on every use.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import RequestBodyLimitMiddleware
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from baton import datadir
from baton.config import Config, ConfigError
from baton.errors import UnknownToken

log = logging.getLogger("baton.auth")

SCOPE_READ = "baton.read"
SCOPE_WRITE = "baton.write"
ALL_SCOPES = [SCOPE_READ, SCOPE_WRITE]

# The public, unauthenticated endpoints keep state, so all of it is bounded.
MAX_CLIENTS = 50
MAX_PENDING_LOGINS = 20
UNUSED_CLIENT_TTL = 24 * 3600
MAX_REGISTRATION_BODY = 8 * 1024
MAX_AUTHORIZE_QUERY = 4 * 1024
ENDPOINT_LIMITS = {"/register": (10, 30), "/authorize": (30, 100)}  # per hour: (per source, overall)
RATE_WINDOW = 3600
MAX_LOGIN_FAILURES_PER_HOUR = 5

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300
LOGIN_TTL = 600


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> tuple[str, str]:
    """A fresh bearer token and the digest that goes in the config."""
    token = "baton_" + secrets.token_urlsafe(32)
    return token, token_digest(token)


def hash_passphrase(passphrase: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(passphrase.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_passphrase(passphrase: str, stored: str) -> bool:
    _, salt, _ = stored.split("$")
    return hmac.compare_digest(hash_passphrase(passphrase, bytes.fromhex(salt)), stored)


def static_token(config: Config, token: str) -> AccessToken | None:
    digest = token_digest(token)
    for entry in config.tokens:
        if hmac.compare_digest(entry.sha256, digest):
            if entry.sha256 in revoked_digests(config):
                return None
            return AccessToken(token=token, client_id=f"token:{entry.name}", scopes=ALL_SCOPES,
                               resource=config.resource_url, subject=entry.role, claims={"role": entry.role})
    return None


# The revocation list as last read, by path: (inode, mtime, size) and its digests.
_revoked_cache: dict[Path, tuple[tuple[int, int, int], frozenset[str]]] = {}


def revoked_digests(config: Config) -> frozenset[str]:
    """Digests of bearer tokens ended by `baton token revoke`, so a revocation needs no restart.

    Checked on every call: a cheap stat notices any change, and only then is the
    file read through the checked data directory. With no revocations there is
    nothing to read.
    """
    path = config.revoked_tokens_path
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return frozenset()
    signature = (info.st_ino, info.st_mtime_ns, info.st_size)
    cached = _revoked_cache.get(path)
    if cached is not None and cached[0] == signature:
        return cached[1]
    with datadir.opened(config.data_dir) as dir_fd:
        found = datadir.read_file(dir_fd, path.name, directory=config.data_dir)
    if found is None:
        return frozenset()
    digests = frozenset(json.loads(found[1])["revoked"])
    _revoked_cache[path] = ((found[0].st_ino, found[0].st_mtime_ns, found[0].st_size), digests)
    return digests


def revoke_token(config: Config, name: str) -> dict:
    """End the bearer token configured as `name` from its next call on. Remove its config entry when convenient."""
    matches = [entry for entry in config.tokens if entry.name == name]
    if not matches:
        raise UnknownToken(f"no auth.tokens entry is named {name!r}; nothing was revoked")
    if len(matches) > 1:  # the loader refuses this; a config built another way must not half-revoke
        raise ConfigError(f"auth.tokens has more than one entry named {name!r}; nothing was revoked")
    entry = matches[0]
    directory = config.data_dir
    with datadir.opened(directory) as dir_fd:
        lock = datadir.open_file(dir_fd, config.revoked_tokens_path.stem + ".lock", os.O_RDWR | os.O_CREAT,
                                 directory=directory)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            found = datadir.read_file(dir_fd, config.revoked_tokens_path.name, directory=directory)
            state = json.loads(found[1]) if found else {"revoked": {}}
            state["revoked"][entry.sha256] = {"name": name, "role": entry.role,
                                              "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            datadir.replace_file(dir_fd, config.revoked_tokens_path.name, json.dumps(state).encode())
        finally:
            os.close(lock)
    log.info("revoked bearer token %s (role %s)", name, entry.role)
    return {"revoked": name, "role": entry.role}


def credential_authority(token: AccessToken) -> dict:
    """What a credential is, so a later check can tell whether that same credential still holds that authority.

    A bearer token is its config entry's name, digest and role; an OAuth
    credential is its client, its grant (stable across refreshes), role,
    scopes and resource. Integrations store this when a client subscribes.
    """
    role = role_of(token)
    if token.client_id.startswith("token:"):
        return {"kind": "token", "name": token.client_id.removeprefix("token:"), "sha256": token_digest(token.token),
                "role": role}
    return {"kind": "oauth", "client_id": token.client_id, "grant": (token.claims or {}).get("grant"), "role": role,
            "scopes": sorted(token.scopes), "resource": token.resource}


def authority_live(config: Config, authority: dict) -> bool:
    """Whether the credential described by `credential_authority` still exists with the same role.

    A bearer token must still be configured under the same name, digest and
    role. An OAuth grant must still have an unexpired token, its role must still
    be one OAuth may grant, and it must be for this resource with read scope.
    `baton oauth revoke-all` and a normal refresh behave as expected: the first
    ends the grant, the second keeps it.
    """
    if authority.get("kind") == "token":
        return authority["sha256"] not in revoked_digests(config) and any(
            (entry.name, entry.sha256, entry.role) == (authority["name"], authority["sha256"], authority["role"])
            for entry in config.tokens)
    if (authority.get("kind") != "oauth" or not config.oauth.enabled or authority.get("grant") is None
            or authority["role"] not in config.oauth.roles or authority["resource"] != config.resource_url):
        return False
    store = TokenStore.load(config.oauth_state_path)
    now = time.time()
    return any((grant["client_id"], grant.get("grant"), grant["role"]) == (authority["client_id"], authority["grant"],
                                                                          authority["role"])
               and grant["expires_at"] > now and SCOPE_READ in grant["scopes"]
               and grant.get("resource") == authority["resource"]
               for grant in [*store.access.values(), *store.refresh.values()])


def role_of(token: AccessToken | None) -> str | None:
    return (token.claims or {}).get("role") if token else None


class StaticTokenVerifier:
    """The verifier when OAuth is off: only configured bearer tokens are accepted."""

    def __init__(self, config: Config) -> None:
        self._config = config

    async def verify_token(self, token: str) -> AccessToken | None:
        return static_token(self._config, token)


def carries_iss(uri: str) -> bool:
    """Whether a callback already has an `iss` parameter (RFC 9207 reserves it for the issuer the host adds)."""
    try:
        query = urlparse(uri).query
    except ValueError:
        return True
    return any(key.lower() == "iss" for key, _ in parse_qsl(query, keep_blank_values=True))


def valid_redirect_uri(uri: str, config: Config) -> bool:
    """Exactly a configured URI, or (if allowed) a plain-HTTP loopback address on any port (RFC 8252 §7.3)."""
    if uri in config.oauth.redirect_uris:
        return True
    parsed = urlparse(uri)
    return (config.oauth.allow_loopback_redirects and parsed.scheme == "http"
            and parsed.hostname in ("127.0.0.1", "::1", "localhost")
            and parsed.username is None and parsed.password is None and not parsed.fragment)


@dataclass
class TokenStore:
    """Clients and token digests in one 0600 JSON file, which is the source of truth.

    Reads re-load the file when it has changed, so `baton oauth revoke-all`
    takes effect at once. Writes run inside `mutation()`, under an exclusive
    lock and after a fresh load, so a write never resurrects a revoked grant.
    """

    path: Path
    clients: dict[str, dict] = field(default_factory=dict)
    access: dict[str, dict] = field(default_factory=dict)
    refresh: dict[str, dict] = field(default_factory=dict)
    unused: dict[str, float] = field(default_factory=dict)  # registrations not yet authorized, by arrival
    _signature: tuple[int, int, int] | None = None

    @classmethod
    def load(cls, path: Path) -> TokenStore:
        store = cls(path)
        store.sync()
        return store

    def sync(self) -> None:
        directory = self.path.parent
        with datadir.opened(directory) as dir_fd:
            try:
                fd = datadir.open_file(dir_fd, self.path.name, os.O_RDONLY, directory=directory)
            except FileNotFoundError:
                signature, raw = None, b"{}"
            else:
                with os.fdopen(fd, "rb") as state:
                    info = os.fstat(state.fileno())
                    signature = (info.st_ino, info.st_mtime_ns, info.st_size)
                    raw = state.read() if signature != self._signature else b""
        if signature == self._signature:
            return
        data = json.loads(raw)
        self.clients, self.access = data.get("clients", {}), data.get("access", {})
        self.refresh, self.unused = data.get("refresh", {}), data.get("unused", {})
        self._signature = signature

    @contextmanager
    def mutation(self) -> Iterator[TokenStore]:
        directory = self.path.parent
        with datadir.opened(directory) as dir_fd:
            lock = datadir.open_file(dir_fd, self.path.stem + ".lock", os.O_RDWR | os.O_CREAT, directory=directory)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self._signature = None
                self.sync()
                self._drop_expired()
                yield self
                self._save(dir_fd)
            finally:
                os.close(lock)

    def _save(self, dir_fd: int) -> None:
        body = json.dumps({"clients": self.clients, "access": self.access, "refresh": self.refresh,
                           "unused": self.unused}).encode()
        info = datadir.replace_file(dir_fd, self.path.name, body)
        self._signature = (info.st_ino, info.st_mtime_ns, info.st_size)

    def _drop_expired(self) -> None:
        now = time.time()
        self.access = {key: value for key, value in self.access.items() if value["expires_at"] > now}
        self.refresh = {key: value for key, value in self.refresh.items() if value["expires_at"] > now}
        for client_id in [cid for cid, at in self.unused.items() if at < now - UNUSED_CLIENT_TTL]:
            self.clients.pop(client_id, None)
            del self.unused[client_id]


def revoke_all(state_path: Path) -> int:
    """End every OAuth grant; registrations stay, so a client can authorize again."""
    store = TokenStore.load(state_path)
    with store.mutation():
        revoked = len(store.access) + len(store.refresh)
        store.access, store.refresh = {}, {}
    return revoked


@dataclass
class PendingLogin:
    client_id: str
    params: AuthorizationParams
    scopes: list[str]
    expires_at: float


class PassphraseProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, config: Config) -> None:
        self.config = config
        self.issuer = config.public_url  # exactly the metadata's `issuer`; RFC 9207 clients compare it as a string
        self.store = TokenStore.load(config.oauth_state_path)
        self.codes: dict[str, tuple[AuthorizationCode, str]] = {}  # code -> (code, role)
        self.pending: dict[str, PendingLogin] = {}
        self.login_failures: list[float] = []

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        self.store.sync()
        data = self.store.clients.get(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if carries_iss(str(uri)):
                log.warning("registration refused: redirect URI carries the reserved iss parameter: %s",
                            loggable_uri(str(uri)))
                raise RegistrationError("invalid_redirect_uri", "a redirect URI may not carry an iss parameter")
            if not valid_redirect_uri(str(uri), self.config):
                log.warning("registration refused: redirect URI not allowed: %s", loggable_uri(str(uri)))
                raise RegistrationError("invalid_redirect_uri", "redirect URI is not on the host's allowlist")
        with self.store.mutation() as store:
            if len(store.clients) >= MAX_CLIENTS:
                raise RegistrationError("invalid_client_metadata", "too many registered clients")
            store.clients[client_info.client_id] = client_info.model_dump(mode="json")
            store.unused[client_info.client_id] = time.time()
        log.info("registered client %s", client_info.client_id)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.config.resource_url:
            raise AuthorizeError("invalid_target", "unknown resource")
        self.sweep()
        if len(self.pending) >= MAX_PENDING_LOGINS:
            raise AuthorizeError("temporarily_unavailable", "too many logins in progress")
        scopes = [scope for scope in (params.scopes or ALL_SCOPES) if scope in ALL_SCOPES]
        login_id = secrets.token_urlsafe(32)
        self.pending[login_id] = PendingLogin(client.client_id, params, scopes, time.time() + LOGIN_TTL)
        return f"{self.config.public_url}/login?id={login_id}"

    def complete_login(self, login_id: str, passphrase: str, role: str) -> str | None:
        """The client's redirect URL on success; None for a wrong, expired or locked-out attempt."""
        now = time.time()
        self.sweep()
        self.login_failures = [at for at in self.login_failures if at > now - 3600]
        if len(self.login_failures) >= MAX_LOGIN_FAILURES_PER_HOUR:
            log.warning("login locked out for the rest of the hour")
            return None
        login = self.pending.get(login_id)
        if login is None or role not in self.config.oauth.roles:
            return None
        if not check_passphrase(passphrase, self.config.oauth.passphrase_hash):
            self.login_failures.append(now)
            log.warning("wrong passphrase (%d this hour)", len(self.login_failures))
            return None
        del self.pending[login_id]
        code = secrets.token_urlsafe(32)
        self.codes[code] = (AuthorizationCode(
            code=code, scopes=login.scopes, expires_at=now + CODE_TTL, client_id=login.client_id,
            code_challenge=login.params.code_challenge, redirect_uri=login.params.redirect_uri,
            redirect_uri_provided_explicitly=login.params.redirect_uri_provided_explicitly,
            resource=self.config.resource_url, subject=role,
        ), role)
        log.info("login approved for client %s as role %s", login.client_id, role)
        return construct_redirect_uri(str(login.params.redirect_uri), code=code, state=login.params.state,
                                      iss=self.issuer)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        entry = self.codes.get(authorization_code)
        return entry[0] if entry and entry[0].client_id == client.client_id else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        _, role = self.codes.pop(authorization_code.code)
        with self.store.mutation() as store:
            store.unused.pop(client.client_id, None)
            return self._issue(client.client_id, authorization_code.scopes, role, secrets.token_hex(16))

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        self.store.sync()
        data = self.store.refresh.get(token_digest(refresh_token))
        if data is None or data["client_id"] != client.client_id or data["expires_at"] < time.time():
            return None
        return RefreshToken(token=refresh_token, client_id=data["client_id"], scopes=data["scopes"],
                            expires_at=data["expires_at"], resource=data["resource"], subject=data["role"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        if scopes and not set(scopes) <= set(refresh_token.scopes):
            raise TokenError("invalid_scope")
        with self.store.mutation() as store:
            # Rotation: the presented token is spent. One revoked since it was loaded is refused.
            spent = store.refresh.pop(token_digest(refresh_token.token), None)
            if spent is None:
                raise TokenError("invalid_grant", "refresh token is no longer valid")
            return self._issue(client.client_id, scopes or refresh_token.scopes, spent["role"],
                               spent.get("grant") or secrets.token_hex(16))

    async def load_access_token(self, token: str) -> AccessToken | None:
        static = static_token(self.config, token)
        if static is not None:
            return static
        self.store.sync()
        data = self.store.access.get(token_digest(token))
        if data is None or data["expires_at"] < time.time() or data["role"] not in self.config.oauth.roles:
            return None
        # The SDK refuses a token whose resource is not this server's (validate_token_resource).
        return AccessToken(token=token, client_id=data["client_id"], scopes=data["scopes"],
                           expires_at=data["expires_at"], resource=data["resource"], subject=data["role"],
                           claims={"role": data["role"], "grant": data.get("grant")})

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoking either token ends every grant the client holds."""
        with self.store.mutation() as store:
            for table in (store.access, store.refresh):
                for key in [key for key, value in table.items() if value["client_id"] == token.client_id]:
                    del table[key]

    def sweep(self) -> None:
        now = time.time()
        self.pending = {key: login for key, login in self.pending.items() if login.expires_at > now}
        self.codes = {key: entry for key, entry in self.codes.items() if entry[0].expires_at > now}

    def _issue(self, client_id: str, scopes: list[str], role: str, grant_id: str | None) -> OAuthToken:
        """Mint a token pair; the caller holds `store.mutation()`. `grant_id` stays the same across refreshes."""
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        grant = {"client_id": client_id, "scopes": scopes, "resource": self.config.resource_url, "role": role,
                 "grant": grant_id}
        self.store.access[token_digest(access)] = {**grant, "expires_at": now + ACCESS_TTL}
        self.store.refresh[token_digest(refresh)] = {**grant, "expires_at": now + REFRESH_TTL}
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL, refresh_token=refresh,
                          scope=" ".join(scopes))


class EndpointGuard:
    """ASGI middleware that bounds `/register` and `/authorize` before either keeps any state.

    Over-long requests, hourly limits per source and overall, and the caps on
    clients and pending logins are checked first, so a refused request leaves
    nothing behind.
    """

    def __init__(self, app: ASGIApp, provider: PassphraseProvider) -> None:
        self._app = app
        self._register_app = RequestBodyLimitMiddleware(app, MAX_REGISTRATION_BODY)
        self._provider = provider
        self._totals: dict[str, deque[float]] = {}
        self._sources: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path not in ENDPOINT_LIMITS:
            return await self._app(scope, receive, send)
        refusal = self._refusal(scope, path)
        if refusal is not None:
            log.warning("%s refused: %s", path, refusal[1])
            return await JSONResponse({"error": refusal[1]}, status_code=refusal[0])(scope, receive, send)
        await (self._register_app if path == "/register" else self._app)(scope, receive, send)

    def _refusal(self, scope: Scope, path: str) -> tuple[int, str] | None:
        if path == "/authorize" and len(scope.get("query_string", b"")) > MAX_AUTHORIZE_QUERY:
            return 414, "request_too_long"
        self._provider.sweep()
        if path == "/register" and len(self._provider.store.clients) >= MAX_CLIENTS:
            with self._provider.store.mutation():
                pass  # drops registrations never used in time, so they stop counting
            if len(self._provider.store.clients) >= MAX_CLIENTS:
                return 429, "too_many_clients"
        if path == "/authorize" and len(self._provider.pending) >= MAX_PENDING_LOGINS:
            return 429, "too_many_pending_logins"
        if not self._admit(path, _source(scope)):
            return 429, "rate_limited"
        return None

    def _admit(self, path: str, source: str) -> bool:
        """Count a call against its source's and the overall hourly limit, or refuse it.

        A source is tracked only while it has a call inside the window, so
        tracking never exceeds the sum of the overall limits.
        """
        now = time.monotonic()
        while self._sources and next(iter(self._sources.values()))[-1] < now - RATE_WINDOW:
            self._sources.popitem(last=False)
        per_source, overall = ENDPOINT_LIMITS[path]
        total = _within_window(self._totals.setdefault(path, deque()), now)
        key = (path, source)
        calls = _within_window(self._sources.get(key, deque()), now)
        if len(total) >= overall or len(calls) >= per_source:
            return False
        total.append(now)
        calls.append(now)
        self._sources[key] = calls
        self._sources.move_to_end(key)
        return True


class IssuerIdentification:
    """ASGI middleware for RFC 9207: advertise issuer identification, and add `iss` to `/authorize` error redirects.

    The SDK serves the authorization-server metadata and sends `/authorize`
    errors back to the client itself, without `iss`. This adds the metadata
    flag, and `iss` to every `/authorize` redirect that goes back to a client.
    The redirect to the host's own consent page is left alone; the consent
    page adds `iss` to its success redirect itself (`complete_login`).
    """

    METADATA_PATH = "/.well-known/oauth-authorization-server"

    def __init__(self, app: ASGIApp, provider: PassphraseProvider) -> None:
        self._app = app
        self._issuer = provider.issuer
        self._consent_page = provider.config.public_url + "/login?"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path not in (self.METADATA_PATH, "/authorize"):
            return await self._app(scope, receive, send)
        if path == "/authorize":
            return await self._app(scope, receive, self._with_iss(send))
        await self._app(scope, receive, self._with_flag(send))

    def _with_iss(self, send: Send) -> Send:
        async def wrapped(message: dict) -> None:
            if message["type"] == "http.response.start" and message["status"] in (302, 303, 307):
                headers = []
                for name, value in message.get("headers", []):
                    location = value.decode("latin-1")
                    if name.lower() == b"location" and not location.startswith(self._consent_page):
                        value = construct_redirect_uri(location, iss=self._issuer).encode("latin-1")
                    headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        return wrapped

    def _with_flag(self, send: Send) -> Send:
        start: dict | None = None
        body = b""

        async def wrapped(message: dict) -> None:
            nonlocal start, body
            if message["type"] == "http.response.start":
                start = message
                return
            body += message.get("body", b"")
            if message.get("more_body"):
                return
            assert start is not None
            if start["status"] == 200:
                metadata = json.loads(body)
                metadata["authorization_response_iss_parameter_supported"] = True
                body = json.dumps(metadata).encode()
            headers = [(n, str(len(body)).encode() if n.lower() == b"content-length" else v)
                       for n, v in start.get("headers", [])]
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        return wrapped


MAX_LOGGED_URI = 200


def loggable_uri(uri: str) -> str:
    """A refused redirect URI as it may be logged: scheme, host and path only, printable, capped.

    Userinfo, the port, the query and the fragment are dropped, since they may
    carry credentials, codes or tokens; control characters are removed, so a
    hostile URI can't forge log lines.
    """
    try:
        parsed = urlparse(uri)
        shown = f"{parsed.scheme}://{parsed.hostname or ''}{parsed.path}"
    except ValueError:
        shown = "<unparseable>"
    printable = "".join(ch for ch in shown if ch.isprintable())
    return printable if len(printable) <= MAX_LOGGED_URI else printable[:MAX_LOGGED_URI] + "...(truncated)"


def _within_window(calls: deque[float], now: float) -> deque[float]:
    while calls and calls[0] < now - RATE_WINDOW:
        calls.popleft()
    return calls


def _source(scope: Scope) -> str:
    """The caller's address. Behind a local reverse proxy or tunnel, the peer is loopback: use X-Forwarded-For."""
    peer = (scope.get("client") or ("unknown", 0))[0]
    if peer in ("127.0.0.1", "::1"):
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                return value.decode(errors="replace").split(",")[0].strip()
    return peer


REGISTRATION = ClientRegistrationOptions(enabled=True, valid_scopes=ALL_SCOPES, default_scopes=ALL_SCOPES)
REVOCATION = RevocationOptions(enabled=True)


def auth_settings(config: Config) -> AuthSettings:
    oauth = config.oauth.enabled
    return AuthSettings(issuer_url=config.public_url, resource_server_url=config.resource_url,
                        validate_token_resource=True, client_registration_options=REGISTRATION if oauth else None,
                        revocation_options=REVOCATION if oauth else None)


LOGIN_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Approve baton access</title>
<body style="font:16px system-ui;max-width:28rem;margin:3rem auto;padding:0 1rem">
<h1 style="font-size:1.3rem">Approve baton access</h1>
<p><b>{client}</b> asks to use this baton host (<code>{scopes}</code>).<br>
It will return to <code>{host}</code>.</p>
<p style="color:#b00">{error}</p>
<form method=post><input type=hidden name=id value="{login_id}">
<label>Act as role <select name=role>{roles}</select></label><br><br>
<input type=password name=passphrase autocomplete=current-password autofocus required
 style="width:100%;font-size:1rem;padding:.5rem" placeholder="Host passphrase">
<button style="margin-top:1rem;font-size:1rem;padding:.5rem 1rem">Approve</button></form>"""

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https: http:",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def register_login_routes(server: MCPServer, provider: PassphraseProvider) -> None:
    async def login_form(request: Request) -> Response:
        return _login_page(provider, request.query_params.get("id", ""))

    async def login_submit(request: Request) -> Response:
        form = await request.form()
        login_id = str(form.get("id", ""))
        redirect = provider.complete_login(login_id, str(form.get("passphrase", "")), str(form.get("role", "")))
        if redirect is None:
            return _login_page(provider, login_id, "Not approved. Check the passphrase, or wait after several tries.")
        return RedirectResponse(redirect, status_code=302, headers=SECURITY_HEADERS)

    server.custom_route("/login", methods=["GET"])(login_form)
    server.custom_route("/login", methods=["POST"])(login_submit)


def _login_page(provider: PassphraseProvider, login_id: str, error: str = "") -> Response:
    login = provider.pending.get(login_id)
    if login is None or login.expires_at < time.time():
        return HTMLResponse("This approval link has expired. Start the connection again.", 400, SECURITY_HEADERS)
    client = provider.store.clients.get(login.client_id, {})
    roles = "".join(f"<option>{html.escape(role)}</option>" for role in provider.config.oauth.roles)
    return HTMLResponse(LOGIN_PAGE.format(
        client=html.escape(client.get("client_name") or login.client_id),
        scopes=html.escape(" ".join(login.scopes)),
        host=html.escape(urlparse(str(login.params.redirect_uri)).netloc),
        error=html.escape(error), login_id=html.escape(login_id), roles=roles,
    ), headers=SECURITY_HEADERS)
