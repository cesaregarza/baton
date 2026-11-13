"""Manual native export, validated graph production and derived publication on synthetic SQLite."""

import copy
import json
from dataclasses import replace

import pytest
from test_graph_builder import proposal, snapshot
from test_index_writer import converse, index_config, say

from baton import cli, derived
from baton.agents import Agent
from baton.database.store import connect
from baton.errors import InvalidRequest
from baton.graph.contract import GraphError
from baton.graph.export import export_source
from baton.graph.operations import apply, prepare
from baton.graph.publication import publish_graph, render_summary
from baton.graph.source import load_json, load_source


def advance(source, state, tmp_path, end):
    request_path, proposal_path = tmp_path / f"request{end}.json", tmp_path / f"proposal{end}.json"
    request = prepare(source, state, request_path, batch_size=end)
    start, end = (request["binding"]["batch"][key] for key in ("start", "end"))
    proposal = {
        "binding": request["binding"],
        "statements": [],
        "relevance_updates": [],
        "edges": [],
        "dispositions": [],
        "decisions": [],
        "strands": [
            {
                "id": "S1",
                "title": "Release",
                "summary": f"Notes through {end}.",
                "state": "active",
                "open_work": ["Review the notes."],
                "evidence": [f"{end}-1"],
            }
        ],
    }
    for post in request["new_posts"]:
        proposal["statements"].append(
            {
                "id": f"{post['seq']}-1",
                "seq": post["seq"],
                "text": post["text"],
                "quotes": [post["text"]],
                "strand_ids": ["S1"],
                "relevance": "directly_relevant",
                "surviving_text": None,
                "evidence": [f"post:{post['seq']}"],
            }
        )
    if start == 1:
        proposal["decisions"] = [
            {"id": "D1", "text": "Keep the notes.", "status": "active", "applies_to": ["all"], "evidence": ["post:1"]}
        ]
    proposal_path.write_text(json.dumps(proposal))
    apply(source, state, request_path, proposal_path)


def test_export_publish_incremental_retry_and_actual_context(tmp_path):
    config = index_config(tmp_path, page_items=1)
    config = replace(config, search=replace(config.search, enabled=True))
    with connect(config.db_path) as db:
        cid, mids = converse(db, config, 5)
        source, state = tmp_path / "source.json", tmp_path / "graph.json"
        export_source(db, config, cid, source)
        native = load_source(source)
        assert len(native["messages"]) == 5  # two recipient copies per post collapse to one native ID
        assert [p["message_id"] for p in native["messages"]] == mids
        advance(source, state, tmp_path, 2)
        checkpoint = state.read_bytes()
        first = publish_graph(db, config, "alpha", source, state)
        assert first["created"] and first["covered_through_seq"] == 2
        assert state.read_bytes() == checkpoint
        assert not publish_graph(db, config, "alpha", source, state)["created"]
        assert db.execute("SELECT COUNT(*) FROM artifact_versions").one()[0] == 1
        context = Agent(db, config, "reviewer").thread(cid).read_context(limit=1)
        assert (context["head_seq"], context["covered_through_seq"]) == (5, 2)
        assert [p["seq"] for p in context["messages"]] == [3]
        assert context["continuation"] == {"after_seq": 3, "through_seq": 5}
        summary = db.execute("SELECT body, citations FROM artifact_versions").one()
        assert "S1" in summary["body"] and "D1" in summary["body"] and "summary through seq 2" in summary["body"]
        assert set(json.loads(summary["citations"])) == {mids[0], mids[1]}
        say(db, config, cid, "note 6")
        export_source(db, config, cid, source)
        advance(source, state, tmp_path, 4)
        second = publish_graph(db, config, "alpha", source, state)
        assert second["created"] and second["covered_through_seq"] == 6
        assert second["publication_receipt"] != first["publication_receipt"]
        assert second["versions"][0]["version"] == 2
        # Continuation keeps H=5 and raw progress despite a new post and new summary through 6.
        next_page = Agent(db, config, "reviewer").thread(cid).read_context(after_seq=3, through_seq=5, limit=1)
        assert [p["seq"] for p in next_page["messages"]] == [4]
        assert next_page["summary"] is None and next_page["head_seq"] == 5
        assert not publish_graph(db, config, "alpha", source, state)["created"]
        assert db.execute("SELECT COUNT(*) FROM artifact_versions").one()[0] == 2


