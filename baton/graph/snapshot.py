"""Canonical graph snapshot: one projection shared by the producer and the host commit validator.

A snapshot is the graph checkpoint through W without local receipts, dispositions or raw post bodies. Its
digest binds the structured publication to the compact working view; the host recomputes it and validates
every record against the canonical native posts before anything commits.
"""

import re

from baton.graph.contract import require
from baton.graph.source import canonical, digest, prefix_digest
from baton.graph.validation import check_dag, check_evidence, check_relevance

SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 20_000_000
DIGEST_FIELDS = (
    "schema_version",
    "conversation_id",
    "covered_through_seq",
    "revision",
    "source_prefix_digest",
    "statements",
    "strands",
    "decisions",
    "edges",
)
FIELDS = (*DIGEST_FIELDS, "graph_digest")
STATEMENT_FIELDS = (
    "id",
    "seq",
    "text",
    "quotes",
    "strand_ids",
    "relevance",
    "surviving_text",
    "evidence",
    "source",
    "relevance_evidence",
    "relevance_history",
)
STRAND_FIELDS = (
    "id",
    "title",
    "summary",
    "open_work",
    "state",
    "statement_ids",
    "summary_through",
    "evidence",
    "last_changed",
)
DECISION_FIELDS = ("id", "text", "applies_to", "status", "evidence", "history", "last_changed")
EDGE_FIELDS = ("from", "to", "kind", "evidence")
EDGE_KINDS = ("responds_to", "supports", "contradicts", "supersedes", "depends_on")
RELEVANCE = ("directly_relevant", "partially_superseded", "completely_superseded")
STATEMENT_ID = re.compile(r"([1-9][0-9]*)-([1-9][0-9]*)")
STRAND_ID = re.compile(r"S[1-9][0-9]*")
DECISION_ID = re.compile(r"D[1-9][0-9]*")


def statement_key(statement_id):
    """Numeric (seq, ordinal) order of a stable `{post}-{ordinal}` statement ID."""
    match = STATEMENT_ID.fullmatch(statement_id) if isinstance(statement_id, str) else None
    if match is None:
        require(False, "invalid statement ID")
        raise AssertionError("unreachable")
    return int(match.group(1)), int(match.group(2))


def ordinal_key(stable_id):
    """Numeric order of an S or D identifier."""
    require(isinstance(stable_id, str) and bool(re.fullmatch(r"[SD][1-9][0-9]*", stable_id)), "invalid stable ID")
    return int(stable_id[1:])


def edge_key(edge):
    return statement_key(edge["from"]), statement_key(edge["to"]), edge["kind"]


def ordered_edges(edges):
    return sorted(edges, key=edge_key)


def digest_of(snapshot):
    """SHA-256 of exactly the digest fields, in canonical JSON; the digest field itself is never hashed."""
    return digest({key: snapshot[key] for key in DIGEST_FIELDS})


def _record(item, fields):
    return {key: item[key] for key in fields if key in item}


def project(state):
    """The publishable projection of a checkpoint: immutable records only, edges in canonical order."""
    require(type(state.get("watermark")) is int and state["watermark"] > 0, "graph has no completed prefix")
    require(type(state.get("revision")) is int and state["revision"] > 0, "graph has no accepted revision")
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "conversation_id": state["conversation_id"],
        "covered_through_seq": state["watermark"],
        "revision": state["revision"],
        "source_prefix_digest": state["source_prefix_digest"],
        "statements": {key: _record(value, STATEMENT_FIELDS) for key, value in state["statements"].items()},
        "strands": {key: _record(value, STRAND_FIELDS) for key, value in state["strands"].items()},
        "decisions": {key: _record(value, DECISION_FIELDS) for key, value in state["decisions"].items()},
        "edges": ordered_edges([_record(edge, EDGE_FIELDS) for edge in state["edges"]]),
    }
    snapshot["graph_digest"] = digest_of(snapshot)
    return snapshot


def _text(value, label):
    require(isinstance(value, str) and bool(value.strip()), f"{label} must be nonempty text")


def _refs(value, label):
    require(isinstance(value, list) and bool(value) and all(isinstance(v, str) and v for v in value), f"{label} refs")
    require(len(set(value)) == len(value), f"{label} repeats a reference")


