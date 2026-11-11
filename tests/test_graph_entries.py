"""One-line graph entries: what each line says, what a search ranks, and what an id loads.

A hand-written snapshot, no database and no model. It has a partly replaced statement, a fully replaced one,
a statement over the excerpt size, one in no strand, a superseded decision and decisions resting on replaced
statements, so each scenario can check what a reader is shown and what is left out.
"""

from __future__ import annotations

import pytest

from baton.database import store
from baton.errors import BatonError
from baton.graph.entries import WHOLE_BYTES, Entries

LONG = "Rotate the signing key every ninety days. " + "The schedule is reviewed by the operator each quarter. " * 8


def statement(sid: str, text: str, strands: list[str], *, relevance="directly_relevant", surviving=None) -> dict:
    return {
        "id": sid,
        "seq": int(sid.split("-")[0]),
        "text": text,
        "strand_ids": strands,
        "relevance": relevance,
        "surviving_text": surviving,
    }


def decision(did: str, text: str, applies_to: list[str], evidence: list[str], changed: int, status="active") -> dict:
    return {
        "id": did,
        "text": text,
        "applies_to": applies_to,
        "evidence": evidence,
        "last_changed": changed,
        "status": status,
    }


def strand(sid: str, title: str, state: str, summary: str, work: list[str], members: list[str], through: int) -> dict:
    return {
        "id": sid,
        "title": title,
        "state": state,
        "summary": summary,
        "open_work": work,
        "statement_ids": members,
        "summary_through": through,
    }


def graph() -> dict:
    statements = [
        statement(
            "1-1",
            "Store receipts as JSON\n  for a fortnight.",
            ["S1"],
            relevance="partially_superseded",
            surviving="Store receipts as JSON.",
        ),
        statement("2-1", "Keep the index in SQLite.", ["S2"], relevance="completely_superseded"),
        statement("3-1", "Keep receipts for one hour.", ["S1"]),
        statement("3-2", "Keep the index in flat files.", ["S2"]),
        statement("4-1", LONG, ["S2", "S1"]),
        statement("5-1", "Nobody owns the migration yet.", []),
        statement("12-1", "Flat files survived the restart drill.", ["S2"]),
    ]
    decisions = [
        decision("D1", "Quote the source exactly.", ["all"], ["4-1", "post:5"], 5),
        decision("D2", "Use SQLite for the index.", ["S2"], ["2-1"], 3, status="superseded"),
        decision("D3", "Receipts are JSON.", ["S2", "S1"], ["1-1", "2-1"], 3),
        decision("D4", "Nobody is assigned.", ["S2"], ["2-1"], 12),
    ]
    strands = [
        strand(
            "S1", "Receipt  format", "resolved", "Receipts are JSON, kept one hour.\n", [], ["1-1", "3-1", "4-1"], 5
        ),
        strand(
            "S2",
            "Index storage",
            "active",
            "The index moved from SQLite to flat files.",
            ["Write the reader.", "Pick an owner\nfor the migration."],
            ["2-1", "3-2", "4-1", "12-1"],
            12,
        ),
    ]
    edges = [  # deliberately out of canonical order: connections must not depend on how the edges are stored
        ("12-1", "3-2", "supports"),
        ("4-1", "3-2", "depends_on"),
        ("4-1", "1-1", "responds_to"),
        ("3-2", "2-1", "supersedes"),
        ("3-1", "1-1", "supersedes"),
        ("2-1", "1-1", "responds_to"),
    ]
    return {
        "covered_through_seq": 12,
        "statements": {item["id"]: item for item in statements},
        "decisions": {item["id"]: item for item in decisions},
        "strands": {item["id"]: item for item in strands},
        "edges": [{"from": source, "to": target, "kind": kind} for source, target, kind in edges],
    }


def line(entries: Entries, wanted: str) -> str:
    [(found, text)] = entries.find(wanted)
    assert found == wanted and "\n" not in text
    return text


