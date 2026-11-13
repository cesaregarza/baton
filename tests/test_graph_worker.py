"""The background graph worker on synthetic SQLite with scripted producers: scheduling, recovery, publication.

Nothing here runs a model. A scripted `Transport` stands in for every provider so the host's own
guarantees can be checked: saved candidates reused before new calls, terminal blocks that survive restarts
and new posts, compact-only retries, zero-call publication recovery, and the atomic snapshot publication
that the MCP readers later serve. The call budget is checked in `test_graph_worker_budget` and the compact
contract in `test_graph_compact`; both use the scripted producers defined here.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_index_writer import converse, index_config, say

from baton import config as config_module
from baton.config import ConfigError
from baton.database.store import connect
from baton.graph import ledger
from baton.graph import snapshot as snapshot_module
from baton.graph.contract import GraphError, current
from baton.graph.failure import TransportError
from baton.graph.source import load_json
from baton.graph.worker import Worker, reset, status

DAY = 86_400.0


def worker_config(tmp_path: Path, indexer: dict | None = None, **worker) -> config_module.Config:
    config = index_config(tmp_path, **(indexer or {}))
    raw = {
        "enabled": True,
        "producer": "alpha",
        "state_dir": str(tmp_path / "graph-state"),
        "conversations": [],
        "provider": "http",
        "model": "synthetic-model",
        "base_url": "https://provider.invalid/v1",
        "allow_no_key": True,
        "batch_size": 2,
        **worker,
    }
    return replace(
        config,
        search=replace(config.search, enabled=True),
        graph_worker=config_module.graph_worker.parse(raw, config.indexer, config.data_dir),
    )


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class ScriptedTransport:
    """Answers extraction requests with the synthetic graph fixture and compact requests with a scripted view.

    `script` is consumed one entry per call: "ok", "garbage", "crash", a dict of compact overrides, or a failure
    category, raised as that category. `verify_failures` verifications fail before verification passes.
    """

    mode = "scripted"

    def __init__(self, script=(), verify_failures=0) -> None:
        self.script = list(script)
        self.calls: list[dict] = []
        self.verified = 0
        self.verify_failures = verify_failures

    def verify(self):
        self.verified += 1
        if self.verified <= self.verify_failures:
            raise TransportError("unverified_client", "scripted verification failure")
        return {"mode": self.mode}

    def produce(self, request, schema, limits, *, stop=None):
        self.calls.append({"request": request, "schema": schema, "limits": limits})
        action = self.script.pop(0) if self.script else "ok"
        if isinstance(action, str) and action not in ("ok", "garbage", "crash"):
            raise TransportError(action, f"scripted {action}")
        if action == "crash":
            raise RuntimeError("scripted crash inside the transport")
        if action == "garbage":
            candidate = {"unexpected": True}
        elif "compact_schema" in request:
            candidate = compact_candidate(request, action if isinstance(action, dict) else {})
        else:
            candidate = extraction_candidate(request)
        return {"candidate": candidate, "usage": {"total_tokens": 7}, "input_bytes": 1, "output_bytes": 2, "turns": 1}


def extraction_candidate(request):
    """One statement per new post in strand S1, a decision on the first batch: valid for any synthetic thread."""
    start, end = (request["binding"]["batch"][key] for key in ("start", "end"))
    candidate = {
        "binding": request["binding"],
        "statements": [],
        "relevance_updates": [],
        "edges": [],
        "dispositions": [],
        "decisions": [],
        "strands": [],
    }
    for post in request["new_posts"]:
        candidate["statements"].append(
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
    candidate["strands"] = [
        {
            "id": "S1",
            "title": "Release",
            "summary": f"Notes through {end}.",
            "state": "active",
            "open_work": ["Review the notes."],
            "evidence": [f"{end}-1"],
        }
    ]
    if start == 1:
        candidate["decisions"] = [
            {"id": "D1", "text": "Keep the notes.", "status": "active", "applies_to": ["all"], "evidence": ["post:1"]}
        ]
    return candidate


def compact_candidate(request, overrides):
    return {
        "schema_version": 1,
        "graph_digest": request["graph_digest"],
        "body": "Release: review the notes (orchestrator asked). Keep the notes.",
        "strand_ids": [s["id"] for s in request["strands"]],
        "decision_ids": [d["id"] for d in request["decisions"]],
        "evidence": sorted({ref for item in [*request["strands"], *request["decisions"]] for ref in item["evidence"]}),
        **overrides,
    }


def published(db, conversation_id):
    return [
        tuple(row)
        for row in db.execute(
            "SELECT v.covered_through_seq, v.version, g.revision FROM artifact_versions v"
            " LEFT JOIN graph_snapshots g ON g.version_id = v.id WHERE v.conversation_id = ? ORDER BY v.version",
            (conversation_id,),
        )
    ]


def run_until_idle(worker, clock, *, cycles=12):
    results = []
    for _ in range(cycles):
        results.append(worker.cycle())
        phases = {r["phase"] for r in results[-1]["records"].values()}
        if phases <= {"idle", "blocked"}:
            break
        clock.now += ledger.RETRY_DELAY + 1
    return results


def test_disabled_worker_makes_no_calls_and_enabled_config_is_validated(tmp_path) -> None:
    config = index_config(tmp_path)
    assert config.graph_worker.enabled is False
    with pytest.raises(Exception, match="disabled"):
        Worker(config, transport=ScriptedTransport())
    assert status(config) == {"enabled": False}
    base = {
        "enabled": True,
        "producer": "alpha",
        "state_dir": str(tmp_path / "graph-state"),
        "conversations": ["conv_a"],
        "provider": "http",
        "model": "m",
        "base_url": "https://provider.invalid/v1",
        "allow_no_key": True,
    }
    native_base = {"provider": "codex", "base_url": None, "allow_no_key": False, "client_version": "1.2.3"}
    for bad, match in (
        ({"producer": "nobody"}, "indexer.producers"),
        ({"state_dir": str(config.data_dir / "graphs")}, "outside host.data_dir"),
        ({"conversations": []}, "conversations"),
        ({"conversations": ["../escape"]}, "conversations"),
        ({"provider": "magic"}, "provider"),
        ({"model": ""}, "model"),
        ({"allow_no_key": False}, "api_key_env"),
        ({"daily_call_cap": -1}, "daily_call_cap"),
        ({"batch_size": 1.5}, "whole number"),
        ({"max_input_bytes": 2000.5}, "whole number"),
        ({"publication_attempts": 2.0}, "whole number"),
        ({**native_base, "credential_env": ["BATON_TOKEN"]}, "Baton"),
        ({**native_base, "provider": "claude", "command": []}, "command"),
        # The trusted client version is operator config: required for a native client, refused for http.
        ({**native_base, "client_version": None}, "client_version"),
        ({**native_base, "provider": "claude", "client_version": "latest"}, "client_version"),
        ({"client_version": "1.2.3"}, "client_version"),
    ):
        with pytest.raises(ConfigError, match=match):
            config_module.graph_worker.parse({**base, **bad}, config.indexer, config.data_dir)
    native = config_module.graph_worker.parse(
        {**base, **native_base, "credential_env": ["CODEX_HOME"]}, config.indexer, config.data_dir
    )
    assert native.command == ("codex",) and native.credential_env == ("CODEX_HOME",)
    assert native.client_version == "1.2.3"
    durations = config_module.graph_worker.parse(
        {**base, "poll_seconds": 2.5, "call_timeout_seconds": 0.5 + 1}, config.indexer, config.data_dir
    )
    assert (durations.poll_seconds, durations.call_timeout_seconds) == (2.5, 1.5)  # only durations may be fractional
    # Zero is the explicit unlimited-day setting; leaving the cap out keeps the ordinary budget.
    unlimited = config_module.graph_worker.parse({**base, "daily_call_cap": 0}, config.indexer, config.data_dir)
    assert (durations.daily_call_cap, unlimited.daily_call_cap) == (8, 0)
    # A disabled section ignores everything else it says.
    assert (
        config_module.graph_worker.parse({**base, "enabled": False}, config.indexer, config.data_dir).enabled is False
    )


def test_a_host_without_full_text_search_is_refused_when_the_worker_starts(tmp_path, monkeypatch) -> None:
    config = worker_config(tmp_path, conversations=["conv_a"])
    monkeypatch.setattr("baton.database.store.fts5_available", lambda conn: False)
    # Extraction chooses what to show the model by search; a worker that could not would only block later.
    with pytest.raises(GraphError, match="FTS5"):
        Worker(config, transport=ScriptedTransport())


def test_worker_extracts_compacts_publishes_and_readers_see_the_snapshot(tmp_path) -> None:
    config = worker_config(tmp_path, conversations=["placeholder"])
    with connect(config.db_path) as db:
        cid, mids = converse(db, config, 3)
    config = worker_config(tmp_path, conversations=[cid])
    clock = Clock()
    transport = ScriptedTransport()
    worker = Worker(config, transport=transport, clock=clock)
    first = worker.cycle()
    record = first["records"][cid]
    assert (record["phase"], record["accepted_W"], record["published_W"], record["target_head"]) == (
        "compacting",
        2,
        0,
        3,
    )
    assert first["model_calls"] == 1 and transport.calls[0]["schema"]["properties"].keys() >= {"binding", "statements"}
    second = worker.cycle()  # the deterministic view fits the bounds, so compaction needs no model call
    assert second["records"][cid]["phase"] == "publishing" and second["model_calls"] == 0
    third = worker.cycle()
    assert (third["records"][cid]["phase"], third["records"][cid]["published_W"]) == ("pending", 2)
    with connect(config.db_path) as db:
        assert published(db, cid) == [(2, 1, 1)]
        snapshot = json.loads(db.execute("SELECT snapshot FROM graph_snapshots").one()[0])
        assert set(snapshot["statements"]) == {"1-1", "2-1"} and snapshot["graph_digest"] == snapshot_module.digest_of(
            snapshot
        )
        assert "text" not in snapshot and all("messages" not in value for value in snapshot["statements"].values())
    results = run_until_idle(worker, clock)
    final = results[-1]["records"][cid]
    assert (final["phase"], final["accepted_W"], final["published_W"]) == ("idle", 3, 3)
    assert len(transport.calls) == 2  # one extraction call per batch; compaction stayed deterministic
    with connect(config.db_path) as db:
        assert published(db, cid) == [(2, 1, 1), (3, 2, 2)]
        say(db, config, cid, "note 4")
    polled = worker.cycle()
    assert polled["records"][cid]["phase"] in ("compacting", "extracting")  # a new head reopens work
    assert all(call["limits"].max_input_bytes == 512_000 for call in transport.calls)
    ledger_state = status(config)
    assert ledger_state["enabled"] and ledger_state["in_flight"] is None
    assert all(entry["outcome"] == "returned" and "usage" in entry for entry in ledger_state["recent_calls"])
    # Operator reset is explicit administration and applies to records only.
    assert reset(config, cid)["phase"] == "pending"
    with pytest.raises(Exception, match="unknown worker conversation"):
        reset(config, "conv_nowhere")


def test_restart_consumes_the_in_flight_reservation_and_reuses_saved_candidates(tmp_path) -> None:
    config = worker_config(tmp_path, conversations=["placeholder"])
    with connect(config.db_path) as db:
        cid, _ = converse(db, config, 2)
    config = worker_config(tmp_path, conversations=[cid])
    clock = Clock()
    crashing = ScriptedTransport(["crash"])
    worker = Worker(config, transport=crashing, clock=clock)
    with pytest.raises(RuntimeError, match="scripted crash"):
        worker.cycle()
    ledger_file = load_json(config.graph_worker.state_dir / "ledger.json")
    assert ledger_file["in_flight"] is None and ledger_file["recent_calls"][-1]["outcome"] == "crash"
    assert ledger_file["conversations"][cid]["attempts"] == 1
    # Simulate a process that died between reserving and settling: the reservation is consumed on restart.
    ledger_file["in_flight"] = {"conversation_id": cid, "phase": "extracting", "request_digest": "x", "attempt": 2}
    ledger_file["conversations"][cid]["attempts"] = 2
    ledger_file["calls"] = {ledger.utc_day(clock.now): 2}
    # Written in the version-1 format, without the refusal counters: opening it upgrades it in place.
    ledger_file["version"] = 1
    ledger_file.pop("refused", None)
    ledger_file["conversations"][cid].pop("refusals", None)
    (config.graph_worker.state_dir / "ledger.json").write_text(json.dumps(ledger_file))
    restarted = Worker(config, transport=ScriptedTransport(), clock=clock)
    first = restarted.cycle()
    upgraded = load_json(config.graph_worker.state_dir / "ledger.json")
    assert (upgraded["version"], upgraded.get("refused"), upgraded["calls"]) == (2, {}, {ledger.utc_day(clock.now): 2})
    assert upgraded["conversations"][cid].get("refusals") == 0
    assert first["interrupted"]["attempt"] == 2
    assert first["records"][cid]["phase"] == "blocked"  # both extraction calls were spent without a candidate
    assert restarted.transport.calls == []
    # A saved candidate is validated and reused before any new call: write one by hand and reset the record.
    reset(config, cid)
    paths = restarted.paths(cid)
    request = load_json(next((paths.root / "requests").glob("*.json")))
    candidate = extraction_candidate(request)
    paths.proposal(request["binding"]["request_id"], 9).parent.mkdir(exist_ok=True)
    paths.proposal(request["binding"]["request_id"], 9).write_text(json.dumps(candidate))
    reused = restarted.cycle()
    assert reused["records"][cid]["accepted_W"] == 2 and reused["model_calls"] == 0
    assert restarted.transport.calls == []


def test_a_saved_request_from_another_contract_is_prepared_again_with_a_full_attempt_count(tmp_path) -> None:
    config = worker_config(tmp_path, conversations=["placeholder"])
    with connect(config.db_path) as db:
        cid, _ = converse(db, config, 2)
    config = worker_config(tmp_path, conversations=[cid])
    clock = Clock()
    worker = Worker(config, transport=ScriptedTransport(["timeout"]), clock=clock)
    worker.cycle()
    # What an upgrade finds on disk: a request an earlier contract wrote, with both of its calls already spent.
    request_path = next((worker.paths(cid).root / "requests").glob("*.json"))
    older = {**load_json(request_path), "version": 1, "instructions": "An earlier producer contract."}
    older["binding"] = {**older["binding"], "request_id": "older"}
    request_path.write_text(json.dumps(older))
    ledger_path = config.graph_worker.state_dir / "ledger.json"
    ledger_file = load_json(ledger_path)
    ledger_file["conversations"][cid].update(request_id="older", attempts=2)
    ledger_path.write_text(json.dumps(ledger_file))
    clock.now += ledger.RETRY_DELAY
    restarted = Worker(config, transport=ScriptedTransport(), clock=clock)
    accepted = restarted.cycle()
    # No answer to the older request could be applied, so the batch is prepared again instead of blocking.
    assert accepted["records"][cid]["accepted_W"] == 2 and len(restarted.transport.calls) == 1
    request = restarted.transport.calls[0]["request"]
    assert current(request) and request["binding"]["request_id"] != "older"
    assert load_json(request_path) == request


def test_compact_retries_alone_and_publication_recovers_with_zero_calls(tmp_path, monkeypatch) -> None:
    # The deterministic rendering no longer fits, so compaction needs a call; both bounds differ from the defaults.
    limits = {"max_artifact_bytes": 120, "max_citations": 7}
    config = worker_config(tmp_path, limits, conversations=["placeholder"])
    with connect(config.db_path) as db:
        cid, _ = converse(db, config, 2)
    config = worker_config(tmp_path, limits, conversations=[cid])
    clock = Clock()
    # Extraction, then a compact view that cites nothing recorded (rejected), then a good one.
    transport = ScriptedTransport(["ok", {"evidence": ["post:2"], "strand_ids": []}, "ok"])
    worker = Worker(config, transport=transport, clock=clock)
    worker.cycle()
    compacting = worker.cycle()
    record = compacting["records"][cid]
    assert (record["phase"], record["attempts"], record["error_category"]) == ("compacting", 1, "rejected")
    assert record["accepted_W"] == 2 and record["request_id"].startswith("compact:")
    clock.now += ledger.RETRY_DELAY
    corrected = worker.cycle()
    assert corrected["records"][cid]["phase"] == "publishing" and len(transport.calls) == 3
    assert (
        "correction" in transport.calls[2]["request"]
        and "rejected_output" in transport.calls[2]["request"]["correction"]
    )
    # The first request and its correction both state the configured bounds the host will enforce.
    for call in transport.calls[1:]:
        assert call["request"]["limits"] == {"max_body_bytes": 120, "max_citations": 7}
    snapshot_digest = corrected["records"][cid]["request_id"].removeprefix("publish:")
    view = load_json(worker.paths(cid).compact_view(snapshot_digest))
    assert view["body"].startswith("Release: review the notes")

    from baton.graph import worker as worker_module

    original = worker_module.publish_graph
    interruptions = {"left": 2}

    def interrupted(*args, **kwargs):
        if interruptions["left"]:
            interruptions["left"] -= 1
            raise OSError("synthetic publication interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(worker_module, "publish_graph", interrupted)
    failed = worker.cycle()
    record = failed["records"][cid]
    assert (record["phase"], record["attempts"], record["error_category"]) == ("publishing", 1, "publication_failed")
    assert worker.cycle()["stepped"] == []  # no tight loop: the retry waits out its delay
    clock.now += ledger.RETRY_DELAY
    assert worker.cycle()["records"][cid]["attempts"] == 2
    clock.now += ledger.RETRY_DELAY
    done = worker.cycle()
    assert (done["records"][cid]["phase"], done["records"][cid]["published_W"]) == ("idle", 2)
    assert len(transport.calls) == 3  # publication recovery never called a model
    with connect(config.db_path) as db:
        [(covered, version, revision)] = published(db, cid)
        assert (covered, version, revision) == (2, 1, 1)
        body = db.execute("SELECT body FROM artifact_versions").one()[0]
        assert body == view["body"]


def test_publication_exhaustion_blocks_and_input_overflow_blocks_before_any_call(tmp_path, monkeypatch) -> None:
    config = worker_config(tmp_path, conversations=["placeholder"])
    with connect(config.db_path) as db:
        cid, _ = converse(db, config, 2)
    config = worker_config(tmp_path, conversations=[cid], publication_attempts=1)
    clock = Clock()
    worker = Worker(config, transport=ScriptedTransport(), clock=clock)
    worker.cycle()
    worker.cycle()
    from baton.graph import worker as worker_module

    monkeypatch.setattr(worker_module, "publish_graph", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    blocked = worker.cycle()
    assert (blocked["records"][cid]["phase"], blocked["records"][cid]["error_category"]) == (
        "blocked",
        "publication_failed",
    )
    small = worker_config(tmp_path / "small", conversations=[cid], max_input_bytes=1000)
    with connect(small.db_path) as db:
        other, _ = converse(db, small, 1)
    small = worker_config(tmp_path / "small", conversations=[other], max_input_bytes=1000)
    transport = ScriptedTransport()
    overflow = Worker(small, transport=transport, clock=clock).cycle()
    assert overflow["records"][other]["phase"] == "blocked"
    assert overflow["records"][other]["error_category"] == "input_limit" and transport.calls == []
    # The prepare failure's type decides the block, never its wording.
    from baton.graph.operations import InputOverflow

    for raised, category in ((InputOverflow("recall"), "input_limit"), (GraphError("exceeds"), "unsafe_source")):
        root = tmp_path / category
        prepared = worker_config(root, conversations=["placeholder"])
        with connect(prepared.db_path) as db:
            third, _ = converse(db, prepared, 1)
        monkeypatch.setattr(worker_module, "prepare", lambda *a, raised=raised, **k: (_ for _ in ()).throw(raised))
        prepared = worker_config(root, conversations=[third])
        record = Worker(prepared, transport=transport, clock=clock).cycle()["records"][third]
        assert (record["phase"], record["error_category"], transport.calls) == ("blocked", category, [])


def test_second_worker_exits_without_inference_and_tampered_snapshot_is_rejected(tmp_path) -> None:
    config = worker_config(tmp_path, conversations=["placeholder"])
    with connect(config.db_path) as db:
        cid, _ = converse(db, config, 2)
    config = worker_config(tmp_path, conversations=[cid])
    clock = Clock()
    with ledger.Ledger.open(config.graph_worker.state_dir):
        second = Worker(config, transport=ScriptedTransport(), clock=clock)
        with pytest.raises(ledger.WorkerBusy):
            second.cycle()
        assert second.transport.calls == []
    worker = Worker(config, transport=ScriptedTransport(), clock=clock)
    worker.cycle()
    worker.cycle()
    # A forged attachment is refused by the host's own validation, independently of local files.
    from baton.database.store import connect as open_db
    from baton.derived import begin_batch, commit_batch, read_batch
    from baton.errors import InvalidRequest

    paths = worker.paths(cid)
    state = load_json(paths.state)
    snapshot = snapshot_module.project(state)
    forged = json.loads(json.dumps(snapshot))
    forged["statements"]["1-1"]["quotes"] = ["never said"]
    forged["graph_digest"] = snapshot_module.digest_of(forged)
    with open_db(config.db_path) as db:
        manifest = begin_batch(db, config, "beta", cid, ["summary"], through_seq=2)
        for page in range(1, manifest["pages"] + 1):
            read_batch(db, config, "beta", manifest["batch_id"], page)
        artifact = {"kind": "summary", "body": "forged", "citations": [], "expected_version": 0}
        with pytest.raises(InvalidRequest, match="quote is not exact"):
            commit_batch(
                db,
                config,
                "beta",
                manifest["batch_id"],
                {"request_id": "forged", "artifacts": [{**artifact, "graph_snapshot": forged}]},
            )
        bad_order = json.loads(json.dumps(snapshot))
        bad_order["revision"] = 7  # a changed revision alone breaks the digest binding
        with pytest.raises(InvalidRequest, match="digest"):
            commit_batch(
                db,
                config,
                "beta",
                manifest["batch_id"],
                {"request_id": "forged-2", "artifacts": [{**artifact, "graph_snapshot": bad_order}]},
            )
        with pytest.raises(InvalidRequest, match="summary artifact only"):
            commit_batch(
                db,
                config,
                "beta",
                manifest["batch_id"],
                {
                    "request_id": "forged-3",
                    "artifacts": [{**artifact, "kind": "decisions", "graph_snapshot": snapshot}],
                },
            )
        assert db.execute("SELECT COUNT(*) FROM graph_snapshots").one()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM artifact_versions").one()[0] == 0
        # The genuine snapshot commits with its summary in one transaction, and a replay with another graph conflicts.
        good = {"request_id": "good", "artifacts": [{**artifact, "graph_snapshot": snapshot}]}
        committed = commit_batch(db, config, "beta", manifest["batch_id"], good)
        assert committed["created"] and db.execute("SELECT COUNT(*) FROM graph_snapshots").one()[0] == 1
        assert not commit_batch(db, config, "beta", manifest["batch_id"], good)["created"]
        from baton.errors import IdempotencyConflict

        with pytest.raises(IdempotencyConflict):
            commit_batch(db, config, "beta", manifest["batch_id"], {**good, "artifacts": [artifact]})


def test_removed_allowlist_entries_keep_their_records_but_are_never_scheduled(tmp_path) -> None:
    config = worker_config(tmp_path, conversations=["placeholder"])
    with connect(config.db_path) as db:
        cid, _ = converse(db, config, 2)
        other, _ = converse(db, config, 1)
    clock = Clock()
    first = Worker(worker_config(tmp_path, conversations=[cid]), transport=ScriptedTransport(), clock=clock)
    assert first.cycle()["records"][cid]["phase"] == "compacting"
    narrowed = worker_config(tmp_path, conversations=[other])
    transport = ScriptedTransport()
    restarted = Worker(narrowed, transport=transport, clock=clock)
    results = run_until_idle(restarted, clock)
    records = results[-1]["records"]
    assert records[cid]["phase"] == "compacting" and records[cid]["accepted_W"] == 2  # retained, untouched
    assert cid not in {step for result in results for step in result["stepped"]}
    assert all("compact_schema" not in call["request"] for call in transport.calls)  # only `other` was served
    assert records[other]["published_W"] == 1
    with connect(config.db_path) as db:
        assert published(db, cid) == []


def test_restart_between_apply_and_bookkeeping_publishes_the_accepted_batch_first(tmp_path, monkeypatch) -> None:
    config = worker_config(tmp_path, conversations=["placeholder"])
    with connect(config.db_path) as db:
        cid, _ = converse(db, config, 4)
    config = worker_config(tmp_path, conversations=[cid])
    clock = Clock()
    from baton.graph import worker as worker_module

    original = worker_module.Worker._after_apply
    crashes = {"left": 1}

    def crash_once(self, ledger_, conversation_id, state):
        if crashes["left"]:
            crashes["left"] -= 1
            raise RuntimeError("synthetic crash after apply, before bookkeeping")
        return original(self, ledger_, conversation_id, state)

    monkeypatch.setattr(worker_module.Worker, "_after_apply", crash_once)
    first = Worker(config, transport=ScriptedTransport(), clock=clock)
    with pytest.raises(RuntimeError, match="after apply"):
        first.cycle()
    assert load_json(first.paths(cid).state)["watermark"] == 2  # the checkpoint advanced; the ledger did not see it
    transport = ScriptedTransport()
    restarted = Worker(config, transport=transport, clock=clock)
    recovered = restarted.cycle()
    record = recovered["records"][cid]
    assert (record["phase"], record["accepted_W"], recovered["model_calls"]) == ("compacting", 2, 0)
    results = run_until_idle(restarted, clock)
    assert results[-1]["records"][cid]["published_W"] == 4
    with connect(config.db_path) as db:
        assert published(db, cid) == [(2, 1, 1), (4, 2, 2)]  # W=2 was published before any further extraction
    assert len(transport.calls) == 1  # only the 3-4 batch needed a model call after the restart


def test_a_fitting_deterministic_view_needs_no_model_input_budget(tmp_path) -> None:
    config = worker_config(tmp_path, conversations=["placeholder"])
    with connect(config.db_path) as db:
        cid, _ = converse(db, config, 2)
    config = worker_config(tmp_path, conversations=[cid])
    clock = Clock()
    Worker(config, transport=ScriptedTransport(), clock=clock).cycle()  # extraction under the normal ceiling
    tight = worker_config(tmp_path, conversations=[cid], max_input_bytes=1000)  # below any compact request
    transport = ScriptedTransport()
    worker = Worker(tight, transport=transport, clock=clock)
    compacted = worker.cycle()
    assert compacted["records"][cid]["phase"] == "publishing" and compacted["model_calls"] == 0
    done = worker.cycle()
    assert (done["records"][cid]["phase"], done["records"][cid]["published_W"]) == ("idle", 2)
    assert transport.calls == []
    with connect(config.db_path) as db:
        assert published(db, cid) == [(2, 1, 1)]


def wait_for(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_service_owns_its_directory_for_life_and_stop_cancels_the_native_call(tmp_path) -> None:
    from test_graph_transports import FAKE_VERSION, alive, fake, wait_gone

    from baton.graph import native
    from baton.graph.worker import serve

    config = worker_config(tmp_path, conversations=["placeholder"])
    with connect(config.db_path) as db:
        cid, _ = converse(db, config, 2)
    config = worker_config(tmp_path, conversations=[cid], poll_seconds=3600, call_timeout_seconds=600)
    command, dump = fake(tmp_path, "codex", "hang")
    worker = Worker(config, transport=native.CodexTransport(command, "m", [], FAKE_VERSION))
    stop = threading.Event()
    service = threading.Thread(target=serve, args=(config, stop), kwargs={"worker": worker}, daemon=True)
    service.start()
    assert wait_for((dump / "descendant.pid").exists), "the native client never started"
    descendant = int((dump / "descendant.pid").read_text())
    assert alive(descendant)
    # While the service owns the directory, a reset is queued for it and a second service ends without a call.
    assert reset(config, cid)["reset"] == "requested"
    other = Worker(config, transport=ScriptedTransport())
    serve(config, threading.Event(), worker=other)
    assert other.transport.calls == [] and other.transport.verified == 0  # not even verification ran
    stop.set()
    service.join(timeout=15)
    assert not service.is_alive(), "shutdown did not wait for the cancelled call"
    assert wait_gone(descendant), "the model process group survived shutdown"
    state = status(config)
    assert state["in_flight"] is None and state["recent_calls"][-1]["outcome"] == "cancelled"
    record = state["conversations"][cid]
    assert (record["phase"], record["attempts"], record["accepted_W"]) == ("extracting", 1, 0)
    assert (
        not list((worker.paths(cid).root / "proposals").glob("*"))
        if (worker.paths(cid).root / "proposals").exists()
        else True
    )
    with connect(config.db_path) as db:
        assert published(db, cid) == []
    # A failed verification at start does not turn the worker off, and the lock also covers idle waits.
    idle_transport = ScriptedTransport(verify_failures=1)
    idle_worker = Worker(worker_config(tmp_path, conversations=[cid], poll_seconds=3600), transport=idle_transport)
    idle_stop = threading.Event()
    idle_service = threading.Thread(target=serve, args=(idle_worker.config, idle_stop), kwargs={"worker": idle_worker})
    idle_service.start()
    # Its first cycle applies the queued reset and moves the record on; then it waits its hour, still owning.
    assert wait_for(lambda: status(config)["conversations"][cid]["phase"] == "compacting")
    assert idle_transport.verified == 1 and not list((config.graph_worker.state_dir / "reset-requests").iterdir())
    with pytest.raises(ledger.WorkerBusy), ledger.Ledger.open(config.graph_worker.state_dir):
        pass
    idle_stop.set()
    idle_service.join(timeout=15)
    assert not idle_service.is_alive()
    assert reset(config, cid)["phase"] == "pending"  # released with the service