def _ids(value, pattern, label, *, allow_empty=False):
    require(isinstance(value, list) and (allow_empty or bool(value)), f"{label} must be a list of IDs")
    require(all(isinstance(v, str) and pattern.fullmatch(v) for v in value), f"{label} has an invalid ID")
    require(len(set(value)) == len(value), f"{label} repeats an ID")


def _keyed(records, fields, label):
    require(isinstance(records, dict), f"{label} must be keyed by ID")
    for key, record in records.items():
        require(isinstance(record, dict) and set(record) == set(fields), f"{label} record has missing or unknown keys")
        require(record["id"] == key, f"{label} key differs from its record ID")


def _check_statements(snapshot, source, end):
    statements = snapshot["statements"]
    _keyed(statements, STATEMENT_FIELDS, "statement")
    by_seq: dict[int, list[str]] = {}
    for key, item in statements.items():
        seq, _ = statement_key(key)
        require(
            type(item["seq"]) is int and item["seq"] == seq and 1 <= seq <= end,
            "statement seq does not match its ID within W",
        )
        by_seq.setdefault(seq, []).append(key)
        _text(item["text"], "statement text")
        post = source["messages"][seq - 1]
        _refs(item["quotes"], "quotes")
        require(all(quote in post["text"] for quote in item["quotes"]), "quote is not exact source text")
        # Canonical JSON keeps booleans and integers apart, where Python's equality would not.
        require(
            canonical(item["source"]) == canonical({k: v for k, v in post.items() if k != "text"}),
            "statement source differs from native post",
        )
        _ids(item["strand_ids"], STRAND_ID, "strand_ids")
        require(all(sid in snapshot["strands"] for sid in item["strand_ids"]), "statement references unknown strand")
        require(item["relevance"] in RELEVANCE, "unsupported relevance")
        check_relevance(item)
        _refs(item["relevance_evidence"], "relevance_evidence")
        require(isinstance(item["relevance_history"], list), "relevance_history must be a list")
        for entry in item["relevance_history"]:
            require(
                isinstance(entry, dict) and set(entry) == {"relevance", "surviving_text", "evidence"},
                "relevance_history entry has missing or unknown keys",
            )
            require(entry["relevance"] in RELEVANCE, "unsupported historical relevance")
            _refs(entry["evidence"], "relevance_history evidence")
    for seq, ids in by_seq.items():
        require(set(ids) == {f"{seq}-{i}" for i in range(1, len(ids) + 1)}, "statement ordinals must start at 1")


def _check_strands(snapshot, end):
    _keyed(snapshot["strands"], STRAND_FIELDS, "strand")
    for key, strand in snapshot["strands"].items():
        require(STRAND_ID.fullmatch(key) is not None, "invalid strand ID")
        _text(strand["title"], "strand title")
        _text(strand["summary"], "strand summary")
        require(
            isinstance(strand["open_work"], list)
            and all(isinstance(w, str) and w.strip() for w in strand["open_work"]),
            "open_work must list nonempty text",
        )
        require(strand["state"] in ("active", "resolved"), "unsupported strand state")
        _ids(strand["statement_ids"], STATEMENT_ID, "statement_ids")
        actual = {sid for sid, statement in snapshot["statements"].items() if key in statement["strand_ids"]}
        require(set(strand["statement_ids"]) == actual, "strand membership must exactly match statements")
        require(type(strand["summary_through"]) is int and 1 <= strand["summary_through"] <= end, "invalid frontier")
        require(type(strand["last_changed"]) is int and 1 <= strand["last_changed"] <= end, "invalid strand change seq")


