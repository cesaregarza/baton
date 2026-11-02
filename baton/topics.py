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
from baton.store import new_topic_id, transaction

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


def _state(row: sqlite3.Row) -> dict[str, Any]:
    """What a topic change's history row records of a topic: its editable fields, never its id or path."""
    return {"title": row["title"], "description": row["description"], "members": json.loads(row["members"])}


def history_actor(actor: str, identity: dict[str, str] | None) -> str:
    """The name a history row gives the caller: its role, and for an alias-bound call the alias's immutable id.

    The id, not the display name, because a name can be claimed again and a reader must be able to tell
    the alias from a direct credential of the same role.
    """
    return actor if identity is None else f"{actor} via alias {identity['alias_id']}"


def record(conn: sqlite3.Connection, *, topic_id: str, operation: str, actor: str,
           before: dict[str, Any] | None, after: dict[str, Any] | None) -> None:
    """Append one change to the history. The caller holds the write lock and has made the change."""
    conn.execute("INSERT INTO topic_changes (topic_id, operation, actor, grant_id, before, after, at)"
                 " VALUES (?, ?, ?, NULL, ?, ?, ?)",
                 (topic_id, operation, actor, _encoded(before), _encoded(after), events.now()))


def _encoded(state: dict[str, Any] | None) -> str | None:
    return None if state is None else json.dumps(state)


def id_of(conn: sqlite3.Connection, path: str) -> str | None:
    """The id of the topic at `path`, or None when there is none."""
    row = _row(conn, path)
    return None if row is None else row["id"]


# The tools whose audit line names a topic: by `path` (the topic itself, or a rename's old one), or by `topic`
# (the topic a thread is filed in, or a listing filters by).
PATH_TOOLS = frozenset({"create_topic", "update_topic", "rename_topic", "get_topic"})
AUDIT_TOOLS = PATH_TOOLS | {"set_topic", "create_conversation", "list_conversations"}


def record_seen(seen: dict[str, Any] | None, path: str, row: sqlite3.Row | None = None, **more: str) -> None:
    """Note in `seen`, when the caller gave one, the topic this call resolved: its path, and its id if it has a row.

    The audit line is written after the call's transaction has committed, and by then another writer may have
    moved this topic and given its path to a different one, so a call records what it resolved while it held it.
    """
    if seen is not None:
        seen.update({"topic": path, **({} if row is None else {"topic_id": row["id"]}), **more})


def note_path(conn: sqlite3.Connection, seen: dict[str, Any] | None, path: str | None) -> None:
    """record_seen for the topic now at `path`, which may be none; a path with no topic records no id."""
    if path is not None:
        record_seen(seen, path, _row(conn, path))


def _is_path(value: Any) -> bool:
    """Whether `value` is a topic path as conversations.check_topic accepts it.

    No binding token passes: it is secrets.token_urlsafe(32), 43 characters with no slash, and a topic
    segment is at most MAX_TOPIC_SEGMENT (40) characters.
    """
    from baton import conversations  # lazily: conversations imports this module

    try:
        return conversations.check_topic(value) is not None
    except InvalidRequest:
        return False


def audit_fields(tool: str, arguments: dict[str, Any], outcome: str, seen: dict[str, Any]) -> dict[str, Any]:
    """What a topic tool's audit line adds: the topic and, on success, its id, and a rename's new path.

    A success writes what the call recorded in `seen`. A refusal writes only the path it named, never an id,
    because its transaction rolled back and `seen` may hold part of what it did. Either way a topic or new
    path is written only if it is a well-formed topic path: a refused argument is otherwise anything the
    caller sent, a binding token included.
    """
    if tool not in AUDIT_TOOLS:
        return {}
    if outcome != "ok":
        seen = {"topic": arguments.get("path" if tool in PATH_TOOLS else "topic")}
    return {key: value for key, value in seen.items() if key not in ("topic", "new_path") or _is_path(value)}


def create(conn: sqlite3.Connection, config: Config, *, actor: str, path: Any, title: Any = None,
           description: Any = None, members: Any = None, request_id: str | None = None,
           identity: dict[str, str] | None = None, seen: dict[str, Any] | None = None) -> dict[str, Any]:
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
            record_seen(seen, existing["path"], existing)
            return {**_summary(existing), "request_id": request_id, "created": False}
        _require_parent(conn, checked)
        if _row(conn, checked) is not None:
            raise InvalidRequest(f"topic {checked!r} already exists")
        stamp = events.now()
        topic_id = new_topic_id()
        conn.execute("INSERT INTO topics (path, title, description, members, created_by, created_at,"
                     " request_id, request_digest, id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (checked, title, description, json.dumps(chosen), actor, stamp, request_id, digest,
                      topic_id))
        created = _row(conn, checked)
        record_seen(seen, checked, created)
        record(conn, topic_id=topic_id, operation="create_topic", actor=history_actor(actor, identity),
               before=None, after={"path": checked, **_state(created)})
        made = _summary(created)
    return {**made, "request_id": request_id, "created": True}


