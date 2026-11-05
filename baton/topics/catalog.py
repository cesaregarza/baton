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
from typing import Any

from baton import board_read, events, messages
from baton.agents.permissions import Capability, Permissions
from baton.config import Config
from baton.database import Connection, Row
from baton.database.store import new_topic_id
from baton.errors import InvalidRequest, UnknownRole, UnknownTopic

from . import grants as topic_grants
from . import paths as topic_paths

MAX_TITLE = 100
MAX_DESCRIPTION = 1000
# Tells "the caller left members out" apart from "the caller asked for no members": on create the two
# mean the same empty list, but on update one keeps the current list and the other clears it.
UNSET: Any = object()


def _may_manage(config: Config, actor: str) -> None:
    Permissions.for_role(config, actor).require(
        Capability.CREATE_TOPICS,
        f"{actor} may not create or change topics (roles.{actor}.can_create_topics)",
    )


def check_path(path: Any, field: str = "path") -> str:
    checked = topic_paths.check_topic(path, field=field)
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


def _row(conn: Connection, path: str) -> Row | None:
    return conn.execute("SELECT * FROM topics WHERE path = ?", (path,)).fetchone()


def _require_parent(conn: Connection, path: str) -> None:
    if "/" in path and _row(conn, path.split("/")[0]) is None:
        raise InvalidRequest(f"topic {path!r} needs its parent {path.split('/')[0]!r} to exist first")


def exists(conn: Connection, path: str) -> bool:
    return _row(conn, path) is not None


def require_exists(conn: Connection, path: str, field: str = "topic") -> Row:
    """The topic, or a refusal naming the path. Callers hold the write lock, so this cannot go stale."""
    row = _row(conn, path)
    if row is None:
        raise InvalidRequest(f"{field} {path!r} is not a topic; a core role opens one with create_topic")
    return row


def require_topic(conn: Connection, path: str) -> Row:
    """The topic at `path`, or the `unknown_topic` refusal `get_topic` gives for a path no topic holds."""
    row = _row(conn, path)
    if row is None:
        raise UnknownTopic(f"no topic {path!r}")
    return row


def members_for_thread(conn: Connection, config: Config, path: str) -> list[str]:
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


def _summary(row: Row) -> dict[str, Any]:
    return {"path": row["path"], "title": row["title"], "description": row["description"],
            "members": json.loads(row["members"]), "created_by": row["created_by"],
            "created_at": row["created_at"]}


def _state(row: Row) -> dict[str, Any]:
    """What a topic change's history row records of a topic: its editable fields, never its id or path."""
    return {"title": row["title"], "description": row["description"], "members": json.loads(row["members"])}


def history_actor(actor: str, identity: dict[str, str] | None) -> str:
    """The name a history row gives the caller: its role, and for an alias-bound call the alias's immutable id.

    The id, not the display name, because a name can be claimed again and a reader must be able to tell
    the alias from a direct credential of the same role.
    """
    return actor if identity is None else f"{actor} via alias {identity['alias_id']}"


