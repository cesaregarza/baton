"""Source-bound graph reads over a published snapshot: the working view, row drilldown and one-line entries.

Every operation reads in one database snapshot and serves a graph only through the existing served-artifact
predicate: the summary must be the latest valid version of an active generation whose every input message the
caller may read. Authorization runs before any identifier, count or error that could reveal content. Pages are
byte-bounded, stop between whole rows, and continue through strictly validated opaque cursors bound to one
snapshot, tool, collection, target and captured head. Nothing here writes, and no model ever runs.

The entry reads (`search_graph`, `read_graph_entries`) are how scoped work loads a thread: a search or a list
of ids answered with the one-line entries of `baton.graph.entries`. They keep the same byte cap but take no
cursor, because the response itself names what did not fit.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from baton import search
from baton.config import Config
from baton.conversations import require_enabled
from baton.database import Connection
from baton.errors import GraphUnavailable, InvalidRequest, ItemTooLarge, SnapshotUnavailable
from baton.graph.entries import Entries, words
from baton.graph.snapshot import edge_key, ordinal_key, statement_key
from baton.graph.source import canonical

MAX_LIMIT = 50
MAX_RESPONSE_BYTES = 32 * 1024
HISTORY_LIMIT = 20
MAX_QUERIES = 3
MAX_QUERY_CHARS = 200
MAX_IDS = 12
MAX_EXCLUDE = 200
MAX_ID_CHARS = 32
STATES = ("active", "all")
COLLECTIONS = {
    "read_graph": ("strands", "decisions"),
    "read_graph_strand": ("statements", "decisions"),
    "read_graph_statement": ("edges", "strands"),
    "search_graph": ("entries",),
    "read_graph_entries": ("entries",),
}
CURSOR_FIELDS = ("v", "tool", "conversation_id", "snapshot_id", "collection", "state", "target", "head_seq", "after")


def read_graph(
    conn: Connection,
    config: Config,
    role: str,
    conversation_id: str,
    *,
    producer: str | None = None,
    snapshot_id: str | None = None,
    state: str = "active",
    collection: str = "strands",
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Open a conversation's working view and page its strand cards or applicable decisions."""
    if state not in STATES:
        raise InvalidRequest("state is active or all")
    if producer is not None and not isinstance(producer, str):
        raise InvalidRequest("producer is a name")
    request = _Request("read_graph", conversation_id, snapshot_id, collection, limit, cursor, state=state)
    with conn.snapshot():
        opened = _open(conn, config, role, request, producer)
        snapshot = opened["snapshot"]
        if collection == "strands":
            rows = [_card(strand) for strand in _strands(snapshot, state)]
        else:
            rows = _decisions(snapshot, {s["id"] for s in _strands(snapshot, state)}, history=state == "all")
        response = _envelope(opened, request)
        if request.after is None:  # the initial page carries the view and the raw-tail navigation
            response["working_view"] = {"body": opened["version"]["body"], "citations": opened["version"]["citations"]}
            response["tail"] = _tail(conversation_id, snapshot["covered_through_seq"], opened["head_seq"])
        return _page(response, rows, request, opened)


