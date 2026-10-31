"""The shared visibility rule for retained board conversations and their messages."""

from __future__ import annotations

import sqlite3
from typing import Any

from baton.config import Config


def sees_all(config: Config, role: str) -> bool:
    """Whether the configured principal may read every board conversation."""
    return role in config.roles and config.roles[role].can_view_all_conversations


def message_condition(config: Config, role: str, alias: str = "m") -> tuple[str, dict[str, Any]]:
    """SQL condition for a retained message copy readable by ``role``.

    Direct messages remain sender/recipient only. Board access is controlled by
    ``can_view_all_conversations``; otherwise a role must still be a participant
    and hold a retained copy.
    """
    member = (f"({alias}.recipient = :reader OR {alias}.sender = :reader) AND EXISTS "
             f"(SELECT 1 FROM participants p WHERE p.conversation_id = {alias}.conversation_id "
             "AND p.role = :reader)")
    conversation = "1" if sees_all(config, role) else member
    return (f"(({alias}.conversation_id IS NULL AND ({alias}.recipient = :reader OR "
            f"{alias}.sender = :reader)) OR ({alias}.conversation_id IS NOT NULL AND {conversation}))",
            {"reader": role})


def may_read_conversation(conn: sqlite3.Connection, config: Config, role: str, conversation_id: str) -> bool:
    """Apply the conversation-level part of the same rule before reading history."""
    if sees_all(config, role):
        return True
    return conn.execute("SELECT 1 FROM participants WHERE conversation_id = ? AND role = ?",
                        (conversation_id, role)).fetchone() is not None
