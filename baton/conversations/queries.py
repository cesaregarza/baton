"""Conversation listing, summaries, and thread/participant reads."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from baton import board_read, topics
from baton.config import Config
from baton.database import Connection, Row
from baton.errors import InvalidRequest, UnknownConversation
from baton.messages import limits as message_limits
from baton.topics.paths import check_topic

MAX_TITLE = 200
MIN_PARTICIPANTS = 2
# The most conversations one list_for or list_all call returns.
MAX_LISTED = 200


def list_for(
    conn: Connection,
    role: str,
    topic: Any = None,
    *,
    limit: int = MAX_LISTED,
    before: tuple[str, str] | None = None,
    changed_since: str | None = None,
    _lookahead: bool = False,
) -> list[dict[str, Any]]:
    """The conversations `role` takes part in, newest first, with their participants.

    `topic` narrows them to that topic and anything under it. It never widens what the role may see: a
    conversation it is not in stays out whether it has a topic or not. One call returns at most `limit`;
    to read on, pass the last one's `page_cursor(...)` as `before`. `changed_since`, an ISO time, keeps only
    those with a message after it: a discovery aid, not a guarantee of completeness.
    """
    where, values = _under(topic)
    after, cursor = _before(before, "c.")
    since, moment = (
        ("", ())
        if changed_since is None
        else (" AND COALESCE(c.last_activity_at, c.created_at) > ?", (utc_iso(changed_since, "changed_since"),))
    )
    rows = conn.execute(
        "SELECT c.* FROM conversations c JOIN participants p ON p.conversation_id = c.id"
        f" WHERE p.role = ?{where}{after}{since} ORDER BY c.created_at DESC, c.id DESC LIMIT ?",
        (role, *values, *cursor, *moment, _limit(limit + int(_lookahead), allow_lookahead=_lookahead)),
    ).fetchall()
    return [_summary(conn, row) for row in rows]


def list_all(
    conn: Connection,
    topic: Any = None,
    *,
    limit: int = MAX_LISTED,
    before: tuple[str, str] | None = None,
    changed_since: str | None = None,
    _lookahead: bool = False,
) -> list[dict[str, Any]]:
    """Every conversation, newest first, paged like `list_for`: only for a viewer the config lets see them all."""
    where, values = _under(topic)
    after, cursor = _before(before, "")
    since, moment = (
        ("", ())
        if changed_since is None
        else (" AND COALESCE(last_activity_at, created_at) > ?", (utc_iso(changed_since, "changed_since"),))
    )
    rows = conn.execute(
        f"SELECT * FROM conversations WHERE 1 = 1{where}{after}{since} ORDER BY created_at DESC, id DESC LIMIT ?",
        (*values, *cursor, *moment, _limit(limit + int(_lookahead), allow_lookahead=_lookahead)),
    ).fetchall()
    return [_summary(conn, row) for row in rows]


def page_cursor(summary: dict[str, Any]) -> tuple[str, str]:
    """Where the next page of `list_for` or `list_all` starts: after this conversation."""
    return summary["created_at"], summary["conversation_id"]


def list_readable(
    conn: Connection,
    config: Config,
    role: str,
    topic: Any = None,
    *,
    limit: int = MAX_LISTED,
    before: tuple[str, str] | None = None,
    changed_since: str | None = None,
    _lookahead: bool = False,
    seen: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """List the conversations this role may read under the shared board predicate.

    The topic's id and the listing are read in one snapshot, so a rename and reuse of the path in between
    cannot make the listing describe a different topic from the one the audit line names.
    """
    with conn.snapshot():
        topics.note_path(conn, seen, check_topic(topic))
        if board_read.sees_all(config, role):
            return list_all(conn, topic, limit=limit, before=before, changed_since=changed_since, _lookahead=_lookahead)
        return list_for(
            conn, role, topic, limit=limit, before=before, changed_since=changed_since, _lookahead=_lookahead
        )


def list_readable_page(
    conn: Connection, config: Config, role: str, topic: Any = None, *, limit: int = MAX_LISTED, offset: int = 0
) -> tuple[int, list[dict[str, Any]]]:
    """Return the authorized total and one numbered page in a single read snapshot.

    This is for views whose page number is part of the link, rather than a cursor. The count and
    rows use the same board-read rule and topic filter; the page query never materializes earlier
    conversations just to skip them.
    """
    where, values = _under(topic)
    limit = _limit(limit)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidRequest("offset must be a non-negative integer")
    with conn.snapshot():
        if board_read.sees_all(config, role):
            total = conn.execute(f"SELECT COUNT(*) FROM conversations WHERE 1 = 1{where}", values).one()[0]
            rows = conn.execute(
                f"SELECT * FROM conversations WHERE 1 = 1{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
        else:
            # participants has one row per (conversation, role), so this join keeps count and page at conversation
            # grain while enforcing the same membership boundary as list_for.
            total = conn.execute(
                "SELECT COUNT(*) FROM conversations c JOIN participants p "
                "ON p.conversation_id = c.id WHERE p.role = ?" + where,
                (role, *values),
            ).one()[0]
            rows = conn.execute(
                "SELECT c.* FROM conversations c JOIN participants p "
                "ON p.conversation_id = c.id WHERE p.role = ?"
                + where
                + " ORDER BY c.created_at DESC, c.id DESC LIMIT ? OFFSET ?",
                (role, *values, limit, offset),
            ).fetchall()
        return total, [_summary(conn, row) for row in rows]


def readable_summary(conn: Connection, config: Config, role: str, conversation_id: str) -> dict[str, Any] | None:
    """Return one conversation's summary only if the same board-read rule permits it."""
    with conn.snapshot():
        if not board_read.may_read_conversation(conn, config, role, conversation_id):
            return None
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return None if row is None else _summary(conn, row)


