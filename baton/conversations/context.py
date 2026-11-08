"""Summary-first reads composed with authorized history in one database snapshot."""

from __future__ import annotations

from typing import Any

from baton import search
from baton.config import Config
from baton.conversations import Conversation, require_enabled
from baton.conversations.queries import _load
from baton.database import Connection
from baton.errors import Forbidden, InvalidRequest
from baton.messages.limits import MAX_PAGE_MESSAGES


def read_context(
    conn: Connection,
    config: Config,
    role: str,
    conversation_id: str,
    *,
    producer: str | None = None,
    limit: int = 50,
    after_seq: int | None = None,
    through_seq: int | None = None,
) -> dict[str, Any]:
    """Open a thread, or continue raw messages using both fields of its fixed-head cursor.

    Derived text is advisory. Only the existing served-artifact predicate can select a summary;
    current history visibility applies to every raw page, including continuations after membership changes.
    This operation marks nothing read and never advances an inbox cursor.
    """
    require_enabled(config)
    if role == "guest" or role not in config.roles:
        raise Forbidden("read_context requires a core role")
    if producer is not None and not isinstance(producer, str):
        raise InvalidRequest("producer is a name")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_MESSAGES:
        raise InvalidRequest(f"limit must be 1-{MAX_PAGE_MESSAGES}")
    continuing = after_seq is not None or through_seq is not None
    if continuing and (type(after_seq) is not int or type(through_seq) is not int or not 0 <= after_seq <= through_seq):
        raise InvalidRequest("continuation requires both after_seq and through_seq, with 0 <= after_seq <= through_seq")
    with conn.snapshot():
        current_head = _load(conn, conversation_id)["last_seq"]
        head = current_head
        summary = None
        fallback = None
        if continuing:
            assert after_seq is not None and through_seq is not None
            head = through_seq
            start = after_seq
        else:
            if config.search.enabled:
                summary = search.summary_winner(
                    search.read_artifacts(conn, config, role, conversation_id, producer)["artifacts"], head
                )
                if summary is None:
                    fallback = "no_accessible_summary"
            else:
                fallback = "search_disabled"
            start = summary["covered_through_seq"] if summary is not None else 0
        conversation = Conversation(conn, conversation_id)
        page = conversation.history(role, start, limit, config=config, through_seq=head)
        # History authorizes first: a forged ceiling must not reveal a private thread's head.
        if head > current_head:
            raise InvalidRequest("through_seq cannot exceed the conversation head")
        # Rendering may return fewer than limit because of its byte budget. Probe from the last
        # actually returned seq, never infer completion from the number of messages in the page.
        more = bool(conversation.history(role, page["next_seq"], 1, config=config, through_seq=head)["messages"])
        return {
            "conversation_id": conversation_id,
            "head_seq": head,
            "covered_through_seq": (
                summary["covered_through_seq"] if summary is not None else (None if continuing else 0)
            ),
            "summary": summary,
            "fallback": fallback,
            "messages": page["messages"],
            "next_seq": page["next_seq"],
            "retention_gaps": page["retention_gaps"],
            "tail_complete": not more,
            "continuation": {"after_seq": page["next_seq"], "through_seq": head} if more else None,
        }
