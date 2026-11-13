"""The compact contract: what a compact candidate must cover and bind before it replaces a graph view."""

import json

import pytest
from test_graph_worker import compact_candidate

from baton.graph import compact
from baton.graph import snapshot as snapshot_module


def test_compact_contract_rejects_coverage_gaps_and_stale_digests() -> None:
    source = {
        "conversation_id": "c",
        "messages": [
            {
                "seq": 1,
                "message_id": "m1",
                "author": "a",
                "at": "t1",
                "text": "one",
                "reply_to": None,
                "conversation_id": "c",
            },
            {
                "seq": 2,
                "message_id": "m2",
                "author": "b",
                "at": "t2",
                "text": "two",
                "reply_to": None,
                "conversation_id": "c",
            },
        ],
    }
    statement = {
        "id": "1-1",
        "seq": 1,
        "text": "one",
        "quotes": ["one"],
        "strand_ids": ["S1"],
        "relevance": "directly_relevant",
        "surviving_text": None,
        "evidence": ["post:1"],
        "source": {k: v for k, v in source["messages"][0].items() if k != "text"},
        "relevance_evidence": ["post:1"],
        "relevance_history": [],
    }
    state = {
        "conversation_id": "c",
        "watermark": 2,
        "revision": 1,
        "source_prefix_digest": snapshot_module.prefix_digest(source, 2),
        "statements": {
            "1-1": statement,
            "2-1": {
                **statement,
                "id": "2-1",
                "seq": 2,
                "text": "two",
                "quotes": ["two"],
                "strand_ids": ["S2"],
                "evidence": ["post:2"],
                "relevance_evidence": ["post:2"],
                "source": {k: v for k, v in source["messages"][1].items() if k != "text"},
            },
        },
        "strands": {
            "S1": {
                "id": "S1",
                "title": "T",
                "summary": [{"id": "S1.1", "text": "S"}],
                "open_work": [],
                "state": "active",
                "statement_ids": ["1-1"],
                "summary_through": 2,
                "evidence": ["post:1"],
                "last_changed": 2,
            },
            "S2": {
                "id": "S2",
                "title": "Closed",
                "summary": [{"id": "S2.1", "text": "Done"}],
                "open_work": [],
                "state": "resolved",
                "statement_ids": ["2-1"],
                "summary_through": 2,
                "evidence": ["post:2"],
                "last_changed": 2,
            },
        },
        "decisions": {
            "D1": {
                "id": "D1",
                "text": "Global",
                "applies_to": ["all"],
                "status": "active",
                "evidence": ["post:2"],
                "history": [],
                "last_changed": 2,
            },
            "D2": {
                "id": "D2",
                "text": "Closed only",
                "applies_to": ["S2"],
                "status": "active",
                "evidence": ["post:2"],
                "history": [],
                "last_changed": 2,
            },
        },
        "edges": [],
    }
    snapshot = snapshot_module.project(state)
    snapshot_module.validate(snapshot, source)
    # Booleans equal integers in Python; the attachment boundary must not accept them as versions or seqs.
    for mutate, match in (
        (lambda s: s.update(schema_version=True), "schema_version"),
        (lambda s: s["statements"]["1-1"].update(seq=True), "statement seq"),
        (lambda s: s["statements"]["1-1"]["source"].update(seq=True), "source differs"),
        # A strand's lines carry its own ids, and its summary keeps at least one.
        (lambda s: s["strands"]["S1"]["summary"][0].update(id="S2.1"), "summary line ID"),
        (lambda s: s["strands"]["S1"].update(open_work=[{"id": "S1.1", "text": "Do it."}]), "open_work line ID"),
        (lambda s: s["strands"]["S1"].update(summary=[]), "needs a line"),
    ):
        forged = json.loads(json.dumps(snapshot))
        mutate(forged)
        forged["graph_digest"] = snapshot_module.digest_of(forged)
        with pytest.raises(Exception, match=match):
            snapshot_module.validate(forged, source)
    request = compact.compact_request(snapshot, source, max_body_bytes=12_000, max_citations=64)
    assert [s["id"] for s in request["strands"]] == ["S1"] and [d["id"] for d in request["decisions"]] == ["D1"]
    assert [a["author"] for a in request["authors"]] == ["a", "b"]  # a deduplicated author table for the evidence
    good = compact_candidate(request, {})
    accepted = compact.validate_compact(good, snapshot, source, max_body_bytes=12_000, max_citations=64)
    assert accepted["citations"] == ["m1", "m2"]
    for bad, match in (
        ({"strand_ids": ["S1", "S2"]}, "strand_ids"),
        ({"decision_ids": ["D1", "D2"]}, "decision_ids"),
        ({"evidence": ["post:2"]}, "no recorded evidence for S1"),
        ({"evidence": ["post:3"]}, "beyond completed W"),
        ({"graph_digest": "0" * 64}, "digest"),
        ({"body": "x" * 13}, "byte limit"),
        ({"extra": 1}, "unknown keys"),
    ):
        with pytest.raises(Exception, match=match):
            compact.validate_compact(
                {**good, **bad}, snapshot, source, max_body_bytes=12 if "body" in bad else 12_000, max_citations=64
            )
    with pytest.raises(Exception, match="max_citations"):
        compact.validate_compact(good, snapshot, source, max_body_bytes=12_000, max_citations=1)
    rendered = compact.render_view(snapshot, source)
    assert "S1: T" in rendered["body"] and "S2" not in rendered["body"] and "D2" not in rendered["body"]
    assert compact.fits(rendered, snapshot, source, max_body_bytes=12_000, max_citations=64)
    assert not compact.fits(rendered, snapshot, source, max_body_bytes=10, max_citations=64)
