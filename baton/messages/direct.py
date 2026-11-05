"""One direct-message submission, including its retry identity and atomic delivery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from baton import events
from baton.database import Connection
from baton.errors import MailboxFull

from . import limits
from .content import check, new_request_id, only_in_a_conversation, request_time
from .mailbox import new_message_id, store_copy, unread


@dataclass(frozen=True)
class DirectMessage:
    """A direct message to persist for its recipient."""

    sender: str
    recipient: str
    text: str
    request_id: str | None = None
    job_id: str | None = None
    relayed: Any = None
    author_kind: str = "agent"

    def __post_init__(self) -> None:
        # A message object represents one submission, including when its caller retries send().
        if not self.request_id:
            object.__setattr__(self, "request_id", new_request_id())

    def send(self, conn: Connection) -> dict[str, Any]:
        """Store this message as the recipient's next seq; retries are safe."""
        request_id = self.request_id
        created_at = request_time(request_id)
        relayed = check(self.text, self.relayed)
        # `relayed` joins the digest only when present, so older requests still replay.
        digest = events.request_digest(
            "send",
            {
                "sender": self.sender,
                "recipient": self.recipient,
                "text": self.text,
                "job_id": self.job_id,
                **({"relayed": relayed} if relayed else {}),
            },
        )
        with conn.transaction():
            existing = events.replayed(conn, "message_requests", request_id, digest)
            if existing is not None:
                return {
                    "message_id": existing["message_id"],
                    "seq": existing["seq"],
                    "request_id": request_id,
                    "created": False,
                }
            if self.job_id is not None:
                from baton import jobs  # lazily: jobs imports this module

                jobs.require_exists(conn, self.job_id)
            if unread(conn, self.recipient) >= limits.MAX_UNREAD:
                raise MailboxFull(f"{self.recipient} has {limits.MAX_UNREAD} unread messages; retry after it reads")
            message_id = new_message_id()
            seq = store_copy(
                conn,
                message_id=message_id,
                sender=self.sender,
                recipient=self.recipient,
                text=self.text,
                job_id=self.job_id,
                relayed=relayed,
                author_kind=self.author_kind,
            )
            conn.execute(
                "INSERT INTO message_requests (request_id, request_digest, message_id, seq, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (request_id, digest, message_id, seq, created_at.isoformat()),
            )
        return {"message_id": message_id, "seq": seq, "request_id": request_id, "created": True}


def send(
    conn: Connection,
    *,
    sender: str,
    recipient: str,
    text: str,
    request_id: str | None = None,
    job_id: str | None = None,
    relayed: Any = None,
    author_kind: str = "agent",
    reply_to: str | None = None,
    attention: Any = None,
    needs_decision: bool = False,
) -> dict[str, Any]:
    """Store a message as the recipient's next seq. Retrying with the same request_id is safe.

    `author_kind` is recorded with the message (see `Config.author_kind`), so a later change to the
    sender role's config never changes how a message already sent reads.

    `reply_to`, `attention` and `needs_decision` belong to a conversation send and are refused here, so
    no interface can pass them to a direct message and have them quietly dropped.
    """
    only_in_a_conversation(reply_to=reply_to, attention=attention, needs_decision=needs_decision)
    return DirectMessage(
        sender=sender,
        recipient=recipient,
        text=text,
        request_id=request_id,
        job_id=job_id,
        relayed=relayed,
        author_kind=author_kind,
    ).send(conn)
