"""Offline graph scenarios: incremental publication and one-call provider boundaries."""

import contextlib
import copy
import json
import subprocess
import sys

import httpx
import pytest

import baton.graph.cli as graph_cli
from baton.graph.contract import GraphError
from baton.graph.operations import apply, prepare, raw, read
from baton.graph.provider import ProviderOptions, run
from baton.graph.source import load_json, load_source


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def snapshot(count=6):
    texts = [
        "Use JSON — keep 24-hour receipts.",
        "Use SQLite.",
        "Use files instead of SQLite; keep receipts one hour.",
        "Exact source quotes remain mandatory. Format discussion resolved.",
        "Implement the reader.",
        "New unprocessed question?",
    ]
    return {
        "conversation": {"conversation_id": "native-conversation", "last_seq": count},
        "through_seq": count,
        "retention_gaps": [],
        "messages": [
            {
                "seq": i,
                "message_id": f"m{i}",
                "from_role": f"author{i % 2}",
                "text": text,
                "at": f"2026-11-07T10:00:0{i}Z",
                "reply_to": {"message_id": "m1", "seq": 1} if i > 1 else None,
                "conversation_id": "native-conversation",
            }
            for i, text in enumerate(texts[:count], 1)
        ],
    }


def statement(seq, text, strands, *, ordinal=1, quote=None):
    return {
        "id": f"{seq}-{ordinal}",
        "seq": seq,
        "text": text,
        "quotes": [quote or text],
        "strand_ids": strands,
        "relevance": "directly_relevant",
        "surviving_text": None,
        "evidence": [f"post:{seq}"],
    }


def strand(sid, end, *, resolved=False):
    return {
        "id": sid,
        "title": "Format" if sid == "S1" else "Storage and reader",
        "summary": "JSON remains applicable; receipt retention is one hour." if end > 2 else "Initial proposal.",
        "open_work": [] if resolved else ["Implement the reader using files, preserving quotes."],
        "state": "resolved" if resolved else "active",
        "evidence": [f"post:{end}"],
    }


def edge(a, b, kind="responds_to", seq=3):
    return {"from": a, "to": b, "kind": kind, "evidence": [f"post:{seq}"]}


def proposal(request):
    result = {
        "binding": request["binding"],
        "statements": [],
        "relevance_updates": [],
        "strands": [],
        "decisions": [],
        "edges": [],
        "dispositions": [],
    }
    if request["binding"]["batch"]["start"] == 1:
        result["statements"] = [
            statement(1, "Use JSON — keep 24-hour receipts.", ["S1"]),
            statement(2, "Use SQLite.", ["S2"]),
        ]
        result["strands"] = [strand("S1", 2), strand("S2", 2)]
        result["edges"] = [edge("2-1", "1-1", seq=2)]
    else:
        result["statements"] = [
            statement(3, "Keep receipts for one hour.", ["S1"], quote="keep receipts one hour"),
            statement(3, "Use files instead of SQLite.", ["S2"], ordinal=2, quote="Use files instead of SQLite"),
            statement(4, "Exact source quotes remain mandatory.", ["S1", "S2"]),
            statement(5, "Implement the reader.", ["S2"]),
        ]
        result["relevance_updates"] = [
            {"id": "1-1", "relevance": "partially_superseded", "surviving_text": "Use JSON.", "evidence": ["3-1"]},
            {"id": "2-1", "relevance": "completely_superseded", "surviving_text": None, "evidence": ["3-2"]},
        ]
        result["strands"] = [strand("S1", 5, resolved=True), strand("S2", 5)]
        result["decisions"] = [
            {
                "id": "D1",
                "text": "Retain exact source quotes.",
                "applies_to": ["all"],
                "status": "active",
                "evidence": ["4-1"],
            },
            {
                "id": "D2",
                "text": "Use JSON.",
                "applies_to": ["S1"],
                "status": "active",
                "evidence": ["1-1"],
            },
        ]
        result["edges"] = [
            edge("3-1", "1-1", "supersedes"),
            edge("3-2", "2-1", "supersedes"),
            edge("4-1", "1-1", seq=4),
            edge("4-1", "3-2", seq=4),
        ]
    return result


def paths(tmp_path):
    return [tmp_path / name for name in ("source.json", "state.json", "request.json", "proposal.json")]