def read_graph_strand(
    conn: Connection,
    config: Config,
    role: str,
    conversation_id: str,
    snapshot_id: str,
    strand_id: str,
    *,
    collection: str = "statements",
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """One strand's summary with its member statements or the shared decisions that apply to it."""
    request = _Request("read_graph_strand", conversation_id, snapshot_id, collection, limit, cursor, target=strand_id)
    with conn.snapshot():
        opened = _open(conn, config, role, request, None)
        snapshot = opened["snapshot"]
        strand = snapshot["strands"].get(strand_id) if isinstance(strand_id, str) else None
        if strand is None:
            raise InvalidRequest(f"this graph has no strand {strand_id}")
        if collection == "statements":
            rows = [
                _statement_row(snapshot["statements"][sid])
                for sid in sorted(strand["statement_ids"], key=statement_key)
            ]
        else:
            rows = _decisions(snapshot, {strand_id}, history=True)
        response = _envelope(opened, request)
        response["strand"] = {
            key: strand[key] for key in ("id", "title", "state", "summary", "open_work", "summary_through", "evidence")
        }
        return _page(response, rows, request, opened)


def read_graph_statement(
    conn: Connection,
    config: Config,
    role: str,
    conversation_id: str,
    snapshot_id: str,
    statement_id: str,
    *,
    collection: str = "edges",
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """One immutable statement with its quotes and evidence, plus its DAG edges or strand memberships."""
    request = _Request(
        "read_graph_statement", conversation_id, snapshot_id, collection, limit, cursor, target=statement_id
    )
    with conn.snapshot():
        opened = _open(conn, config, role, request, None)
        snapshot = opened["snapshot"]
        statement = snapshot["statements"].get(statement_id) if isinstance(statement_id, str) else None
        if statement is None:
            raise InvalidRequest(f"this graph has no statement {statement_id}")
        if collection == "edges":
            rows = [
                {"direction": "outgoing" if edge["from"] == statement_id else "incoming", **edge}
                for edge in snapshot["edges"]
                if statement_id in (edge["from"], edge["to"])
            ]
        else:
            rows = [_card(snapshot["strands"][sid]) for sid in sorted(statement["strand_ids"], key=ordinal_key)]
        response = _envelope(opened, request)
        response["statement"] = dict(statement)
        return _page(response, rows, request, opened)


def search_graph(
    conn: Connection,
    config: Config,
    role: str,
    conversation_id: str,
    *,
    queries: list[str] | None = None,
    exclude: list[str] | None = None,
    snapshot_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """The standing statements and active decisions that best match the queries; with none, the strand outline.

    `exclude` names entries the caller already holds; a search leaves them out and returns the next best.
    """
    queries, exclude = _queries(queries), _exclude(exclude)
    request = _Request("search_graph", conversation_id, snapshot_id, "entries", limit, None)
    with conn.snapshot():
        opened = _open(conn, config, role, request, None)
    entries = Entries(opened["snapshot"])
    lines = entries.search(queries, limit, exclude) if queries else entries.outline()
    return _fit(_label(opened), [(line, [line]) for line in lines], lambda rest: ("omitted", len(rest)))


def read_graph_entries(
    conn: Connection,
    config: Config,
    role: str,
    conversation_id: str,
    ids: list[str],
    *,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Whole entries by id, in the order asked and each once; `skipped` says why an id returned nothing."""
    ids = _ids(ids)
    request = _Request("read_graph_entries", conversation_id, snapshot_id, "entries", len(ids), None)
    with conn.snapshot():
        opened = _open(conn, config, role, request, None)
    entries = Entries(opened["snapshot"])
    response, groups, skipped, seen = _label(opened), [], {}, set()
    for wanted in ids:
        found = entries.find(wanted)
        if not found:
            skipped[wanted] = entries.why_missing(wanted)
            continue
        groups.append((wanted, [text for uid, text in found if uid not in seen]))
        seen.update(uid for uid, _ in found)
    if skipped:
        response["skipped"] = skipped
    return _fit(response, groups, lambda rest: ("remaining", [wanted for wanted, _ in rest]))


class _Request:
    """Validated arguments of one read, with its cursor decoded and checked field by field."""

    def __init__(self, tool, conversation_id, snapshot_id, collection, limit, cursor, *, state=None, target=None):
        if collection not in COLLECTIONS[tool]:
            raise InvalidRequest(f"collection is one of {', '.join(COLLECTIONS[tool])}")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise InvalidRequest(f"limit must be 1-{MAX_LIMIT}")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise InvalidRequest("conversation_id is required")
        if snapshot_id is not None and (not isinstance(snapshot_id, str) or not snapshot_id):
            raise InvalidRequest("snapshot_id is a graph snapshot id from a summary label or read_graph")
        self.tool, self.conversation_id, self.collection, self.limit = tool, conversation_id, collection, limit
        self.snapshot_id, self.state, self.target = snapshot_id, state, target
        self.head_seq: int | None = None
        self.after: Any = None
        if cursor is not None:
            self._decode(cursor)

    def _decode(self, cursor):
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        except (ValueError, TypeError, AttributeError, binascii.Error):
            decoded = None
        if not isinstance(decoded, dict) or set(decoded) != set(CURSOR_FIELDS) or decoded["v"] != 1:
            raise InvalidRequest("cursor must be a next_cursor from this tool, unchanged")
        bound = {
            "tool": self.tool,
            "conversation_id": self.conversation_id,
            "collection": self.collection,
            "state": self.state,
            "target": self.target,
        }
        if any(decoded[key] != value for key, value in bound.items()):
            raise InvalidRequest("cursor belongs to a different tool, conversation, collection or target")
        if self.snapshot_id is not None and decoded["snapshot_id"] != self.snapshot_id:
            raise InvalidRequest("cursor belongs to a different graph snapshot")
        if type(decoded["head_seq"]) is not int or decoded["head_seq"] < 1 or decoded["after"] is None:
            raise InvalidRequest("cursor position is invalid")
        self.snapshot_id, self.head_seq, self.after = decoded["snapshot_id"], decoded["head_seq"], decoded["after"]

    def cursor(self, after):
        value = {
            "v": 1,
            "tool": self.tool,
            "conversation_id": self.conversation_id,
            "snapshot_id": self.snapshot_id,
            "collection": self.collection,
            "state": self.state,
            "target": self.target,
            "head_seq": self.head_seq,
            "after": after,
        }
        return base64.urlsafe_b64encode(canonical(value).encode("utf-8")).decode("ascii")


def _open(conn, config, role, request, producer):
    """Authorize and select the served graph in the caller's snapshot; nothing is revealed before this passes."""
    require_enabled(config)
    if not config.search.enabled:
        raise GraphUnavailable("derived graphs are not served on this host")
    head_row = conn.execute("SELECT last_seq FROM conversations WHERE id = ?", (request.conversation_id,)).fetchone()
    served = [
        search._artifact(row) for row in search.served_summaries(conn, config, role, request.conversation_id, producer)
    ]
    if head_row is None or not served:  # unknown, unreadable and unpublished all read the same
        raise GraphUnavailable("no derived graph is available to you for this conversation")
    current_head = head_row["last_seq"]
    if request.snapshot_id is None:
        version = search.summary_winner(served, current_head)
        if version is None:
            raise GraphUnavailable("no derived graph is available to you for this conversation")
    else:
        matching = [item for item in served if item["version_id"] == request.snapshot_id]
        if not matching:
            raise SnapshotUnavailable("that graph snapshot is no longer served for this conversation; open it again")
        version = matching[0]
    if version["graph_snapshot_id"] is None:
        raise GraphUnavailable("the current summary for this conversation has no graph attachment")
    stored = conn.execute("SELECT snapshot FROM graph_snapshots WHERE version_id = ?", (version["version_id"],)).one()
    if request.head_seq is None:
        request.head_seq = current_head
    elif request.head_seq > current_head:
        raise SnapshotUnavailable("the captured head is ahead of this conversation; open the graph again")
    request.snapshot_id = version["version_id"]
    return {"version": version, "snapshot": json.loads(stored["snapshot"]), "head_seq": request.head_seq}


def _envelope(opened, request):
    version = opened["version"]
    return {
        "conversation_id": request.conversation_id,
        "snapshot_id": version["version_id"],
        "producer": version["producer"],
        "generation": version["generation"],
        "revision": opened["snapshot"]["revision"],
        "covered_through_seq": opened["snapshot"]["covered_through_seq"],
        "head_seq": opened["head_seq"],
        "derived": True,
        "authoritative": False,
    }


def _label(opened):
    """An entry read before its lines: what they came from, kept short because the caller pays for every field."""
    return {
        "snapshot_id": opened["version"]["version_id"],
        "covered_through_seq": opened["snapshot"]["covered_through_seq"],
        "head_seq": opened["head_seq"],
        "derived": True,
        "authoritative": False,
        "entries": [],
    }


def _queries(queries):
    if queries is None:
        return []
    if not isinstance(queries, list) or len(queries) > MAX_QUERIES:
        raise InvalidRequest(f"queries is a list of at most {MAX_QUERIES} strings")
    for query in queries:
        if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS or not words(query):
            raise InvalidRequest(f"each query is at most {MAX_QUERY_CHARS} characters and has at least one word")
    return queries


def _ids(ids):
    if not _id_list(ids, 1, MAX_IDS):
        raise InvalidRequest(f"ids is a list of 1-{MAX_IDS} entry ids such as 120-2, D12, S3 or post:120")
    return list(dict.fromkeys(ids))


def _exclude(exclude):
    if exclude is None:
        return []
    if not _id_list(exclude, 0, MAX_EXCLUDE):
        raise InvalidRequest(f"exclude is a list of at most {MAX_EXCLUDE} ids of entries you already hold")
    return exclude


def _id_list(value, least, most):
    listed = isinstance(value, list) and least <= len(value) <= most
    return listed and all(isinstance(item, str) and 0 < len(item) <= MAX_ID_CHARS for item in value)


def _fit(response, groups, left_out):
    """Add whole groups of lines, in order, for as long as the response stays within the byte cap.

    `left_out(rest)` is the (field, value) that tells the caller which groups did not fit, and it is budgeted
    like everything else in the response. No group is returned in part, so a first group that cannot fit is
    refused rather than cut.
    """
    for index, (_, lines) in enumerate(groups):
        rest = groups[index + 1 :]
        trial = response | {"entries": response["entries"] + lines} | dict([left_out(rest)] if rest else [])
        if len(canonical(trial).encode("utf-8")) > MAX_RESPONSE_BYTES:
            if index == 0:
                raise ItemTooLarge("this graph entry exceeds the response cap and is never returned in part")
            return response | dict([left_out(groups[index:])])
        response["entries"] = trial["entries"]
    return response


def _tail(conversation_id, covered, head):
    if covered >= head:
        return {"status": "complete", "from_seq": covered + 1, "through_seq": head, "read_history_args": None}
    return {
        "status": "unprocessed",
        "from_seq": covered + 1,
        "through_seq": head,
        "read_history_args": {
            "conversation_id": conversation_id,
            "after_seq": covered,
            "through_seq": head,
            "limit": HISTORY_LIMIT,
        },
        "notice": "Posts from from_seq through through_seq are absent from the working view; read them raw.",
    }


def _strands(snapshot, state):
    ordered = sorted(snapshot["strands"].values(), key=lambda s: ordinal_key(s["id"]))
    return [s for s in ordered if state == "all" or s["state"] == "active"]


def _decisions(snapshot, strand_ids, *, history):
    return [
        snapshot["decisions"][key]
        for key in sorted(snapshot["decisions"], key=ordinal_key)
        for decision in (snapshot["decisions"][key],)
        if (history or decision["status"] == "active")
        and (decision["applies_to"] == ["all"] or set(decision["applies_to"]) & strand_ids)
    ]


def _card(strand):
    return {
        "id": strand["id"],
        "title": strand["title"],
        "state": strand["state"],
        "summary_through": strand["summary_through"],
        "statement_count": len(strand["statement_ids"]),
    }


def _statement_row(statement):
    return {key: statement[key] for key in ("id", "seq", "text", "surviving_text", "relevance", "strand_ids", "source")}


def _row_key(request, row):
    match request.collection:
        case "statements":
            return list(statement_key(row["id"]))
        case "edges":
            return [row["from"], row["to"], row["kind"]]
        case _:
            return ordinal_key(row["id"])


def _sort_key(request, key):
    if request.collection == "edges":
        return edge_key({"from": key[0], "to": key[1], "kind": key[2]})
    return tuple(key) if isinstance(key, list) else key


def _check_after(request):
    after = request.after
    match request.collection:
        case "statements":
            valid = isinstance(after, list) and len(after) == 2 and all(type(v) is int and v > 0 for v in after)
        case "edges":
            valid = isinstance(after, list) and len(after) == 3 and all(isinstance(v, str) for v in after)
            if valid:
                try:
                    _sort_key(request, after)
                except ValueError:
                    valid = False
        case _:
            valid = type(after) is int and after > 0
    if not valid:
        raise InvalidRequest("cursor position is invalid")


def _page(response, rows, request, opened):
    """Whole rows after the cursor position, within the limit and the byte cap; a cursor only when rows remain."""
    keyed = [(_row_key(request, row), row) for row in rows]
    if request.after is not None:
        _check_after(request)
        start = _sort_key(request, request.after)
        keyed = [(key, row) for key, row in keyed if _sort_key(request, key) > start]
    response["items"], response["next_cursor"] = [], None
    size = len(canonical(response).encode("utf-8"))
    if size > MAX_RESPONSE_BYTES:
        raise ItemTooLarge("this graph detail exceeds the response cap and is never returned in part")
    for index, (_, row) in enumerate(keyed):
        if index >= request.limit:
            break
        grown = size + len(canonical(row).encode("utf-8")) + 1
        if grown > MAX_RESPONSE_BYTES:
            if not response["items"]:
                raise ItemTooLarge("this graph row exceeds the response cap and is never returned in part")
            break
        response["items"].append(row)
        size = grown
    if len(response["items"]) < len(keyed):
        # The cursor is part of the response, so it is budgeted too: trim whole rows until everything fits.
        while response["items"]:
            response["next_cursor"] = request.cursor(_row_key(request, response["items"][-1]))
            if len(canonical(response).encode("utf-8")) <= MAX_RESPONSE_BYTES:
                break
            response["items"].pop()
        else:
            raise ItemTooLarge("this graph row exceeds the response cap and is never returned in part")
    return response