def record(conn: Connection, *, topic_id: str, operation: str, actor: str,
           before: dict[str, Any] | None, after: dict[str, Any] | None, grant_id: str | None = None) -> None:
    """Append one change to the history. The caller holds the write lock and has made the change.

    `grant_id` is the topic grant a guest acted under, and is None for a core role.
    """
    conn.execute("INSERT INTO topic_changes (topic_id, operation, actor, grant_id, before, after, at)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (topic_id, operation, actor, grant_id, _encoded(before), _encoded(after), events.now()))


def _encoded(state: dict[str, Any] | None) -> str | None:
    return None if state is None else json.dumps(state)


def id_of(conn: Connection, path: str) -> str | None:
    """The id of the topic at `path`, or None when there is none."""
    row = _row(conn, path)
    return None if row is None else row["id"]


# The tools whose audit line names a topic: by `path` (the topic itself, or a rename's old one), or by `topic`
# (the topic a thread is filed in, a listing filters by, or a reference is kept in).
PATH_TOOLS = frozenset({"create_topic", "update_topic", "rename_topic", "get_topic"})
# The reference tools, whose line also names the reference, by its `name`.
REFERENCE_AUDIT = frozenset({"edit_reference", "read_reference", "reference_history"})
AUDIT_TOOLS = PATH_TOOLS | {"set_topic", "create_conversation", "list_conversations"} | REFERENCE_AUDIT


def record_seen(seen: dict[str, Any] | None, path: str, row: Row | None = None,
                **more: str | int | None) -> None:
    """Note in `seen`, when the caller gave one, the topic this call resolved: `path` and the id of `row`, if any.

    `path` is what the call named, and `row` the topic its result is, or is in, read while the call held a
    consistent view. The audit line is written after the call's transaction has committed, and by then another
    writer may have moved that topic and given its path to a different one, so a call records what it resolved
    while it held it. A replay therefore passes the path it was asked for, not the path its topic has now.
    An extra whose value is None is left out, so a core role's line has no `grant_id`.
    """
    if seen is not None:
        seen.update({"topic": path, **({} if row is None else {"topic_id": row["id"]}),
                     **{key: value for key, value in more.items() if value is not None}})


def seen_by(call: dict[str, Any] | None) -> dict[str, Any] | None:
    """Where a topic tool's body records the topic it resolved, for the audit line."""
    return None if call is None else call.setdefault("topic", {})


def note_path(conn: Connection, seen: dict[str, Any] | None, path: str | None) -> None:
    """record_seen for the topic now at `path`, which may be none; a path with no topic records no id."""
    if path is not None and seen is not None:
        record_seen(seen, path, _row(conn, path))


def note_replay(conn: Connection, seen: dict[str, Any] | None, path: str | None,
                filed_in: str | None) -> None:
    """record_seen for a replay: the path it named, and the id of the topic its returned thread is in now.

    That is `filed_in`, the thread's current topic path, not whatever topic holds `path` today. A thread with no
    topic now records no id.
    """
    if path is not None and seen is not None:
        record_seen(seen, path, None if filed_in is None else _row(conn, filed_in))


def _is_path(value: Any) -> bool:
    """Whether `value` is a topic path as topic_paths.check_topic accepts it.

    No binding token passes: it is secrets.token_urlsafe(32), 43 characters with no slash, and a topic
    segment is at most MAX_TOPIC_SEGMENT (40) characters.
    """
    try:
        return topic_paths.check_topic(value) is not None
    except InvalidRequest:
        return False


def _well_formed(key: str, value: Any) -> bool:
    """Whether an audit field may be written as given. A topic, a rename's new path and a reference name come from
    the caller's own arguments when a call is refused, so each must be well formed; every other field is the host's."""
    if key in ("topic", "new_path"):
        return _is_path(value)
    if key == "reference":
        return topic_paths.is_segment(value)
    return True


def audit_fields(tool: str, arguments: dict[str, Any], outcome: str, seen: dict[str, Any]) -> dict[str, Any]:
    """What a topic tool's audit line adds: the topic and, on success, its id, and a rename's new path.

    `topic` is always the path the call named. `topic_id` is the stable id of the topic the call's result is, or
    is in, read while the call held a consistent view of the database. A success writes what the call recorded
    in `seen`. A refusal writes only the path it named, never an id,
    because its transaction rolled back and `seen` may hold part of what it did. Either way a topic or new
    path is written only if it is a well-formed topic path: a refused argument is otherwise anything the
    caller sent, a binding token included.

    A reference tool's line also names the reference, under the same rule, and an edit's the revision it wrote
    or replayed.
    """
    if tool not in AUDIT_TOOLS:
        return {}
    if outcome != "ok":
        seen = {"topic": arguments.get("path" if tool in PATH_TOOLS else "topic"),
                **({"reference": arguments.get("name")} if tool in REFERENCE_AUDIT else {})}
    return {key: value for key, value in seen.items() if _well_formed(key, value)}


def _no_members(members: Any) -> list[str]:
    """A grantee takes its parent's members, so it may not name any, not even none."""
    if members is not None:
        raise InvalidRequest("a topic grant cannot set members; a new subtopic takes its parent's members")
    return []


def _grant_and_parent(conn: Connection, identity: dict[str, str] | None, grant_key: str | None,
                      path: str) -> tuple[Row | None, Row | None]:
    """The grant the key opens and the parent it names for a new topic at `path`; neither for a core role."""
    if grant_key is None:
        return None, None
    grant = topic_grants.grant_for_use(conn, identity, grant_key)
    return grant, topic_grants.require_names(conn, grant, "topic.create_sub", path)


def create(conn: Connection, config: Config, *, actor: str, path: Any, title: Any = None,
           description: Any = None, members: Any = None, request_id: str | None = None,
           identity: dict[str, str] | None = None, seen: dict[str, Any] | None = None,
           grant_key: str | None = None) -> dict[str, Any]:
    """Open a topic. A replay rechecks authority and the parent, then returns the topic as it now stands.

    With `grant_key` the caller is a guest, and may only open a subtopic of the topic its key's grant names. Its
    request digest holds its alias, since every guest has the same role. The replay runs the grant check first,
    so a grant that has ended answers no retry from the stored result.
    """
    if grant_key is None:
        _may_manage(config, actor)
    checked = check_path(path)
    title = _check_text(title, "title", MAX_TITLE, default=checked.split("/")[-1])
    description = _check_text(description, "description", MAX_DESCRIPTION)
    chosen = _check_members(config, members) if grant_key is None else _no_members(members)
    request_id = request_id or messages.new_request_id()
    if not 1 <= len(request_id) <= 200:
        raise InvalidRequest("request_id is 1-200 characters")
    whose = {} if grant_key is None else {"alias_id": (identity or {}).get("alias_id")}
    digest = events.request_digest("create_topic", {"actor": actor, "path": checked, "title": title,
                                                    "description": description, "members": chosen, **whose})
    with conn.transaction():
        grant, parent = _grant_and_parent(conn, identity, grant_key, checked)
        grant_id = None if grant is None else grant["id"]
        existing = events.replayed(conn, "topics", request_id, digest)
        if existing is not None:
            record_seen(seen, checked, existing, grant_id=grant_id)
            return {**_summary(existing), "request_id": request_id, "created": False}
        _require_parent(conn, checked)
        if _row(conn, checked) is not None:
            raise InvalidRequest(f"topic {checked!r} already exists")
        if parent is not None:
            chosen = json.loads(parent["members"])
        stamp = events.now()
        topic_id = new_topic_id()
        conn.execute("INSERT INTO topics (path, title, description, members, created_by, created_at,"
                     " request_id, request_digest, id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (checked, title, description, json.dumps(chosen), actor, stamp, request_id, digest,
                      topic_id))
        created = _row(conn, checked)
        record_seen(seen, checked, created, grant_id=grant_id)
        record(conn, topic_id=topic_id, operation="create_topic", actor=history_actor(actor, identity),
               before=None, after={"path": checked, **_state(created)}, grant_id=grant_id)
        made = _summary(created)
    return {**made, "request_id": request_id, "created": True}


