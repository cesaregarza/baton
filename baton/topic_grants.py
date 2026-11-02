"""Topic grant records: an owner's permission for one alias to act on one topic, proposed, authorized and revoked.

The owner moves a grant through three steps. `propose` writes its terms and returns them, with a digest, for the
owner to read. `authorize` takes that digest back exactly and mints the key, which is returned once and never
stored: only its sha256 is kept. `revoke` ends a grant, and nothing is deleted. A grant's state is never stored
either. It is derived from the row and the clock on every read, so it cannot drift from the terms it describes.

A guest honours a grant by presenting its key. `grant_for_use` finds the one grant that key opens and refuses it
unless it is the presenting alias's own and active; `require_covers` and `require_names` then check what the call
touches. All three are read-only and run on the caller's connection, so a tool asks them inside its own write
transaction. `covering_grant` is the older question, which grant of an alias covers a topic, and does not look at
a key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from baton.aliases import alias_code
from baton.errors import Forbidden, IllegalTransition, InvalidRequest, UnknownTopic
from baton.store import transaction

CAPABILITIES = ("reference.edit", "thread.organise", "topic.create_sub")
MAX_DAYS = 30
# A proposal the owner does not authorize within a day lapses, so a reading of the terms from long ago cannot be
# authorized later by accident.
PROPOSAL_HOURS = 24
MAX_REASON = 500
OWNER = "owner"
NEEDS_BINDING = "a topic grant key works only with the alias's binding token"
UNKNOWN_KEY = "unknown topic grant key"
# A grant joined to the topic it is on, read by id: that topic's path is its current one.
WITH_ROOT = ("SELECT g.*, root.path AS root_path FROM topic_grants g"
             " LEFT JOIN topics root ON root.id = g.topic_id")


class GrantRefused(Forbidden):
    """A refusal that names a grant. The caller is told why, and `record_refused_use` tells the grant's log.

    The two are separate on purpose: the refusal rolls back the operation's transaction, and the log line must not.
    """

    def __init__(self, message: str, grant_id: str, reason: str) -> None:
        super().__init__(message)
        self.grant_id, self.reason = grant_id, reason


def _now() -> datetime:
    """The one clock this module reads. Tests replace it. Stored times are its isoformat()."""
    return datetime.now(UTC)


def _at(text: str) -> datetime:
    """A stored time, parsed back. Comparing the text would be right only by accident of format."""
    return datetime.fromisoformat(text)


def _same(given: str, expected: str) -> bool:
    """Equal in constant time, so a digest cannot be found by timing how much of it matched."""
    return hmac.compare_digest(given.encode(), expected.encode())


def _digest(row: Any) -> str:
    """The digest the owner reads and `authorize` takes back: sha256 over the terms and the proposal's nonce.

    Checks always recompute it from the row. A stored copy would only say what the row claimed when it was
    written, so an edit made since would go unnoticed.
    """
    covered = {"alias_id": row["alias_id"], "alias_name": row["alias_name_seen"],
               "alias_code": row["alias_code_seen"], "topic_id": row["topic_id"],
               "topic_path": row["topic_path_seen"], "capabilities": json.loads(row["capabilities"]),
               "inherit": row["inherit"], "expires_at": row["expires_at"], "proposed_by": row["proposed_by"],
               "proposed_at": row["proposed_at"], "nonce": row["nonce"]}
    text = json.dumps(covered, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def _state(row: Any, now: datetime) -> str:
    """A grant's state. The first condition that holds decides it, in this order.

    Strict boundaries: at the instant a proposal lapses or a grant expires, it is already over. An edited grant is
    `invalid` and never active, whatever else holds.
    """
    if row["revoked_at"] is not None:
        return "revoked"
    if row["authorized_digest"] is None:
        return "pending" if now < _at(row["proposal_expires_at"]) else "lapsed"
    if not _same(row["authorized_digest"], _digest(row)):
        return "invalid"
    return "active" if now < _at(row["expires_at"]) else "expired"


def _proposal_order(row: Any) -> tuple[datetime, str]:
    """The order grants are listed in, and the tie-break between covering grants: when each was proposed, then id."""
    return (_at(row["proposed_at"]), row["id"])


def _render(row: Any, now: datetime) -> dict[str, Any]:
    """A grant as the owner reads it, with its state. Never the proof digest or the nonce."""
    return {"id": row["id"], "alias_id": row["alias_id"], "alias_name": row["alias_name_seen"],
            "alias_code": row["alias_code_seen"], "topic_id": row["topic_id"], "topic_path": row["topic_path_seen"],
            "capabilities": json.loads(row["capabilities"]), "inherit": row["inherit"],
            "proposed_by": row["proposed_by"], "proposed_at": row["proposed_at"],
            "proposal_expires_at": row["proposal_expires_at"], "expires_at": row["expires_at"],
            "digest": row["digest"], "authorized_by": row["authorized_by"], "authorized_at": row["authorized_at"],
            "revoked_by": row["revoked_by"], "revoked_at": row["revoked_at"], "revoke_reason": row["revoke_reason"],
            "state": _state(row, now)}


def _row(conn: sqlite3.Connection, grant_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM topic_grants WHERE id = ?", (grant_id,)).fetchone()
    if row is None:
        raise InvalidRequest(f"no topic grant {grant_id!r}")
    return row


def _event(conn: sqlite3.Connection, grant_id: str, event: str, now: datetime, actor: str = OWNER,
           detail: dict[str, str] | None = None) -> None:
    """One line in the grant's log. `detail` holds only ids, states, tool names and checked topic paths, never
    free-form text, so it cannot carry a key ("tgk_" plus urlsafe base64 is never a lowercase slug)."""
    conn.execute("INSERT INTO topic_grant_events (grant_id, event, actor, detail, at) VALUES (?, ?, ?, ?, ?)",
                 (grant_id, event, actor, None if detail is None else json.dumps(detail), now.isoformat()))


def _checked_capabilities(capabilities: Any) -> list[str]:
    """One or more of the three names, sorted and without repeats. Any other name is refused, never dropped: a typo
    must not quietly grant less than the owner asked for."""
    if not isinstance(capabilities, (list, tuple)) or not capabilities \
            or not all(name in CAPABILITIES for name in capabilities):
        raise InvalidRequest(f"capabilities are one or more of {', '.join(CAPABILITIES)}")
    return sorted(set(capabilities))


def _checked_days(days: Any) -> None:
    # A bool is an int in Python, and `--days true` must not pass as one day.
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
        raise InvalidRequest(f"days must be a whole number from 1 to {MAX_DAYS}")


def _checked_reason(reason: Any) -> str:
    """A revocation's reason, stripped. It is what the owner reads later, so blank is no reason at all."""
    text = reason.strip() if isinstance(reason, str) else ""
    if not 1 <= len(text) <= MAX_REASON:
        raise InvalidRequest(f"a revoke needs a reason of 1-{MAX_REASON} characters")
    return text


