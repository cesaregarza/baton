"""The role an agent acts as, composed with permissions, an inbox and thread access."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from baton.config import Config
from baton.database import Connection
from baton.errors import Forbidden, InvalidRequest, UnknownRole

from .permissions import Permissions

if TYPE_CHECKING:
    from baton.messages.inbox import Inbox

    from .thread import Thread


@dataclass(frozen=True)
class Agent:
    """Access to Baton as one configured role, selected by the authenticating entrypoint.

    Human roles use the same access object; their posts retain human authorship.
    Configured capabilities belong to Permissions. Membership and grants are
    checked against the database by each operation, including retries.
    """

    connection: Connection
    config: Config
    role: str

    @property
    def permissions(self) -> Permissions:
        return Permissions.for_role(self.config, self.role)

    @property
    def inbox(self) -> Inbox:
        from baton.messages.inbox import Inbox

        return Inbox(self.connection, self.role, self.config)

    def thread(self, conversation_id: str) -> Thread:
        """Bind thread operations to this agent's role; access is checked on use."""
        from .thread import Thread

        return Thread(self, conversation_id)

    def create_thread(
        self,
        title: str,
        *,
        participants: list[str] | None = None,
        topic: str | None = None,
        request_id: str | None = None,
        identity: dict[str, str] | None = None,
        seen: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from baton.conversations import Conversation

        return Conversation.create(
            self.connection,
            self.config,
            actor=self.role,
            title=title,
            participants=participants,
            topic=topic,
            request_id=request_id,
            identity=identity,
            seen=seen,
        )

    def send(
        self,
        text: str,
        *,
        to: str | None = None,
        conversation_id: str | None = None,
        job_id: str | None = None,
        relayed: Any = None,
        reply_to: str | None = None,
        attention: Any = None,
        needs_decision: bool = False,
        request_id: str | None = None,
        identity: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send as this role, checking permission, destination and authorship."""
        from baton.messages.content import only_in_a_conversation
        from baton.messages.direct import DirectMessage

        if (to is None) == (conversation_id is None):
            raise InvalidRequest("send_message takes exactly one of to and conversation_id")
        permissions = self.permissions
        refusal = permissions.message_refusal(
            to=to, conversation_id=conversation_id, job_id=job_id, needs_decision=needs_decision
        )
        if refusal is not None:
            raise Forbidden(refusal)
        if conversation_id is not None:
            from baton.conversations import Conversation

            return Conversation(self.connection, conversation_id).send(
                self.config,
                sender=self.role,
                text=text,
                relayed=relayed,
                request_id=request_id,
                job_id=job_id,
                reply_to=reply_to,
                attention=attention,
                needs_decision=needs_decision,
                identity=identity,
            )
        if to not in self.config.roles:
            raise UnknownRole(f"no role {to!r}; call whoami for the list")
        only_in_a_conversation(reply_to=reply_to, attention=attention, needs_decision=needs_decision)
        message = DirectMessage(
            sender=self.role,
            recipient=to,
            text=text,
            request_id=request_id,
            job_id=job_id,
            relayed=relayed,
            author_kind=permissions.author_kind,
        )
        return message.send(self.connection)
