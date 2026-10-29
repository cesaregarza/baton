"""Refusals every interface reports the same way: a stable `code` and a human-readable detail."""

from __future__ import annotations


class BatonError(Exception):
    code = "baton_error"


class InvalidRequest(BatonError):
    code = "invalid_request"


class IdempotencyConflict(BatonError):
    code = "idempotency_conflict"


class UnknownJob(BatonError):
    code = "unknown_job"


class UnknownRole(BatonError):
    code = "unknown_role"


class UnknownToken(BatonError):
    """No configured bearer token has that name."""

    code = "unknown_token"


class IllegalTransition(BatonError):
    code = "illegal_transition"


class StaleRevision(BatonError):
    """The job's state changed since the revision the caller read."""

    code = "stale_revision"


class MailboxFull(BatonError):
    """The recipient has too many unread messages: backpressure, not loss. Retry after it reads."""

    code = "mailbox_full"


class RequestTooOld(BatonError):
    code = "request_too_old"


class Forbidden(BatonError):
    """The caller's role lacks the permission the config gives for this."""

    code = "forbidden"


class ConversationsDisabled(BatonError):
    code = "conversations_disabled"


class UnknownConversation(BatonError):
    code = "unknown_conversation"


class NotAParticipant(BatonError):
    code = "not_a_participant"


class ConversationFull(BatonError):
    """A recipient's mailbox is full, so no participant got the message. Retry after that role reads."""

    code = "conversation_full"


class ConversationRateLimited(BatonError):
    code = "conversation_rate_limited"
