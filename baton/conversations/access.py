"""Conversation permissions, role checks, and guest admission."""

from __future__ import annotations

from typing import Any

from baton import board_read
from baton.agents.permissions import Capability, Permissions
from baton.config import Config, Conversations
from baton.conversations import queries
from baton.database import Connection, Row
from baton.errors import ConversationsDisabled, Forbidden, InvalidRequest, NotAParticipant, UnknownRole
from baton.topics import grants as topic_grants
from baton.topics.paths import check_topic as check_topic


def _authorize_move(
    conn: Connection,
    actor: str,
    conversation_id: str,
    before: str | None,
    after: str | None,
    identity: dict[str, str] | None,
    grant_key: str | None,
) -> Row | None:
    """A core role must be in the thread. A guest needs the grant its key opens to cover both ends of the move.

    Both ends, because the source is as much a claim as the destination: a grant on the destination alone would let
    its holder pull any thread in, or one on the source alone push threads out of its reach. A thread with no
    topic, or a label with no topic row, covers nothing.
    """
    if grant_key is None:
        if actor not in queries._participants(conn, conversation_id):
            raise NotAParticipant(f"{actor} is not in {conversation_id}")
        return None
    grant = topic_grants.grant_for_use(conn, identity, grant_key)
    # The refusal does not name the source: the caller never supplied it and may not be able to read the thread.
    topic_grants.require_covers(conn, grant, "thread.organise", before, shown="this thread's current topic")
    topic_grants.require_covers(conn, grant, "thread.organise", after)
    return grant


def _admit_guest_thread(config: Config, identity: dict[str, str] | None) -> None:
    """A guest needs a live alias binding, and a guest role that already reads the board.

    The read requirement is what keeps this from widening anything: starting a thread never grants a
    guest sight of a conversation it could not already see.
    """
    if not identity or not identity.get("alias_id"):
        raise Forbidden("alias binding is missing, invalid or expired")
    if not Permissions.for_role(config, "guest").allows(Capability.VIEW_ALL_CONVERSATIONS):
        raise Forbidden(
            "this host's guests do not read board conversations "
            "(roles.guest.can_view_all_conversations), so they cannot start a thread either"
        )


def _may_manage(config: Config, actor: str, participant: str, *, allow_guest: bool = False) -> Conversations:
    settings = require_enabled(config)
    Permissions.for_role(config, actor).require(
        Capability.MANAGE_PARTICIPANTS,
        f"{actor} may not manage participants (roles.{actor}.can_manage_participants)",
    )
    _check_roles(config, [participant], allow_guest=allow_guest)
    return settings


def _check_roles(config: Config, roles: Any, field: str = "participants", *, allow_guest: bool = False) -> list[str]:
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise InvalidRequest(f"{field} must be a list of role names")
    unknown = [role for role in roles if role not in config.roles]
    if unknown:
        raise UnknownRole(f"no role {unknown[0]!r}; call whoami for the list")
    if "guest" in roles and not allow_guest:
        raise InvalidRequest("guest cannot be a conversation participant")
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


def _check_reply_to(conn: Connection, config: Config, conversation_id: str, sender: str, reply_to: Any) -> None:
    """A reply answers a message of this conversation that the sender can see, and nothing else.

    Refusing a parent the sender cannot see keeps a reply from pointing at a message that was never
    the sender's to answer: one from before it joined, one from another conversation or none at all.
    """
    if reply_to is None:
        return
    condition, values = board_read.message_condition(config, sender)
    seen = (
        isinstance(reply_to, str)
        and conn.execute(
            f"SELECT 1 FROM messages m WHERE m.conversation_id = :conversation_id AND m.id = :reply_to "
            f"AND {condition} LIMIT 1",
            {**values, "conversation_id": conversation_id, "reply_to": reply_to},
        ).fetchone()
        is not None
    )
    if not seen:
        raise InvalidRequest(
            f"reply_to must be the message_id of a message in {conversation_id} that you can "
            "see; this host has no such message for you"
        )


def require_enabled(config: Config) -> Conversations:
    if not config.conversations.enabled:
        raise ConversationsDisabled("this host has group conversations turned off ([conversations] enabled)")
    return config.conversations
