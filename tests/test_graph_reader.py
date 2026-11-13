"""Source-bound graph reads over a published snapshot: working view, drilldown, paging, raw-tail navigation.

Synthetic SQLite, the graph builder's synthetic thread, no model. The published graph has an active and a
resolved strand that overlap on one statement, a global decision and one scoped to the resolved strand, and
superseding edges, so the scenarios can check what each collection shows and hides. Authorization is the
existing served-artifact predicate: every page re-checks that the caller may read every source input.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import make_config
from test_aliases import dispatch, payload, rpc
from test_artifact_reads import publish as publish_prose
from test_artifact_reads import reads_config
from test_graph_builder import as_version_1, edit, proposal, snapshot
from test_index_writer import lose, say
from test_index_writer import snapshot as table_snapshot

from baton import auth, conversations, messages, search
from baton.agents import Agent
from baton.config import Role, StaticToken
from baton.database import store
from baton.database.store import connect
from baton.errors import BatonError
from baton.graph import compact, reader
from baton.graph import snapshot as snapshot_module
from baton.graph.export import export_source
from baton.graph.operations import apply, prepare
from baton.graph.publication import publish_graph
from baton.graph.source import canonical, load_json, load_source

TEXTS = [post["text"] for post in snapshot()["messages"]]
GROUP = ["orchestrator", "reviewer", "assistant"]


def refused(code: str, operation, *args, **kwargs) -> BatonError:
    with pytest.raises(BatonError) as raised:
        operation(*args, **kwargs)
    assert raised.value.code == code, f"{raised.value.code}: {raised.value}"
    return raised.value


def thread(db, config, participants=GROUP, count=6) -> str:
    conversation_id = conversations.create(
        db, config, actor="orchestrator", title="Format and storage", participants=participants
    )["conversation_id"]
    for index, text in enumerate(TEXTS[:count]):
        say(db, config, conversation_id, text, sender=GROUP[index % 2])
    return conversation_id


def build_graph(db, config, conversation_id, tmp_path: Path, *, producer="alpha", batches=(2, 3)) -> dict:
    """Export the thread, apply the synthetic batches and publish the compact view with its snapshot."""
    source, state = tmp_path / f"{producer}-source.json", tmp_path / f"{producer}-graph.json"
    export_source(db, config, conversation_id, source)
    for number, count in enumerate(batches, 1):
        request_path, proposal_path = tmp_path / f"{producer}-r{number}.json", tmp_path / f"{producer}-p{number}.json"
        request = prepare(source, state, request_path, batch_size=count)
        proposal_path.write_text(json.dumps(proposal(request)))
        apply(source, state, request_path, proposal_path)
    projected = snapshot_module.project(load_json(state))
    view = compact.render_view(projected, load_source(source))
    return publish_graph(db, config, producer, source, state, view=view, attach_graph=True)


def advance_published(db, config, conversation_id, tmp_path: Path, producer="alpha") -> dict:
    """One more batch over the live tail and a new publication; the previous snapshot is no longer served."""
    source, state = tmp_path / f"{producer}-source.json", tmp_path / f"{producer}-graph.json"
    export_source(db, config, conversation_id, source)
    request_path, proposal_path = tmp_path / f"{producer}-r-more.json", tmp_path / f"{producer}-p-more.json"
    request = prepare(source, state, request_path, batch_size=1)
    end = request["binding"]["batch"]["end"]
    post = request["new_posts"][0]
    candidate = {
        "binding": request["binding"],
        "statements": [
            {
                "id": f"{end}-1",
                "seq": end,
                "text": post["text"],
                "quotes": [post["text"]],
                "strand_ids": ["S2"],
                "relevance": "directly_relevant",
                "surviving_text": None,
                "evidence": [f"post:{end}"],
            }
        ],
        "relevance_updates": [],
        "new_strands": [],
        "strand_edits": [edit("S2", end, replace=[{"id": "S2.1", "text": f"Reader asked about seq {end}."}])],
        "decisions": [],
        "edges": [],
        "dispositions": [],
    }
    proposal_path.write_text(json.dumps(candidate))
    apply(source, state, request_path, proposal_path)
    projected = snapshot_module.project(load_json(state))
    view = compact.render_view(projected, load_source(source))
    return publish_graph(db, config, producer, source, state, view=view, attach_graph=True)


@pytest.fixture
def graphed(tmp_path):
    config = reads_config(tmp_path)
    db = connect(config.db_path)
    conversation_id = thread(db, config)
    say(db, config, conversation_id, "raw tail after W", sender="assistant")  # seq 7: beyond the graph
    result = build_graph(db, config, conversation_id, tmp_path)
    yield db, config, conversation_id, result
    db.close()


def test_working_view_cards_decisions_and_source_bound_drilldown(graphed) -> None:
    db, config, cid, result = graphed
    before = table_snapshot(db)
    unread = {role: messages.unread(db, role) for role in GROUP}
    page = reader.read_graph(db, config, "reviewer", cid)
    envelope = {
        k: page[k] for k in ("producer", "revision", "covered_through_seq", "head_seq", "derived", "authoritative")
    }
    assert envelope == {
        "producer": "alpha",
        "revision": 2,
        "covered_through_seq": 5,
        "head_seq": 7,
        "derived": True,
        "authoritative": False,
    }
    assert page["snapshot_id"] == result["versions"][0]["version_id"] and page["conversation_id"] == cid
    stored = db.execute("SELECT body, citations FROM artifact_versions WHERE id = ?", (page["snapshot_id"],)).one()
    assert page["working_view"] == {"body": stored["body"], "citations": json.loads(stored["citations"])}
    assert "S2:" in page["working_view"]["body"] and "S1:" not in page["working_view"]["body"]
    assert page["tail"] == {
        "status": "unprocessed",
        "from_seq": 6,
        "through_seq": 7,
        "read_history_args": {"conversation_id": cid, "after_seq": 5, "through_seq": 7, "limit": 20},
        "notice": page["tail"]["notice"],
    }
    assert "absent from the working view" in page["tail"]["notice"]
    assert page["items"] == [
        {"id": "S2", "title": "Storage and reader", "state": "active", "summary_through": 5, "statement_count": 4}
    ]
    assert page["next_cursor"] is None
    history = reader.read_graph(db, config, "reviewer", cid, state="all")
    assert [(s["id"], s["state"]) for s in history["items"]] == [("S1", "resolved"), ("S2", "active")]
    active_decisions = reader.read_graph(db, config, "reviewer", cid, collection="decisions")
    assert [d["id"] for d in active_decisions["items"]] == ["D1"]  # D2 applies only to the resolved strand
    all_decisions = reader.read_graph(db, config, "reviewer", cid, state="all", collection="decisions")
    assert [(d["id"], d["applies_to"]) for d in all_decisions["items"]] == [("D1", ["all"]), ("D2", ["S1"])]
    assert "working_view" in all_decisions and "tail" in all_decisions

    strand = reader.read_graph_strand(db, config, "reviewer", cid, page["snapshot_id"], "S1")
    assert strand["strand"]["state"] == "resolved" and strand["strand"]["summary_through"] == 5
    assert set(strand["strand"]) == {"id", "title", "state", "summary", "open_work", "summary_through", "evidence"}
    rows = {row["id"]: row for row in strand["items"]}
    assert list(rows) == ["1-1", "3-1", "4-1"]  # numeric (seq, ordinal) order
    assert rows["1-1"]["relevance"] == "partially_superseded" and rows["1-1"]["surviving_text"] == "Use JSON."
    assert rows["4-1"]["strand_ids"] == ["S1", "S2"]  # the overlap statement names both strands
    assert rows["1-1"]["source"]["author"] == "orchestrator" and rows["3-1"]["source"]["author"] == "orchestrator"
    assert rows["4-1"]["source"]["author"] == "reviewer"
    assert all("quotes" not in row and "messages" not in row for row in strand["items"])
    shared = reader.read_graph_strand(db, config, "reviewer", cid, page["snapshot_id"], "S1", collection="decisions")
    assert [d["id"] for d in shared["items"]] == ["D1", "D2"]
    only_global = reader.read_graph_strand(
        db, config, "reviewer", cid, page["snapshot_id"], "S2", collection="decisions"
    )
    assert [d["id"] for d in only_global["items"]] == ["D1"]

    statement = reader.read_graph_statement(db, config, "reviewer", cid, page["snapshot_id"], "4-1")
    full = statement["statement"]
    assert full["quotes"] == ["Exact source quotes remain mandatory."] and full["evidence"] == ["post:4"]
    assert set(full) == set(snapshot_module.STATEMENT_FIELDS)
    assert [(e["direction"], e["to"], e["kind"]) for e in statement["items"]] == [
        ("outgoing", "1-1", "responds_to"),
        ("outgoing", "3-2", "responds_to"),
    ]
    incoming = reader.read_graph_statement(db, config, "reviewer", cid, page["snapshot_id"], "1-1")
    assert [(e["direction"], e["from"], e["kind"]) for e in incoming["items"]] == [
        ("incoming", "2-1", "responds_to"),
        ("incoming", "3-1", "supersedes"),
        ("incoming", "4-1", "responds_to"),
    ]
    members = reader.read_graph_statement(db, config, "reviewer", cid, page["snapshot_id"], "4-1", collection="strands")
    assert [card["id"] for card in members["items"]] == ["S1", "S2"]
    raw = search.get_message(db, config, "reviewer", full["source"]["message_id"])
    assert full["quotes"][0] in raw["text"] and raw["seq"] == 4
    # Labels carry the exact graph id; `read_context` opens the same winner.
    [label] = search.read_artifacts(db, config, "reviewer", cid)["artifacts"]
    assert label["graph_snapshot_id"] == page["snapshot_id"] and "snapshot" not in label
    context = Agent(db, config, "reviewer").thread(cid).read_context()
    assert context["summary"]["graph_snapshot_id"] == page["snapshot_id"]
    assert table_snapshot(db) == before and {role: messages.unread(db, role) for role in GROUP} == unread
    for arguments, code in (
        ({"state": "resolved"}, "invalid_request"),
        ({"collection": "edges"}, "invalid_request"),
        ({"limit": 0}, "invalid_request"),
        ({"limit": 51}, "invalid_request"),
        ({"cursor": "not-a-cursor"}, "invalid_request"),
    ):
        refused(code, reader.read_graph, db, config, "reviewer", cid, **arguments)
    refused("invalid_request", reader.read_graph_strand, db, config, "reviewer", cid, page["snapshot_id"], "S9")
    refused("invalid_request", reader.read_graph_statement, db, config, "reviewer", cid, page["snapshot_id"], "9-9")


def test_pages_stop_between_whole_rows_and_cursors_are_bound(graphed, monkeypatch) -> None:
    db, config, cid, _ = graphed
    first = reader.read_graph(db, config, "reviewer", cid, state="all", limit=1)
    assert [s["id"] for s in first["items"]] == ["S1"] and first["next_cursor"]
    second = reader.read_graph(db, config, "reviewer", cid, state="all", limit=1, cursor=first["next_cursor"])
    assert [s["id"] for s in second["items"]] == ["S2"] and second["next_cursor"] is None
    assert "working_view" not in second and "tail" not in second and second["head_seq"] == first["head_seq"]
    snapshot_id = first["snapshot_id"]
    statements = reader.read_graph_strand(db, config, "reviewer", cid, snapshot_id, "S2", limit=3)
    assert [r["id"] for r in statements["items"]] == ["2-1", "3-2", "4-1"] and statements["next_cursor"]
    rest = reader.read_graph_strand(
        db, config, "reviewer", cid, snapshot_id, "S2", limit=3, cursor=statements["next_cursor"]
    )
    assert [r["id"] for r in rest["items"]] == ["5-1"] and rest["next_cursor"] is None
    # A cursor is bound to its tool, collection and target.
    refused(
        "invalid_request", reader.read_graph, db, config, "reviewer", cid, state="all", cursor=statements["next_cursor"]
    )
    refused(
        "invalid_request",
        reader.read_graph_strand,
        db,
        config,
        "reviewer",
        cid,
        snapshot_id,
        "S1",
        cursor=statements["next_cursor"],
    )
    refused(
        "invalid_request",
        reader.read_graph_strand,
        db,
        config,
        "reviewer",
        cid,
        snapshot_id,
        "S2",
        collection="decisions",
        cursor=statements["next_cursor"],
    )
    # Byte boundaries: the page stops between whole rows, and an indivisible detail is refused whole.
    monkeypatch.setattr(reader, "MAX_RESPONSE_BYTES", 1500)
    bounded = reader.read_graph_strand(db, config, "reviewer", cid, snapshot_id, "S2", limit=10)
    assert 0 < len(bounded["items"]) < 4 and bounded["next_cursor"]
    seen = [row["id"] for row in bounded["items"]]
    while bounded["next_cursor"]:
        bounded = reader.read_graph_strand(
            db, config, "reviewer", cid, snapshot_id, "S2", limit=10, cursor=bounded["next_cursor"]
        )
        seen += [row["id"] for row in bounded["items"]]
    assert seen == ["2-1", "3-2", "4-1", "5-1"]
    monkeypatch.setattr(reader, "MAX_RESPONSE_BYTES", 400)
    refused("item_too_large", reader.read_graph, db, config, "reviewer", cid)
    refused("item_too_large", reader.read_graph_strand, db, config, "reviewer", cid, snapshot_id, "S2")
    # Every page, cursor included, stays within the cap at every boundary, and nothing is skipped.
    for cap in range(1400, 2400, 7):  # from the smallest cap that fits one row beside its cursor
        monkeypatch.setattr(reader, "MAX_RESPONSE_BYTES", cap)
        walked, cursor, pages = [], None, 0
        while True:
            page = reader.read_graph_strand(db, config, "reviewer", cid, snapshot_id, "S2", limit=10, cursor=cursor)
            assert len(canonical(page).encode("utf-8")) <= cap, (cap, pages)
            walked += [row["id"] for row in page["items"]]
            pages += 1
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert walked == ["2-1", "3-2", "4-1", "5-1"], cap


def test_entry_reads_answer_a_search_with_single_lines_and_ids_with_whole_entries(graphed) -> None:
    db, config, cid, result = graphed
    before = table_snapshot(db)
    label = {
        "snapshot_id": result["versions"][0]["version_id"],
        "covered_through_seq": 5,
        "head_seq": 7,
        "derived": True,
        "authoritative": False,
    }
    # No query: one line per strand, the resolved one included.
    assert reader.search_graph(db, config, "reviewer", cid) == label | {
        "entries": [
            "S1 | resolved | 3 statements, summarized through post 5 | Format",
            "S2 | active | 4 statements, summarized through post 5 | Storage and reader",
        ]
    }
    # A search answers with standing statements and active decisions, each whole on one line, best first.
    found = reader.search_graph(db, config, "reviewer", cid, queries=["quotes source", "reader"])
    assert found == label | {
        "entries": [
            "D1 | all | rests on 4-1 | changed at post 5 | Retain exact source quotes.",
            "5-1 | S2 | - | Implement the reader.",
            "4-1 | S1 S2 | responds_to>1-1 responds_to>3-2 | Exact source quotes remain mandatory.",
        ]
    }
    best = reader.search_graph(db, config, "reviewer", cid, queries=["quotes source", "reader"], limit=1)
    assert best["entries"] == found["entries"][:1]
    # A reader that names what it holds gets what it has not seen: without D1, 4-1 leads the first query.
    rest = reader.search_graph(db, config, "reviewer", cid, queries=["quotes source", "reader"], exclude=["D1", "9-9"])
    assert rest == label | {"entries": [found["entries"][2], found["entries"][1]]}
    # 1-1 is searched by the text that still stands, so only the statement that replaced the rest mentions receipts.
    receipts = reader.search_graph(db, config, "reviewer", cid, queries=["receipts"])
    assert receipts["entries"] == ["3-1 | S1 | supersedes>1-1 | Keep receipts for one hour."]

    # Ids load whole entries in the order asked, each once: post:3 brings both of its statements, so asking
    # for 3-1 after it adds nothing. An id that names nothing standing is skipped with the reason.
    opened = reader.read_graph_entries(
        db, config, "reviewer", cid, ["4-1", "D2", "2-1", "S2", "post:3", "3-1", "post:9"]
    )
    assert opened == label | {
        "entries": [
            "4-1 | S1 S2 | responds_to>1-1 responds_to>3-2 | Exact source quotes remain mandatory.",
            "D2 | S1 | rests on 1-1 | changed at post 5 | Use JSON.",
            "S2 | active | Storage and reader\nS2.1 JSON remains applicable; receipt retention is one hour.\n"
            "Open work:\nS2.w1 Implement the reader using files, preserving quotes.",
            "3-1 | S1 | supersedes>1-1 | Keep receipts for one hour.",
            "3-2 | S2 | responds_to<4-1 | Use files instead of SQLite.",
        ],
        "skipped": {
            "2-1": "replaced in full by 3-2",
            "post:9": "that post is after this graph's coverage; read it with read_history",
        },
    }
    assert table_snapshot(db) == before

    for arguments in (
        {"queries": "json"},
        {"queries": ["a", "b", "c", "d"]},
        {"queries": [""]},
        {"queries": ["!!!"]},
        {"queries": ["x" * 201]},
        {"queries": [5]},
        {"limit": 0},
        {"limit": 51},
        {"limit": True},
        {"snapshot_id": ""},
        {"exclude": "D1"},
        {"exclude": [""]},
        {"exclude": ["x" * 33]},
        {"exclude": [1]},
        {"exclude": ["1-1"] * 201},
    ):
        refused("invalid_request", reader.search_graph, db, config, "reviewer", cid, **arguments)
    for ids in ("1-1", [], ["1-1"] * 13, [""], ["x" * 33], [1]):
        refused("invalid_request", reader.read_graph_entries, db, config, "reviewer", cid, ids)


def test_entry_reads_stop_between_whole_ids_and_say_what_is_left(graphed, monkeypatch) -> None:
    db, config, cid, _ = graphed
    ids = ["4-1", "S2", "post:3", "D1"]
    whole = reader.read_graph_entries(db, config, "reviewer", cid, ids)["entries"]
    outline = reader.search_graph(db, config, "reviewer", cid)
    assert len(whole) == 5 and "remaining" not in whole and "omitted" not in outline

    # What did not fit is named in the response, and that note is inside the cap too. Asking again for the
    # remaining ids loads everything, whatever the cap.
    monkeypatch.setattr(reader, "MAX_RESPONSE_BYTES", 420)
    first = reader.read_graph_entries(db, config, "reviewer", cid, ids)
    assert first["entries"] == whole[:2] and first["remaining"] == ["post:3", "D1"]
    for cap in range(350, 620, 9):
        monkeypatch.setattr(reader, "MAX_RESPONSE_BYTES", cap)
        loaded, wanted = [], ids
        while wanted:
            page = reader.read_graph_entries(db, config, "reviewer", cid, wanted)
            assert len(canonical(page).encode("utf-8")) <= cap, cap
            loaded += page["entries"]
            wanted = page.get("remaining", [])
        assert loaded == whole, cap

    # A search or outline that is cut says how many lines were left out.
    monkeypatch.setattr(reader, "MAX_RESPONSE_BYTES", len(canonical(outline).encode("utf-8")) - 1)
    cut = reader.search_graph(db, config, "reviewer", cid)
    assert cut["entries"] == outline["entries"][:1] and cut["omitted"] == 1
    # An entry is never returned in part.
    monkeypatch.setattr(reader, "MAX_RESPONSE_BYTES", 150)
    refused("item_too_large", reader.search_graph, db, config, "reviewer", cid)
    refused("item_too_large", reader.read_graph_entries, db, config, "reviewer", cid, ["S2"])


def test_reasons_for_skipped_ids_count_toward_the_cap_with_or_without_entries(graphed, monkeypatch) -> None:
    db, config, cid, _ = graphed

    def read(ids: list[str]) -> dict:
        return reader.read_graph_entries(db, config, "reviewer", cid, ids)

    def size(response: dict) -> int:
        return len(canonical(response).encode("utf-8"))

    absent = ["2-1", "post:9", "nine"]  # replaced in full, beyond the graph's coverage, not an id at all
    alone, mixed = read(absent), read(["4-1", *absent, "D1"])
    assert alone["entries"] == [] and list(alone["skipped"]) == absent and "remaining" not in alone
    assert len(mixed["entries"]) == 2 and mixed["skipped"] == alone["skipped"]

    # A read that finds nothing is measured like any other. When its reasons do not fit it is refused, with
    # or without entries to add, and fewer ids at a time do fit.
    monkeypatch.setattr(reader, "MAX_RESPONSE_BYTES", size(alone))
    assert read(absent) == alone
    monkeypatch.setattr(reader, "MAX_RESPONSE_BYTES", size(alone) - 1)
    assert "fewer ids" in str(refused("item_too_large", read, absent))
    assert "fewer ids" in str(refused("item_too_large", read, ["4-1", *absent]))
    assert read(absent[:2])["skipped"] == {wanted: alone["skipped"][wanted] for wanted in absent[:2]}

    # Beside entries, the reasons are in the same budget as the lines and the note of what is left.
    monkeypatch.setattr(reader, "MAX_RESPONSE_BYTES", size(mixed) - 1)
    page = read(["4-1", *absent, "D1"])
    assert page == mixed | {"entries": mixed["entries"][:1], "remaining": ["D1"]} and size(page) < size(mixed)


def test_entry_search_needs_fts5_and_the_other_entry_reads_do_not(graphed, monkeypatch) -> None:
    db, config, cid, _ = graphed
    monkeypatch.setattr(store, "fts5_available", lambda conn: False)
    error = refused("graph_unavailable", reader.search_graph, db, config, "reviewer", cid, queries=["json"])
    assert "FTS5" in str(error)
    assert len(reader.search_graph(db, config, "reviewer", cid)["entries"]) == 2
    assert len(reader.read_graph_entries(db, config, "reviewer", cid, ["post:3"])["entries"]) == 2


def test_a_graph_published_before_strands_had_lines_is_read_with_its_lines(graphed) -> None:
    db, config, cid, result = graphed
    version = result["versions"][0]["version_id"]
    current = reader.read_graph_strand(db, config, "reviewer", cid, version, "S2")["strand"]
    card = reader.read_graph_entries(db, config, "reviewer", cid, ["S2"])["entries"]
    stored = json.loads(db.execute("SELECT snapshot FROM graph_snapshots WHERE version_id = ?", (version,)).one()[0])
    older = stored | {"schema_version": 1, "strands": as_version_1(stored["strands"])}
    db.execute("UPDATE graph_snapshots SET snapshot = ? WHERE version_id = ?", (json.dumps(older), version))
    # Upgraded on read the way the next apply upgrades the state, so the line ids are the ones it will store.
    assert reader.read_graph_strand(db, config, "reviewer", cid, version, "S2")["strand"] == current
    assert reader.read_graph_entries(db, config, "reviewer", cid, ["S2"])["entries"] == card


def test_authorization_precedes_everything_and_revocations_stop_paging(graphed, tmp_path) -> None:
    db, config, cid, result = graphed
    snapshot_id = result["versions"][0]["version_id"]
    first = reader.read_graph(db, config, "assistant", cid, state="all", limit=1)
    leak_free = {"items", "entries", "head_seq", "working_view", "revision", "snapshot_id", "covered_through_seq"}
    for role in ("outsider", "late"):
        error = refused("graph_unavailable", reader.read_graph, db, config, role, cid)
        assert not (set(error.extra) & leak_free) and cid not in str(error) and "7" not in str(error)
        refused("graph_unavailable", reader.read_graph_strand, db, config, role, cid, snapshot_id, "S1")
        refused("graph_unavailable", reader.read_graph_statement, db, config, role, cid, snapshot_id, "1-1")
        for error in (
            refused("graph_unavailable", reader.search_graph, db, config, role, cid, queries=["json"]),
            refused("graph_unavailable", reader.read_graph_entries, db, config, role, cid, ["1-1"]),
        ):
            assert not (set(error.extra) & leak_free) and cid not in str(error)
    refused("graph_unavailable", reader.read_graph, db, config, "assistant", "conv_missing")
    direct = messages.send(db, sender="reviewer", recipient="assistant", text="private")
    refused("graph_unavailable", reader.read_graph, db, config, "assistant", direct["message_id"])
    # A forged conversation/snapshot pairing is refused even for a reader who may read both threads.
    other = thread(db, config, count=2)
    other_result = build_graph(db, config, other, tmp_path / "other", batches=(2,))
    refused(
        "snapshot_unavailable",
        reader.read_graph,
        db,
        config,
        "assistant",
        cid,
        snapshot_id=other_result["versions"][0]["version_id"],
    )
    refused("snapshot_unavailable", reader.read_graph_strand, db, config, "assistant", other, snapshot_id, "S1")
    refused("snapshot_unavailable", reader.read_graph, db, config, "assistant", cid, snapshot_id="art_forged")
    refused("snapshot_unavailable", reader.search_graph, db, config, "assistant", other, snapshot_id=snapshot_id)
    refused(
        "snapshot_unavailable",
        reader.read_graph_entries,
        db,
        config,
        "assistant",
        other,
        ["1-1"],
        snapshot_id=snapshot_id,
    )
    # Membership loss between pages: the cursor is still valid, the reader no longer is.
    conversations.remove(db, config, actor="orchestrator", conversation_id=cid, participant="assistant")
    refused(
        "graph_unavailable",
        reader.read_graph,
        db,
        config,
        "assistant",
        cid,
        state="all",
        limit=1,
        cursor=first["next_cursor"],
    )
    refused("graph_unavailable", reader.search_graph, db, config, "assistant", cid, queries=["json"])
    refused("graph_unavailable", reader.read_graph_entries, db, config, "assistant", cid, ["1-1"])
    viewer_first = reader.read_graph(db, config, "viewer", cid, state="all", limit=1)
    assert reader.read_graph_entries(db, config, "viewer", cid, ["1-1"])["entries"]
    # Retention loss of one source input hides the graph from everyone, including a board-wide viewer.
    sent = db.execute(
        "SELECT DISTINCT id FROM messages WHERE conversation_id = ? AND conversation_seq = 2", (cid,)
    ).one()[0]
    lose(db, sent)
    refused(
        "graph_unavailable",
        reader.read_graph,
        db,
        config,
        "viewer",
        cid,
        state="all",
        limit=1,
        cursor=viewer_first["next_cursor"],
    )
    refused("graph_unavailable", reader.read_graph, db, config, "viewer", cid)
    refused("graph_unavailable", reader.search_graph, db, config, "viewer", cid)
    refused("graph_unavailable", reader.read_graph_entries, db, config, "viewer", cid, ["1-1"])
    # Generation invalidation applies to the attachment too.
    reviewer_first = reader.read_graph(db, config, "reviewer", other, state="all", limit=1)
    db.execute("UPDATE generations SET active = 0")
    refused("graph_unavailable", reader.read_graph, db, config, "reviewer", other)
    refused(
        "graph_unavailable",
        reader.read_graph,
        db,
        config,
        "reviewer",
        other,
        state="all",
        limit=1,
        cursor=reviewer_first["next_cursor"],
    )
    db.execute("UPDATE generations SET active = 1")
    assert reader.read_graph(db, config, "reviewer", other)["snapshot_id"] == other_result["versions"][0]["version_id"]
    db.execute("UPDATE artifact_versions SET state = 'invalid'")
    refused("graph_unavailable", reader.read_graph, db, config, "reviewer", other)


def test_new_publication_between_pages_and_old_summaries_without_graphs(graphed, tmp_path) -> None:
    db, config, cid, result = graphed
    first = reader.read_graph(db, config, "reviewer", cid, state="all", limit=1)
    stale_snapshot = first["snapshot_id"]
    later = advance_published(db, config, cid, tmp_path)
    assert later["covered_through_seq"] == 6
    error = refused(
        "snapshot_unavailable",
        reader.read_graph,
        db,
        config,
        "reviewer",
        cid,
        state="all",
        limit=1,
        cursor=first["next_cursor"],
    )
    assert "open it again" in str(error)
    refused("snapshot_unavailable", reader.read_graph_strand, db, config, "reviewer", cid, stale_snapshot, "S2")
    refused(
        "snapshot_unavailable",
        reader.read_graph_entries,
        db,
        config,
        "reviewer",
        cid,
        ["S2"],
        snapshot_id=stale_snapshot,
    )
    assert reader.read_graph_entries(db, config, "reviewer", cid, ["post:6"])["covered_through_seq"] == 6
    fresh = reader.read_graph(db, config, "reviewer", cid)
    assert fresh["snapshot_id"] == later["versions"][0]["version_id"] and fresh["covered_through_seq"] == 6
    assert fresh["revision"] == 3 and fresh["tail"]["read_history_args"]["after_seq"] == 6
    # Another producer's newer prose-only summary wins the context selection: no graph is substituted for it.
    say(db, config, cid, "note 8")
    publish_prose(db, config, cid, producer="beta", request_id="beta-prose", body="prose summary through 8")
    context = Agent(db, config, "reviewer").thread(cid).read_context()
    assert context["summary"]["producer"] == "beta" and context["summary"]["graph_snapshot_id"] is None
    error = refused("graph_unavailable", reader.read_graph, db, config, "reviewer", cid)
    assert "no graph attachment" in str(error)
    # The entry reads take the winner or an exact snapshot, so here they need the snapshot id.
    refused("graph_unavailable", reader.search_graph, db, config, "reviewer", cid)
    assert (
        reader.search_graph(db, config, "reviewer", cid, snapshot_id=fresh["snapshot_id"])["covered_through_seq"] == 6
    )
    assert reader.read_graph(db, config, "reviewer", cid, producer="alpha")["covered_through_seq"] == 6
    refused("graph_unavailable", reader.read_graph, db, config, "reviewer", cid, producer="beta")
    # The complete tail: W equals H, so the arguments are null.
    complete_cid = thread(db, config, count=2)
    build_graph(db, config, complete_cid, tmp_path / "complete", batches=(2,))
    complete = reader.read_graph(db, config, "reviewer", complete_cid)
    assert complete["tail"] == {"status": "complete", "from_seq": 3, "through_seq": 2, "read_history_args": None}
    disabled = replace(config, search=replace(config.search, enabled=False))
    refused("graph_unavailable", reader.read_graph, db, disabled, "reviewer", cid)
    refused("graph_unavailable", reader.search_graph, db, disabled, "reviewer", cid)


def test_history_through_seq_keeps_a_fixed_head_and_authorizes_first(graphed, capsys, monkeypatch) -> None:
    db, config, cid, _ = graphed
    reviewer = Agent(db, config, "reviewer").thread(cid)
    page = reviewer.read(5, 1, through_seq=7)
    assert [m["seq"] for m in page["messages"]] == [6] and page["next_seq"] == 6
    say(db, config, cid, "note 8")  # a later post never enters a fixed-head page
    page = reviewer.read(page["next_seq"], 20, through_seq=7)
    assert [m["seq"] for m in page["messages"]] == [7] and page["next_seq"] == 7
    assert reviewer.read(0, 20)["next_seq"] == 8  # without the ceiling, behavior is unchanged
    refused("invalid_request", reviewer.read, 0, 20, before_seq=3, through_seq=7)
    refused("invalid_request", reviewer.read, 0, 20, through_seq=99)
    refused("not_a_participant", Agent(db, config, "outsider").thread(cid).read, 0, 20, through_seq=99)
    # A retained gap inside the fixed head is reported as a gap, and the final empty page advances to the head.
    lost = db.execute(
        "SELECT DISTINCT id FROM messages WHERE conversation_id = ? AND conversation_seq = 7", (cid,)
    ).one()[0]
    lose(db, lost)
    final = reviewer.read(6, 20, through_seq=7)
    assert (
        final["messages"] == [] and final["next_seq"] == 7 and final["retention_gaps"] == [{"from_seq": 7, "to_seq": 7}]
    )
    from baton import cli

    monkeypatch.setattr("baton.config.load", lambda _: config)
    assert cli.main(["conversation", "history", cid, "--as", "reviewer", "--after-seq", "5", "--through-seq", "6"]) == 0
    assert [m["seq"] for m in json.loads(capsys.readouterr().out)["messages"]] == [6]


def guest_graph_host(tmp_path, *, board_visible=True):
    extra = """
        [conversations]
        enabled = true
        [search]
        enabled = true
        [indexer]
        producers = ["alpha"]
    """
    host = make_config(tmp_path, extra=extra)
    raw, digest = auth.new_token()
    host.tokens["guest"] = raw
    roles = {
        **host.config.roles,
        "planner": replace(host.config.roles["planner"], can_create_conversations=True, can_manage_participants=True),
        "guest": Role(name="guest", can_view_all_conversations=board_visible),
    }
    host.config = replace(
        host.config, roles=roles, tokens=(*host.config.tokens, StaticToken("guest-test", "guest", digest))
    )
    return host


async def call(client, host, name, arguments=None, role="guest"):
    response = await rpc(client, host, "tools/call", {"name": name, "arguments": arguments or {}}, role=role)
    return payload(response)


def test_guest_reads_the_graph_through_mcp_and_follows_the_raw_tail_to_the_captured_head(tmp_path, monkeypatch) -> None:
    host = guest_graph_host(tmp_path)
    config = host.config
    with connect(config.db_path) as db:
        cid = conversations.create(db, config, actor="planner", title="Board", participants=["planner", "worker"])[
            "conversation_id"
        ]
        for text in TEXTS:
            say(db, config, cid, text, sender="planner")
        say(db, config, cid, "tail seven", sender="worker")
        build_graph(db, config, cid, tmp_path / "graph")
    from baton.messages import limits

    monkeypatch.setattr(limits, "MAX_PAGE_BYTES", 1)  # every history page carries one message

    async def scenario(client):
        opened = await call(client, host, "read_graph", {"conversation_id": cid, "limit": 1})
        assert opened["derived"] is True and opened["authoritative"] is False, opened
        assert opened["covered_through_seq"] == 5 and opened["head_seq"] == 7
        assert "Storage and reader" in opened["working_view"]["body"]
        args = opened["tail"]["read_history_args"]
        assert args == {"conversation_id": cid, "after_seq": 5, "through_seq": 7, "limit": 20}
        with connect(config.db_path) as db:
            say(db, config, cid, "posted while the guest reads", sender="planner")  # seq 8: outside the head
            lost = db.execute(
                "SELECT DISTINCT id FROM messages WHERE conversation_id = ? AND conversation_seq = 7", (cid,)
            ).one()[0]
            lose(db, lost)  # seq 7 is gone: the tail ends in an explicit gap
        seen, gaps = [], []
        while True:
            page = await call(client, host, "read_history", args)
            assert "error" not in page, page
            seen += [m["seq"] for m in page["messages"]]
            gaps += page["retention_gaps"]
            if page["next_seq"] >= args["through_seq"]:
                break
            assert len(page["messages"]) < args["limit"]  # byte-short pages never mean completion
            args = {**args, "after_seq": page["next_seq"]}
        assert seen == [6] and {(g["from_seq"], g["to_seq"]) for g in gaps} == {(7, 7)}
        strand = await call(
            client,
            host,
            "read_graph_strand",
            {"conversation_id": cid, "snapshot_id": opened["snapshot_id"], "strand_id": "S2"},
        )
        assert [row["id"] for row in strand["items"]] == ["2-1", "3-2", "4-1", "5-1"]
        statement = await call(
            client,
            host,
            "read_graph_statement",
            {"conversation_id": cid, "snapshot_id": opened["snapshot_id"], "statement_id": "3-2"},
        )
        assert statement["statement"]["quotes"] == ["Use files instead of SQLite"]
        raw = await call(client, host, "get_message", {"message_id": statement["statement"]["source"]["message_id"]})
        assert statement["statement"]["quotes"][0] in raw["text"]
        found = await call(client, host, "search_graph", {"conversation_id": cid, "queries": ["files sqlite"]})
        assert found["entries"] == ["3-2 | S2 | responds_to<4-1 | Use files instead of SQLite."]
        assert found["snapshot_id"] == opened["snapshot_id"] and found["authoritative"] is False
        loaded = await call(client, host, "read_graph_entries", {"conversation_id": cid, "ids": ["3-2", "2-1"]})
        assert loaded["entries"] == found["entries"] and loaded["skipped"] == {"2-1": "replaced in full by 3-2"}
        # Losing a source input (not a tail post) hides the graph from everyone; the refusal carries no ids or counts.
        with connect(config.db_path) as db:
            lose(db, statement["statement"]["source"]["message_id"])
        refused_page = await call(client, host, "read_graph", {"conversation_id": cid})
        assert refused_page == {"error": "graph_unavailable", "detail": refused_page["detail"]}
        assert cid not in refused_page["detail"]
        core = await call(client, host, "read_graph", {"conversation_id": cid}, role="planner")
        assert core["error"] == "graph_unavailable"

    dispatch(host, scenario)


def test_guest_without_board_visibility_gains_nothing_from_the_graph_tools(tmp_path) -> None:
    host = guest_graph_host(tmp_path, board_visible=False)
    config = host.config
    with connect(config.db_path) as db:
        cid = conversations.create(db, config, actor="planner", title="Board", participants=["planner", "worker"])[
            "conversation_id"
        ]
        for text in TEXTS:
            say(db, config, cid, text, sender="planner")
        result = build_graph(db, config, cid, tmp_path / "graph")
        before = table_snapshot(db)

    async def scenario(client):
        for name, arguments in (
            ("read_graph", {"conversation_id": cid}),
            (
                "read_graph_strand",
                {"conversation_id": cid, "snapshot_id": result["versions"][0]["version_id"], "strand_id": "S2"},
            ),
            (
                "read_graph_statement",
                {"conversation_id": cid, "snapshot_id": result["versions"][0]["version_id"], "statement_id": "1-1"},
            ),
            ("search_graph", {"conversation_id": cid, "queries": ["json"]}),
            ("read_graph_entries", {"conversation_id": cid, "ids": ["1-1"]}),
        ):
            refusal = await call(client, host, name, arguments)
            assert refusal["error"] == "graph_unavailable" and set(refusal) == {"error", "detail"}, name
        assert (await call(client, host, "read_history", {"conversation_id": cid}))["error"] == "not_a_participant"
        worker = await call(client, host, "read_graph", {"conversation_id": cid}, role="worker")
        assert worker["covered_through_seq"] == 5  # a participant reads it through the same adapter

    dispatch(host, scenario)
    with connect(config.db_path) as db:
        assert table_snapshot(db) == before  # reads wrote nothing, moved no cursor
