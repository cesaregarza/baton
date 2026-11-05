"""Conversation message fanout, limits, and escalation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from baton import changes, events, messages
from baton.config import Config, Conversations
from baton.conversations import queries
from baton.database import Connection, Row
from baton.errors import ConversationFull, LimitExceeded
from baton.messages import limits as message_limits

RATE_WINDOW = timedelta(hours=1)


def _tell_of_move(
    conn: Connection,
    conversation_id: str,
    grant: Row,
    identity: dict[str, str],
    who: str,
    before: str | None,
    after: str | None,
) -> None:
    """A host notice to every participant that a grantee moved their thread, and under which grant."""
    notice = {
        "event": "topic_moved",
        "from": before,
        "to": after,
        "by": who,
        "alias": identity["alias"],
        "alias_code": identity["code"],
        "grant_id": grant["id"],
    }
    text = (
        f"alias {identity['alias']} ({identity['code']}) moved this thread from {before} to {after} "
        f"under topic grant {grant['id']}"
    )
    _fan_out(conn, conversation_id, queries._participants(conn, conversation_id), sender=None, text=text, notice=notice)


def _thread_cap_refusal(conn: Connection, config: Config, identity: dict[str, str]) -> LimitExceeded | None:
    """Whether this guest thread is over either hourly cap, counted inside the caller's write lock.

    Both windows are counted from `conversations` itself, so there is no second table to keep in step,
    and the write lock the transaction already holds is what stops two creates both slipping under a
    cap. A refusal is returned, not raised: the guest-wide case also stores an alert that has to be
    committed by this transaction rather than rolled back by the exception.
    """
    since = (datetime.now(UTC) - RATE_WINDOW).isoformat()
    settings = config.aliases
    mine = conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE created_by_alias_id = ? AND created_at > ?",
        (identity["alias_id"], since),
    ).fetchone()[0]
    everyone = conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE created_by = 'guest' AND created_at > ?", (since,)
    ).fetchone()[0]
    over_alias = mine >= settings.max_threads_per_alias_per_hour
    over_guests = everyone >= settings.max_threads_per_guest_per_hour
    if not over_alias and not over_guests:
        return None
    reached = []
    if over_alias:
        reached.append(f"{settings.max_threads_per_alias_per_hour} for this alias")
    if over_guests:
        reached.append(f"{settings.max_threads_per_guest_per_hour} across all guests")
        _alert_guest_cap(conn, config)
    return LimitExceeded(
        "new guest threads are over their hourly limit: " + " and ".join(reached),
        "threads_per_guest" if over_guests else "threads_per_alias",
    )


def _alert_guest_cap(conn: Connection, config: Config) -> None:
    """Tell the alert role the guest-wide cap filled, at most once an hour.

    The timestamp is persisted in the same transaction as the refusal, so a restart cannot turn one
    alert into a flood, and a full mailbox leaves it unset so the next refusal tries again rather than
    the hour passing in silence. Nothing is stored when no alert role is configured.
    """
    role = config.aliases.guest_cap_alert_role
    if role is None:
        return
    now = datetime.now(UTC)
    row = conn.execute("SELECT value FROM log_meta WHERE key = 'guest_thread_cap_alerted_at'").fetchone()
    if row is not None and row["value"] > (now - RATE_WINDOW).isoformat():
        return
    if messages.unread(conn, role) >= message_limits.MAX_UNREAD:
        return  # Storing would raise MailboxFull and lose the refusal; the timestamp stays unset.
    limit = config.aliases.max_threads_per_guest_per_hour
    conn.execute(
        "INSERT INTO log_meta (key, value) VALUES ('guest_thread_cap_alerted_at', ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (now.isoformat(),),
    )
    messages.store_copy(
        conn,
        message_id=messages.new_message_id(),
        sender=None,
        recipient=role,
        text=f"Guest threads reached their hourly limit of {limit} across all guests. "
        "Further guest threads are refused until the window moves.",
        notice={"event": "guest_threads.cap_reached", "limit": limit, "window": "1h"},
    )


def _fan_out(
    conn: Connection,
    conversation_id: str,
    recipients: list[str],
    *,
    sender: str | None,
    text: str,
    relayed: list[dict] | None = None,
    notice: dict[str, str] | None = None,
    job_id: str | None = None,
    message_id: str | None = None,
    author_kind: str | None = None,
    reply_to: str | None = None,
    attention: list[str] | None = None,
    needs_decision: bool = False,
    identity: dict[str, str] | None = None,
) -> dict[str, int]:
    """Store one copy for each recipient under one message_id, or refuse before storing any."""
    recipients = [role for role in recipients if role != "guest"]
    full = [role for role in recipients if messages.unread(conn, role) >= message_limits.MAX_UNREAD]
    if full:
        raise ConversationFull(
            f"{', '.join(full)} has {message_limits.MAX_UNREAD} unread messages, so no participant "
            "got this; retry after it reads"
        )
    message_id = message_id or messages.new_message_id()
    conn.execute(
        "UPDATE conversations SET last_seq = last_seq + 1, last_activity_at = ? WHERE id = ?",
        (events.now(), conversation_id),
    )
    conversation_seq = queries._load(conn, conversation_id)["last_seq"]
    changes.record(conn, conversation_id, "message_added", message_id=message_id)
    return {
        role: messages.store_copy(
            conn,
            message_id=message_id,
            sender=sender,
            recipient=role,
            text=text,
            job_id=job_id,
            conversation_id=conversation_id,
            conversation_seq=conversation_seq,
            relayed=relayed,
            notice=notice,
            author_kind=author_kind,
            reply_to=reply_to,
            attention=attention,
            needs_decision=needs_decision,
            identity=identity,
        )
        for role in recipients
    }


def _sent_within_window(conn: Connection, conversation_id: str) -> int:
    """Messages participants sent to the conversation in the last hour; host notices don't count."""
    since = (datetime.now(UTC) - RATE_WINDOW).isoformat()
    return conn.execute(
        "SELECT COUNT(DISTINCT id) FROM messages WHERE conversation_id = ? AND sender IS NOT NULL AND at > ?",
        (conversation_id, since),
    ).fetchone()[0]


def _escalate(conn: Connection, settings: Conversations, conversation: Row, sender: str) -> None:
    """Address one content-free event to the escalation role, unless one already went out within the window."""
    since = (datetime.now(UTC) - RATE_WINDOW).isoformat()
    if settings.escalation_role is None or (conversation["escalated_at"] or "") > since:
        return
    conn.execute("UPDATE conversations SET escalated_at = ? WHERE id = ?", (events.now(), conversation["id"]))
    events.append(
        conn,
        job_id=None,
        seq=0,
        actor=sender,
        audience=settings.escalation_role,
        kind="conversation.rate_limited",
        summary=f"{conversation['id']} is over its hourly message limit",
        data={"conversation_id": conversation["id"]},
    )