def test_incremental_native_publication_live_tail_and_history(tmp_path):
    source, state, request_path, proposal_path = paths(tmp_path)
    save(source, snapshot(3))
    request = prepare(source, state, request_path, batch_size=2)
    assert request["source_head"] == 3 and len(request["new_posts"]) == 2
    assert request["context"] == {
        "outline": [],
        "cards": [],
        "entries": [],
        "refresh": [],
        "next_ids": {"strand": "S1", "decision": "D1"},
    }
    # A later append is live tail, not a reason to discard the prepared batch.
    save(source, snapshot())
    save(proposal_path, proposal(request))
    receipt = apply(source, state, request_path, proposal_path)
    assert apply(source, state, request_path, proposal_path) == receipt
    view = read(source, state)
    assert (view["H"], view["W"]) == (6, 2)
    assert [p["seq"] for p in view["tail"]] == [3, 4, 5, 6]
    assert view["tail"][0]["author"] == "author1"
    assert raw(source, [2])["posts"][0]["reply_to"] == {"message_id": "m1", "seq": 1}
    request2 = prepare(source, state, tmp_path / "r2.json", batch_size=3)
    assert [p["seq"] for p in request2["new_posts"]] == [3, 4, 5]
    # The graph so far reaches the model as the lines readers get, never as stored rows or old posts.
    assert request2["context"] == {
        "outline": [
            "S1 | active | 1 statements, summarized through post 2 | Format",
            "S2 | active | 1 statements, summarized through post 2 | Storage and reader",
        ],
        "cards": [
            "S1 | active | Format\nInitial proposal.\nOpen work:\n"
            "- Implement the reader using files, preserving quotes.\nEvidence: post:2",
            "S2 | active | Storage and reader\nInitial proposal.\nOpen work:\n"
            "- Implement the reader using files, preserving quotes.\nEvidence: post:2",
        ],
        "entries": [
            "1-1 | S1 | responds_to<2-1 | Use JSON — keep 24-hour receipts.",
            "2-1 | S2 | responds_to>1-1 | Use SQLite.",
        ],
        "refresh": [],
        "next_ids": {"strand": "S3", "decision": "D1"},
    }
    save(tmp_path / "p2.json", proposal(request2))
    apply(source, state, tmp_path / "r2.json", tmp_path / "p2.json")
    view = read(source, state)
    assert set(view["strands"]) == {"S2"} and set(view["decisions"]) == {"D1"}
    assert view["statements"] == {} and view["W"] == 5 and [p["seq"] for p in view["tail"]] == [6]
    assert "2-1" not in read(source, state, expand_statements=True)["statements"]
    history = read(source, state, history=True)
    assert history["statements"]["1-1"]["source"]["author"] == "author1"
    assert history["statements"]["1-1"]["text"] == "Use JSON — keep 24-hour receipts."
    assert history["statements"]["1-1"]["surviving_text"] == "Use JSON."
    assert history["statements"]["1-1"]["quotes"] == ["Use JSON — keep 24-hour receipts."]
    assert history["strands"]["S1"]["summary_through"] == 5
    assert history["strands"]["S2"]["statement_ids"] == ["2-1", "3-2", "4-1", "5-1"]
    assert set(history["decisions"]) == {"D1", "D2"}
    assert set(read(source, state, strands=["S1"])["decisions"]) == {"D1", "D2"}
    assert set(read(source, state, strands=["S2"])["decisions"]) == {"D1"}
    assert set(read(source, state, strands=["S1"], history=True)["decisions"]) == {"D1", "D2"}
    assert read(source, state, strands=["S1"])["decisions"]["D1"]["status"] == "active"
    # Installed and module command surfaces share the same parser without host config.
    result = subprocess.run(
        [sys.executable, "-m", "baton.graph", "read", "--source", str(source), "--state", str(state)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["W"] == 5


def test_rejections_leave_state_bytes_unchanged_and_allow_correction(tmp_path):
    source, state, request_path, proposal_path = paths(tmp_path)
    save(source, snapshot())
    request = prepare(source, state, request_path, batch_size=2)
    stale = prepare(source, state, tmp_path / "stale.json", batch_size=1)
    first = proposal(request)
    save(proposal_path, first)
    apply(source, state, request_path, proposal_path)
    committed = state.read_bytes()
    save(tmp_path / "stale-proposal.json", {**proposal(stale), "binding": stale["binding"]})
    with pytest.raises(GraphError, match="stale"):
        apply(source, state, tmp_path / "stale.json", tmp_path / "stale-proposal.json")
    changed = copy.deepcopy(first)
    changed["statements"][0]["text"] = "Different replay"
    save(proposal_path, changed)
    with pytest.raises(GraphError, match="different proposal"):
        apply(source, state, request_path, proposal_path)
    request2 = prepare(source, state, tmp_path / "r2.json", batch_size=3)
    valid = proposal(request2)
    invalid = []
    bad = copy.deepcopy(valid)
    bad["statements"] = [s for s in bad["statements"] if s["seq"] != 4]
    invalid.append((bad, "coverage"))
    bad = copy.deepcopy(valid)
    bad["statements"][0]["quotes"] = ["invented quote"]
    invalid.append((bad, "quote"))
    bad = copy.deepcopy(valid)
    bad["edges"].append(edge("1-1", "4-1"))
    invalid.append((bad, "cycle"))
    bad = copy.deepcopy(valid)
    bad["strands"] = bad["strands"][:1]
    invalid.append((bad, "refreshed summary"))
    bad = copy.deepcopy(valid)
    bad["strands"][0]["statement_ids"] = ["1-1"]  # the host keeps the membership; a proposal cannot state it
    invalid.append((bad, "unknown keys"))
    bad = copy.deepcopy(valid)
    bad["relevance_updates"][0]["evidence"] = ["post:6"]
    invalid.append((bad, "unavailable source"))
    bad = copy.deepcopy(valid)
    bad["relevance_updates"][0]["surviving_text"] = None
    invalid.append((bad, "surviving_text"))
    for bad, diagnostic in invalid:
        save(tmp_path / "p2.json", bad)
        with pytest.raises(GraphError, match=diagnostic):
            apply(source, state, tmp_path / "r2.json", tmp_path / "p2.json")
        assert state.read_bytes() == committed
    edited = snapshot()
    edited["messages"][0]["from_role"] = "changed-author"
    save(source, edited)
    save(tmp_path / "p2.json", valid)
    with pytest.raises(GraphError, match="prefix was edited"):
        apply(source, state, tmp_path / "r2.json", tmp_path / "p2.json")
    assert state.read_bytes() == committed
    save(source, snapshot())
    apply(source, state, tmp_path / "r2.json", tmp_path / "p2.json")
    # Preparing again includes no old raw prefix, only the actual tail.
    request3 = prepare(source, state, tmp_path / "r3.json")
    assert [p["seq"] for p in request3["new_posts"]] == [6]
    # What still stands of a partly replaced statement is shown; a fully replaced one is not shown at all.
    assert request3["context"]["entries"] == [
        "1-1 | S1 | supersedes<3-1 responds_to<4-1 | Use JSON.",
        "3-1 | S1 | supersedes>1-1 | Keep receipts for one hour.",
        "3-2 | S2 | responds_to<4-1 | Use files instead of SQLite.",
        "4-1 | S1 S2 | responds_to>1-1 responds_to>3-2 | Exact source quotes remain mandatory.",
        "5-1 | S2 | - | Implement the reader.",
        "D1 | all | rests on 4-1 | changed at post 5 | Retain exact source quotes.",
        "D2 | S1 | rests on 1-1 | changed at post 5 | Use JSON.",
    ]
    assert request3["context"]["cards"][0] == (
        "S1 | resolved | Format\nJSON remains applicable; receipt retention is one hour.\n"
        "Open work:\n(none)\nEvidence: post:5"
    )
    assert request3["context"]["next_ids"] == {"strand": "S3", "decision": "D3"}


def test_command_line_prepares_a_request_and_forwards_run_limits(tmp_path, monkeypatch):
    source, state, request_path, _ = paths(tmp_path)
    save(source, snapshot())
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "baton.graph",
            "prepare",
            "--source",
            str(source),
            "--state",
            str(state),
            "--request",
            str(request_path),
            "--batch-size",
            "3",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["source_head"] == 6
    assert load_json(request_path)["binding"]["batch"] == {"start": 1, "end": 3}

    forwarded = {}
    monkeypatch.setattr(graph_cli, "run", lambda *args, **kwargs: forwarded.update(kwargs) or {})
    args = graph_cli.parser().parse_args(
        [
            "run",
            "--source",
            str(source),
            "--state",
            str(state),
            "--request",
            str(tmp_path / "cli-run-request.json"),
            "--proposal",
            str(tmp_path / "cli-run-proposal.json"),
            "--base-url",
            "https://provider.invalid/v1",
            "--model",
            "explicit-model",
            "--allow-no-key",
            "--batch-size",
            "3",
            "--max-input-bytes",
            "5000",
        ]
    )
    graph_cli.dispatch(args)
    assert forwarded == {"batch_size": 3, "max_input_bytes": 5000}


def test_source_limits_artifact_aliases_and_disposition(tmp_path):
    source, state, request_path, proposal_path = paths(tmp_path)
    save(source, snapshot(1))
    for mutation in (
        lambda s: s["messages"][0].update(seq=2),
        lambda s: s.update(retention_gaps=[{"start": 1, "end": 1}]),
        lambda s: s["messages"][0].update(conversation_id="other"),
    ):
        bad = snapshot(1)
        mutation(bad)
        save(source, bad)
        with pytest.raises(GraphError):
            load_source(source)
    save(source, snapshot(1))
    with pytest.raises(GraphError, match="exceeds"):
        prepare(source, state, request_path, max_input_bytes=1)
    assert not state.exists() and not request_path.exists()
    with pytest.raises(GraphError, match="distinct"):
        prepare(source, state, source)
    request = prepare(source, state, request_path)
    patch = {
        "binding": request["binding"],
        "statements": [],
        "relevance_updates": [],
        "strands": [],
        "decisions": [],
        "edges": [],
        "dispositions": [{"seq": 1, "reason": "Ceremony."}],
    }
    save(proposal_path, patch)
    apply(source, state, request_path, proposal_path)
    with pytest.raises(GraphError, match="no new posts"):
        prepare(source, state, tmp_path / "empty.json")
    assert load_json(state)["dispositions"][0]["reason"] == "Ceremony."


def test_api_serialization_converges_with_native_and_never_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPH_TEST_KEY", "private-test-secret")
    source = tmp_path / "source.json"
    save(source, snapshot())
    calls = []
    mode = ["success"]

    def provider(http_request):
        calls.append(http_request)
        assert http_request.url.path == "/v1/chat/completions"
        assert http_request.headers["Authorization"] == "Bearer private-test-secret"
        payload = json.loads(http_request.content)
        assert payload["stream"] is False and payload["store"] is False
        assert payload["max_completion_tokens"] == 300
        assert payload["messages"][0]["role"] == "system"
        assert payload["response_format"]["json_schema"]["strict"] is True
        request = json.loads(payload["messages"][1]["content"])
        assert [p["seq"] for p in request["new_posts"]] == list(
            range(request["binding"]["batch"]["start"], request["binding"]["batch"]["end"] + 1)
        )
        shown = [line.split(" | ")[0] for line in request["context"]["entries"]]
        assert shown == ([] if request["binding"]["batch"]["start"] == 1 else ["1-1", "2-1"])
        graph = proposal(request)
        message = {"content": json.dumps(graph)}
        finish = "stop"
        if mode[0] == "truncated":
            finish = "length"
        elif mode[0] == "refusal":
            message["refusal"] = "private source details must not be logged"
        elif mode[0] == "malformed":
            message["content"] = "not JSON"
        elif mode[0] == "absent":
            message = {}
        elif mode[0] == "invalid":
            graph["statements"] = []
            message["content"] = json.dumps(graph)
        elif mode[0] == "http_error":
            return httpx.Response(429, text="private-test-secret full source body")
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": finish, "message": message}],
                "usage": {"prompt_tokens": 70, "completion_tokens": 90},
            },
        )

    options = ProviderOptions(
        "https://provider.invalid/v1", "explicit-model", api_key_env="GRAPH_TEST_KEY", max_output_tokens=300
    )
    with contextlib.nullcontext(httpx.AsyncClient(transport=httpx.MockTransport(provider))) as client:
        result = run(
            source,
            tmp_path / "api-state",
            tmp_path / "api-request",
            tmp_path / "api-proposal",
            options,
            batch_size=2,
            client=client,
        )
        assert result["usage"] == {"prompt_tokens": 70, "completion_tokens": 90}
        assert result["provider_calls"] == len(calls) == 1
        native_request = prepare(source, tmp_path / "native-state", tmp_path / "native-request", batch_size=2)
        save(tmp_path / "native-proposal", proposal(native_request))
        apply(source, tmp_path / "native-state", tmp_path / "native-request", tmp_path / "native-proposal")
        assert load_json(tmp_path / "api-state") == load_json(tmp_path / "native-state")
        second = run(
            source,
            tmp_path / "api-state",
            tmp_path / "api-request-2",
            tmp_path / "api-proposal-2",
            options,
            batch_size=3,
            client=client,
        )
        assert second["provider_calls"] == 1 and len(calls) == 2
        for artifact in ("api-state", "api-request", "api-proposal"):
            assert "private-test-secret" not in (tmp_path / artifact).read_text()
        for failure in ("truncated", "refusal", "malformed", "absent", "invalid", "http_error"):
            mode[0] = failure
            folder = tmp_path / failure
            folder.mkdir()
            before = len(calls)
            with pytest.raises(GraphError) as caught:
                run(
                    source,
                    folder / "state",
                    folder / "request",
                    folder / "proposal",
                    options,
                    batch_size=2,
                    client=client,
                )
            assert len(calls) == before + 1 and not (folder / "state").exists()
            assert "private" not in str(caught.value)
        before = len(calls)
        with pytest.raises(GraphError, match="exceeds"):
            run(
                source,
                tmp_path / "bounded-state",
                tmp_path / "bounded-request",
                tmp_path / "bounded-proposal",
                options,
                batch_size=2,
                max_input_bytes=1,
                client=client,
            )
        assert len(calls) == before
