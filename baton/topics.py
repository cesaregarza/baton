"""Topics: the containers threads are filed in, opened by a core role.

A topic is a record keyed by the same 1-2 segment path a conversation's `topic` has always held, so
nothing about filtering or reading changes; what changes is that the path now has to exist, and that
somebody with `can_create_topics` had to make it. A subtopic is one level under its parent and needs
the parent to be there first. Depth stays at two.

`members` is the list of roles a guest's thread in this topic is delivered to. It is read once, when
the thread is created, and copied into that thread's participants: editing a topic later never adds
or removes anybody from a thread that already exists, and never changes who may read one. A role
starting its own thread still names its participants itself, exactly as before.

Only the paths and the membership live here. Who may read a conversation, who may post to it and the
hourly post caps are all unchanged and still belong to `baton.conversations`.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from baton import board_read, events, messages
from baton.config import Config
from baton.errors import Forbidden, InvalidRequest, UnknownRole, UnknownTopic
from baton.store import transaction

MAX_TITLE = 100
MAX_DESCRIPTION = 1000
# Tells "the caller left members out" apart from "the caller asked for no members": on create the two
# mean the same empty list, but on update one keeps the current list and the other clears it.
UNSET: Any = object()


def _may_manage(config: Config, actor: str) -> None:
    if not config.roles[actor].can_create_topics:
        raise Forbidden(f"{actor} may not create or change topics (roles.{actor}.can_create_topics)")


def _check_path(path: Any, field: str = "path") -> str:
    from baton import conversations  # lazily: conversations imports this module

    checked = conversations.check_topic(path, field=field)
    if checked is None:
        raise InvalidRequest(f"{field} is required: a topic is 1-2 lowercase slug segments")
    return checked


def _check_text(value: Any, field: str, limit: int, default: str | None = None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str) or not 1 <= len(value) <= limit or value != value.strip():
        raise InvalidRequest(f"{field} is trimmed text of 1-{limit} characters, or left out")
    return value


def _check_members(config: Config, members: Any) -> list[str]:
    """The roles a guest thread here would be delivered to: known, non-guest, no duplicates.

    Zero members is allowed, and means the topic takes no guest thread until a core role sets some.
    The 2-and-up invariant is admission's, not the topic's, so a topic can be opened before its
    membership is settled.
    """
    if members is None or members is UNSET:
        return []
    if not isinstance(members, list) or not all(isinstance(role, str) for role in members):
        raise InvalidRequest("members is a list of role names")
    if len(members) != len(set(members)):
        raise InvalidRequest("members names a role twice")
    unknown = [role for role in members if role not in config.roles]
    if unknown:
        raise UnknownRole(f"no role {unknown[0]!r}; call whoami for the list")
    if "guest" in members:
        raise InvalidRequest("guest cannot be a topic member: a guest is never a conversation participant")
    if len(members) > config.conversations.max_participants:
        raise InvalidRequest(f"a topic has at most {config.conversations.max_participants} members "
                             "(conversations.max_participants)")
    return members


def _row(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM topics WHERE path = ?", (path,)).fetchone()


def _require_parent(conn: sqlite3.Connection, path: str) -> None:
    if "/" in path and _row(conn, path.split("/")[0]) is None:
        raise InvalidRequest(f"topic {path!r} needs its parent {path.split('/')[0]!r} to exist first")


def exists(conn: sqlite3.Connection, path: str) -> bool:
    return _row(conn, path) is not None


def require_exists(conn: sqlite3.Connection, path: str, field: str = "topic") -> sqlite3.Row:
    """The topic, or a refusal naming the path. Callers hold the write lock, so this cannot go stale."""
    row = _row(conn, path)
    if row is None:
        raise InvalidRequest(f"{field} {path!r} is not a topic; a core role opens one with create_topic")
    return row


def members_for_thread(conn: sqlite3.Connection, config: Config, path: str) -> list[str]:
    """The participants a guest's thread in `path` gets, or a refusal saying what is wrong with them.

    The config-dependent checks live here rather than in the schema upgrade, which sees no config: a
    membership can go stale when a role leaves the config, or when `max_participants` is lowered, and
    this is the moment it matters.
    """
    members = json.loads(require_exists(conn, path)["members"])
    missing = [role for role in members if role not in config.roles]
    if missing:
        raise InvalidRequest(f"topic {path!r} has members this host no longer configures ({missing[0]!r}): "
                             "a core role can set them with update_topic")
    from baton import conversations  # lazily: conversations imports this module

    if not conversations.MIN_PARTICIPANTS <= len(members) <= config.conversations.max_participants:
        raise InvalidRequest(f"topic {path!r} has {len(members)} members, and a thread here needs "
                             f"{conversations.MIN_PARTICIPANTS}-{config.conversations.max_participants}: "
                             "a core role can set them with update_topic")
    return members


def _summary(row: sqlite3.Row) -> dict[str, Any]:
    return {"path": row["path"], "title": row["title"], "description": row["description"],
            "members": json.loads(row["members"]), "created_by": row["created_by"],
            "created_at": row["created_at"]}


def create(conn: sqlite3.Connection, config: Config, *, actor: str, path: Any, title: Any = None,
           description: Any = None, members: Any = None, request_id: str | None = None) -> dict[str, Any]:
    """Open a topic. A replay rechecks authority and the parent, then returns the topic as it now stands."""
    _may_manage(config, actor)
    checked = _check_path(path)
    title = _check_text(title, "title", MAX_TITLE, default=checked.split("/")[-1])
    description = _check_text(description, "description", MAX_DESCRIPTION)
    chosen = _check_members(config, members)
    request_id = request_id or messages.new_request_id()
    if not 1 <= len(request_id) <= 200:
        raise InvalidRequest("request_id is 1-200 characters")
    digest = events.request_digest("create_topic", {"actor": actor, "path": checked, "title": title,
                                                    "description": description, "members": chosen})
    with transaction(conn):
        existing = events.replayed(conn, "topics", request_id, digest)
        if existing is not None:
            return {**_summary(existing), "request_id": request_id, "created": False}
        _require_parent(conn, checked)
        if _row(conn, checked) is not None:
            raise InvalidRequest(f"topic {checked!r} already exists")
        stamp = events.now()
        conn.execute("INSERT INTO topics (path, title, description, members, created_by, created_at,"
                     " request_id, request_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (checked, title, description, json.dumps(chosen), actor, stamp, request_id, digest))
        made = _summary(_row(conn, checked))
    return {**made, "request_id": request_id, "created": True}


def update(conn: sqlite3.Connection, config: Config, *, actor: str, path: Any, title: Any = None,
           description: Any = UNSET, members: Any = UNSET) -> dict[str, Any]:
    """Edit a topic's title, description or members. An omitted argument leaves that field as it is."""
    _may_manage(config, actor)
    checked = _check_path(path)
    new_title = _check_text(title, "title", MAX_TITLE)
    new_description = (UNSET if description is UNSET
                       else _check_text(description, "description", MAX_DESCRIPTION))
    new_members = UNSET if members is UNSET else _check_members(config, members)
    with transaction(conn):
        row = require_exists(conn, checked)
        conn.execute("UPDATE topics SET title = ?, description = ?, members = ? WHERE path = ?",
                     (new_title or row["title"],
                      row["description"] if new_description is UNSET else new_description,
                      row["members"] if new_members is UNSET else json.dumps(new_members),
                      checked))
        return _summary(_row(conn, checked))


