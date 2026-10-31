"""Permanent guest alias ownership; short-lived proofs carry continuity, never authority.

Callers supply the authenticated AccessToken from MCP middleware, never a typed role or session alias.
Claim admission commits separately so a refused passphrase still consumes its quota.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import unicodedata

from mcp.server.auth.provider import AccessToken

from baton import auth
from baton.config import Aliases, Config
from baton.errors import Forbidden, InvalidRequest, LimitExceeded
from baton.store import transaction

MAX_NAME = 64
MAX_PASSPHRASE = 1024
CLAIM_WINDOW = 3600


def normalize(name: str) -> str:
    if not isinstance(name, str) or len(name) > MAX_NAME:
        raise InvalidRequest("alias name must be 1-64 lowercase ASCII letters, digits or hyphens")
    name = unicodedata.normalize("NFKC", name).casefold()
    if not re.fullmatch(r"[a-z0-9-]{1,64}", name):
        raise InvalidRequest("alias name must be 1-64 lowercase ASCII letters, digits or hyphens")
    return name


def validate_passphrase(passphrase: object) -> None:
    """Reject malformed text before admission/scrypt, without exposing any part of the secret."""
    if not isinstance(passphrase, str) or not 1 <= len(passphrase) <= MAX_PASSPHRASE:
        raise InvalidRequest("passphrase must contain 1-1024 characters")
    try:
        passphrase.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidRequest("passphrase must be valid UTF-8 text") from None


def skeleton(name: str) -> str:
    return name.replace("1", "l").replace("0", "o").replace("rn", "m").replace("vv", "w")


def _reserved(config: Config, name: str) -> bool:
    names = {"hilary", "system", *config.roles}
    names.update(role.display_name for role in config.roles.values() if role.human and role.display_name)
    return skeleton(name) in {skeleton(unicodedata.normalize("NFKC", value).casefold()) for value in names}


def _credential(principal: AccessToken | None) -> str:
    if principal is None or auth.role_of(principal) != "guest":
        raise Forbidden("alias continuity requires an authenticated guest credential")
    # Stable across OAuth access-token refresh; no raw bearer value is stored.
    authority = auth.credential_authority(principal)
    return auth.token_digest(json.dumps(authority, sort_keys=True, separators=(",", ":")))


def exists(conn: sqlite3.Connection, name: str) -> bool:
    """Read-only exact normalized-name occupancy, without ownership or code disclosure."""
    return conn.execute("SELECT 1 FROM aliases WHERE name = ?", (normalize(name),)).fetchone() is not None


def _admit(conn: sqlite3.Connection, limits: Aliases, credential: str, name: str, now: float) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM alias_claims WHERE at <= ?", (now - CLAIM_WINDOW,))
        counts = conn.execute("SELECT COUNT(*), COALESCE(SUM(credential = ?), 0), COALESCE(SUM(name = ?), 0)"
                              " FROM alias_claims", (credential, skeleton(name))).fetchone()
        caps = (limits.max_claims_per_hour, limits.max_claims_per_credential_per_hour,
                limits.max_claims_per_alias_per_hour)
        if any(count >= cap for count, cap in zip(counts, caps, strict=True)):
            raise LimitExceeded("alias claims are over their hourly limit", "alias_claims")
        conn.execute("INSERT INTO alias_claims VALUES (?, ?, ?)", (credential, skeleton(name), now))


def _identity(row: sqlite3.Row) -> dict:
    code = hashlib.sha256(b"baton-alias-code-v1\x00" + row["id"].encode()).hexdigest()[:4]
    return {"alias_id": row["id"], "alias": row["name"], "badge": "guest", "code": code}


def claim(conn: sqlite3.Connection, config: Config, principal: AccessToken | None, name: str,
          passphrase: str) -> dict:
    credential = _credential(principal)
    binding_ttl = config.aliases.binding_ttl_seconds
    if type(binding_ttl) is not int or not 1 <= binding_ttl <= 86400:
        raise InvalidRequest("binding lifetime must be a whole number from 1 to 86400 seconds")
    name = normalize(name)
    validate_passphrase(passphrase)
    now = time.time()
    _admit(conn, config.aliases, credential, name, now)
    if _reserved(config, name):
        raise Forbidden("alias name is reserved")
    with transaction(conn):
        row = conn.execute("SELECT * FROM aliases WHERE name = ?", (name,)).fetchone()
        if row is None and conn.execute("SELECT 1 FROM aliases WHERE skeleton = ?", (skeleton(name),)).fetchone():
            raise Forbidden("alias name conflicts with an existing alias")
        if row is not None and conn.execute("SELECT COUNT(*) FROM alias_bindings WHERE alias_id = ? AND expires_at > ?",
                                            (row["id"], now)).fetchone()[0] >= config.aliases.max_bindings_per_alias:
            raise LimitExceeded("alias has reached its live binding capacity", "bindings_per_alias")
        if row is None:
            alias_id = secrets.token_urlsafe(24)
            verifier = auth.hash_passphrase(passphrase)
            conn.execute("INSERT INTO aliases VALUES (?, ?, ?, ?, ?)",
                         (alias_id, name, skeleton(name), verifier, now))
            row = conn.execute("SELECT * FROM aliases WHERE id = ?", (alias_id,)).fetchone()
        elif not auth.check_passphrase(passphrase, row["verifier"]):
            raise Forbidden("alias is taken; passphrase did not match")
        conn.execute("DELETE FROM alias_bindings WHERE expires_at <= ?", (now,))
        proof = secrets.token_urlsafe(32)
        expires_at = now + binding_ttl
        conn.execute("INSERT INTO alias_bindings VALUES (?, ?, ?)", (auth.token_digest(proof), row["id"], expires_at))
        return {**_identity(row), "binding_token": proof, "expires_at": expires_at}


def validate_proof(proof: object) -> None:
    if not isinstance(proof, str) or not proof or len(proof) > 256 or not proof.isascii():
        raise Forbidden("alias binding is missing, invalid or expired")


def resolve_binding(conn: sqlite3.Connection, principal: AccessToken | None, proof: str | None) -> dict:
    """Resolve a portable proof on any authenticated guest credential; PR4 owns posting integration."""
    _credential(principal)
    validate_proof(proof)
    row = conn.execute("SELECT a.* FROM alias_bindings b JOIN aliases a ON a.id = b.alias_id"
                       " WHERE b.digest = ? AND b.expires_at > ?", (auth.token_digest(proof), time.time())).fetchone()
    if row is None:
        raise Forbidden("alias binding is missing, invalid or expired")
    return _identity(row)


def admit_post(conn: sqlite3.Connection, config: Config, principal: AccessToken, identity: dict) -> None:
    """Count admitted calls, including retries/refusals, like Guard's existing tool quotas; prune old attempts."""
    credential = _credential(principal)
    now = time.time()
    with transaction(conn):
        conn.execute("DELETE FROM alias_posts WHERE at <= ?", (now - CLAIM_WINDOW,))
        counts = conn.execute("SELECT COALESCE(SUM(alias_id = ?), 0), COALESCE(SUM(credential = ?), 0)"
                              " FROM alias_posts", (identity["alias_id"], credential)).fetchone()
        caps = (config.aliases.max_posts_per_alias_per_hour, config.aliases.max_posts_per_credential_per_hour)
        for count, cap, limit in zip(counts, caps, ("posts_per_alias", "posts_per_credential"), strict=True):
            if count >= cap:
                raise LimitExceeded("guest posting is over its hourly limit", limit)
        conn.execute("INSERT INTO alias_posts VALUES (?, ?, ?)", (identity["alias_id"], credential, now))
