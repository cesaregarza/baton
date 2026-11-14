"""Keyword search over each wiki page's current text, by the message search's query rules, limit and index choice.

Only current text is searched. The FTS5 index holds one row per page, replaced in the transaction of every revision
written (`baton.database.store`), and the scan used without FTS5 joins each page to its highest revision only. A hit
points at a page and quotes a snippet of it, never the body: older text is reached through wiki_history and read_wiki.
Nothing here writes, and every page is open to every caller who may search.
"""

from __future__ import annotations

from typing import Any

from baton import revisions
from baton import search as keyword
from baton.database import Connection, Row
from baton.wiki import pages

INDEX = "wiki_fts"
_HIT = "p.path, r.revision, r.at, r.author_role, r.author_alias_id, r.author_alias_name, r.author_alias_code"


def search(conn: Connection, query: Any, *, prefix: Any = None, limit: Any = 20) -> dict[str, Any]:
    """The pages whose current text holds every word of `query`, best first, at and under `prefix` if given."""
    keyword.check_limit(limit)
    terms = keyword.query_terms(query)
    scope, values = pages.under(None if prefix is None else pages.check_path(prefix, "prefix"))
    mode = keyword.keyword_mode(conn, INDEX)
    found = _ranked if mode == "fts5" else _scanned
    return {"hits": found(conn, terms, scope, {**values, "limit": limit}), "keyword_index": mode}


def _ranked(conn: Connection, terms: list[str], scope: str, values: dict[str, Any]) -> list[dict[str, Any]]:
    # Materialized, as the message search is, so the ranking functions run per matching row before the limit.
    rows = conn.execute(
        f"WITH matched AS MATERIALIZED (SELECT {_HIT}, {keyword.fts_snippet(INDEX)} AS snippet,"
        f" bm25({INDEX}) AS score FROM {INDEX} JOIN wiki_pages p ON p.id = {INDEX}.rowid {pages.CURRENT}"
        f" WHERE {INDEX} MATCH :match{scope}) SELECT * FROM matched ORDER BY score, path LIMIT :limit",
        {**values, "match": keyword.fts_match(terms)},
    ).fetchall()
    return [_hit(row, row["snippet"]) for row in rows]


def _scanned(conn: Connection, terms: list[str], scope: str, values: dict[str, Any]) -> list[dict[str, Any]]:
    # Newest first, as the message search's scan orders, since a scan has no rank.
    likes = keyword.like_all(terms, "r.body", values)
    rows = conn.execute(
        f"SELECT {_HIT}, r.body FROM wiki_pages p {pages.CURRENT} WHERE {likes}{scope}"
        " ORDER BY r.at DESC, p.path LIMIT :limit",
        values,
    ).fetchall()
    return [_hit(row, keyword.snippet(row["body"], terms)) for row in rows]


def _hit(row: Row, snippet: str) -> dict[str, Any]:
    return {
        "path": row["path"],
        "revision": row["revision"],
        "at": row["at"],
        "author": revisions.author(row),
        "snippet": snippet,
    }
