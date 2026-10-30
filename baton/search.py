"""Finding messages: `get_message` by id, and keyword `search`, each limited to what the caller may read.

What a role may read is what `read_history` shows it, applied to single copies:

* a conversation copy, if the role is a current participant and that copy is to it or from it, so a role
  added later sees nothing from before its join and a removed role sees nothing;
* with `can_view_all_conversations`, every conversation copy; the flag widens to conversations only;
* a direct message, only to its sender and its recipient.

Search applies that rule inside the query, before ranking and before the limit, so a perfect match the
caller can't read never displaces, or reveals itself through, a hit it can. Hits are one per message, however
many copies matched. Keyword search uses SQLite FTS5 where the host's SQLite has it, otherwise a bounded LIKE
scan; `keyword_mode` says which. Nothing here writes, so searching and fetching consume nothing.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from baton import messages
from baton.config import Config
from baton.errors import InvalidRequest, UnknownMessage, UnsupportedKind

MAX_HITS = 50
MAX_TERMS = 8
MAX_QUERY = 200
SNIPPET_CHARS = 160
TOPIC = re.compile(r"[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9-]*)?")


def keyword_mode(conn: sqlite3.Connection) -> str:
    found = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'messages_fts'").fetchone()
    return "fts5" if found else "like"


def readable(config: Config, role: str) -> tuple[str, dict[str, Any]]:
    """The SQL condition, on a `messages m` row, that `role` may read that copy; and its parameters."""
    view_all = config.roles[role].can_view_all_conversations
    member = ("(m.recipient = :reader OR m.sender = :reader) AND EXISTS (SELECT 1 FROM participants p"
              " WHERE p.conversation_id = m.conversation_id AND p.role = :reader)")
    return (f"((m.conversation_id IS NULL AND (m.recipient = :reader OR m.sender = :reader))"
            f" OR (m.conversation_id IS NOT NULL AND {'1' if view_all else member}))", {"reader": role})


def get_message(conn: sqlite3.Connection, config: Config, role: str, message_id: str) -> dict[str, Any]:
    """One message the caller may read. Missing and not permitted give the same refusal."""
    condition, values = readable(config, role)
    row = conn.execute(f"SELECT m.* FROM messages m WHERE m.id = :message_id AND {condition}"
                       " ORDER BY m.recipient = :reader DESC LIMIT 1",
                       {**values, "message_id": str(message_id)}).fetchone()
    if row is None:
        raise UnknownMessage(f"there is no message {message_id} that you can read")
    [shown] = messages.as_page_for(conn, role, [row])  # resolves what it replies to, as this reader sees it
    if row["conversation_id"] is not None:
        shown["seq"] = row["conversation_seq"]  # conversation order, as read_history numbers it
    return shown


def search(conn: sqlite3.Connection, config: Config, role: str, query: str, *, conversation_id: str | None = None,
           topic: str | None = None, kinds: list[str] | None = None, limit: int = 20,
           before: str | None = None) -> dict[str, Any]:
    """Messages matching every word of `query` that the caller may read, best first, one hit per message."""
    if kinds is not None and (not isinstance(kinds, list) or set(kinds) != {"message"}):
        raise UnsupportedKind("only message hits exist so far; derived kinds (summaries, decisions, open "
                              "questions) aren't searchable yet")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_HITS:
        raise InvalidRequest(f"limit must be 1-{MAX_HITS}")
    terms = re.findall(r"\w+", str(query)[:MAX_QUERY])[:MAX_TERMS]
    if not terms:
        raise InvalidRequest("give at least one word to search for")
    condition, values = readable(config, role)
    filters, extra = _filters(conversation_id, topic, before)
    values |= extra
    mode = keyword_mode(conn)
    if mode == "fts5":
        values["match"] = " ".join('"' + term.replace('"', '""') + '"' for term in terms)
        # The readable matches are ranked first (materialized, so the ranking functions run per row), then
        # reduced to one hit per message.
        rows = conn.execute(
            "WITH matched AS MATERIALIZED (SELECT m.id AS message_id, m.conversation_id, m.conversation_seq, m.seq,"
            " m.sender, m.at, snippet(messages_fts, 0, '[', ']', '…', 12) AS snippet, bm25(messages_fts) AS score"
            " FROM messages_fts JOIN messages m ON m.recipient = messages_fts.recipient AND m.seq = messages_fts.seq"
            f" WHERE messages_fts MATCH :match AND {condition}{filters})"
            " SELECT message_id, conversation_id, conversation_seq, seq, sender, at, snippet, MIN(score) AS best"
            " FROM matched GROUP BY message_id ORDER BY best, at DESC LIMIT :limit",
            {**values, "limit": limit}).fetchall()
    else:
        likes = []
        for index, term in enumerate(terms):
            values[f"term{index}"] = "%" + re.sub(r"([\\%_])", r"\\\1", term) + "%"
            likes.append(f"m.text LIKE :term{index} ESCAPE '\\'")
        rows = conn.execute(
            "SELECT m.id AS message_id, m.conversation_id, m.conversation_seq, m.seq, m.sender, MAX(m.at) AS at,"
            f" m.text AS text FROM messages m WHERE {' AND '.join(likes)} AND {condition}{filters}"
            " GROUP BY m.id ORDER BY at DESC LIMIT :limit", {**values, "limit": limit}).fetchall()
    hits = [{
        "message_id": row["message_id"], "conversation_id": row["conversation_id"],
        "seq": row["conversation_seq"] if row["conversation_id"] is not None else row["seq"],
        "from_role": row["sender"], "at": row["at"],
        "snippet": row["snippet"] if mode == "fts5" else _snippet(row["text"], terms), "derived": False,
    } for row in rows]
    return {"hits": hits, "keyword_index": mode}


def _filters(conversation_id: str | None, topic: str | None, before: str | None) -> tuple[str, dict[str, Any]]:
    sql, values = "", {}
    if conversation_id is not None:
        sql += " AND m.conversation_id = :conversation_id"
        values["conversation_id"] = str(conversation_id)
    if topic is not None:
        if not TOPIC.fullmatch(str(topic)):
            raise InvalidRequest("topic is 1-2 lowercase slug segments, such as project or project/subtopic")
        sql += (" AND m.conversation_id IN (SELECT id FROM conversations WHERE topic = :topic"
                " OR topic LIKE :topic || '/%')")
        values["topic"] = str(topic)
    if before is not None:
        sql += " AND m.at < :before"
        values["before"] = str(before)
    return sql, values


def _snippet(text: str, terms: list[str]) -> str:
    lowered = text.lower()
    found = min((lowered.find(term.lower()) for term in terms if term.lower() in lowered), default=0)
    start = max(0, found - SNIPPET_CHARS // 3)
    end = start + SNIPPET_CHARS
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
