"""Topic grant records: an owner's permission for one alias to act on one topic, proposed, authorized and revoked.

The owner moves a grant through three steps. `propose` writes its terms and returns them, with a digest, for the
owner to read. `authorize` takes that digest back exactly and mints the key, which is returned once and never
stored: only its sha256 is kept. `revoke` ends a grant, and nothing is deleted. A grant's state is never stored
either. It is derived from the row and the clock on every read, so it cannot drift from the terms it describes.

No tool honours a grant yet. `covering_grant` answers the one question a later guard will ask. It is read-only and
runs on the caller's connection, so the guard can ask it inside its own write transaction.
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


def _event(conn: sqlite3.Connection, grant_id: str, event: str, now: datetime) -> None:
    """One line in the grant's log. `detail` stays NULL: an event never carries a value that could be a key."""
    conn.execute("INSERT INTO topic_grant_events (grant_id, event, actor, at) VALUES (?, ?, ?, ?)",
                 (grant_id, event, OWNER, now.isoformat()))


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
    rows = conn.execute("SELECT g.*, root.path AS root_path FROM topic_grants g"
                        " LEFT JOIN topics root ON root.id = g.topic_id WHERE g.alias_id = ?", (alias_id,)).fetchall()
    covering = [row for row in rows if _covers(row, topic["id"], topic_path, capability)
                and _state(row, now) == "active"]
    return min(covering, key=_proposal_order, default=None)


def _covers(row: sqlite3.Row, topic_id: str, topic_path: str, capability: str) -> bool:
    """Whether a grant names this capability and this topic: the topic itself, or, when it inherits, a direct child.

    A text prefix would let a grant on `job-hunt` reach `job-hunting`, so paths are compared segment by segment.
    """
    if capability not in json.loads(row["capabilities"]):
        return False
    if row["topic_id"] == topic_id:
        return True
    return bool(row["inherit"]) and row["root_path"] is not None \
        and topic_path.split("/")[:-1] == row["root_path"].split("/")
