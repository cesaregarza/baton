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

import json
import math
import re
import sqlite3
import struct
from typing import Any

from baton import derived, messages
from baton.config import Config
from baton.conversations import utc_iso
from baton.errors import InvalidRequest, UnknownMessage, UnsupportedKind

MAX_HITS = 50
MAX_TERMS = 8
MAX_QUERY = 200
SNIPPET_CHARS = 160
ARTIFACT_KINDS = (*derived.KINDS, derived.RECENT)
# The first message a version was made from, which a recent window reports as where it starts.
FIRST_INPUT = "(SELECT MIN(i.from_seq) FROM artifact_inputs i WHERE i.version_id = v.id) AS first_input"
TOPIC = re.compile(r"[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9-]*)?")


def keyword_mode(conn: sqlite3.Connection) -> str:
    found = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'messages_fts'").fetchone()
    return "fts5" if found else "like"


def readable(config: Config, role: str) -> tuple[str, dict[str, Any]]:
    """The SQL condition, on a `messages m` row, that `role` may read that copy; and its parameters."""
    if role == "guest":
        member = ("((m.recipient = :reader OR m.sender = :reader) AND EXISTS (SELECT 1 FROM participants p"
                  " WHERE p.conversation_id = m.conversation_id AND p.role = :reader))")
        conversations = "1" if config.roles[role].can_view_all_conversations else member
        return f"(m.conversation_id IS NOT NULL AND {conversations})", {"reader": role}
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
           before: str | None = None, producer: str | None = None, query_vector: list[float] | None = None,
           model: str | None = None) -> dict[str, Any]:
    """Messages, and derived artifacts when `kinds` asks for them, matching every word of `query` that the
    caller may read, best first: one hit per message, and per artifact version.

    With `query_vector`, ranking stays keyword-first: the same authorized keyword matches, a wider pool of
    them (POOL_FACTOR times the limit of each kind searched), are re-ordered by similarity to the vectors
    producers stored for `model`, by the profile's distance (cosine or inner product); matches without a
    vector follow in keyword order. Nothing the caller can't read is ever a candidate, so none can be lifted
    in or ahead by its vector. Every active profile for `model` must have the query's dimension.
    """
    if kinds is None:
        kinds = ["message"]
    if not isinstance(kinds, list) or not kinds or not set(kinds) <= {"message", *ARTIFACT_KINDS}:
        raise UnsupportedKind(f"kinds is a non-empty list from message, {', '.join(ARTIFACT_KINDS)}")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_HITS:
        raise InvalidRequest(f"limit must be 1-{MAX_HITS}")
    if producer is not None and not isinstance(producer, str):
        raise InvalidRequest("producer is a name")
    # Every word must appear, so a query is refused whole rather than shortened to fit.
    if len(str(query)) > MAX_QUERY:
        raise InvalidRequest(f"a query is at most {MAX_QUERY} characters")
    terms = re.findall(r"\w+", str(query))
    if not terms:
        raise InvalidRequest("give at least one word to search for")
    if len(terms) > MAX_TERMS:
        raise InvalidRequest(f"a query is at most {MAX_TERMS} words, and this one has {len(terms)}")
    profiles = _query_profiles(conn, config, query_vector, model) if query_vector is not None or model else None
    pool = min(limit * POOL_FACTOR, MAX_HITS * POOL_FACTOR) if profiles else limit
    mode = keyword_mode(conn)
    ranked: list[tuple[Any, dict[str, Any]]] = []
    if "message" in kinds:
        ranked += _message_hits(conn, config, role, terms, mode, pool, conversation_id, topic, before)
    wanted = [kind for kind in kinds if kind != "message"]
    if wanted:
        ranked += _artifact_hits(conn, config, role, terms, mode, pool, wanted, conversation_id, topic, before,
                                 producer)
    if mode == "fts5":
        ranked.sort(key=lambda pair: pair[0])  # bm25: lower is better
    else:
        ranked.sort(key=lambda pair: pair[1]["at"], reverse=True)  # newest first; UTC ISO times sort as text
    hits = [hit for _, hit in ranked]
    if profiles:
        hits = _rerank(conn, hits, query_vector, model, profiles)
    return {"hits": hits[:limit], "keyword_index": mode}


