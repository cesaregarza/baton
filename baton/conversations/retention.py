"""Retained conversation history and sequence-gap helpers."""

from __future__ import annotations

import json
from typing import Any

from baton import messages
from baton.database import Connection, Row


def _access_start_seq(
    conn: Connection, conversation_id: str, role: str, participant: Row | None, conversation: Row, view_all: bool
) -> int | None:
    """Earliest sequence whose visibility is independently established, or None if unknown.

    A client cursor is not proof that a role could read earlier messages. For late joiners, the retained
    participant-added notice is the only sequence-level evidence available without a schema change.
    """
    if view_all or (participant is not None and participant["added_at"] == conversation["created_at"]):
        return 1
    if participant is None:
        return None
    rows = conn.execute(
        "SELECT conversation_seq, notice FROM messages WHERE conversation_id = ? "
        "AND recipient = ? AND notice IS NOT NULL AND at >= ? ORDER BY conversation_seq",
        (conversation_id, role, participant["added_at"]),
    )
    for row in rows:
        notice = json.loads(row["notice"])
        if notice.get("event") == "participant_added" and notice.get("participant") == role:
            return row["conversation_seq"]
    return None


def _has_readable_seq(
    conn: Connection, condition: str, values: dict[str, Any], conversation_id: str, start: int, end: int
) -> bool:
    if end < start:
        return False
    return (
        conn.execute(
            f"SELECT 1 FROM messages m WHERE m.conversation_id = :conversation_id "
            "AND m.conversation_seq BETWEEN :start_seq AND :end_seq "
            f"AND {condition} LIMIT 1",
            {**values, "conversation_id": conversation_id, "start_seq": start, "end_seq": end},
        ).fetchone()
        is not None
    )


def gap_words(gap: dict[str, int]) -> str:
    """What a page says where `read_history` reports a gap."""
    first, last = gap["from_seq"], gap["to_seq"]
    return (f"Message #{first} is" if first == last else f"Messages #{first}–#{last} are") + " no longer kept."


def _retention_gaps(
    page: list[dict[str, Any]],
    lower_seq: int | None,
    upper_seq: int | None,
    access_start: int | None,
    joined_at: str | None,
) -> list[dict[str, int]]:
    """Report only missing ranges after access is independently established."""
    if not page:
        return []
    gaps = []
    first_seq = page[0]["seq"]
    if lower_seq is not None and first_seq > lower_seq:
        gaps.append({"from_seq": lower_seq, "to_seq": first_seq - 1})
    for previous, current in zip(page, page[1:], strict=False):
        start = previous["seq"] + 1
        if access_start is not None:
            start = max(start, access_start)
        elif joined_at is None or previous["at"] < joined_at:
            continue
        if current["seq"] > start:
            gaps.append({"from_seq": start, "to_seq": current["seq"] - 1})
    last = page[-1]
    start = last["seq"] + 1
    if access_start is not None:
        start = max(start, access_start)
    elif joined_at is None or last["at"] < joined_at:
        return gaps
    if upper_seq is not None and upper_seq >= start:
        gaps.append({"from_seq": start, "to_seq": upper_seq})
    return gaps


def _history_entry(row: Row, reply_to: dict[str, Any] | None = None) -> dict[str, Any]:
    """A message as history shows it: numbered in the conversation, not in any one mailbox."""
    return {**messages.as_dict(row, reply_to), "seq": row["conversation_seq"]}
