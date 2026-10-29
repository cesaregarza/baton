"""Group conversations: a titled thread whose every message reaches each other participant.

A conversation send stores one copy per other participant, each as that
recipient's next mailbox seq and all under one shared message_id, in one
transaction: every recipient gets its copy or none does. Each copy keeps what
a direct message guarantees: the reader's gap-free seq and cursors, coalesced
wakes and retention. The sender gets no copy, so it is never woken by its own
message.

`history` is separate from that delivery: it returns the conversation as a
participant has seen it, its own messages included, and moves no bookmark.

A message may answer another through `reply_to`, and may single out the
participants it asks to look at it through `attention`, or ask the
conversation for a decision through `needs_decision`. Those three are what a
reader and a quiet role's wake policy go on; none of them is an approval, and
naming a role neither adds it to the conversation nor speaks for it.

A conversation may carry an optional `topic`, a readable path of one or
two lowercase slug segments. It is a label for people, kept apart from
the conversation's id, so renaming it moves nothing; listing filters by
it a whole segment at a time.

The config says which roles may create conversations and manage
participants. A message's author is always the sender, the role its
credential is bound to. Words the sender passes on from someone else travel
in `relayed`, apart from its own text, and are never their authenticated
authorship. Messages are information, never authority: nothing here touches
a job.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from baton import events, messages
from baton.config import Config, Conversations
from baton.errors import (
    ConversationFull,
    ConversationRateLimited,
    ConversationsDisabled,
    Forbidden,
    InvalidRequest,
    NotAParticipant,
    UnknownConversation,
    UnknownRole,
)
from baton.store import transaction

MAX_TITLE = 200
MIN_PARTICIPANTS = 2
RATE_WINDOW = timedelta(hours=1)
# A topic is one or two lowercase slug segments: `project` or `project/subtopic`. A segment is lowercase
# ASCII letters, digits and inner hyphens, so it can never carry a SQL LIKE metacharacter and a prefix
# filter is safe to match literally. It is a label for people, separate from the conversation's id.
MAX_TOPIC_SEGMENT = 40
MAX_TOPIC_SEGMENTS = 2
TOPIC_SEGMENT = re.compile(rf"[a-z0-9](?:[a-z0-9-]{{0,{MAX_TOPIC_SEGMENT - 2}}}[a-z0-9])?")


def check_topic(topic: Any, field: str = "topic") -> str | None:
    """A topic as stored: one or two lowercase slug segments, or None for no topic at all.

    None is how "no topic" arrives, and setting it clears one. Anything else must match the grammar
    whole: a refusal names the field. Because a segment admits neither of SQL's LIKE metacharacters, a
    validated topic is safe to match literally in a prefix filter.
    """
    if topic is None:
        return None
    if not isinstance(topic, str):
        raise InvalidRequest(f"{field} must be a path of 1-{MAX_TOPIC_SEGMENTS} lowercase slug segments, "
                             "such as project or project/subtopic, or left out")
    segments = topic.split("/")
    if not 1 <= len(segments) <= MAX_TOPIC_SEGMENTS or not all(TOPIC_SEGMENT.fullmatch(s) for s in segments):
        raise InvalidRequest(f"{field} {topic!r} must be 1-{MAX_TOPIC_SEGMENTS} segments joined by /, each "
                             f"1-{MAX_TOPIC_SEGMENT} characters of a-z, 0-9 and inner hyphens")
    return topic


def set_topic(conn: sqlite3.Connection, config: Config, conversation_id: str, topic: Any, *,
              actor: str) -> dict[str, Any]:
    """Set, rename or clear a conversation's topic. `None` clears it; the conversation's id never changes.

    Takes the same permission as managing participants, and membership of this conversation: a label on
    a conversation is the conversation's own business.
    """
    require_enabled(config)
    if not config.roles[actor].can_manage_participants:
        raise Forbidden(f"{actor} may not change a conversation's topic "
                        f"(roles.{actor}.can_manage_participants)")
    topic = check_topic(topic)
    with transaction(conn):
        before = _load(conn, conversation_id)["topic"]
        if actor not in _participants(conn, conversation_id):
            raise NotAParticipant(f"{actor} is not in {conversation_id}")
        if topic != before:
            conn.execute("UPDATE conversations SET topic = ? WHERE id = ?", (topic, conversation_id))
    return {"conversation_id": conversation_id, "topic": topic, "changed": topic != before}


def require_enabled(config: Config) -> Conversations:
    if not config.conversations.enabled:
        raise ConversationsDisabled("this host has group conversations turned off ([conversations] enabled)")
    return config.conversations


def create(conn: sqlite3.Connection, config: Config, *, actor: str, title: str, participants: list[str],
           topic: Any = None, request_id: str | None = None) -> dict[str, Any]:
    """Open a conversation among `participants`, who must include `actor`. A retry with the same request_id replays."""
    settings = require_enabled(config)
    if not config.roles[actor].can_create_conversations:
        raise Forbidden(f"{actor} may not create conversations (roles.{actor}.can_create_conversations)")
    request_id = request_id or messages.new_request_id()
    if not 1 <= len(request_id) <= 200 or not 1 <= len(title) <= MAX_TITLE:
        raise InvalidRequest(f"request_id is 1-200 characters and title 1-{MAX_TITLE}")
    topic = check_topic(topic)
    roles = _check_roles(config, participants)
    if not MIN_PARTICIPANTS <= len(roles) <= settings.max_participants:
        raise InvalidRequest(f"a conversation has {MIN_PARTICIPANTS}-{settings.max_participants} participants")
    if actor not in roles:
        raise InvalidRequest(f"the creator, {actor}, must be one of the participants")
    digest = events.request_digest("create_conversation", {"actor": actor, "title": title,
                                                           "participants": sorted(roles),
                                                           **({"topic": topic} if topic else {})})
    with transaction(conn):
        existing = events.replayed(conn, "conversations", request_id, digest)
        if existing is not None:
            return {**_summary(conn, existing), "request_id": request_id, "created": False}
        conversation_id = f"conv_{uuid.uuid4().hex[:16]}"
        stamp = events.now()
        conn.execute("INSERT INTO conversations (id, request_id, request_digest, title, created_by,"
                     " created_at, topic) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (conversation_id, request_id, digest, title, actor, stamp, topic))
        conn.executemany("INSERT INTO participants (conversation_id, role, added_at) VALUES (?, ?, ?)",
                         [(conversation_id, role, stamp) for role in roles])
        summary = _summary(conn, _load(conn, conversation_id))
    return {**summary, "request_id": request_id, "created": True}


def list_for(conn: sqlite3.Connection, role: str, topic: Any = None) -> list[dict[str, Any]]:
    """The conversations `role` takes part in, newest first, with their participants.

    `topic` narrows them to that topic and anything under it. It never widens what the role may see: a
    conversation it is not in stays out whether it has a topic or not.
    """
    where, values = _under(topic)
    rows = conn.execute("SELECT c.* FROM conversations c JOIN participants p ON p.conversation_id = c.id"
                        f" WHERE p.role = ?{where} ORDER BY c.created_at DESC LIMIT 200",
                        (role, *values)).fetchall()
    return [_summary(conn, row) for row in rows]


def list_all(conn: sqlite3.Connection, topic: Any = None) -> list[dict[str, Any]]:
    """Every conversation, newest first: only for a viewer the config lets see them all."""
    where, values = _under(topic)
    rows = conn.execute(f"SELECT * FROM conversations WHERE 1 = 1{where}"
                        " ORDER BY created_at DESC LIMIT 200", values).fetchall()
    return [_summary(conn, row) for row in rows]


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


def add(conn: sqlite3.Connection, config: Config, conversation_id: str, participant: str, *,
        actor: str) -> dict[str, Any]:
    """Add a participant and tell every participant, the new one included. Adding a present one changes nothing."""
    settings = _may_manage(config, actor, participant)
    with transaction(conn):
        _load(conn, conversation_id)
        current = _participants(conn, conversation_id)
        if participant not in current:
            if len(current) >= settings.max_participants:
                raise InvalidRequest(f"{conversation_id} already has the most participants allowed, "
                                     f"{settings.max_participants}")
            conn.execute("INSERT INTO participants (conversation_id, role, added_at) VALUES (?, ?, ?)",
                         (conversation_id, participant, events.now()))
            _fan_out(conn, conversation_id, [*current, participant], sender=None, text=f"{actor} added {participant}",
                     notice={"event": "participant_added", "participant": participant, "by": actor})
        participants = _participants(conn, conversation_id)
    return {"conversation_id": conversation_id, "participants": participants, "changed": participant not in current}


def remove(conn: sqlite3.Connection, config: Config, conversation_id: str, participant: str, *,
           actor: str) -> dict[str, Any]:
    """Remove a participant and tell the ones who remain; it receives nothing from then on."""
    _may_manage(config, actor, participant)
    with transaction(conn):
        _load(conn, conversation_id)
        current = _participants(conn, conversation_id)
        if participant in current:
            if len(current) <= MIN_PARTICIPANTS:
                raise InvalidRequest(f"a conversation keeps at least {MIN_PARTICIPANTS} participants")
            conn.execute("DELETE FROM participants WHERE conversation_id = ? AND role = ?",
                         (conversation_id, participant))
            _fan_out(conn, conversation_id, [role for role in current if role != participant], sender=None,
                     text=f"{actor} removed {participant}",
                     notice={"event": "participant_removed", "participant": participant, "by": actor})
        participants = _participants(conn, conversation_id)
    return {"conversation_id": conversation_id, "participants": participants, "changed": participant in current}


def send(conn: sqlite3.Connection, config: Config, *, sender: str, conversation_id: str, text: str,
         relayed: Any = None, request_id: str | None = None, job_id: str | None = None,
         reply_to: str | None = None, attention: Any = None, needs_decision: bool = False) -> dict[str, Any]:
    """Send to every other participant at once; `seqs` gives each recipient's seq. A retry replays.

    `reply_to` is the message_id of a message in this conversation that the
    sender can see. `attention` names the participants it asks to look, and
    `needs_decision` asks the conversation for one; both only draw attention,
    and a role woken only when addressed goes by them.

    Refused whole, so no recipient gets a copy, when the sender isn't a
    participant, a recipient's mailbox is full (`conversation_full`), or the
    conversation is over its hourly limit (`conversation_rate_limited`, which
    also wakes the escalation role, at most once an hour).
    """
    settings = require_enabled(config)
    request_id = request_id or messages.new_request_id()
    created_at = messages.request_time(request_id)
    relayed = messages.check(text, relayed)
    attention = _check_attention(config, attention)
    digest = events.request_digest("send", {"sender": sender, "conversation_id": conversation_id, "text": text,
                                            "job_id": job_id, **({"relayed": relayed} if relayed else {}),
                                            **({"reply_to": reply_to} if reply_to else {}),
                                            **({"attention": attention} if attention else {}),
                                            **({"needs_decision": True} if needs_decision else {})})
    with transaction(conn):
        existing = events.replayed(conn, "message_requests", request_id, digest)
        if existing is not None:
            return {"message_id": existing["message_id"], "conversation_id": conversation_id,
                    "seqs": json.loads(existing["seqs"]), "request_id": request_id, "created": False}
        conversation = _load(conn, conversation_id)
        participants = _participants(conn, conversation_id)
        if sender not in participants:
            raise NotAParticipant(f"{sender} is not in {conversation_id}")
        outside = [role for role in attention or () if role not in participants]
        if outside:
            raise NotAParticipant(f"attention names {outside[0]}, which is not in {conversation_id}; naming a "
                                  "role never adds it to a conversation")
        _check_reply_to(conn, conversation_id, sender, reply_to)
        if job_id is not None:
            from baton import jobs  # lazily: jobs imports messages, which this module imports

            jobs.require_exists(conn, job_id)
        limited = _sent_within_window(conn, conversation_id) >= settings.max_messages_per_hour
        if limited:
            _escalate(conn, settings, conversation, sender)
        else:
            message_id = messages.new_message_id()
            seqs = _fan_out(conn, conversation_id, [role for role in participants if role != sender], sender=sender,
                            text=text, relayed=relayed, job_id=job_id, message_id=message_id,
                            author_kind=config.author_kind(sender), reply_to=reply_to, attention=attention,
                            needs_decision=needs_decision)
            conn.execute("INSERT INTO message_requests (request_id, request_digest, message_id, seqs, created_at)"
                         " VALUES (?, ?, ?, ?, ?)", (request_id, digest, message_id, json.dumps(seqs),
                                                     created_at.isoformat()))
    # Raised after the transaction commits, so the escalation it recorded stays.
    if limited:
        raise ConversationRateLimited(f"{conversation_id} has had {settings.max_messages_per_hour} messages in the "
                                      "last hour; retry later")
    return {"message_id": message_id, "conversation_id": conversation_id, "seqs": seqs, "request_id": request_id,
            "created": True}


def history(conn: sqlite3.Connection, role: str, conversation_id: str, after_seq: int = 0,
            limit: int = 50) -> dict[str, Any]:
    """The conversation as `role` saw it, oldest first, after `after_seq`: what it received and what it sent.

    That is exactly the messages sent while it was a participant, so a role
    added later sees nothing from before its join. Only a current participant
    may read it. `seq` here is the conversation's order, the same for every
    participant. Separate from delivery: it marks nothing read and moves no
    cursor, so `read_messages` and `wait` still hand over each unread message
    once. A message is gone from history once retention deletes the reader's
    copy.
    """
    if not 1 <= limit <= messages.MAX_PAGE_MESSAGES or after_seq < 0:
        raise InvalidRequest(f"limit must be 1-{messages.MAX_PAGE_MESSAGES} and after_seq at least 0")
    with transaction(conn):
        _load(conn, conversation_id)
        if role not in _participants(conn, conversation_id):
            raise NotAParticipant(f"{role} is not in {conversation_id}")
        # The reader's copy of each message to it, and one copy of each it sent, which it holds none of.
        rows = conn.execute("SELECT * FROM messages WHERE conversation_id = ? AND conversation_seq > ?"
                            " AND (recipient = ? OR sender = ?) GROUP BY conversation_seq"
                            " ORDER BY conversation_seq LIMIT ?", (conversation_id, after_seq, role, role, limit)
                            ).fetchall()
        page = messages.as_page_for(conn, role, rows, _history_entry)
    return {"messages": page, "next_seq": page[-1]["seq"] if page else after_seq}


def full_history(conn: sqlite3.Connection, conversation_id: str, after_seq: int = 0,
                 limit: int = 50) -> dict[str, Any]:
    """Every message in the conversation, oldest first, after `after_seq`, as `history` shapes them.

    Only for a viewer the config lets see every conversation. Like `history`
    it marks nothing read and moves no cursor. A message is gone once
    retention has deleted every copy of it; a reply to such a message
    gives its parent's `seq` as None, as `history` does for a parent its
    reader can't see.
    """
    if not 1 <= limit <= messages.MAX_PAGE_MESSAGES or after_seq < 0:
        raise InvalidRequest(f"limit must be 1-{messages.MAX_PAGE_MESSAGES} and after_seq at least 0")
    with transaction(conn):
        _load(conn, conversation_id)
        rows = conn.execute("SELECT * FROM messages WHERE conversation_id = ? AND conversation_seq > ?"
                            " GROUP BY conversation_seq ORDER BY conversation_seq LIMIT ?",
                            (conversation_id, after_seq, limit)).fetchall()
        parents = {row["reply_to"] for row in rows if row["reply_to"]}
        kept = {found["id"]: found["conversation_seq"] for found in conn.execute(
            f"SELECT id, conversation_seq FROM messages WHERE conversation_id = ? AND id IN"
            f" ({', '.join('?' * len(parents))})", (conversation_id, *parents))} if parents else {}
    references = {parent: {"message_id": parent, "seq": kept.get(parent)} for parent in parents}
    page = messages.as_page(rows, lambda row: _history_entry(row, references.get(row["reply_to"])))
    return {"messages": page, "next_seq": page[-1]["seq"] if page else after_seq}


def _fan_out(conn: sqlite3.Connection, conversation_id: str, recipients: list[str], *, sender: str | None,
             text: str, relayed: list[dict] | None = None, notice: dict[str, str] | None = None,
             job_id: str | None = None, message_id: str | None = None, author_kind: str | None = None,
             reply_to: str | None = None, attention: list[str] | None = None,
             needs_decision: bool = False) -> dict[str, int]:
    """Store one copy for each recipient under one message_id, or refuse before storing any."""
    full = [role for role in recipients if messages.unread(conn, role) >= messages.MAX_UNREAD]
    if full:
        raise ConversationFull(f"{', '.join(full)} has {messages.MAX_UNREAD} unread messages, so no participant "
                               "got this; retry after it reads")
    message_id = message_id or messages.new_message_id()
    conn.execute("UPDATE conversations SET last_seq = last_seq + 1 WHERE id = ?", (conversation_id,))
    conversation_seq = _load(conn, conversation_id)["last_seq"]
    return {role: messages.store_copy(conn, message_id=message_id, sender=sender, recipient=role, text=text,
                                      job_id=job_id, conversation_id=conversation_id,
                                      conversation_seq=conversation_seq, relayed=relayed, notice=notice,
                                      author_kind=author_kind, reply_to=reply_to, attention=attention,
                                      needs_decision=needs_decision)
            for role in recipients}


def _sent_within_window(conn: sqlite3.Connection, conversation_id: str) -> int:
    """Messages participants sent to the conversation in the last hour; host notices don't count."""
    since = (datetime.now(UTC) - RATE_WINDOW).isoformat()
    return conn.execute("SELECT COUNT(DISTINCT id) FROM messages WHERE conversation_id = ? AND sender IS NOT NULL"
                        " AND at > ?", (conversation_id, since)).fetchone()[0]