# With a query vector, each kind of keyword match searched (messages, and derived artifacts) is fetched up to this
# many times the limit before the re-rank, so the pool is at most 4x the limit of each: 8x with both.
POOL_FACTOR = 4


def _query_profiles(conn: sqlite3.Connection, config: Config, query_vector: Any, model: Any) -> dict[str, dict]:
    """The active generations that embed with `model`, by id; or a refusal naming what doesn't fit."""
    if not config.search.embeddings:
        raise InvalidRequest("query vectors need [search] embeddings = true")
    if not isinstance(model, str) or not model:
        raise InvalidRequest("a query vector names its model")
    if not isinstance(query_vector, list) or not query_vector or not all(
            isinstance(x, int | float) and not isinstance(x, bool) and derived.fits_float32(x) for x in query_vector):
        raise InvalidRequest("query_vector is a list of finite numbers that each fit a float32")
    profiles, producers = {}, {}
    for generation, producer, profile in conn.execute(
            "SELECT id, producer, profile FROM generations WHERE active = 1 ORDER BY producer, id"):
        embedding = json.loads(profile).get("embedding")
        if embedding and embedding["model"] == model:
            profiles[generation], producers[generation] = embedding, producer
    if not profiles:
        raise InvalidRequest(f"no active generation embeds with {model}")
    # Activation keeps one signature per model; should a database hold two anyway, refuse rather than score.
    signatures = {json.dumps(derived.signature(embedding), sort_keys=True) for embedding in profiles.values()}
    if len(signatures) > 1:
        held = "; ".join(f"{producers[generation]}'s {generation}: {derived.signature(embedding)}"
                         for generation, embedding in profiles.items())
        raise InvalidRequest(f"{model} has more than one active embedding signature ({held}), so a query vector "
                             "can't be scored")
    for generation, embedding in profiles.items():
        if embedding["dim"] != len(query_vector):
            raise InvalidRequest(f"{producers[generation]}'s active generation {generation} embeds {model} in "
                                 f"{embedding['dim']} dimensions, and the query has {len(query_vector)}")
    return profiles


def _rerank(conn: sqlite3.Connection, hits: list[dict], query_vector: list[float], model: str,
            profiles: dict[str, dict]) -> list[dict]:
    """Keyword matches with a vector first, most similar first; then the rest, in keyword order."""
    marks = ", ".join("?" for _ in profiles)
    scored = []
    for order, hit in enumerate(hits):
        item_kind, item_id = ("artifact", hit["version_id"]) if hit["derived"] else ("message", hit["message_id"])
        rows = conn.execute(f"SELECT generation, dim, data FROM vectors WHERE item_kind = ? AND item_id = ?"
                            f" AND model = ? AND generation IN ({marks})", (item_kind, item_id, model, *profiles))
        similarities = [_similarity(query_vector, list(struct.unpack(f"<{dim}f", data)), profiles[generation])
                        for generation, dim, data in rows]
        hit["similarity"] = max(similarities) if similarities else None
        scored.append((hit["similarity"] is None, -(hit["similarity"] or 0.0), order, hit))
    return [hit for *_, hit in sorted(scored, key=lambda entry: entry[:3])]


def _similarity(a: list[float], b: list[float], embedding: dict) -> float:
    product = sum(x * y for x, y in zip(a, b, strict=True))
    if embedding["distance"] == "inner_product":
        return product
    norms = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return product / norms if norms else 0.0


