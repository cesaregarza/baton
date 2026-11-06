"""Compatibility adapters for the established function API."""

from __future__ import annotations

from typing import Any

from baton.config import Config
from baton.database import Connection


def set_topic(
    conn: Connection,
    config: Config,
    conversation_id: str,
    topic: Any,
    *,
    actor: str,
    identity: dict[str, str] | None = None,
    seen: dict[str, Any] | None = None,
    grant_key: str | None = None,
) -> dict[str, Any]:
    from baton.conversations.conversation import Conversation

    return Conversation(conn, conversation_id).set_topic(
        config, topic, actor=actor, identity=identity, seen=seen, grant_key=grant_key
    )


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
    from baton.conversations.conversation import Conversation

    return Conversation.create(
        conn,
        config,
        actor=actor,
        title=title,
        participants=participants,
        topic=topic,
        request_id=request_id,
        identity=identity,
        seen=seen,
    )


def add(conn: Connection, config: Config, conversation_id: str, participant: str, *, actor: str) -> dict[str, Any]:
    from baton.conversations.conversation import Conversation

    return Conversation(conn, conversation_id).add(config, participant, actor=actor)


def remove(conn: Connection, config: Config, conversation_id: str, participant: str, *, actor: str) -> dict[str, Any]:
    from baton.conversations.conversation import Conversation

    return Conversation(conn, conversation_id).remove(config, participant, actor=actor)


def send(
    conn: Connection,
    config: Config,
    *,
    sender: str,
    conversation_id: str,
    text: str,
    relayed: Any = None,
    request_id: str | None = None,
    job_id: str | None = None,
    reply_to: str | None = None,
    attention: Any = None,
    needs_decision: bool = False,
    identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    from baton.conversations.conversation import Conversation

    return Conversation(conn, conversation_id).send(
        config,
        sender=sender,
        text=text,
        relayed=relayed,
        request_id=request_id,
        job_id=job_id,
        reply_to=reply_to,
        attention=attention,
        needs_decision=needs_decision,
        identity=identity,
    )


def history(
    conn: Connection,
    role: str,
    conversation_id: str,
    after_seq: int = 0,
    limit: int = 50,
    before_seq: int | None = None,
    config: Config | None = None,
) -> dict[str, Any]:
    from baton.conversations.conversation import Conversation

    return Conversation(conn, conversation_id).history(
        role, after_seq=after_seq, limit=limit, before_seq=before_seq, config=config
    )
