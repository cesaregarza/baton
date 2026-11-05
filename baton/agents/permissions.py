"""Configured role capabilities and the message/tool permission rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from baton.config import Config
from baton.errors import Forbidden


class Capability(StrEnum):
    CREATE_CONVERSATIONS = "create_conversations"
    MANAGE_PARTICIPANTS = "manage_participants"
    CREATE_TOPICS = "create_topics"
    VIEW_ALL_CONVERSATIONS = "view_all_conversations"
    MUTATE_JOBS = "mutate_jobs"


@dataclass(frozen=True)
class Permissions:
    """A value snapshot of one role's configured capabilities."""

    role: str
    capabilities: frozenset[Capability]
    _author_kind: str = "agent"

    @classmethod
    def for_role(cls, config: Config, name: str) -> Permissions:
        entry = config.roles.get(name)
        if entry is None:
            return cls(name, frozenset())
        capabilities = frozenset(
            capability
            for capability, enabled in (
                (Capability.CREATE_CONVERSATIONS, entry.can_create_conversations),
                (Capability.MANAGE_PARTICIPANTS, entry.can_manage_participants),
                (Capability.CREATE_TOPICS, entry.can_create_topics),
                (Capability.VIEW_ALL_CONVERSATIONS, entry.can_view_all_conversations),
                (Capability.MUTATE_JOBS, entry.job_authority),
            )
            if enabled
        )
        return cls(name, capabilities, "human" if entry.human else "agent")

    def allows(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def require(self, capability: Capability, detail: str) -> None:
        if not self.allows(capability):
            raise Forbidden(detail)

    @property
    def is_guest(self) -> bool:
        return self.role == "guest"

    @property
    def author_kind(self) -> str:
        return self._author_kind

    def message_refusal(
        self,
        *,
        to: Any = None,
        conversation_id: Any = None,
        job_id: Any = None,
        needs_decision: Any = False,
    ) -> str | None:
        """Return the existing send-message refusal for this role, if any."""
        if self.is_guest and (conversation_id is None or to is not None or job_id is not None or needs_decision):
            return "guest posts require a conversation, without jobs or decisions"
        if not self.is_guest and to == "guest":
            return "direct messages to guest are not allowed"
        return None

    def tool_refusal(
        self,
        name: str,
        *,
        guest_access: str,
        requires_job_authority: bool,
        has_binding: bool,
        has_grant: bool,
        arguments: dict[str, Any],
    ) -> str | None:
        """Transport-boundary refusal using the tool catalog's primitive policy values."""
        if self.is_guest:
            if guest_access == "no" or (guest_access == "grant" and not has_grant):
                return f"guest may not call {name}"
            if guest_access == "binding" and not has_binding:
                return "alias binding is missing, invalid or expired"
            if name == "search" and (
                arguments.get("producer") is not None
                or arguments.get("query_vector") is not None
                or arguments.get("model") is not None
                or (
                    arguments.get("kinds") is not None
                    and (
                        not isinstance(arguments["kinds"], list)
                        or not all(kind == "message" for kind in arguments["kinds"])
                    )
                )
            ):
                return "guest search is limited to messages"
            if name == "send_message":
                refusal = self.message_refusal(
                    to=arguments.get("to"),
                    conversation_id=arguments.get("conversation_id"),
                    job_id=arguments.get("job_id"),
                    needs_decision=arguments.get("needs_decision"),
                )
                if refusal is not None:
                    return refusal
        if requires_job_authority and not self.allows(Capability.MUTATE_JOBS):
            return f"role {self.role} may not mutate jobs"
        if name == "send_message" and not self.is_guest:
            refusal = self.message_refusal(
                to=arguments.get("to"),
                conversation_id=arguments.get("conversation_id"),
                job_id=arguments.get("job_id"),
                needs_decision=arguments.get("needs_decision"),
            )
            if refusal is not None:
                return refusal
        return None