def _message_hits(conn: sqlite3.Connection, config: Config, role: str, terms: list[str], mode: str, limit: int,
                  conversation_id: str | None, topic: str | None, before: str | None) -> list[tuple[Any, dict]]:
    condition, values = readable(config, role)
    filters, extra = _filters(conversation_id, topic, before)
    values |= extra
    if mode == "fts5":
        values["match"] = _match(terms)
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
        likes = _likes(terms, "m.text", values)
        rows = conn.execute(
            "SELECT m.id AS message_id, m.conversation_id, m.conversation_seq, m.seq, m.sender, MAX(m.at) AS at,"
            f" m.text AS text, 0 AS best FROM messages m WHERE {likes} AND {condition}{filters}"
            " GROUP BY m.id ORDER BY at DESC LIMIT :limit", {**values, "limit": limit}).fetchall()
    return [(row["best"], {
        "message_id": row["message_id"], "conversation_id": row["conversation_id"],
        "seq": row["conversation_seq"] if row["conversation_id"] is not None else row["seq"],
        "from_role": row["sender"], "at": row["at"],
        "snippet": row["snippet"] if mode == "fts5" else _snippet(row["text"], terms), "derived": False,
    }) for row in rows]


def _artifact_hits(conn: sqlite3.Connection, config: Config, role: str, terms: list[str], mode: str, limit: int,
                   kinds: list[str], conversation_id: str | None, topic: str | None, before: str | None,
                   producer: str | None) -> list[tuple[Any, dict]]:
    """Artifact versions served to `role`, filtered in SQL before they are ranked or limited."""
    condition, values = served(config, role)
    filters, extra = _artifact_filters(kinds, conversation_id, topic, before, producer)
    values |= extra
    if mode == "fts5":
        values["match"] = _match(terms)
        rows = conn.execute(
            f"WITH matched AS MATERIALIZED (SELECT v.*, c.last_seq, {FIRST_INPUT},"
            " snippet(artifacts_fts, 0, '[', ']', '…', 12)"
            " AS snippet, bm25(artifacts_fts) AS score FROM artifacts_fts"
            " JOIN artifact_versions v ON v.id = artifacts_fts.version_id"
            " JOIN conversations c ON c.id = v.conversation_id"
            f" WHERE artifacts_fts MATCH :match AND {condition}{filters})"
            " SELECT * FROM matched ORDER BY score, created_at DESC LIMIT :limit",
            {**values, "limit": limit}).fetchall()
    else:
        likes = _likes(terms, "v.body", values)
        rows = conn.execute(
            f"SELECT v.*, c.last_seq, {FIRST_INPUT}, 0 AS score FROM artifact_versions v JOIN conversations c ON c.id ="
            f" v.conversation_id WHERE {likes} AND {condition}{filters} ORDER BY v.created_at DESC LIMIT :limit",
            {**values, "limit": limit}).fetchall()
    return [(row["score"], {**_labels(row), "at": row["created_at"],
                            "snippet": row["snippet"] if mode == "fts5" else _snippet(row["body"], terms)})
            for row in rows]


def read_artifacts(conn: sqlite3.Connection, config: Config, role: str, conversation_id: str,
                   producer: str | None = None) -> dict[str, Any]:
    """A conversation's artifacts that `role` may read: each kind's latest valid version from each producer's
    active generation, if `role` can read every message it was made from. `producer` only filters."""
    if producer is not None and not isinstance(producer, str):
        raise InvalidRequest("producer is a name")
    condition, values = served(config, role)
    filters, extra = _artifact_filters(list(ARTIFACT_KINDS), str(conversation_id), None, None, producer)
    rows = conn.execute(
        f"SELECT v.*, c.last_seq, {FIRST_INPUT} FROM artifact_versions v"
        f" JOIN conversations c ON c.id = v.conversation_id WHERE {condition}{filters} ORDER BY v.kind, v.producer",
        {**values, **extra}).fetchall()
    return {"artifacts": [_artifact(row) for row in rows]}


