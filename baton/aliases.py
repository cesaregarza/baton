"""Permanent alias ownership; short-lived proofs carry continuity, and an enrolled alias's proof carries its role.

Callers supply the authenticated AccessToken from MCP middleware, never a typed role or session alias.
Claim admission commits separately so a refused passphrase still consumes its quota.

A role enrols once, on an owner's arm, as an alias of its own name (`arm_enrol`, then that role's own
credential claims it). The alias is then mapped to the role: a proof for it, on any guest credential, makes a
call run as the role (`resolve_binding`'s `effective_role`). An owner's rekey arm (`arm_rekey`) lets the next
claim with a new passphrase replace the alias's verifier and end its proofs.
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
ARM_SECONDS = 900


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
    if principal is None or auth.role_of(principal) is None:
        raise Forbidden("alias continuity requires an authenticated credential")
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


def _identity(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    code = hashlib.sha256(b"baton-alias-code-v1\x00" + row["id"].encode()).hexdigest()[:4]
    mapped = conn.execute("SELECT role FROM alias_roles WHERE alias_id = ?", (row["id"],)).fetchone()
    return {"alias_id": row["id"], "alias": row["name"], "badge": "guest", "code": code,
            "effective_role": mapped["role"] if mapped else "guest"}


def claim(conn: sqlite3.Connection, config: Config, principal: AccessToken | None, name: str,
          passphrase: str) -> dict:
    """Validate, then renew on the current passphrase, else reset on a live rekey arm, else refuse.

    A name nobody holds is created: by a guest unless it is reserved, or by a fixed role enrolling itself. Only a
    successful armed reset skips the hourly claim caps and the live-binding cap, so recovery works at the caps.
    """
    credential, role = _credential(principal), auth.role_of(principal)
    _check_lifetimes(config.aliases)
    name = normalize(name)
    validate_passphrase(passphrase)
    resetting = _reset_requested(conn, name, passphrase)
    if not resetting:
        _admit(conn, config.aliases, credential, name, time.time())
    with transaction(conn):
        now = time.time()  # read again: an arm that expired since the early check is not spent
        row = conn.execute("SELECT * FROM aliases WHERE name = ?", (name,)).fetchone()
        if resetting:
            if not _take_arm(conn, "rekey", name, now):
                raise Forbidden("alias is taken; passphrase did not match")
            conn.execute("UPDATE aliases SET verifier = ? WHERE id = ?", (auth.hash_passphrase(passphrase), row["id"]))
            conn.execute("DELETE FROM alias_bindings WHERE alias_id = ?", (row["id"],))
        elif row is None:
            row = _create(conn, config, role, name, passphrase, now)
        elif conn.execute("SELECT COUNT(*) FROM alias_bindings WHERE alias_id = ? AND expires_at > ?",
                          (row["id"], now)).fetchone()[0] >= config.aliases.max_bindings_per_alias:
            raise LimitExceeded("alias has reached its live binding capacity", "bindings_per_alias")
        elif not auth.check_passphrase(passphrase, row["verifier"]):
            raise Forbidden("alias is taken; passphrase did not match")
        elif role != "guest" and not conn.execute("SELECT 1 FROM core_enrolment WHERE role = ? AND alias_id = ?",
                                                  (role, row["id"])).fetchone():
            raise Forbidden("a fixed-role credential renews only its own enrolled alias")
        return _bind(conn, config, row, now)


def _check_lifetimes(limits: Aliases) -> None:
    for label, value, high in (("binding lifetime", limits.binding_ttl_seconds, 86400),
                               ("mapped binding lifetime", limits.mapped_binding_ttl_seconds, 604800)):
        if type(value) is not int or not 1 <= value <= high:
            raise InvalidRequest(f"{label} must be a whole number from 1 to {high} seconds")


def _reset_requested(conn: sqlite3.Connection, name: str, passphrase: str) -> bool:
    """Whether this claim asks for an armed reset: a live rekey arm, and a passphrase that isn't the current one.

    The scrypt check runs before admission only while an owner's arm is live; the reset itself rechecks and spends
    the same arm inside its own transaction.
    """
    row = conn.execute("SELECT a.verifier FROM aliases a JOIN alias_arms r ON r.kind = 'rekey' AND r.target = a.name"
                       " WHERE a.name = ? AND r.expires_at > ?", (name, time.time())).fetchone()
    return row is not None and not auth.check_passphrase(passphrase, row["verifier"])


def _bind(conn: sqlite3.Connection, config: Config, row: sqlite3.Row, now: float) -> dict:
    """Issue a fresh proof; a mapped alias's lasts the mapped lifetime, a guest's the guest one."""
    identity = _identity(conn, row)
    lifetime = (config.aliases.binding_ttl_seconds if identity["effective_role"] == "guest"
                else config.aliases.mapped_binding_ttl_seconds)
    conn.execute("DELETE FROM alias_bindings WHERE expires_at <= ?", (now,))
    proof = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO alias_bindings VALUES (?, ?, ?)", (auth.token_digest(proof), row["id"], now + lifetime))
    return {**identity, "binding_token": proof, "expires_at": now + lifetime}


def _create(conn: sqlite3.Connection, config: Config, role: str, name: str, passphrase: str,
            now: float) -> sqlite3.Row:
    """A new guest alias, or a fixed role's one enrolment, which also maps the alias to the role."""
    if role == "guest":
        if _reserved(config, name):
            raise Forbidden("alias name is reserved")
    elif name != role or not _take_arm(conn, "enrol", role, now):
        raise Forbidden("a fixed-role credential only enrols its own role, once, on an owner's arm")
    if conn.execute("SELECT 1 FROM aliases WHERE skeleton = ?", (skeleton(name),)).fetchone():
        raise Forbidden("alias name conflicts with an existing alias")
    alias_id = secrets.token_urlsafe(24)
    conn.execute("INSERT INTO aliases VALUES (?, ?, ?, ?, ?)",
                 (alias_id, name, skeleton(name), auth.hash_passphrase(passphrase), now))
    if role != "guest":
        conn.execute("INSERT INTO core_enrolment VALUES (?, ?, ?, NULL)", (role, alias_id, now))
        conn.execute("INSERT INTO alias_roles VALUES (?, ?)", (role, alias_id))
    return conn.execute("SELECT * FROM aliases WHERE id = ?", (alias_id,)).fetchone()


