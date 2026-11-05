"""An agent or person acting as a role on an already authenticated connection.

Authentication belongs to the entrypoint. A Participant binds the resulting role
to its outgoing messages and inbox; constructing one is not proof of identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from baton import messages
from baton.config import Config
from baton.conversation import Conversation
from baton.database import Connection
from baton.errors import InvalidRequest, UnknownRole
from baton.inbox import Inbox
from baton.messages import DirectMessage


@dataclass(frozen=True)
class Participant:
    """The role an agent or person acts as, with its messaging behavior."""

    connection: Connection
    config: Config
    role: str

    @property
    def inbox(self) -> Inbox:
        return Inbox(self.connection, self.role, self.config)

    def send(self, text: str, *, to: str | None = None, conversation_id: str | None = None,
             job_id: str | None = None, relayed: Any = None, reply_to: str | None = None,
             attention: Any = None, needs_decision: bool = False, request_id: str | None = None,
             identity: dict[str, str] | None = None) -> dict[str, Any]:
        """Send as this role, deriving authorship and checking the destination.

        Exactly one destination is required. Conversation-only fields never leak
        into a direct message. A guest alias's resolved identity is per call.
        """
        if (to is None) == (conversation_id is None):
            raise InvalidRequest("send_message takes exactly one of to and conversation_id")
        if conversation_id is not None:
            return Conversation(self.connection, conversation_id).send(
                self.config, sender=self.role, text=text, relayed=relayed, request_id=request_id,
                job_id=job_id, reply_to=reply_to, attention=attention, needs_decision=needs_decision,
                identity=identity)
        if to not in self.config.roles:
            raise UnknownRole(f"no role {to!r}; call whoami for the list")
        messages.only_in_a_conversation(reply_to=reply_to, attention=attention, needs_decision=needs_decision)
        message = DirectMessage(sender=self.role, recipient=to, text=text, request_id=request_id, job_id=job_id,
                                relayed=relayed, author_kind=self.config.author_kind(self.role))
        return message.send(self.connection)