def _check_decisions(snapshot, end):
    _keyed(snapshot["decisions"], DECISION_FIELDS, "decision")
    for key, decision in snapshot["decisions"].items():
        require(DECISION_ID.fullmatch(key) is not None, "invalid decision ID")
        _text(decision["text"], "decision text")
        require(type(decision["last_changed"]) is int and 1 <= decision["last_changed"] <= end, "invalid change seq")
        _check_applies(decision["applies_to"], snapshot["strands"])
        require(decision["status"] in ("active", "superseded"), "unsupported decision status")
        require(isinstance(decision["history"], list), "decision history must be a list")
        for entry in decision["history"]:
            require(
                isinstance(entry, dict) and set(entry) == {"status", "applies_to", "evidence"},
                "decision history entry has missing or unknown keys",
            )
            require(entry["status"] in ("active", "superseded"), "unsupported historical decision status")
            _check_applies(entry["applies_to"], snapshot["strands"])
            _refs(entry["evidence"], "decision history evidence")


def _check_applies(applies, strands):
    require(isinstance(applies, list) and bool(applies), "applies_to must be nonempty")
    if applies != ["all"]:
        _ids(applies, STRAND_ID, "applies_to")
        require(all(sid in strands for sid in applies), "unknown decision applicability")


def _check_edges(snapshot):
    edges = snapshot["edges"]
    require(isinstance(edges, list), "edges must be a list")
    for edge in edges:
        require(isinstance(edge, dict) and set(edge) == set(EDGE_FIELDS), "edge has missing or unknown keys")
        require(edge["kind"] in EDGE_KINDS, "unsupported edge kind")
        for key in ("from", "to"):
            require(edge[key] in snapshot["statements"], "edge references unknown statement")
    require(edges == ordered_edges(edges), "edges are not in canonical order")
    check_dag(edges, snapshot["statements"])


def validate(snapshot, source):
    """Reject anything but a well-formed, digest-bound snapshot of `source` through its covered W.

    `source` is the canonical native prefix (`graph.source.load_source` or `graph.export.native_prefix` shape)
    reaching at least W. Validation never trusts the producer's local files: every quote, source attribution,
    membership, edge and evidence reference is checked here against the posts themselves.
    """
    require(isinstance(snapshot, dict) and set(snapshot) == set(FIELDS), "graph snapshot has missing or unknown keys")
    require(
        type(snapshot["schema_version"]) is int and snapshot["schema_version"] == SCHEMA_VERSION,
        "unsupported graph snapshot schema_version",
    )
    require(snapshot["conversation_id"] == source["conversation_id"], "graph snapshot conversation mismatch")
    end = snapshot["covered_through_seq"]
    require(type(end) is int and 1 <= end <= len(source["messages"]), "graph snapshot W is outside the native prefix")
    require(type(snapshot["revision"]) is int and snapshot["revision"] >= 1, "graph revision must be positive")
    require(snapshot["source_prefix_digest"] == prefix_digest(source, end), "graph snapshot source prefix differs")
    require(snapshot["graph_digest"] == digest_of(snapshot), "graph digest does not match its content")
    require(len(canonical(snapshot).encode("utf-8")) <= MAX_SNAPSHOT_BYTES, "graph snapshot exceeds 20000000 bytes")
    _check_strands(snapshot, end)
    _check_statements(snapshot, source, end)
    _check_decisions(snapshot, end)
    _check_edges(snapshot)
    items = (
        list(snapshot["statements"].values())
        + list(snapshot["strands"].values())
        + list(snapshot["decisions"].values())
        + snapshot["edges"]
    )
    check_evidence(items, snapshot["statements"], end)
    for item in snapshot["statements"].values():
        check_evidence([{"evidence": item["relevance_evidence"]}], snapshot["statements"], end)
        check_evidence(item["relevance_history"], snapshot["statements"], end)
    for item in snapshot["decisions"].values():
        check_evidence(item["history"], snapshot["statements"], end)


def resolve_evidence(refs, snapshot, source):
    """Native message IDs for evidence references that resolve within W, or a GraphError."""
    end = snapshot["covered_through_seq"]
    seqs = set()
    for ref in refs:
        if ref.startswith("post:"):
            require(bool(re.fullmatch(r"post:[1-9][0-9]*", ref)), "invalid graph evidence")
            seq = int(ref[5:])
        else:
            require(ref in snapshot["statements"], "unknown graph statement evidence")
            seq = snapshot["statements"][ref]["seq"]
        require(1 <= seq <= end, "graph evidence is beyond completed W")
        seqs.add(seq)
    return [source["messages"][seq - 1]["message_id"] for seq in sorted(seqs)]