def update(conn: Connection, config: Config, *, actor: str, path: Any, title: Any = None,
           description: Any = UNSET, members: Any = UNSET, identity: dict[str, str] | None = None,
           seen: dict[str, Any] | None = None) -> dict[str, Any]:
    """Edit a topic's title, description or members. An omitted argument leaves that field as it is.

    A call that changes none of them writes no history row, as a set_topic to the topic it already has does not.
    """
    _may_manage(config, actor)
    checked = check_path(path)
    new_title = _check_text(title, "title", MAX_TITLE)
    new_description = (UNSET if description is UNSET
                       else _check_text(description, "description", MAX_DESCRIPTION))
    new_members = UNSET if members is UNSET else _check_members(config, members)
    with conn.transaction():
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


def _require_acknowledged(path: str, affected: list[str], acknowledged: list[str] | None) -> None:
    """A rename that changes what active grants cover must name exactly those grants, so no one renames a topic
    out from under a grantee without having been told. Naming ids when none is affected is refused too: it
    would be an acknowledgement of something that is not so."""
    named = set(acknowledged or [])
    if named == set(affected):
        return
    if not affected:
        raise InvalidRequest(f"none of the topic grants are affected by renaming {path}, "
                             "so leave acknowledge_topic_grants out")
    raise InvalidRequest(f"renaming {path} changes what topic grants {', '.join(affected)} cover; "
                         "repeat the call with acknowledge_topic_grants set to exactly those ids")