def test_prefix_conflicts_and_exact_manifest_limits_never_publish(tmp_path):
    config = index_config(tmp_path)
    with connect(config.db_path) as db:
        cid, mids = converse(db, config, 3)
        source, state = tmp_path / "source.json", tmp_path / "graph.json"
        export_source(db, config, cid, source)
        original = source.read_bytes()
        advance(source, state, tmp_path, 2)
        for field, value in (
            ("text", "edited"),
            ("author", "wrong"),
            ("reply_to", mids[1]),
            ("message_id", "wrong"),
            ("at", "wrong"),
        ):
            modified = json.loads(original)
            modified["messages"][0][field] = value
            source.write_text(json.dumps(modified))
            with pytest.raises(GraphError):
                publish_graph(db, config, "alpha", source, state)
            assert db.execute("SELECT COUNT(*) FROM artifact_versions").one()[0] == 0
        source.write_bytes(original)
        limited = replace(config, indexer=replace(config.indexer, max_manifest_items=1))
        with pytest.raises(InvalidRequest, match="manifest limits"):
            publish_graph(db, limited, "alpha", source, state)
        assert db.execute("SELECT COUNT(*) FROM batches").one()[0] == 0
        for limits in ({"max_artifact_bytes": 1}, {"max_citations": 1}):
            with pytest.raises(GraphError, match="exceeds"):
                publish_graph(db, replace(config, indexer=replace(config.indexer, **limits)), "alpha", source, state)
        assert db.execute("SELECT COUNT(*) FROM artifact_versions").one()[0] == 0
        with pytest.raises(GraphError, match="shorten"):
            export_source(db, config, cid, source, through_seq=2)
        with pytest.raises(GraphError, match="max-posts"):
            export_source(db, config, cid, source, max_posts=2)
        with pytest.raises(GraphError, match="max-bytes"):
            export_source(db, config, cid, source, max_bytes=10)
        with pytest.raises(GraphError):
            export_source(db, config, cid, config.db_path)
        with pytest.raises(GraphError):
            publish_graph(db, config, "alpha", source, state, receipt_path=source)
        original_state = state.read_bytes()
        tampered = json.loads(original_state)
        tampered["watermark"] = 3
        state.write_text(json.dumps(tampered))
        with pytest.raises(GraphError):
            publish_graph(db, config, "alpha", source, state)
        state.write_bytes(original_state)
        db.execute("UPDATE messages SET text = 'corrupt copy' WHERE id = ? AND recipient = 'reviewer'", (mids[0],))
        with pytest.raises(GraphError, match="inconsistent native copies"):
            export_source(db, config, cid, tmp_path / "bad.json")
        with pytest.raises(GraphError):
            publish_graph(db, config, "alpha", source, state)
        assert db.execute("SELECT COUNT(*) FROM artifact_versions").one()[0] == 0
        db.execute("DELETE FROM messages WHERE id = ?", (mids[1],))
        with pytest.raises(GraphError):
            export_source(db, config, cid, tmp_path / "gap.json")


def test_interrupted_commit_replays_saved_batch_and_ceiling_rejects_recent(tmp_path, monkeypatch):
    config = index_config(tmp_path)
    with connect(config.db_path) as db:
        cid, _ = converse(db, config, 3)
        source, state, receipt = (tmp_path / name for name in ("source.json", "graph.json", "receipt.json"))
        export_source(db, config, cid, source)
        advance(source, state, tmp_path, 2)
        commit = derived.commit_batch

        def interrupted(*args, **kwargs):
            result = commit(*args, **kwargs)
            assert result["created"]
            raise RuntimeError("synthetic post-commit interruption")

        monkeypatch.setattr(derived, "commit_batch", interrupted)
        with pytest.raises(RuntimeError, match="interruption"):
            publish_graph(db, config, "alpha", source, state, receipt_path=receipt)
        attempt = load_json(receipt)
        assert "result" not in attempt
        monkeypatch.setattr(derived, "commit_batch", commit)
        replay = publish_graph(db, config, "alpha", source, state, receipt_path=receipt)
        assert not replay["created"] and replay["versions"][0]["version"] == 1
        assert db.execute("SELECT COUNT(*) FROM batches").one()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM artifact_versions").one()[0] == 1
        altered = copy.deepcopy(attempt)
        altered["payload"]["artifacts"][0]["body"] = "tampered"
        receipt.write_text(json.dumps(altered))
        with pytest.raises(GraphError, match="payload was modified"):
            publish_graph(db, config, "alpha", source, state, receipt_path=receipt)
        for value in (0, True, 4):
            with pytest.raises(InvalidRequest):
                derived.begin_batch(db, config, "beta", cid, ["summary"], through_seq=value)
        recent = replace(config, search=replace(config.search, rolling_window_messages=2))
        with pytest.raises(InvalidRequest, match="summary batch only"):
            derived.begin_batch(db, recent, "beta", cid, ["summary_recent"], through_seq=2)


def test_host_local_export_publish_cli_and_protected_configuration(tmp_path, monkeypatch, capsys):
    config = index_config(tmp_path)
    monkeypatch.setattr("baton.config.load", lambda _: config)
    with connect(config.db_path) as db:
        cid, _ = converse(db, config, 3)
    source, state = tmp_path / "source.json", tmp_path / "graph.json"
    assert cli.main(["index", "graph-export", "--conversation", cid, "--file", str(source)]) == 0
    assert json.loads(capsys.readouterr().out)["through_seq"] == 3
    advance(source, state, tmp_path, 2)
    args = ["index", "graph-publish", "--producer", "alpha", "--source", str(source), "--state", str(state)]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["covered_through_seq"] == 2
    assert cli.main(args) == 0
    assert not json.loads(capsys.readouterr().out)["created"]
    protected = tmp_path / "baton.toml"
    protected.write_text("host configuration")
    assert (
        cli.main(["--config", str(protected), "index", "graph-export", "--conversation", cid, "--file", str(protected)])
        == 1
    )
    assert protected.read_text() == "host configuration"


def test_published_view_keeps_applicable_decision_after_its_discussion_resolves(tmp_path):
    source, state = tmp_path / "source.json", tmp_path / "graph.json"
    source.write_text(json.dumps(snapshot()))
    for number, count in enumerate((2, 3), 1):
        request_path, proposal_path = tmp_path / f"r{number}.json", tmp_path / f"p{number}.json"
        request = prepare(source, state, request_path, batch_size=count)
        proposal_path.write_text(json.dumps(proposal(request)))
        apply(source, state, request_path, proposal_path)
    body, citations = render_summary(load_json(state), load_source(source))
    assert "S2:" in body and "S1:" not in body
    assert "D1 (applies to all): Retain exact source quotes." in body
    assert "D2" not in body and set(citations) == {"m4", "m5"}
