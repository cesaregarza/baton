"""Thread access bound to the agent reading or changing it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from baton.conversations import Conversation, require_enabled

if TYPE_CHECKING:
    from .agent import Agent


@dataclass(frozen=True)
class Thread:
    agent: Agent
    id: str

    def read(self, after_seq: int = 0, limit: int = 50, *, before_seq: int | None = None) -> dict[str, Any]:
        """Read retained history visible to this agent without advancing its inbox."""
        require_enabled(self.agent.config)
        return Conversation(self.agent.connection, self.id).history(
            self.agent.role, after_seq, limit, before_seq=before_seq, config=self.agent.config
        )

    def read_context(
        self,
        *,
        producer: str | None = None,
        limit: int = 50,
        after_seq: int | None = None,
        through_seq: int | None = None,
    ) -> dict[str, Any]:
        """Open with an authorized cumulative summary and raw tail, or continue that fixed tail."""
        from baton.conversations.context import read_context

        return read_context(
            self.agent.connection,
            self.agent.config,
            self.agent.role,
            self.id,
            producer=producer,
            limit=limit,
            after_seq=after_seq,
            through_seq=through_seq,
        )

    def send(
        self,
        text: str,
        *,
        job_id: str | None = None,
        relayed: Any = None,
        reply_to: str | None = None,
        attention: Any = None,
        needs_decision: bool = False,
        request_id: str | None = None,
        identity: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self.agent.send(
            text,
            conversation_id=self.id,
            job_id=job_id,
            relayed=relayed,
            reply_to=reply_to,
            attention=attention,
            needs_decision=needs_decision,
            request_id=request_id,
            identity=identity,
        )

    def add_participant(self, role: str) -> dict[str, Any]:
        return Conversation(self.agent.connection, self.id).add(self.agent.config, role, actor=self.agent.role)

    def remove_participant(self, role: str) -> dict[str, Any]:
        return Conversation(self.agent.connection, self.id).remove(self.agent.config, role, actor=self.agent.role)

    def set_topic(
        self,
        topic: str | None,
        *,
        identity: dict[str, str] | None = None,
        seen: dict[str, Any] | None = None,
        grant_key: str | None = None,
    ) -> dict[str, Any]:
        return Conversation(self.agent.connection, self.id).set_topic(
            self.agent.config, topic, actor=self.agent.role, identity=identity, seen=seen, grant_key=grant_key
        )
