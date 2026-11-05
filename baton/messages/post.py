"""The stable message shape read from Baton's persisted message rows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from baton.database import Row


@dataclass(frozen=True)
class Post:
    """A stored message, including the optional persisted guest attribution."""

    seq: int
    message_id: str
    sender: str | None
    author_kind: str | None
    conversation_id: str | None
    job_id: str | None
    text: str
    relayed: list[dict[str, str]] | None
    notice: dict[str, str] | None
    stored_reply_to: str | None
    attention: list[str] | None
    needs_decision: bool
    at: str
    alias_id: str | None = None
    alias: str | None = None
    alias_code: str | None = None

    @classmethod
    def from_row(cls, row: Row) -> Post:
        """Decode the persisted columns used by the message read model."""
        return cls(
            seq=row["seq"],
            message_id=row["id"],
            sender=row["sender"],
            author_kind=row["author_kind"],
            conversation_id=row["conversation_id"],
            job_id=row["job_id"],
            text=row["text"],
            relayed=json.loads(row["relayed"]) if row["relayed"] else None,
            notice=json.loads(row["notice"]) if row["notice"] else None,
            stored_reply_to=row["reply_to"],
            attention=json.loads(row["attention"]) if row["attention"] else None,
            needs_decision=bool(row["needs_decision"]),
            at=row["at"],
            alias_id=row["alias_id"],
            alias=row["alias"],
            alias_code=row["alias_code"],
        )

    def to_dict(self, reply_to: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render the established wire payload, with the caller's reader-specific reply view."""
        attribution = ({"alias_id": self.alias_id, "alias": self.alias,
                        "badge": "guest", "code": self.alias_code} if self.alias_id else {})
        return {
            **attribution,
            "seq": self.seq,
            "message_id": self.message_id,
            "sender": self.sender,
            "from_role": self.sender,
            "author_kind": self.author_kind,
            "conversation_id": self.conversation_id,
            "job_id": self.job_id,
            "text": self.text,
            "relayed": self.relayed,
            "notice": self.notice,
            "reply_to": reply_to,
            "attention": self.attention,
            "needs_decision": self.needs_decision,
            "at": self.at,
        }
