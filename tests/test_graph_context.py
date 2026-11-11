"""What an extraction request shows of the graph so far, and what a proposal may do with the rest.

The synthetic thread of `test_graph_builder` after both of its batches: five posts in the graph, a sixth new.
"""

import json

import pytest
from test_graph_builder import edge, proposal, save, snapshot, statement, strand

import baton.graph.cli as graph_cli
import baton.graph.context as context_module
from baton.graph.context import Context, queries, shown_ids
from baton.graph.contract import GraphError
from baton.graph.operations import apply, prepare
from baton.graph.source import canonical, load_json, load_source
from baton.graph.storage import load_state
from baton.graph.validation import build_patch


def extracted(tmp_path):
    source, state = tmp_path / "source.json", tmp_path / "state.json"
    save(source, snapshot())
    for batch in (2, 3):
        request_path, proposal_path = tmp_path / f"request-{batch}.json", tmp_path / f"proposal-{batch}.json"
        save(proposal_path, proposal(prepare(source, state, request_path, batch_size=batch)))
        apply(source, state, request_path, proposal_path)
    return source, state


def size(request, context):
    """This request's size with another context: the measure `prepare` holds against max-input-bytes."""
    return len(canonical({**request, "context": context}).encode("utf-8"))


def ids(lines):
    return [line.split(" | ", 1)[0] for line in lines]


def empty(binding):
    return {
        "binding": binding,
        "statements": [],
        "relevance_updates": [],
        "strands": [],
        "decisions": [],
        "edges": [],
        "dispositions": [],
    }


def test_recent_statements_lead_then_changed_decisions_then_what_each_new_post_is_about(tmp_path, monkeypatch) -> None:
    source, state = extracted(tmp_path)
    posts = load_source(source)["messages"]
    graph = load_state(state, load_source(source))
    # Newest post first; 2-1 was replaced in full, so it no longer stands and is never shown.
    assert Context(graph).ranked(posts[5:]) == ["5-1", "4-1", "3-1", "3-2", "1-1", "D1", "D2"]
    monkeypatch.setattr(context_module, "RECENT_POSTS", 1)
    about_receipts = {**posts[5], "text": "Should receipts be kept for one hour?"}
    assert Context(graph).ranked([about_receipts]) == ["5-1", "D1", "D2", "3-1"]  # search reaches the older 3-1
    assert queries("Keep keep receipts.\nOne hour") == ["keep receipts", "one hour"]
    # A card comes with every strand a shown entry belongs to; a decision for all strands brings none.
    context = Context(graph)
    assert ids(context.render(["5-1"])["cards"]) == ["S2"] and ids(context.render(["D2"])["cards"]) == ["S1"]
    assert context.render(["5-1"])["cards"][0].endswith("\nEvidence: post:5")
    assert context.render(["D1"])["cards"] == []
    bare = context.render([])
    assert (bare["cards"], bare["entries"], bare["refresh"]) == ([], [], [])
    assert ids(bare["outline"]) == ["S1", "S2"] and bare["next_ids"] == {"strand": "S3", "decision": "D3"}


def test_a_proposal_may_change_only_what_its_request_showed(tmp_path) -> None:
    source, state = extracted(tmp_path)
    request = prepare(source, state, tmp_path / "request.json")
    posts, graph = load_source(source), load_state(state, load_source(source))
    shown = shown_ids(request["context"])
    assert shown == {"1-1", "3-1", "3-2", "4-1", "5-1", "D1", "D2", "S1", "S2"}
    base = empty(request["binding"]) | {
        "statements": [statement(6, "New unprocessed question?", ["S2"])],
        "strands": [strand("S2", 6)],
    }
    both = [strand("S1", 6, resolved=True), strand("S2", 6)]
    replaced = {"id": "3-1", "relevance": "completely_superseded", "surviving_text": None, "evidence": ["post:6"]}
    retired = {"id": "D2", "text": "Use JSON.", "applies_to": ["S1"], "status": "superseded", "evidence": ["post:6"]}
    for change, hidden, refusal in (
        ({"relevance_updates": [replaced], "strands": both}, "3-1", "relevance update targets a statement"),
        ({"edges": [edge("6-1", "1-1", seq=6)], "strands": both}, "1-1", "edge names a statement"),
        ({}, "S2", "strand refresh needs a card"),
        ({"decisions": [retired]}, "D2", "decision change targets a decision"),
    ):
        candidate = base | change
        with pytest.raises(GraphError, match=f"{refusal} the request did not show"):
            build_patch(graph, candidate, posts, shown - {hidden})
        build_patch(graph, candidate, posts, shown)