def update(conn: sqlite3.Connection, config: Config, *, actor: str, path: Any, title: Any = None,
           description: Any = UNSET, members: Any = UNSET, identity: dict[str, str] | None = None,
           seen: dict[str, Any] | None = None) -> dict[str, Any]:
    """Edit a topic's title, description or members. An omitted argument leaves that field as it is.

    A call that changes none of them writes no history row, as a set_topic to the topic it already has does not.
    """
    _may_manage(config, actor)
    checked = _check_path(path)
    new_title = _check_text(title, "title", MAX_TITLE)
    new_description = (UNSET if description is UNSET
                       else _check_text(description, "description", MAX_DESCRIPTION))
    new_members = UNSET if members is UNSET else _check_members(config, members)
    with transaction(conn):
        row = require_exists(conn, checked)
        record_seen(seen, checked, row)
        conn.execute("UPDATE topics SET title = ?, description = ?, members = ? WHERE path = ?",
                     (new_title or row["title"],
                      row["description"] if new_description is UNSET else new_description,
                      row["members"] if new_members is UNSET else json.dumps(new_members),
                      checked))
        updated = _row(conn, checked)
        if _state(updated) != _state(row):
            record(conn, topic_id=row["id"], operation="update_topic", actor=history_actor(actor, identity),
                   before=_state(row), after=_state(updated))
        return _summary(updated)


def rename(conn: sqlite3.Connection, config: Config, *, actor: str, path: Any, new_path: Any,
           identity: dict[str, str] | None = None, seen: dict[str, Any] | None = None) -> dict[str, Any]:
    """Move a topic, its subtopics and every thread filed under them, in one transaction.

    Conversation ids never change, and neither do any thread's participants: a rename is a change of
    label, so nobody gains or loses access to anything. Every refusal is decided before the first
    write, so a refused rename leaves the topic exactly as it was. The history gets one row, for this
    topic: its subtopics moved with it, and their ids did not change.
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
        renamed = _row(conn, target)
        record_seen(seen, checked, renamed, new_path=target)
        record(conn, topic_id=renamed["id"], operation="rename_topic", actor=history_actor(actor, identity),
               before={"path": checked}, after={"path": target})
        return {**_summary(renamed), "renamed_from": checked, "subtopics": len(children)}


def record_move(conn: sqlite3.Connection, *, conversation_id: str, actor: str, before: str | None,
                after: str | None) -> None:
    """Record a conversation's topic changing: a move, or a clear, which is filed under the topic it left.

    Both sides carry their ids, so the move can be followed after either topic has been renamed.
    """
    before_id = None if before is None else id_of(conn, before)
    after_id = None if after is None else id_of(conn, after)
    record(conn, topic_id=after_id or before_id, operation="set_topic", actor=actor,
           before={"conversation_id": conversation_id, "topic": before, "topic_id": before_id},
           after={"conversation_id": conversation_id, "topic": after, "topic_id": after_id})


def may_open(conn: sqlite3.Connection, config: Config, reader: str, path: str) -> bool:
    """Whether this reader may be shown `path` at all: the board rule, or a readable thread in its subtree.

    The subtree, not the topic alone, because a parent whose only readable thread sits in a subtopic has
    to stay reachable: the directory keeps such a parent so its subtopic is not orphaned, and a link to
    a page that refuses would be worse than useless. Opening it discloses only that something under it
    is readable, which the subtopic already says.
    """
    return board_read.sees_all(config, reader) or _readable(conn, config, reader, path)[0] > 0


def get(conn: sqlite3.Connection, config: Config, *, reader: str, path: Any,
        seen: dict[str, Any] | None = None) -> dict[str, Any]:
    """One topic, with the number of threads in it this reader may see and the subtopics it may open."""
    checked = _check_path(path)
    row = _row(conn, checked)
    if row is None:
        raise UnknownTopic(f"no topic {checked!r}")
    record_seen(seen, checked, row)
    threads, _ = _readable(conn, config, reader, checked)
    # Filtered, because the subtopic list is the other way this could name a topic the reader may not
    # see into -- the page renders these as links, and the tool hands them to whoever called it.
    children = [entry[0] for entry in conn.execute(
        "SELECT path FROM topics WHERE path LIKE ? ORDER BY path", (f"{checked}/%",))]
    return {**_summary(row), "threads": threads,
            "subtopics": [child for child in children if may_open(conn, config, reader, child)]}


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
    """The threads in this topic's subtree the reader may see, and the newest activity among those same ones.

    The subtree, because a topic's page has always listed itself and everything one separator deeper: a
    count of only the threads filed exactly there contradicted the list printed beside it, and showed a
    root as empty when its subtopics held the work.

    Both numbers come from one query on purpose. They were two, and only the count was filtered by
    reader, so a newer conversation the reader is not in lent the topic its timestamp -- which disclosed
    that something had happened there, and when. Asking once makes that divergence impossible rather
    than merely fixed. The board rule decides what counts as readable, not topic membership. A stored
    path carries neither `LIKE` metacharacter, so the prefix matches literally with nothing to escape.
    """
    if board_read.sees_all(config, reader):
        row = conn.execute("SELECT COUNT(*), MAX(last_activity_at) FROM conversations"
                           " WHERE topic = ? OR topic LIKE ?", (path, f"{path}/%")).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*), MAX(c.last_activity_at) FROM conversations c"
                           " WHERE (c.topic = ? OR c.topic LIKE ?) AND EXISTS"
                           " (SELECT 1 FROM participants p WHERE p.conversation_id = c.id"
                           " AND p.role = ?)", (path, f"{path}/%", reader)).fetchone()
    return row[0], row[1]
