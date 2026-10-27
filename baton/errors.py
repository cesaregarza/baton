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