def test_a_request_over_the_limit_drops_its_least_valuable_entries_before_it_is_refused(tmp_path) -> None:
    source, state = extracted(tmp_path)
    full = prepare(source, state, tmp_path / "full.json")
    context = Context(load_state(state, load_source(source)))
    ranked = context.ranked(full["new_posts"])
    assert size(full, context.render(ranked)) == len(canonical(full).encode("utf-8"))
    # Room for four entries: the three ranked last go, not the three listed last.
    trimmed = prepare(source, state, tmp_path / "trimmed.json", max_input_bytes=size(full, context.render(ranked[:4])))
    assert ids(trimmed["context"]["entries"]) == ["3-1", "3-2", "4-1", "5-1"]
    assert (trimmed["new_posts"], trimmed["context"]["outline"]) == (full["new_posts"], full["context"]["outline"])
    floor = size(full, context.render([]))
    bare = prepare(source, state, tmp_path / "bare.json", max_input_bytes=floor)
    assert (bare["context"]["entries"], bare["context"]["cards"]) == ([], [])
    assert bare["context"]["outline"] == full["context"]["outline"]
    with pytest.raises(GraphError, match="exceeds"):
        prepare(source, state, tmp_path / "refused.json", max_input_bytes=floor - 1)
    assert not (tmp_path / "refused.json").exists()
    # A batch takes as many whole posts as fit with their full context, never part of one.
    unread = tmp_path / "unread-state.json"
    two = prepare(source, unread, tmp_path / "two.json", batch_size=2)
    limit = len(canonical(two).encode("utf-8"))
    six = prepare(source, unread, tmp_path / "six.json", batch_size=6, max_input_bytes=limit)
    assert six["new_posts"] == two["new_posts"]


def test_a_strand_left_without_its_card_falls_behind_and_the_next_request_asks_for_its_refresh(tmp_path) -> None:
    source, state = tmp_path / "source.json", tmp_path / "state.json"
    save(source, snapshot(4))
    save(tmp_path / "p1.json", proposal(prepare(source, state, tmp_path / "r1.json", batch_size=2)))
    apply(source, state, tmp_path / "r1.json", tmp_path / "p1.json")

    def bare_request(name):
        """The next post's request at the smallest size that fits: no entries, and only the cards owed a refresh."""
        graph = load_state(state, load_source(source))
        full = prepare(source, state, tmp_path / f"{name}-full.json", batch_size=1)
        limit = size(full, Context(graph).render([]))
        return prepare(source, state, tmp_path / f"{name}.json", batch_size=1, max_input_bytes=limit)

    def attempt(name, request, **parts):
        save(tmp_path / f"{name}-proposal.json", empty(request["binding"]) | parts)
        return apply(source, state, tmp_path / f"{name}.json", tmp_path / f"{name}-proposal.json")

    third = bare_request("third")
    assert (third["context"]["cards"], third["context"]["entries"], third["context"]["refresh"]) == ([], [], [])
    receipts = statement(3, "Keep receipts for one hour.", ["S1"], quote="keep receipts one hour")
    with pytest.raises(GraphError, match="strand refresh needs a card the request did not show"):
        attempt("third", third, statements=[receipts], strands=[strand("S1", 3)])
    # Filed under a strand the model could not read: accepted, and the strand's summary now trails it.
    attempt("third", third, statements=[receipts])
    stored = load_json(state)["strands"]["S1"]
    assert (stored["statement_ids"], stored["summary_through"]) == (["1-1", "3-1"], 2)

    fourth = bare_request("fourth")
    assert fourth["context"]["refresh"] == ["S1"] and ids(fourth["context"]["cards"]) == ["S1"]
    assert fourth["context"]["entries"] == []
    quotes = statement(4, "Exact source quotes remain mandatory.", ["S2"])
    with pytest.raises(GraphError, match="every affected strand with a card needs a refreshed summary"):
        attempt("fourth", fourth, statements=[quotes])
    attempt("fourth", fourth, statements=[quotes], strands=[strand("S1", 4)])
    strands = load_json(state)["strands"]
    assert (strands["S1"]["summary_through"], strands["S2"]["summary_through"]) == (4, 2)
    save(source, snapshot(5))
    assert prepare(source, state, tmp_path / "fifth.json")["context"]["refresh"] == ["S2"]


def test_a_host_without_full_text_search_cannot_prepare_a_request(tmp_path, monkeypatch, capsys) -> None:
    source, state = extracted(tmp_path)
    request_path = tmp_path / "request.json"
    monkeypatch.setattr("baton.database.store.fts5_available", lambda conn: False)
    with pytest.raises(GraphError, match="FTS5"):
        prepare(source, state, request_path)
    arguments = ["prepare", "--source", str(source), "--state", str(state), "--request", str(request_path)]
    assert graph_cli.main(arguments) == 2
    assert "FTS5" in json.loads(capsys.readouterr().err)["error"]
    assert not request_path.exists()