def rename(conn: Connection, config: Config, *, actor: str, path: Any, new_path: Any,
           identity: dict[str, str] | None = None, seen: dict[str, Any] | None = None,
           acknowledge_topic_grants: list[str] | None = None) -> dict[str, Any]:
    """Move a topic, its subtopics and every thread filed under them, in one transaction.

    Conversation ids never change, and neither do any thread's participants: a rename is a change of
    label, so nobody gains or loses access to anything. Every refusal is decided before the first
    write, so a refused rename leaves the topic exactly as it was. The history gets one row for each
    topic whose path changed, since a grant follows a topic by id and the history must show where it went.
    A rename that changes what active topic grants cover must acknowledge them, and logs a `rename` event on each.
    """
    _may_manage(config, actor)
    checked = check_path(path)
    target = check_path(new_path, field="new_path")
    if target == checked or target.startswith(checked + "/"):
        raise InvalidRequest(f"new_path {target!r} is {path!r} itself or inside it")
    with conn.transaction():
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
        affected = topic_grants.affected_by_rename(conn, checked, target, moving)
        _require_acknowledged(checked, affected, acknowledge_topic_grants)
        ids = {old: _row(conn, old)["id"] for old in moving}
        # Deepest first, so a path never collides with one still to move.
        for old in sorted(moving, key=len, reverse=True):
            conn.execute("UPDATE topics SET path = ? WHERE path = ?", (moving[old], old))
            conn.execute("UPDATE conversations SET topic = ? WHERE topic = ?", (moving[old], old))
        who = history_actor(actor, identity)
        for old, new in moving.items():
            record(conn, topic_id=ids[old], operation="rename_topic", actor=who, before={"path": old},
                   after={"path": new})
        topic_grants.record_rename(conn, affected, who, ids[checked], checked, target)
        renamed = _row(conn, target)
        record_seen(seen, checked, renamed, new_path=target)
        return {**_summary(renamed), "renamed_from": checked, "subtopics": len(children)}


def record_move(conn: Connection, *, conversation_id: str, actor: str, before: str | None,
                after: str | None, grant_id: str | None = None) -> None:
    """Record a conversation's topic changing: a move, or a clear, which is filed under the topic it left.

    Both sides carry their ids, so the move can be followed after either topic has been renamed.
    """
    before_id = None if before is None else id_of(conn, before)
    after_id = None if after is None else id_of(conn, after)
    record(conn, topic_id=after_id or before_id, operation="set_topic", actor=actor,
           before={"conversation_id": conversation_id, "topic": before, "topic_id": before_id},
           after={"conversation_id": conversation_id, "topic": after, "topic_id": after_id}, grant_id=grant_id)


def may_open(conn: Connection, config: Config, reader: str, path: str) -> bool:
    """Whether this reader may be shown `path` at all: the board rule, or a readable thread in its subtree.

    The subtree, not the topic alone, because a parent whose only readable thread sits in a subtopic has
    to stay reachable: the directory keeps such a parent so its subtopic is not orphaned, and a link to
    a page that refuses would be worse than useless. Opening it discloses only that something under it
    is readable, which the subtopic already says.
    """
    return board_read.sees_all(config, reader) or _readable(conn, config, reader, path)[0] > 0


def get(conn: Connection, config: Config, *, reader: str, path: Any,
        seen: dict[str, Any] | None = None) -> dict[str, Any]:
    """One topic, with the number of threads in it this reader may see and the subtopics it may open."""
    checked = check_path(path)
    with conn.snapshot():  # one view, so a rename in between cannot mix two topics into one answer
        row = require_topic(conn, checked)
        record_seen(seen, checked, row)
        threads, _ = _readable(conn, config, reader, checked)
        # Filtered, because the subtopic list is the other way this could name a topic the reader may not
        # see into -- the page renders these as links, and the tool hands them to whoever called it.
        children = [entry[0] for entry in conn.execute(
            "SELECT path FROM topics WHERE path LIKE ? ORDER BY path", (f"{checked}/%",))]
        return {**_summary(row), "threads": threads,
                "subtopics": [child for child in children if may_open(conn, config, reader, child)]}


def listing(conn: Connection, config: Config, *, reader: str,
            parent: Any = None) -> list[dict[str, Any]]:
    """Every topic, or only those directly under `parent`, each with this reader's own thread count."""
    if parent is None:
        rows = conn.execute("SELECT * FROM topics ORDER BY path").fetchall()
    else:
        checked = check_path(parent, field="parent")
        rows = conn.execute("SELECT * FROM topics WHERE path LIKE ? ORDER BY path",
                            (f"{checked}/%",)).fetchall()
    listed = []
    for row in rows:
        threads, last_activity_at = _readable(conn, config, reader, row["path"])
        listed.append({**_summary(row), "threads": threads, "last_activity_at": last_activity_at})
    return listed


def _readable(conn: Connection, config: Config, reader: str,
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
