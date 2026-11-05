"""Behavior-owning object for one group conversation.

The plural :mod:`baton.conversations` module retains listing, shared query helpers,
and compatibility functions for the existing public API.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from baton import board_read, changes, conversations, events, messages, topics
from baton.config import Config
from baton.database import Connection
from baton.errors import (
    ConversationRateLimited,
    Forbidden,
    InvalidRequest,
    NotAParticipant,
)
from baton.topic_paths import check_topic


class Conversation:
    """Operations and history for one conversation, bound to a live connection."""

    def __init__(self, connection: Connection, id: str) -> None:
        self.connection = connection
        self.id = id

    def set_topic(
        self,
        config: Config,
        topic: Any,
        *,
        actor: str,
        identity: dict[str, str] | None = None,
        seen: dict[str, Any] | None = None,
        grant_key: str | None = None,
    ) -> dict[str, Any]:
        """Set, rename or clear a conversation's topic. `None` clears it; the conversation's id never changes.

        A core role needs the permission to manage participants, and membership of this conversation: a label on
        a conversation is the conversation's own business. A guest holding `grant_key` needs neither: it may move a
        thread whose topic and destination its grant covers, and the thread's participants are told. A move that
        changes the topic is recorded in the topic history; one that changes nothing is not.
        """
        conn = self.connection
        conversation_id = self.id
        conversations.require_enabled(config)
        if grant_key is None and not config.roles[actor].can_manage_participants:
            raise Forbidden(f"{actor} may not change a conversation's topic (roles.{actor}.can_manage_participants)")
        topic = check_topic(topic)
        with conn.transaction():
            before = conversations._load(conn, conversation_id)["topic"]
            grant = conversations._authorize_move(conn, actor, conversation_id, before, topic, identity, grant_key)
            grant_id = None if grant is None else grant["id"]
            if topic is not None:
                # A topic is a container now, so a conversation cannot be filed somewhere that isn't one.
                # Clearing it to None stays allowed, and moving a thread never changes its participants.
                topics.record_seen(seen, topic, topics.require_exists(conn, topic), grant_id=grant_id)
            if topic != before:
                conn.execute("UPDATE conversations SET topic = ? WHERE id = ?", (topic, conversation_id))
                who = topics.history_actor(actor, identity)
                topics.record_move(
                    conn, conversation_id=conversation_id, actor=who, before=before, after=topic, grant_id=grant_id
                )
                if grant is not None:
                    conversations._tell_of_move(conn, conversation_id, grant, identity or {}, who, before, topic)
        return {"conversation_id": conversation_id, "topic": topic, "changed": topic != before}

    @staticmethod
    def create(
        conn: Connection,
        config: Config,
        *,
        actor: str,
        title: str,
        participants: list[str] | None = None,
        topic: Any = None,
        request_id: str | None = None,
        identity: dict[str, str] | None = None,
        seen: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Open a conversation. A retry with the same request_id replays rather than opening a second one.

        A role names its own `participants`, which must include it. A guest instead presents an alias
        binding, which the guard resolves into `identity`, and names a topic: the thread goes to that
        topic's members, so a guest decides what to start and the topic decides who hears it. The guest
        path is the only one that takes an identity, and `actor` is already the effective role, so an
        enrolled alias mapped to a role comes through here as that role and takes the role's path.
        """
        settings = conversations.require_enabled(config)
        guest = actor == "guest"
        if guest:
            conversations._admit_guest_thread(config, identity)
        elif not config.roles[actor].can_create_conversations:
            raise Forbidden(f"{actor} may not create conversations (roles.{actor}.can_create_conversations)")
        request_id = request_id or messages.new_request_id()
        if not 1 <= len(request_id) <= 200 or not 1 <= len(title) <= conversations.MAX_TITLE:
            raise InvalidRequest(f"request_id is 1-200 characters and title 1-{conversations.MAX_TITLE}")
        topic = check_topic(topic)
        if guest:
            if participants:
                raise InvalidRequest("a guest does not name participants: the topic's members receive its thread")
            if topic is None:
                raise InvalidRequest("a guest's thread needs a topic to go in; list_topics shows them")
        # The guest digest is deliberately alias-and-topic only: members are derived and may change, and a
        # replay has to match after a member edit or a rename.
        digest = events.request_digest(
            "create_conversation",
            {
                "actor": actor,
                "title": title,
                **({"topic": topic} if topic else {}),
                **(
                    {"alias_id": identity["alias_id"]}
                    if guest
                    else {"participants": sorted(conversations._check_roles(config, participants))}
                ),
            },
        )
        refusal = None
        with conn.transaction():
            existing = events.replayed(conn, "conversations", request_id, digest)
            if existing is not None:
                topics.note_replay(conn, seen, topic, existing["topic"])
                return {**conversations._summary(conn, existing), "request_id": request_id, "created": False}
            topics.note_path(conn, seen, topic)
            if guest:
                roles = topics.members_for_thread(conn, config, topic)
                # Returned rather than raised: when the guest-wide cap is full this also stores the alert,
                # which has to commit with the refusal rather than be rolled back by it.
                refusal = conversations._thread_cap_refusal(conn, config, identity)
            else:
                roles = conversations._check_roles(config, participants)
                if not conversations.MIN_PARTICIPANTS <= len(roles) <= settings.max_participants:
                    raise InvalidRequest(
                        f"a conversation has {conversations.MIN_PARTICIPANTS}-{settings.max_participants} participants"
                    )
                if actor not in roles:
                    raise InvalidRequest(f"the creator, {actor}, must be one of the participants")
                if topic is not None:
                    topics.require_exists(conn, topic)
            if refusal is None:
                conversation_id = f"conv_{uuid.uuid4().hex[:16]}"
                stamp = events.now()
                conn.execute(
                    "INSERT INTO conversations (id, request_id, request_digest, title, created_by,"
                    " created_at, topic, created_by_alias_id, created_by_alias, created_by_alias_code)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        conversation_id,
                        request_id,
                        digest,
                        title,
                        actor,
                        stamp,
                        topic,
                        *((identity["alias_id"], identity["alias"], identity["code"]) if guest else (None, None, None)),
                    ),
                )
                conn.executemany(
                    "INSERT INTO participants (conversation_id, role, added_at) VALUES (?, ?, ?)",
                    [(conversation_id, role, stamp) for role in roles],
                )
                summary = conversations._summary(conn, conversations._load(conn, conversation_id))
        if refusal is not None:
            raise refusal
        return {**summary, "request_id": request_id, "created": True}

    def add(self, config: Config, participant: str, *, actor: str) -> dict[str, Any]:
        """Add a participant and tell every participant, the new one included. Adding a present one changes nothing."""
        conn = self.connection
        conversation_id = self.id
        settings = conversations._may_manage(config, actor, participant)
        with conn.transaction():
            conversations._load(conn, conversation_id)
            current = conversations._participants(conn, conversation_id)
            if participant not in current:
                if len(current) >= settings.max_participants:
                    raise InvalidRequest(
                        f"{conversation_id} already has the most participants allowed, {settings.max_participants}"
                    )
                conn.execute(
                    "INSERT INTO participants (conversation_id, role, added_at) VALUES (?, ?, ?)",
                    (conversation_id, participant, events.now()),
                )
                changes.record(conn, conversation_id, "participant_added", role=participant)
                conversations._fan_out(
                    conn,
                    conversation_id,
                    [*current, participant],
                    sender=None,
                    text=f"{actor} added {participant}",
                    notice={"event": "participant_added", "participant": participant, "by": actor},
                )
            participants = conversations._participants(conn, conversation_id)
        return {"conversation_id": conversation_id, "participants": participants, "changed": participant not in current}

    def remove(self, config: Config, participant: str, *, actor: str) -> dict[str, Any]:
        """Remove a participant and tell the ones who remain; it receives nothing from then on."""
        conn = self.connection
        conversation_id = self.id
        conversations._may_manage(config, actor, participant, allow_guest=True)
        with conn.transaction():
            conversations._load(conn, conversation_id)
            current = conversations._participants(conn, conversation_id)
            if participant in current:
                if len(current) <= conversations.MIN_PARTICIPANTS:
                    raise InvalidRequest(f"a conversation keeps at least {conversations.MIN_PARTICIPANTS} participants")
                conn.execute(
                    "DELETE FROM participants WHERE conversation_id = ? AND role = ?", (conversation_id, participant)
                )
                changes.record(conn, conversation_id, "participant_removed", role=participant)
                conversations._fan_out(
                    conn,
                    conversation_id,
                    [role for role in current if role != participant],
                    sender=None,
                    text=f"{actor} removed {participant}",
                    notice={"event": "participant_removed", "participant": participant, "by": actor},
                )
            participants = conversations._participants(conn, conversation_id)
        return {"conversation_id": conversation_id, "participants": participants, "changed": participant in current}

    def send(
        self,
        config: Config,
        *,
        sender: str,
        text: str,
        relayed: Any = None,
        request_id: str | None = None,
        job_id: str | None = None,
        reply_to: str | None = None,
        attention: Any = None,
        needs_decision: bool = False,
        identity: dict[str, str] | None = None,
    ) -> dict[str, Any]:
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
        conn = self.connection
        conversation_id = self.id
        settings = conversations.require_enabled(config)
        guest = sender == "guest"
        if guest and (identity is None or job_id is not None or needs_decision):
            raise Forbidden("guest posting requires alias continuity and cannot carry jobs or decisions")
        request_id = request_id or messages.new_request_id()
        created_at = messages.request_time(request_id)
        relayed = messages.check(text, relayed)
        attention = conversations._check_attention(config, attention)
        digest = events.request_digest(
            "send",
            {
                "sender": sender,
                "conversation_id": conversation_id,
                "text": text,
                **({"alias_id": identity["alias_id"]} if guest else {}),
                "job_id": job_id,
                **({"relayed": relayed} if relayed else {}),
                **({"reply_to": reply_to} if reply_to else {}),
                **({"attention": attention} if attention else {}),
                **({"needs_decision": True} if needs_decision else {}),
            },
        )
        with conn.transaction():
            # Current authorization first, so a retry never returns a cached result to a sender who lost it.
            conversation = conversations._load(conn, conversation_id)
            participants = conversations._participants(conn, conversation_id)
            if guest and not board_read.may_read_conversation(conn, config, sender, conversation_id):
                raise Forbidden("guest may post only to a readable board conversation")
            if not guest and sender not in participants:
                raise NotAParticipant(f"{sender} is not in {conversation_id}")
            existing = events.replayed(conn, "message_requests", request_id, digest)
            if existing is not None:
                return {
                    "message_id": existing["message_id"],
                    "conversation_id": conversation_id,
                    "seqs": json.loads(existing["seqs"]),
                    "request_id": request_id,
                    "created": False,
                }
            recipients = [role for role in participants if role not in (sender, "guest")]
            if not recipients:
                raise InvalidRequest("conversation has no eligible delivery recipients; no message was stored")
            outside = [role for role in attention or () if role not in participants]
            if outside:
                raise NotAParticipant(
                    f"attention names {outside[0]}, which is not in {conversation_id}; naming a "
                    "role never adds it to a conversation"
                )
            conversations._check_reply_to(conn, config, conversation_id, sender, reply_to)
            if job_id is not None:
                from baton import jobs  # lazily: jobs imports messages, which this module imports

                jobs.require_exists(conn, job_id)
            limited = conversations._sent_within_window(conn, conversation_id) >= settings.max_messages_per_hour
            if limited and not guest:
                conversations._escalate(conn, settings, conversation, sender)
            if not limited:
                message_id = messages.new_message_id()
                seqs = conversations._fan_out(
                    conn,
                    conversation_id,
                    recipients,
                    sender=sender,
                    text=text,
                    relayed=relayed,
                    job_id=job_id,
                    message_id=message_id,
                    author_kind="agent" if guest else config.author_kind(sender),
                    reply_to=reply_to,
                    attention=attention,
                    needs_decision=needs_decision,
                    identity=identity if guest else None,
                )
                conn.execute(
                    "INSERT INTO message_requests (request_id, request_digest, message_id, seqs, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (request_id, digest, message_id, json.dumps(seqs), created_at.isoformat()),
                )
        # Raised after the transaction commits, so the escalation it recorded stays.
        if limited:
            raise ConversationRateLimited(
                f"{conversation_id} has had {settings.max_messages_per_hour} messages in the last hour; retry later"
            )
        return {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "seqs": seqs,
            "request_id": request_id,
            "created": True,
        }

    def history(
        self,
        role: str,
        after_seq: int = 0,
        limit: int = 50,
        before_seq: int | None = None,
        config: Config | None = None,
    ) -> dict[str, Any]:
        """A readable conversation page, oldest first, with retained gaps reported explicitly.

        Non-view-all roles see retained copies while they are participants. A role with
        ``can_view_all_conversations`` sees retained history from before it joined. Reading
        marks nothing read and moves no cursor. ``before_seq`` reads backwards, oldest first.
        """
        conn = self.connection
        conversation_id = self.id
        if not 1 <= limit <= messages.MAX_PAGE_MESSAGES or after_seq < 0 or (before_seq is not None and before_seq < 1):
            raise InvalidRequest(
                f"limit must be 1-{messages.MAX_PAGE_MESSAGES}, after_seq at least 0 and before_seq at least 1"
            )
        with conn.transaction():
            conversation = conversations._load(conn, conversation_id)
            participant = conn.execute(
                "SELECT added_at FROM participants WHERE conversation_id = ? AND role = ?", (conversation_id, role)
            ).fetchone()
            view_all = config is not None and board_read.sees_all(config, role)
            if config is None:
                allowed = participant is not None
                condition = (
                    "(m.recipient = :reader OR m.sender = :reader) AND EXISTS "
                    "(SELECT 1 FROM participants p WHERE p.conversation_id = m.conversation_id "
                    "AND p.role = :reader)"
                )
                values = {"reader": role}
            else:
                allowed = view_all or participant is not None
                condition, values = board_read.message_condition(config, role)
            if not allowed:
                raise NotAParticipant(f"{role} is not in {conversation_id}")
            backwards = before_seq is not None
            upper_seq = min(conversation["last_seq"], before_seq - 1) if backwards else conversation["last_seq"]
            lower_seq = after_seq + 1
            access_start = conversations._access_start_seq(
                conn, conversation_id, role, participant, conversation, view_all
            )
            known_lower = max(lower_seq, access_start) if access_start is not None else None
            rows = conn.execute(
                f"SELECT m.* FROM messages m WHERE m.conversation_id = :conversation_id "
                "AND m.conversation_seq > :after_seq AND m.conversation_seq < :before_seq "
                f"AND {condition} GROUP BY m.conversation_seq "
                f"ORDER BY m.conversation_seq {'DESC' if backwards else ''} LIMIT :limit",
                {
                    **values,
                    "conversation_id": conversation_id,
                    "after_seq": after_seq,
                    "before_seq": before_seq if backwards else 2**62,
                    "limit": limit,
                },
            ).fetchall()
            page = messages.as_page_for(conn, role, rows, conversations._history_entry, config=config)
            if backwards:
                page.reverse()
            seqs = [item["seq"] for item in page]
            if seqs:
                older = known_lower is not None and conversations._has_readable_seq(
                    conn, condition, values, conversation_id, known_lower, seqs[0] - 1
                )
                newer = conversations._has_readable_seq(
                    conn, condition, values, conversation_id, seqs[-1] + 1, upper_seq
                )
                gaps = conversations._retention_gaps(
                    page,
                    known_lower if known_lower is not None and not older else None,
                    upper_seq if not newer else None,
                    access_start,
                    participant["added_at"] if participant is not None else None,
                )
            elif known_lower is not None and upper_seq >= known_lower:
                gaps = [{"from_seq": known_lower, "to_seq": upper_seq}]
            else:
                gaps = []
        next_seq = page[-1]["seq"] if page else (max(after_seq, upper_seq) if not backwards else after_seq)
        return {
            "messages": page,
            "next_seq": next_seq,
            "prev_seq": page[0]["seq"] if page else (before_seq if backwards else after_seq),
            "retention_gaps": gaps,
        }