def _alias_named(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT id, name FROM aliases WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise Forbidden(f"no alias {name!r}")
    return row


def _topic_at(conn: sqlite3.Connection, path: str) -> sqlite3.Row:
    row = conn.execute("SELECT id, path FROM topics WHERE path = ?", (path,)).fetchone()
    if row is None:
        raise UnknownTopic(f"no topic {path!r}")
    return row


def propose(conn: sqlite3.Connection, alias_name: Any, topic_path: Any, capabilities: Any, inherit: Any,
            days: Any) -> dict[str, Any]:
    """Write a proposal and return it for the owner to read. It grants nothing until it is authorized.

    Every refusal comes before the first write, and the whole operation is one transaction.
    """
    names = _checked_capabilities(capabilities)
    _checked_days(days)
    with transaction(conn):
        alias = _alias_named(conn, alias_name)
        topic = _topic_at(conn, topic_path)
        now = _now()
        fields = {
            "id": "tgr_" + secrets.token_hex(8),
            "alias_id": alias["id"], "topic_id": topic["id"], "capabilities": json.dumps(names),
            "inherit": 1 if inherit else 0, "expires_at": (now + timedelta(days=days)).isoformat(),
            "proposed_by": OWNER, "proposed_at": now.isoformat(),
            "proposal_expires_at": (now + timedelta(hours=PROPOSAL_HOURS)).isoformat(),
            # A fresh nonce, so two proposals for the same pair never share a digest.
            "nonce": secrets.token_hex(16),
            "alias_name_seen": alias["name"], "alias_code_seen": alias_code(alias["id"]),
            "topic_path_seen": topic["path"],
        }
        fields["digest"] = _digest(fields)
        conn.execute(f"INSERT INTO topic_grants ({', '.join(fields)}) VALUES ({','.join('?' * len(fields))})",
                     tuple(fields.values()))
        _event(conn, fields["id"], "propose", now)
        return _render(_row(conn, fields["id"]), now)


def authorize(conn: sqlite3.Connection, grant_id: str, digest: Any) -> dict[str, Any]:
    """Authorize a pending grant and mint its key: the only time the key exists in full.

    The digest must be the one the owner read, and the alias and topic must still be the ones it names. The key is
    returned here and nowhere else.
    """
    with transaction(conn):
        row = _row(conn, grant_id)
        now = _now()
        state = _state(row, now)
        if state != "pending":
            raise IllegalTransition(_not_pending(grant_id, state))
        current = _digest(row)
        if not (isinstance(digest, str) and _same(digest, current)):
            raise InvalidRequest("the digest does not match this proposal: read the proposal again, or propose again")
        _require_names_unchanged(conn, row)
        key = "tgk_" + secrets.token_urlsafe(32)
        conn.execute("UPDATE topic_grants SET authorized_by = ?, authorized_at = ?, authorized_digest = ?,"
                     " proof_digest = ? WHERE id = ?",
                     (OWNER, now.isoformat(), current, hashlib.sha256(key.encode()).hexdigest(), grant_id))
        _event(conn, grant_id, "authorize", now)
        return {**_render(_row(conn, grant_id), now), "grant_key": key}


def _not_pending(grant_id: str, state: str) -> str:
    if state in ("lapsed", "revoked"):
        return f"topic grant {grant_id} is {state}: propose again"
    return f"topic grant {grant_id} is already authorized, so no key is minted again"


def _require_names_unchanged(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """The owner read the alias and topic by name. Matching is by id, so a moved name means a different proposal."""
    alias = conn.execute("SELECT name FROM aliases WHERE id = ?", (row["alias_id"],)).fetchone()
    topic = conn.execute("SELECT path FROM topics WHERE id = ?", (row["topic_id"],)).fetchone()
    if alias is None or alias["name"] != row["alias_name_seen"] or topic is None \
            or topic["path"] != row["topic_path_seen"]:
        raise InvalidRequest("the alias or topic changed since this was proposed: propose again")


def revoke(conn: sqlite3.Connection, grant_id: str, reason: Any) -> dict[str, Any]:
    """End a grant in any state, with a reason. The row stays, and only the revocation fields are filled."""
    text = _checked_reason(reason)
    with transaction(conn):
        row = _row(conn, grant_id)
        if row["revoked_at"] is not None:
            raise IllegalTransition(f"topic grant {grant_id} is already revoked; its first reason stands")
        now = _now()
        conn.execute("UPDATE topic_grants SET revoked_at = ?, revoked_by = ?, revoke_reason = ? WHERE id = ?",
                     (now.isoformat(), OWNER, text, grant_id))
        _event(conn, grant_id, "revoke", now)
        return _render(_row(conn, grant_id), now)


def list_grants(conn: sqlite3.Connection, include_all: bool = False) -> list[dict[str, Any]]:
    """The grants, oldest proposal first. By default only those still pending or active, which an owner may act on."""
    now = _now()
    rows = sorted(conn.execute("SELECT * FROM topic_grants").fetchall(), key=_proposal_order)
    listed = [_render(row, now) for row in rows]
    return listed if include_all else [one for one in listed if one["state"] in ("pending", "active")]


def active_grant_ids(conn: sqlite3.Connection, alias_name: str) -> list[str]:
    """The ids of an alias's active grants, oldest first, for the owner's arm-rekey output."""
    alias = conn.execute("SELECT id FROM aliases WHERE name = ?", (alias_name,)).fetchone()
    if alias is None:
        return []
    now = _now()
    rows = conn.execute("SELECT * FROM topic_grants WHERE alias_id = ?", (alias["id"],)).fetchall()
    return [row["id"] for row in sorted(rows, key=_proposal_order) if _state(row, now) == "active"]


def covering_grant(conn: sqlite3.Connection, alias_id: str, topic_path: str, capability: str) -> sqlite3.Row | None:
    """The earliest active grant that lets `alias_id` use `capability` on the topic at `topic_path`, or None.

    Read-only, on the caller's connection: the guard that calls this holds the write lock already, so it must neither
    begin nor end a transaction. Everything matches by id. The paths are compared only to place an inheriting
    grant's root, which is read by id, so a rename moves the grant's subtopics with it.
    """
    topic = conn.execute("SELECT id FROM topics WHERE path = ?", (topic_path,)).fetchone()
    if topic is None:
        return None
    now = _now()
    rows = conn.execute(f"{WITH_ROOT} WHERE g.alias_id = ?", (alias_id,)).fetchall()
    covering = [row for row in rows if _covers(row, topic["id"], topic_path, capability)
                and _state(row, now) == "active"]
    return min(covering, key=_proposal_order, default=None)


def _lists(row: sqlite3.Row, capability: str) -> bool:
    return capability in json.loads(row["capabilities"])


def _covers(row: sqlite3.Row, topic_id: str, topic_path: str, capability: str) -> bool:
    """Whether a grant names this capability and this topic: the topic itself, or, when it inherits, a direct child.

    A text prefix would let a grant on `job-hunt` reach `job-hunting`, so paths are compared segment by segment.
    """
    if not _lists(row, capability):
        return False
    if row["topic_id"] == topic_id:
        return True
    return bool(row["inherit"]) and row["root_path"] is not None \
        and topic_path.split("/")[:-1] == row["root_path"].split("/")


def grant_for_use(conn: sqlite3.Connection, identity: dict[str, str] | None, key: str) -> sqlite3.Row:
    """The grant `key` opens for the alias behind `identity`, or the refusal that says why it does not.

    Only the presented key's own grant counts: another grant the alias holds is never tried in its place, so a
    narrow key cannot borrow a wide one. The key is looked up by its digest, as it is stored, and appears in no text.
    """
    if identity is None:
        raise Forbidden(NEEDS_BINDING)
    # Every key is minted ASCII, so a key that is not opens nothing. It is refused as any unknown key is, before it
    # is hashed: a lone surrogate from JSON has no UTF-8 form to hash.
    if not key.isascii():
        raise Forbidden(UNKNOWN_KEY)
    digest = hashlib.sha256(key.encode()).hexdigest()
    row = conn.execute(f"{WITH_ROOT} WHERE g.proof_digest = ?", (digest,)).fetchone()
    if row is None:
        raise Forbidden(UNKNOWN_KEY)
    if row["alias_id"] != identity["alias_id"]:
        raise GrantRefused("this topic grant key is not for this alias", row["id"], "other_alias")
    state = _state(row, _now())
    if state != "active":
        raise GrantRefused(f"topic grant {row['id']} is {state}", row["id"], state)
    return row


def _not_allowed(grant: sqlite3.Row, capability: str, shown: str) -> GrantRefused:
    return GrantRefused(f"topic grant {grant['id']} does not allow {capability} on {shown}",
                        grant["id"], "not_covered")


def require_covers(conn: sqlite3.Connection, grant: sqlite3.Row, capability: str, path: str | None,
                   shown: str | None = None) -> sqlite3.Row:
    """The topic at `path`, unless the grant does not cover it for `capability`, which is refused.

    No topic at that path (none given, or a stale label with no row) is not covered either. A refusal names the
    path unless `shown` says what to call it instead: a path the caller did not supply, such as the topic a thread
    is filed in, is not the caller's to learn from a refusal.
    """
    topic = None if path is None else conn.execute("SELECT * FROM topics WHERE path = ?", (path,)).fetchone()
    if topic is None or not _covers(grant, topic["id"], topic["path"], capability):
        raise _not_allowed(grant, capability, shown or path or "(no topic)")
    return topic


def require_names(conn: sqlite3.Connection, grant: sqlite3.Row, capability: str, new_path: str) -> sqlite3.Row:
    """The parent of the topic to be made at `new_path`, unless it is not the grant's own topic, which is refused.

    For making a subtopic, which is the named topic's business alone: `inherit` extends the reach of moving threads,
    not of creating, so a grant that merely covers the parent does not qualify. A root has no parent to name.
    """
    parent_path = _parent_path(new_path)
    parent = None if parent_path is None else conn.execute(
        "SELECT * FROM topics WHERE path = ?", (parent_path,)).fetchone()
    if parent is None or parent["id"] != grant["topic_id"] or not _lists(grant, capability):
        raise _not_allowed(grant, capability, new_path)
    return parent


def record_refused_use(conn: sqlite3.Connection, refusal: GrantRefused, actor: str, tool: str) -> None:
    """Log a refusal that named a grant, in a transaction of its own.

    Called after the operation's transaction has rolled back, so the operation itself has written nothing and this
    line is all a refused call leaves. Refusals that name no grant leave none: there is no log to put them in.
    """
    with transaction(conn):
        _event(conn, refusal.grant_id, "refused_use", _now(), actor, {"tool": tool, "reason": refusal.reason})


def _parent_path(path: str) -> str | None:
    return path.rsplit("/", 1)[0] if "/" in path else None


def affected_by_rename(conn: sqlite3.Connection, renamed: str, target: str, moving: dict[str, str]) -> list[str]:
    """The ids of the active grants whose reach a rename changes, oldest first.

    `moving` maps every topic the rename moves, the renamed one and its subtopics, to its new path. A grant is
    affected when its own topic moves, or when it inherits and the renamed topic's parent path, before or after,
    equals the path of the grant's topic, before or after: the renamed topic then enters or leaves its reach. The
    rule is literal, so a rename between siblings under an inheriting root is affected too.
    """
    parents = {parent for parent in (_parent_path(renamed), _parent_path(target)) if parent is not None}
    now = _now()
    affected = []
    for row in sorted(conn.execute(WITH_ROOT).fetchall(), key=_proposal_order):
        if _state(row, now) != "active":
            continue
        before = row["root_path"]
        if before in moving or (row["inherit"] and parents & {before, moving.get(before, before)}):
            affected.append(row["id"])
    return affected


def record_rename(conn: sqlite3.Connection, grant_ids: list[str], actor: str, topic_id: str, old: str,
                  new: str) -> None:
    """One `rename` event on each grant a rename affected, describing the rename: the topic named, and its two paths.

    The caller holds the write transaction the rename runs in, so the events exist only if the rename does.
    """
    now = _now()
    for grant_id in grant_ids:
        _event(conn, grant_id, "rename", now, actor, {"topic_id": topic_id, "from": old, "to": new})
