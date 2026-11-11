"""One-line entries over a graph snapshot: what a scoped read loads instead of whole posts or JSON rows.

Every byte a read returns is context its caller spends, so an entry carries only what a reader acts on, on
one line:

    statement  120-2 | S3 S7 | supersedes>98-1 responds_to<131-4 | text
    decision   D12 | S3 S7 | rests on 120-2 post:131 | changed at post 131 | text
    strand     S3 | active | 14 statements, summarized through post 131 | title

A statement's connections read `kind>id` when it does that to the other statement and `kind<id` when the
other does it to this one. Only standing content is served: a fully replaced statement and a superseded
decision are left out, along with every connection and reference to them, and a partly replaced statement
shows the text that still stands.

Nothing here reads the host database or decides who may read; `baton.graph.reader` authorizes first and hands
the snapshot in. Search runs over a throwaway in-memory index built for the one call.
"""

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from baton.database import Connection, memory, store
from baton.errors import GraphUnavailable
from baton.graph.snapshot import STATEMENT_ID, edge_key, ordinal_key, statement_key

WHOLE_BYTES = 400  # a search hit with more text than this comes back as an excerpt
QUERY_WORDS = 16
FUSE_DEPTH = 20  # each query feeds at least this many of its best hits into the fusion
FUSE_RANK = 60  # reciprocal rank fusion: a hit at rank r scores 1 / (FUSE_RANK + r)
EXCERPT_TOKENS = 24
POST_ID = re.compile(r"post:([1-9][0-9]*)")
MATCH = (
    f"SELECT id, snippet(unit, 1, '', '', ' … ', {EXCERPT_TOKENS}) FROM unit WHERE unit MATCH ? ORDER BY rank LIMIT ?"
)


@dataclass(frozen=True)
class Entry:
    """One standing statement or active decision; `head` is everything on its line before the text."""

    id: str
    head: str
    text: str

    @property
    def line(self) -> str:
        return f"{self.head} | {self.text}"


class Entries:
    """The standing entries of one snapshot: statements in post order, then active decisions."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot
        statements, decisions = snapshot["statements"], snapshot["decisions"]
        standing = sorted(
            (sid for sid, s in statements.items() if s["relevance"] != "completely_superseded"), key=statement_key
        )
        links, self._replaced_by = _connections(snapshot["edges"], set(standing))
        self._entries = {sid: _statement(statements[sid], links[sid]) for sid in standing}
        self._of_post: dict[int, list[str]] = {}
        for sid in standing:
            self._of_post.setdefault(statements[sid]["seq"], []).append(sid)
        for key in sorted(decisions, key=ordinal_key):
            if decisions[key]["status"] == "active":
                self._entries[key] = _decision(decisions[key], set(standing))

    def outline(self) -> list[str]:
        """Every strand, active or resolved, as one line."""
        strands = sorted(self._snapshot["strands"].values(), key=lambda strand: ordinal_key(strand["id"]))
        return [
            f"{s['id']} | {s['state']} | {len(s['statement_ids'])} statements, "
            f"summarized through post {s['summary_through']} | {_flat(s['title'])}"
            for s in strands
        ]

    def search(self, queries: list[str], limit: int, exclude: Collection[str] = ()) -> list[str]:
        """The best `limit` entries across the queries, best first; one ranked high by several queries wins.

        Entries named in `exclude` are ranked as if they were not there, so a caller that names what it already
        holds gets the next best in their place.
        """
        scores: dict[str, float] = {}
        excerpts: dict[str, str] = {}
        depth, exclude = max(limit, FUSE_DEPTH), set(exclude)
        with memory() as index:
            self._load(index)
            for query in queries:
                fresh = [hit for hit in _matches(index, query, depth + len(exclude)) if hit[0] not in exclude]
                for rank, (uid, excerpt) in enumerate(fresh[:depth], 1):
                    scores[uid] = scores.get(uid, 0) + 1 / (FUSE_RANK + rank)
                    excerpts.setdefault(uid, excerpt)
        best = sorted(scores, key=lambda uid: -scores[uid])[:limit]
        return [_hit(self._entries[uid], excerpts[uid]) for uid in best]

    def find(self, wanted: str) -> list[tuple[str, str]]:
        """The whole entries one id names, as (entry id, text); empty when it names nothing standing.

        A statement or decision id names itself, a strand id its summary and open work, and `post:N` every
        standing statement taken from post N.
        """
        if wanted in self._entries:
            return [(wanted, self._entries[wanted].line)]
        if wanted in self._snapshot["strands"]:
            return [(wanted, _card(self._snapshot["strands"][wanted]))]
        post = POST_ID.fullmatch(wanted)
        if post is None:
            return []
        return [(sid, self._entries[sid].line) for sid in self._of_post.get(int(post.group(1)), [])]

    def why_missing(self, wanted: str) -> str:
        """Why `find` returned nothing for this id, in words a reader can act on."""
        if wanted in self._snapshot["statements"]:
            replacers = " ".join(self._replaced_by.get(wanted, []))
            return f"replaced in full by {replacers}" if replacers else "replaced in full"
        if wanted in self._snapshot["decisions"]:
            return "superseded decision"
        post = POST_ID.fullmatch(wanted)
        if post is None:
            return "no such id; ids look like 120-2, D12, S3 or post:120"
        if int(post.group(1)) > self._snapshot["covered_through_seq"]:
            return "that post is after this graph's coverage; read it with read_history"
        return "no standing statements from that post"

    def _load(self, index: Connection) -> None:
        if not store.fts5_available(index):
            raise GraphUnavailable("graph search needs SQLite FTS5, which this host does not have")
        index.execute("CREATE VIRTUAL TABLE unit USING fts5(id UNINDEXED, text, tokenize='porter unicode61')")
        index.executemany(
            "INSERT INTO unit (id, text) VALUES (?, ?)", [(entry.id, entry.text) for entry in self._entries.values()]
        )


def _connections(edges, standing):
    """Each standing statement's connections, in canonical edge order, and what replaced each left-out one."""
    links: dict[str, list[str]] = {sid: [] for sid in standing}
    replaced_by: dict[str, list[str]] = {}
    for edge in sorted(edges, key=edge_key):
        source, target, kind = edge["from"], edge["to"], edge["kind"]
        if source in standing and target in standing:
            links[source].append(f"{kind}>{target}")
            links[target].append(f"{kind}<{source}")
        elif kind == "supersedes" and target not in standing:
            replaced_by.setdefault(target, []).append(source)
    return links, replaced_by