def _before(before: tuple[str, str] | None, alias: str) -> tuple[str, tuple[str, ...]]:
    if before is None:
        return "", ()
    return f" AND ({alias}created_at, {alias}id) < (?, ?)", (str(before[0]), str(before[1]))


def _limit(limit: int, *, allow_lookahead: bool = False) -> int:
    maximum = MAX_LISTED + int(allow_lookahead)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise InvalidRequest(f"limit must be 1-{MAX_LISTED}")
    return limit


def _under(topic: Any) -> tuple[str, tuple[str, ...]]:
    """A filter for a topic and everything under it, matching whole segments only.

    `project` takes `project` and `project/anything`, and never `projectile`, because the only other
    match is on a full segment boundary. The topic is validated first, so it carries no LIKE
    metacharacter and needs no escaping.
    """
    topic = check_topic(topic)
    if topic is None:
        return "", ()
    return " AND (topic = ? OR topic LIKE ?)", (topic, f"{topic}/%")


def _load(conn: Connection, conversation_id: str) -> Row:
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if row is None:
        raise UnknownConversation(conversation_id)
    return row


def _participants(conn: Connection, conversation_id: str) -> list[str]:
    rows = conn.execute("SELECT role FROM participants WHERE conversation_id = ? ORDER BY role", (conversation_id,))
    return [row["role"] for row in rows]


def _summary(conn: Connection, row: Row) -> dict[str, Any]:
    # The alias pair appears only on a thread a guest started, the way a guest post's attribution does,
    # so a role's thread reads exactly as it did before.
    alias = (
        {"created_by_alias": row["created_by_alias"], "created_by_alias_code": row["created_by_alias_code"]}
        if row["created_by_alias_id"]
        else {}
    )
    return {
        "conversation_id": row["id"],
        "title": row["title"],
        "topic": row["topic"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "participants": _participants(conn, row["id"]),
        "revision": row["revision"],
        "last_seq": row["last_seq"],
        "last_activity_at": row["last_activity_at"] or row["created_at"],
        **alias,
    }


def utc_iso(moment: str, name: str) -> str:
    """A time to compare with stored times, in the form they are stored in: UTC `isoformat()`.

    Stored times all come from `datetime.now(UTC).isoformat()`, so once a given time is converted to UTC and
    written the same way, comparing the strings compares the instants. One in another offset is converted;
    one without an offset, or that doesn't parse, is refused.
    """
    try:
        parsed = datetime.fromisoformat(str(moment))
    except ValueError:
        raise InvalidRequest(f"{name} must be an ISO 8601 time with its UTC offset") from None
    if parsed.tzinfo is None:
        raise InvalidRequest(f"{name} must carry its UTC offset")
    return parsed.astimezone(UTC).isoformat()


def attention_signals(
    conn: Connection, config: Config, viewer: str, subject: str, topic: Any = None, *, human_replies_only: bool = False
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[tuple[dict[str, Any], dict[str, Any]]], set[str]]:
    """Collect and classify retained attention/decision messages readable by `viewer`.

    `subject` filters signals but never grants access. A projected role's replies can be limited to
    person-authored messages; a human seat always requests that restriction for its own replies.
    """
    found: list[tuple[dict[str, Any], dict[str, Any]]] = []
    replied: set[str] = set()
    before = None
    while True:
        listed = list_readable(conn, config, viewer, topic, before=before)
        for conversation in listed:
            after = 0
            while True:
                from baton.conversations.conversation import Conversation

                page = Conversation(conn, conversation["conversation_id"]).history(
                    viewer, after_seq=after, limit=message_limits.MAX_PAGE_MESSAGES, config=config
                )
                entries = page["messages"]
                found.extend(
                    (conversation, item)
                    for item in entries
                    if item["from_role"] not in (None, viewer, subject)
                    and (item.get("needs_decision") or subject in (item.get("attention") or []))
                )
                replied.update(
                    item["reply_to"]["message_id"]
                    for item in entries
                    if item["from_role"] == subject
                    and item.get("reply_to")
                    and (not human_replies_only or item.get("author_kind") == "human")
                )
                if not entries:
                    break
                after = page["next_seq"]
        if len(listed) < MAX_LISTED:
            break
        before = page_cursor(listed[-1])
    found.sort(key=lambda pair: (pair[1]["at"], pair[1]["seq"]), reverse=True)
    mine = [pair for pair in found if subject in (pair[1].get("attention") or [])]
    others = [pair for pair in found if subject not in (pair[1].get("attention") or [])]
    return mine, others, replied