def rename(conn: sqlite3.Connection, config: Config, *, actor: str, path: Any, new_path: Any) -> dict[str, Any]:
    """Move a topic, its subtopics and every thread filed under them, in one transaction.

    Conversation ids never change, and neither do any thread's participants: a rename is a change of
    label, so nobody gains or loses access to anything. Every refusal is decided before the first
    write, so a refused rename leaves the topic exactly as it was.
    """
    _may_manage(config, actor)
    checked = _check_path(path)
    target = _check_path(new_path, field="new_path")
    if target == checked or target.startswith(checked + "/"):
        raise InvalidRequest(f"new_path {target!r} is {path!r} itself or inside it")
    with transaction(conn):
        require_exists(conn, checked)
        _require_parent(conn, target)
        children = [row[0] for row in conn.execute(
            "SELECT path FROM topics WHERE path LIKE ? ORDER BY path", (f"{checked}/%",))]
        if children and "/" in target:
            raise InvalidRequest(f"{path!r} has subtopics, so moving it under {target.split('/')[0]!r} "
                                 "would make three levels")
        moving = {old: target + old[len(checked):] for old in [checked, *children]}
        taken = [new for new in moving.values() if _row(conn, new) is not None]
        if taken:
            raise InvalidRequest(f"new_path {taken[0]!r} is already a topic")
        # Deepest first, so a path never collides with one still to move.
        for old in sorted(moving, key=len, reverse=True):
            conn.execute("UPDATE topics SET path = ? WHERE path = ?", (moving[old], old))
            conn.execute("UPDATE conversations SET topic = ? WHERE topic = ?", (moving[old], old))
        return {**_summary(_row(conn, target)), "renamed_from": checked, "subtopics": len(children)}


def get(conn: sqlite3.Connection, config: Config, *, reader: str, path: Any) -> dict[str, Any]:
    """One topic, with the number of threads in it this reader may see."""
    checked = _check_path(path)
    row = _row(conn, checked)
    if row is None:
        raise UnknownTopic(f"no topic {checked!r}")
    threads, _ = _readable(conn, config, reader, checked)
    return {**_summary(row), "threads": threads,
            "subtopics": [entry[0] for entry in conn.execute(
                "SELECT path FROM topics WHERE path LIKE ? ORDER BY path", (f"{checked}/%",))]}


def listing(conn: sqlite3.Connection, config: Config, *, reader: str,
            parent: Any = None) -> list[dict[str, Any]]:
    """Every topic, or only those directly under `parent`, each with this reader's own thread count."""
    if parent is None:
        rows = conn.execute("SELECT * FROM topics ORDER BY path").fetchall()
    else:
        checked = _check_path(parent, field="parent")
        rows = conn.execute("SELECT * FROM topics WHERE path LIKE ? ORDER BY path",
                            (f"{checked}/%",)).fetchall()
    listed = []
    for row in rows:
        threads, last_activity_at = _readable(conn, config, reader, row["path"])
        listed.append({**_summary(row), "threads": threads, "last_activity_at": last_activity_at})
    return listed


def _readable(conn: sqlite3.Connection, config: Config, reader: str,
              path: str) -> tuple[int, str | None]:
    """How many threads filed exactly here the reader may see, and the newest activity among those same ones.

    Both come from one query on purpose. They were two, and only the count was filtered by reader, so a
    newer conversation the reader is not in lent the topic its timestamp -- which disclosed that
    something had happened there, and when. Asking once makes that divergence impossible rather than
    merely fixed. The board rule decides what counts as readable, not topic membership.
    """
    if board_read.sees_all(config, reader):
        row = conn.execute("SELECT COUNT(*), MAX(last_activity_at) FROM conversations WHERE topic = ?",
                           (path,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*), MAX(c.last_activity_at) FROM conversations c"
                           " WHERE c.topic = ? AND EXISTS (SELECT 1 FROM participants p"
                           " WHERE p.conversation_id = c.id AND p.role = ?)", (path, reader)).fetchone()
    return row[0], row[1]