def _statement(statement, links):
    strands = " ".join(sorted(statement["strand_ids"], key=ordinal_key)) or "-"
    head = f"{statement['id']} | {strands} | {' '.join(links) or '-'}"
    return Entry(statement["id"], head, _flat(statement["surviving_text"] or statement["text"]))


def _decision(decision, standing):
    applies = decision["applies_to"]
    strands = "all" if applies == ["all"] else (" ".join(sorted(applies, key=ordinal_key)) or "-")
    refs = [ref for ref in decision["evidence"] if not STATEMENT_ID.fullmatch(ref) or ref in standing]
    head = f"{decision['id']} | {strands} | rests on {' '.join(refs) or '-'}"
    return Entry(decision["id"], f"{head} | changed at post {decision['last_changed']}", _flat(decision["text"]))


def _card(strand):
    """A strand opened whole: its heading line, its summary, then its open work."""
    work = "".join(f"\n- {_flat(item)}" for item in strand["open_work"]) or "\n(none)"
    heading = f"{strand['id']} | {strand['state']} | {_flat(strand['title'])}"
    return f"{heading}\n{strand['summary'].strip()}\nOpen work:{work}"


def words(query: str) -> list[str]:
    """The distinct words a query is searched by, in the order written."""
    return list(dict.fromkeys(re.findall(r"\w+", query.lower())))[:QUERY_WORDS]


def _matches(index, query, depth):
    """(entry id, excerpt) for the entries holding any word of the query, best first.

    Each word is quoted, so nothing a caller writes reaches the index as query syntax.
    """
    match = " OR ".join(f'"{word}"' for word in words(query))
    if not match:
        return []
    return [(row[0], _flat(row[1])) for row in index.execute(MATCH, (match, depth)).fetchall()]


def _hit(entry, excerpt):
    """A search hit: the whole line, or for long text an excerpt that says how much there is to load."""
    size = len(entry.text.encode("utf-8"))
    if size <= WHOLE_BYTES:
        return entry.line
    return f"{entry.head} | {excerpt.strip(' …')} … [{size} bytes in full]"


def _flat(text):
    return " ".join(text.split())
