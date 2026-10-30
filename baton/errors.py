"""Refusals every interface reports the same way: a stable `code` and a human-readable detail."""

from __future__ import annotations


class BatonError(Exception):
    code = "baton_error"
    # Fields a refusal reports beside its code and detail, such as where to resume.
    extra: dict = {}


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


class UnknownMessage(BatonError):
    """No message with that id that the caller may read. The same refusal either way, so it reveals nothing."""

    code = "unknown_message"


class UnsupportedKind(BatonError):
    code = "unsupported_kind"


class UnknownBatch(BatonError):
    code = "unknown_batch"


class BatchExpired(BatonError):
    code = "batch_expired"


class BatchClosed(BatonError):
    code = "batch_closed"


class BatchIncomplete(BatonError):
    code = "batch_incomplete"


class BatchSuperseded(BatonError):
    code = "batch_superseded"


class VersionConflict(BatonError):
    code = "version_conflict"


class CheckpointGap(BatonError):
    code = "checkpoint_gap"


class UpToDate(BatonError):
    code = "up_to_date"


class LimitExceeded(BatonError):
    """A bound was hit; `limit` names which, so the caller can tell one from another without parsing text."""

    code = "limit_exceeded"

    def __init__(self, detail: str, limit: str) -> None:
        super().__init__(detail)
        self.extra = {"limit": limit}


class ProvenanceLimitExceeded(BatonError):
    """A kind's inputs can't be recorded within the caps, so nothing advances for it and it stays dirty."""

    code = "provenance_limit_exceeded"

    def __init__(self, detail: str, *, limit: str, conversation_id: str, kind: str, segments: int,
                 items: int) -> None:
        super().__init__(detail)
        self.extra = {"limit": limit, "conversation_id": conversation_id, "kind": kind, "segments": segments,
                      "items": items}


class NothingToIndex(BatonError):
    code = "nothing_to_index"


class VectorsIncomplete(BatonError):
    code = "vectors_incomplete"


class VectorConflict(BatonError):
    code = "vector_conflict"


class UnknownGeneration(BatonError):
    code = "unknown_generation"


class GenerationDropped(BatonError):
    code = "generation_dropped"


class GenerationActive(BatonError):
    code = "generation_active"


class ActivationRefused(BatonError):
    """A generation can't be switched to yet: `pairs` names (some of) the (conversation, kind) pairs not current."""

    def __init__(self, detail: str, pairs: list[dict]) -> None:
        super().__init__(detail)
        self.extra = {"pairs": pairs[:20], "count": len(pairs)}


class GenerationIncomplete(ActivationRefused):
    code = "generation_incomplete"


class GenerationBehind(ActivationRefused):
    code = "generation_behind"


class ReconcileRequired(BatonError):
    """More pairs than one activation checks: the operator raises the bound in config and retries the same one."""

    code = "reconcile_required"

    def __init__(self, detail: str, pairs: int, bound: int) -> None:
        super().__init__(detail)
        self.extra = {"pairs": pairs, "bound": bound, "key": "indexer.max_activation_reconcile"}


class CursorExpired(BatonError):
    """The change-log position is from another log, or older than what retention kept: start again from state."""

    code = "cursor_expired"

    def __init__(self, detail: str, reset_cursor: str) -> None:
        super().__init__(detail)
        self.extra = {"reset_cursor": reset_cursor, "resync": True}