def _take_arm(conn: sqlite3.Connection, kind: str, target: str, now: float) -> bool:
    """Spend a live arm. The caller's transaction rolls this back if the claim fails later."""
    conn.execute("DELETE FROM alias_arms WHERE expires_at <= ?", (now,))
    return conn.execute("DELETE FROM alias_arms WHERE kind = ? AND target = ?", (kind, target)).rowcount == 1


def arm_enrol(conn: sqlite3.Connection, config: Config, role: str) -> dict:
    """Owner: let `role`'s own credential enrol it, once, within ARM_SECONDS."""
    entry = config.roles.get(role)
    if entry is None or role == "guest" or entry.human or entry.display_only or role != normalize(role):
        raise Forbidden(f"{role!r} is not an agent role that can be mapped")
    with transaction(conn):
        if conn.execute("SELECT 1 FROM core_enrolment WHERE role = ?", (role,)).fetchone():
            raise Forbidden(f"{role} is already enrolled")
        return _arm(conn, "enrol", role)


def arm_rekey(conn: sqlite3.Connection, name: str) -> dict:
    """Owner: let the next claim of alias `name` with a new passphrase replace its verifier, within ARM_SECONDS."""
    name = normalize(name)
    with transaction(conn):
        if not conn.execute("SELECT 1 FROM aliases WHERE name = ?", (name,)).fetchone():
            raise Forbidden(f"no alias {name!r}")
        return _arm(conn, "rekey", name)


def map_alias(conn: sqlite3.Connection, name: str, role: str) -> dict:
    """Owner: make alias `name` act as enrolled `role`, replacing either side's earlier mapping."""
    name = normalize(name)
    with transaction(conn):
        row = conn.execute("SELECT id FROM aliases WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise Forbidden(f"no alias {name!r}")
        if not conn.execute("SELECT 1 FROM core_enrolment WHERE role = ?", (role,)).fetchone():
            raise Forbidden(f"{role!r} has not enrolled, so nothing can be mapped to it")
        affected = [row["id"], *(found["alias_id"] for found in conn.execute(
            "SELECT alias_id FROM alias_roles WHERE role = ?", (role,)))]
        _end_proofs(conn, affected)
        conn.execute("DELETE FROM alias_roles WHERE role = ? OR alias_id = ?", (role, row["id"]))
        conn.execute("INSERT INTO alias_roles VALUES (?, ?)", (role, row["id"]))
    return {"alias": name, "role": role, "mapped": True}


def unmap_alias(conn: sqlite3.Connection, name: str) -> dict:
    """Owner: end alias `name`'s mapping. It keeps renewing, as a guest alias."""
    name = normalize(name)
    with transaction(conn):
        row = conn.execute("SELECT r.role, r.alias_id FROM alias_roles r JOIN aliases a ON a.id = r.alias_id"
                           " WHERE a.name = ?", (name,)).fetchone()
        if row is None:
            raise Forbidden(f"alias {name!r} is not mapped")
        _end_proofs(conn, [row["alias_id"]])
        conn.execute("DELETE FROM alias_roles WHERE alias_id = ?", (row["alias_id"],))
        conn.execute("UPDATE core_enrolment SET unmapped_at = ? WHERE role = ?", (time.time(), row["role"]))
    return {"alias": name, "role": row["role"], "mapped": False}


def _end_proofs(conn: sqlite3.Connection, alias_ids: list[str]) -> None:
    """A mapping change ends the aliases' proofs and events targets, so nothing old is silently up- or downgraded."""
    for alias_id in alias_ids:
        conn.execute("DELETE FROM alias_bindings WHERE alias_id = ?", (alias_id,))
        conn.execute("DELETE FROM events_targets WHERE alias_id = ?", (alias_id,))


def _arm(conn: sqlite3.Connection, kind: str, target: str) -> dict:
    expires_at = time.time() + ARM_SECONDS
    conn.execute("INSERT OR REPLACE INTO alias_arms VALUES (?, ?, ?)", (kind, target, expires_at))
    return {"armed": kind, "target": target, "expires_at": expires_at}


def validate_proof(proof: object) -> None:
    if not isinstance(proof, str) or not proof or len(proof) > 256 or not proof.isascii():
        raise Forbidden("alias binding is missing, invalid or expired")


def resolve_binding(conn: sqlite3.Connection, proof: str | None) -> dict:
    """The alias a live proof binds, with the role its calls run as (`guest` when unmapped)."""
    validate_proof(proof)
    row = conn.execute("SELECT a.* FROM alias_bindings b JOIN aliases a ON a.id = b.alias_id"
                       " WHERE b.digest = ? AND b.expires_at > ?", (auth.token_digest(proof), time.time())).fetchone()
    if row is None:
        raise Forbidden("alias binding is missing, invalid or expired")
    return _identity(conn, row)


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