def _escalate(conn: sqlite3.Connection, settings: Conversations, conversation: sqlite3.Row, sender: str) -> None:
    """Address one content-free event to the escalation role, unless one already went out within the window."""
    since = (datetime.now(UTC) - RATE_WINDOW).isoformat()
    if settings.escalation_role is None or (conversation["escalated_at"] or "") > since:
        return
    conn.execute("UPDATE conversations SET escalated_at = ? WHERE id = ?", (events.now(), conversation["id"]))
    events.append(conn, job_id=None, seq=0, actor=sender, audience=settings.escalation_role,
                  kind="conversation.rate_limited", summary=f"{conversation['id']} is over its hourly message limit",
                  data={"conversation_id": conversation["id"]})


def _may_manage(config: Config, actor: str, participant: str) -> Conversations:
    settings = require_enabled(config)
    if not config.roles[actor].can_manage_participants:
        raise Forbidden(f"{actor} may not manage participants (roles.{actor}.can_manage_participants)")
    _check_roles(config, [participant])
    return settings


def _check_roles(config: Config, roles: Any, field: str = "participants") -> list[str]:
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise InvalidRequest(f"{field} must be a list of role names")
    unknown = [role for role in roles if role not in config.roles]
    if unknown:
        raise UnknownRole(f"no role {unknown[0]!r}; call whoami for the list")
    if len(set(roles)) != len(roles):
        raise InvalidRequest(f"{field} lists a role twice")
    return roles