def served(config: Config, role: str) -> tuple[str, dict[str, Any]]:
    """The SQL condition, on an `artifact_versions v` row, that it is served to `role`; and its parameters.

    Only a kind's latest version, while valid, from its producer's active generation, made from at least one
    message, every one of which `role` can read through `readable`: an artifact never shows what the
    messages it was made from wouldn't.
    """
    condition, values = readable(config, role)
    return ("(v.state = 'valid' AND v.generation IN (SELECT id FROM generations WHERE active = 1)"
            " AND v.version = (SELECT MAX(o.version) FROM artifact_versions o WHERE o.producer = v.producer"
            " AND o.scope_id = v.scope_id AND o.generation = v.generation AND o.conversation_id = v.conversation_id"
            " AND o.kind = v.kind)"
            " AND EXISTS (SELECT 1 FROM artifact_inputs i WHERE i.version_id = v.id)"
            " AND NOT EXISTS (SELECT 1 FROM artifact_inputs i WHERE i.version_id = v.id AND"
            " (SELECT COUNT(DISTINCT m.conversation_seq) FROM messages m WHERE m.conversation_id = i.conversation_id"
            f" AND m.conversation_seq BETWEEN i.from_seq AND i.to_seq AND {condition})"
            " < i.to_seq - i.from_seq + 1))", values)


def _artifact(row: sqlite3.Row) -> dict[str, Any]:
    return {**_labels(row), "body": row["body"], "citations": json.loads(row["citations"])}


def _labels(row: sqlite3.Row) -> dict[str, Any]:
    """What every served artifact says about itself, in reads and in search alike."""
    labels = {
        "version_id": row["id"], "conversation_id": row["conversation_id"], "kind": row["kind"],
        "producer": row["producer"], "scope_id": row["scope_id"], "generation": row["generation"],
        "version": row["version"], "state": _state(row), "covered_through_seq": row["covered_through_seq"],
        "source_revision": row["source_revision"], "derived": True, "authoritative": False,
    }
    if row["kind"] == derived.RECENT:  # a window over the latest messages, never coverage of the whole
        labels |= {"window": "recent", "from_seq": row["first_input"], "to_seq": row["covered_through_seq"]}
    return labels


def _state(row: sqlite3.Row) -> str:
    return "behind" if row["covered_through_seq"] < row["last_seq"] else "valid"


def _artifact_filters(kinds: list[str], conversation_id: str | None, topic: str | None, before: str | None,
                      producer: str | None) -> tuple[str, dict[str, Any]]:
    values: dict[str, Any] = {f"kind{index}": kind for index, kind in enumerate(kinds)}
    sql = f" AND v.kind IN ({', '.join(f':kind{index}' for index in range(len(kinds)))})"
    if conversation_id is not None:
        sql += " AND v.conversation_id = :conversation_id"
        values["conversation_id"] = str(conversation_id)
    if topic is not None:
        if not TOPIC.fullmatch(str(topic)):
            raise InvalidRequest("topic is 1-2 lowercase slug segments, such as project or project/subtopic")
        sql += (" AND v.conversation_id IN (SELECT id FROM conversations WHERE topic = :topic"
                " OR topic LIKE :topic || '/%')")
        values["topic"] = str(topic)
    if before is not None:
        sql += " AND v.created_at < :before"
        values["before"] = utc_iso(before, "before")
    if producer is not None:
        sql += " AND v.producer = :producer"
        values["producer"] = producer
    return sql, values


def _match(terms: list[str]) -> str:
    return " ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _likes(terms: list[str], column: str, values: dict[str, Any]) -> str:
    likes = []
    for index, term in enumerate(terms):
        values[f"term{index}"] = "%" + re.sub(r"([\\%_])", r"\\\1", term) + "%"
        likes.append(f"{column} LIKE :term{index} ESCAPE '\\'")
    return " AND ".join(likes)


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
        values["before"] = utc_iso(before, "before")
    return sql, values


def _snippet(text: str, terms: list[str]) -> str:
    lowered = text.lower()
    found = min((lowered.find(term.lower()) for term in terms if term.lower() in lowered), default=0)
    start = max(0, found - SNIPPET_CHARS // 3)
    end = start + SNIPPET_CHARS
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