def test_a_line_carries_the_id_strands_connections_and_standing_text() -> None:
    entries = Entries(graph())
    # A partly replaced statement shows only what still stands, flattened to one line; its connections name
    # the direction: 3-1 replaces part of it and 4-1 responds to it.
    assert line(entries, "1-1") == "1-1 | S1 | supersedes<3-1 responds_to<4-1 | Store receipts as JSON."
    assert line(entries, "3-1") == "3-1 | S1 | supersedes>1-1 | Keep receipts for one hour."
    # The fully replaced 2-1 is gone, and so is every connection to it.
    assert line(entries, "3-2") == "3-2 | S2 | depends_on<4-1 supports<12-1 | Keep the index in flat files."
    assert line(entries, "4-1") == f"4-1 | S1 S2 | responds_to>1-1 depends_on>3-2 | {LONG.strip()}"
    assert line(entries, "5-1") == "5-1 | - | - | Nobody owns the migration yet."
    assert line(entries, "12-1") == "12-1 | S2 | supports>3-2 | Flat files survived the restart drill."

    # A decision names what it rests on, without the statements that no longer stand.
    assert line(entries, "D1") == "D1 | all | rests on 4-1 post:5 | changed at post 5 | Quote the source exactly."
    assert line(entries, "D3") == "D3 | S1 S2 | rests on 1-1 | changed at post 3 | Receipts are JSON."
    assert line(entries, "D4") == "D4 | S2 | rests on - | changed at post 12 | Nobody is assigned."

    assert entries.outline() == [
        "S1 | resolved | 3 statements, summarized through post 5 | Receipt format",
        "S2 | active | 4 statements, summarized through post 12 | Index storage",
    ]


def test_ids_load_strands_and_posts_and_say_why_nothing_came_back() -> None:
    entries = Entries(graph())
    card = "S1 | resolved | Receipt format\nReceipts are JSON, kept one hour.\nOpen work:\n(none)"
    assert entries.find("S1") == [("S1", card)]
    assert entries.find("S2") == [
        (
            "S2",
            "S2 | active | Index storage\nThe index moved from SQLite to flat files.\n"
            "Open work:\n- Write the reader.\n- Pick an owner for the migration.",
        )
    ]
    assert [found for found, _ in entries.find("post:3")] == ["3-1", "3-2"]
    assert entries.find("post:3")[1] == ("3-2", line(entries, "3-2"))

    reasons = {
        "2-1": "replaced in full by 3-2",
        "D2": "superseded decision",
        "post:2": "no standing statements from that post",
        "post:13": "that post is after this graph's coverage; read it with read_history",
        "post:0": "no such id; ids look like 120-2, D12, S3 or post:120",
        "s1": "no such id; ids look like 120-2, D12, S3 or post:120",
    }
    for wanted, reason in reasons.items():
        assert entries.find(wanted) == [] and entries.why_missing(wanted) == reason, wanted


def test_search_matches_any_word_stemmed_and_ranks_across_queries() -> None:
    entries = Entries(graph())
    ids = lambda lines: [found.split(" | ")[0] for found in lines]  # noqa: E731

    # Any word is enough and words are stemmed; the entry with both words comes first.
    assert ids(entries.search(["receipt hour"], 20)) == ["3-1", "D3", "1-1"]
    assert entries.search(["receipt hour"], 20)[0] == line(entries, "3-1")
    assert ids(entries.search(["receipt hour"], 1)) == ["3-1"]
    # What the caller already holds is left out before ranking, so the next best takes its place.
    assert ids(entries.search(["receipt hour"], 1, ["3-1", "9-9"])) == ["D3"]
    assert ids(entries.search(["receipt hour"], 20, {"3-1", "1-1"})) == ["D3"]
    # An entry ranked by two queries beats one ranked higher by a single query.
    assert ids(entries.search(["receipts json"], 20)) == ["D3", "1-1", "3-1"]
    assert ids(entries.search(["receipts json", "store"], 20)) == ["1-1", "D3", "3-1"]

    # Only standing text is searched: SQLite survives only in the replaced statement, the superseded
    # decision and a strand summary, and "fortnight" only in the replaced half of 1-1.
    assert entries.search(["sqlite"], 20) == [] and entries.search(["fortnight"], 20) == []
    # Whatever a caller writes is searched as words, never as query syntax.
    assert ids(entries.search(['receipts" OR NOT ( *'], 20)) == ["D3", "1-1", "3-1"]
    assert entries.search(["!!!"], 20) == [] and entries.search(["_"], 20) == []


def test_a_long_entry_comes_back_as_an_excerpt_that_says_how_much_there_is() -> None:
    entries = Entries(graph())
    whole = line(entries, "4-1")
    size = len(LONG.strip().encode("utf-8"))
    assert size > WHOLE_BYTES
    [hit] = entries.search(["quarter"], 20)
    assert hit.startswith("4-1 | S1 S2 | responds_to>1-1 depends_on>3-2 | ") and "quarter" in hit
    assert hit.endswith(f" … [{size} bytes in full]") and len(hit) < len(whole) / 2 and "\n" not in hit


def test_search_without_fts5_is_refused_and_the_rest_still_reads(monkeypatch) -> None:
    monkeypatch.setattr(store, "fts5_available", lambda conn: False)
    entries = Entries(graph())
    with pytest.raises(BatonError) as raised:
        entries.search(["receipts"], 20)
    assert raised.value.code == "graph_unavailable" and "FTS5" in str(raised.value)
    assert len(entries.outline()) == 2 and entries.find("3-1")