def _check_attention(config: Config, attention: Any) -> list[str] | None:
    """The participants a message asks to look at it: a structured list of roles, never @names in the text.

    Checked against the config here and against the conversation's current participants by `send`, so a
    message can neither single out a role that left nor reach one that was never in.
    """
    if attention is None:
        return None
    roles = _check_roles(config, attention, "attention")
    if not roles:
        raise InvalidRequest("attention must name at least one participant, or be left out")
    return roles


def _check_reply_to(conn: sqlite3.Connection, conversation_id: str, sender: str, reply_to: Any) -> None:
    """A reply answers a message of this conversation that the sender can see, and nothing else.

    Refusing a parent the sender cannot see keeps a reply from pointing at a message that was never
    the sender's to answer: one from before it joined, one from another conversation or none at all.
    """
    if reply_to is None:
        return
    seen = isinstance(reply_to, str) and conn.execute(
        "SELECT 1 FROM messages WHERE conversation_id = ? AND id = ? AND (recipient = ? OR sender = ?) LIMIT 1",
        (conversation_id, reply_to, sender, sender)).fetchone() is not None
    if not seen:
        raise InvalidRequest(f"reply_to must be the message_id of a message in {conversation_id} that you can "
                             "see; this host has no such message for you")


def _load(conn: sqlite3.Connection, conversation_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if row is None:
        raise UnknownConversation(conversation_id)
    return row


def _participants(conn: sqlite3.Connection, conversation_id: str) -> list[str]:
    rows = conn.execute("SELECT role FROM participants WHERE conversation_id = ? ORDER BY role", (conversation_id,))
    return [row["role"] for row in rows]


def _history_entry(row: sqlite3.Row, reply_to: dict[str, Any] | None = None) -> dict[str, Any]:
    """A message as history shows it: numbered in the conversation, not in any one mailbox."""
    return {**messages.as_dict(row, reply_to), "seq": row["conversation_seq"]}


def _summary(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    return {"conversation_id": row["id"], "title": row["title"], "topic": row["topic"],
            "created_by": row["created_by"], "created_at": row["created_at"],
            "participants": _participants(conn, row["id"])}
