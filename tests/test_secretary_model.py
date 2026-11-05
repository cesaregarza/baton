"""Empirical version 2 secretary tests through its subprocess and HTTP transports."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from secretary_support import conversation, make_world, say, table_hashes

from baton import conversations, messages
from baton.agents.secretary import Secretary, show
from baton.agents.secretary.decide import SecretaryUnavailable

FAKE = Path(__file__).parent / "fixtures" / "secretary_fake_model.py"


class Model:
    def __init__(self, world, tmp_path: Path, *, plan: dict | None = None, timeout: float = 5.0,
                 engine: bool = False) -> None:
        self.world = world
        self.plan_path = tmp_path / "model-plan.json"
        self.log_path = tmp_path / "model-requests.jsonl"
        self.engine_log_path = tmp_path / "engine-requests.jsonl"
        self.set_plan(plan or {})
        self.command = [sys.executable, "-I", "-S", str(FAKE), str(self.plan_path), str(self.log_path)]
        self.engine_command = [sys.executable, "-I", "-S", str(FAKE), str(self.plan_path),
                               str(self.engine_log_path), "engine"]
        self.configure(timeout=timeout, engine=engine)

    def configure(self, *, timeout: float = 5.0, engine: bool = False) -> None:
        value = {"version": 2, "principal": "planner", "model": {"provider": "command",
                 "command": self.command, "timeout_seconds": timeout}}
        if engine:
            value["decision_engine"] = {"provider": "command", "command": self.engine_command,
                                         "timeout_seconds": timeout}
        self.world.policy_path.write_text(json.dumps(value))

    def set_timeout(self, timeout: float) -> None:
        value = json.loads(self.world.policy_path.read_text())
        value["model"]["timeout_seconds"] = timeout
        self.world.policy_path.write_text(json.dumps(value))

    def set_plan(self, value: dict) -> None:
        self.plan_path.write_text(json.dumps(value))

    @property
    def requests(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    @property
    def engine_requests(self) -> list[dict]:
        if not self.engine_log_path.exists():
            return []
        return [json.loads(line) for line in self.engine_log_path.read_text().splitlines()]


def _decide(world, **options) -> dict:
    return asyncio.run(Secretary(world.config, world.state_path, world.policy_path).decide(
        budget_seconds=options.pop("budget_seconds", 5), **options))


def _positions(world) -> tuple[int, int]:
    # The state starts with acknowledged positions at zero; this test reads them from the private state store.
    from baton.agents.secretary.state import AttentionLog
    log = AttentionLog(world.state_path, world.config.data_dir, "planner")
    return log.positions()


def test_command_model_assesses_grouped_conversation_once_caches_and_reads_baton_unchanged(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        model = Model(world, tmp_path)
        first = say(world, room, "worker", "The migration is ready")
        second = say(world, room, "reviewer", "The review found one follow up")
        before = table_hashes(world.baton)

        result = _decide(world, trigger=f"message:{second['seqs']['planner']}")
        [request] = model.requests
        event_sources = [event["source_key"] for event in request["context"]["events"]]
        message_sources = {f"message:{first['message_id']}", f"message:{second['message_id']}"}
        assert message_sources <= set(event_sources) & set(result["sources"])
        assert result["decision"] == "wake" and result["brief"]
        assert request["version"] == 3 and request["context"]["mode"] == "alert"
        assert request["response_schema"]["additionalProperties"] is False
        assert table_hashes(world.baton) == before

        replay = _decide(world, trigger=f"message:{second['seqs']['planner']}")
        assert replay["cached"] is True and replay["decision_id"] == result["decision_id"]
        assert len(model.requests) == 1 and table_hashes(world.baton) == before


def test_acknowledgment_advances_coverage_only_after_delivery_and_next_call_excludes_it(tmp_path) -> None:
    with make_world(tmp_path) as world:
        model = Model(world, tmp_path)
        first = messages.send(world.baton, sender="worker", recipient="planner", text="First incoming note")
        pending = _decide(world)
        assert _positions(world) == (0, 0)
        assert _decide(world)["cached"] is True
        assert _positions(world) == (0, 0)

        assert asyncio.run(Secretary(world.config, world.state_path, world.policy_path).acknowledge(
            pending["decision_id"])) == pending["through_pos"]
        assert _positions(world) == (pending["through_pos"], pending["floor"])
        second = messages.send(world.baton, sender="worker", recipient="planner", text="Second incoming note")
        after = _decide(world)
        [new_request] = model.requests[1:]
        assert [event["source_key"] for event in new_request["context"]["events"]] == [
            f"message:{second['message_id']}"
        ]
        assert f"message:{first['message_id']}" not in after["sources"]


def test_reply_to_an_older_visible_message_supplies_reply_context_after_coverage(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        model = Model(world, tmp_path, plan={"steps": [{}, {"sources": "all_context"}]})
        parent = say(world, room, "planner", "Please check the rollout", attention=["worker"])
        first = _decide(world)
        asyncio.run(Secretary(world.config, world.state_path, world.policy_path).acknowledge(first["decision_id"]))
        reply = say(world, room, "worker", "The rollout is ready", reply_to=parent["message_id"])

        result = _decide(world)
        [request] = model.requests[1:]
        context_sources = {event["source_key"] for event in request["context"]["reply_context"]}
        assert f"message:{parent['message_id']}" in context_sources
        assert f"message:{parent['message_id']}" in result["sources"]
        assert f"message:{reply['message_id']}" in result["sources"]


@pytest.mark.parametrize("decision", ["hold", "keep"])
def test_hold_and_keep_do_not_cover_pending_events(tmp_path, decision) -> None:
    with make_world(tmp_path) as world:
        Model(world, tmp_path, plan={"decision": decision, "summary": "", "sources": []})
        messages.send(world.baton, sender="worker", recipient="planner", text="A later note")
        result = _decide(world)
        assert result["decision"] == decision and result["brief"] == ""
        assert result["covered_through"] == result["since"] == 0
        assert _positions(world) == (0, 0)
        # The alert remains pending because no summary was generated for delivery.
        assert _decide(world)["cached"] is True


@pytest.mark.parametrize(("step", "timeout"), [({"raw": "not json"}, 5), ({"sleep": 2}, 0.2)])
def test_transport_failure_does_not_advance_and_retry_calls_model_again(tmp_path, step, timeout) -> None:
    with make_world(tmp_path) as world:
        model = Model(world, tmp_path, plan={"steps": [step, {}]}, timeout=timeout)
        messages.send(world.baton, sender="worker", recipient="planner", text="Retry this note")
        with pytest.raises(SecretaryUnavailable):
            _decide(world, budget_seconds=3)
        assert _positions(world) == (0, 0)
        model.set_timeout(5)
        model.set_plan({"steps": [{}, {}]})
        result = _decide(world, budget_seconds=5)
        assert result["decision"] == "wake" and len(model.requests) == 2
        assert _positions(world) == (0, 0)


def test_unknown_source_reference_is_rejected_without_saving_assessment(tmp_path) -> None:
    with make_world(tmp_path) as world:
        model = Model(world, tmp_path, plan={"sources": ["message:msg_0000000000000000"]})
        messages.send(world.baton, sender="worker", recipient="planner", text="Cite only real input")
        with pytest.raises(SecretaryUnavailable):
            _decide(world)
        assert _positions(world) == (0, 0)
        model.set_plan({})
        result = _decide(world)
        assert result["decision"] == "wake" and len(model.requests) == 2


def test_configured_decision_tool_is_discretionary_and_needs_no_key_when_skipped(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        model = Model(world, tmp_path)
        monkeypatch.delenv("SECRETARY_UNUSED_JEV_KEY", raising=False)
        policy = json.loads(world.policy_path.read_text())
        policy["decision_engine"] = {"provider": "jev", "api_key_env": "SECRETARY_UNUSED_JEV_KEY"}
        world.policy_path.write_text(json.dumps(policy))
        messages.send(world.baton, sender="worker", recipient="planner", text="A real incoming note")
        result = _decide(world)
        assert len(model.requests) == 1 and model.requests[0]["version"] == 3
        assert model.engine_requests == []
        assert result["engine_status"] == "not_used" and result["decision"] == "wake"


@pytest.mark.parametrize("cache_mode", ["layered", "implicit"])
def test_responses_http_wire_uses_strict_schema_and_skips_reasoning_output(tmp_path, monkeypatch, cache_mode) -> None:
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen.append({"path": self.path, "headers": dict(self.headers),
                         "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            output = {"status": "completed", "output": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps({
                    "decision": "wake", "reason": "Needs a look", "summary": "The note arrived.",
                    "sources": [f"message:{message_id[0]}"]})}]}
            ]}
            payload = json.dumps(output).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args) -> None:
            pass

    with make_world(tmp_path) as world:
        sent = messages.send(world.baton, sender="worker", recipient="planner", text="HTTP path")
        message_id = [sent["message_id"]]
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.setenv("SECRETARY_TEST_KEY", "synthetic-key")
            world.policy_path.write_text(json.dumps({"version": 2, "principal": "planner", "model": {
                "provider": "openai", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "api_key_env": "SECRETARY_TEST_KEY", "cache_mode": cache_mode}}))
            result = _decide(world)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        assert result["decision"] == "wake"
        [request] = seen
        assert request["path"] == "/v1/responses" and request["headers"]["Authorization"] == "Bearer synthetic-key"
        body = request["body"]
        assert body["model"] == "gpt-6-luna" and body["store"] is False
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True
        assert body["text"]["format"]["schema"]["additionalProperties"] is False
        assert len(body["input"]) == 4
        for message in body["input"][:3]:
            assert ("prompt_cache_breakpoint" in message["content"][0]) == (cache_mode == "layered")
        assert ("prompt_cache_options" in body) == (cache_mode == "layered")


def test_revoking_visibility_during_inference_discards_the_summary(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        model = Model(world, tmp_path, plan={"steps": [{"sleep": 0.5}]})
        say(world, room, "worker", "A private progress report")

        async def run():
            task = asyncio.create_task(Secretary(world.config, world.state_path, world.policy_path).decide(
                budget_seconds=5))
            async with asyncio.timeout(3):
                while not model.log_path.exists():
                    await asyncio.sleep(0.01)
            conversations.remove(world.baton, world.config, room, "planner", actor="planner")
            with pytest.raises(SecretaryUnavailable, match="visibility or content changed"):
                await task

        asyncio.run(run())
        assert _positions(world) == (0, 0)
        assert show(world.config, world.state_path, world.policy_path)["assessments"] == []


def test_cancelling_model_reaps_child_and_retry_keeps_the_same_pending_window(tmp_path) -> None:
    with make_world(tmp_path) as world:
        model = Model(world, tmp_path, plan={"steps": [{"sleep": 10}, {}]})
        sent = messages.send(world.baton, sender="worker", recipient="planner", text="Still pending after cancellation")

        async def run():
            secretary = Secretary(world.config, world.state_path, world.policy_path)
            task = asyncio.create_task(secretary.decide(budget_seconds=5))
            async with asyncio.timeout(3):
                while not model.log_path.exists():
                    await asyncio.sleep(0.01)
            [request] = model.requests
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(ProcessLookupError):
                os.kill(request["_pid"], 0)
            assert _positions(world) == (0, 0)
            return await secretary.decide(budget_seconds=5)

        result = asyncio.run(run())
        assert result["sources"] == [f"message:{sent['message_id']}"]
        assert len(model.requests) == 2


def test_hourly_digest_uses_the_model_and_inspection_does_not_infer_again(tmp_path) -> None:
    with make_world(tmp_path) as world:
        model = Model(world, tmp_path, plan={"steps": [
            {"decision": "hold", "summary": "", "sources": []},
            {"decision": "hold", "summary": "The worker has started the requested review."},
        ]})
        messages.send(world.baton, sender="worker", recipient="planner", text="Starting the review")
        assert _decide(world)["decision"] == "hold"
        digest = _decide(world, hourly=True)
        assert digest["brief"] == "The worker has started the requested review."
        assert model.requests[-1]["context"]["mode"] == "digest"
        recorded = show(world.config, world.state_path, world.policy_path)
        assert len(recorded["assessments"]) == 2
        assert len(model.requests) == 2
        asyncio.run(Secretary(world.config, world.state_path, world.policy_path).acknowledge(digest["decision_id"]))
        assert _positions(world)[0] == digest["through_pos"]


def test_context_overflow_is_explicit_and_does_not_infer_from_a_truncated_window(tmp_path) -> None:
    from baton.agents.secretary.inbox import InboxTooLarge
    with make_world(tmp_path) as world:
        model = Model(world, tmp_path)
        for index in range(2):
            messages.send(world.baton, sender="worker", recipient="planner", text=f"Report {index}")
        policy = json.loads(world.policy_path.read_text())
        world.policy_path.write_text(json.dumps({**policy, "max_events": 1}))
        with pytest.raises(InboxTooLarge):
            _decide(world)
        assert model.requests == [] and _positions(world) == (0, 0)
        world.policy_path.write_text(json.dumps({**policy, "max_events": 2}))
        result = _decide(world)
        assert len(result["sources"]) == 2


def test_old_policy_is_rejected_before_model_or_state_access(tmp_path) -> None:
    from baton.config import ConfigError
    with make_world(tmp_path) as world:
        model = Model(world, tmp_path)
        messages.send(world.baton, sender="worker", recipient="planner", text="A new request")
        before = table_hashes(world.baton)
        world.policy_path.write_text(json.dumps({"version": 1, "principal": "planner"}))
        with pytest.raises(ConfigError, match="unsupported secretary policy version"):
            _decide(world)
        assert not world.state_path.exists() and model.requests == []
        assert table_hashes(world.baton) == before


def test_obsolete_state_is_rejected_without_conversion_or_inference(tmp_path) -> None:
    import sqlite3

    from baton.config import ConfigError
    with make_world(tmp_path) as world:
        model = Model(world, tmp_path)
        with sqlite3.connect(world.state_path) as old:
            old.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            old.execute("INSERT INTO schema_version VALUES (2)")
        world.state_path.chmod(0o600)
        before = world.state_path.read_bytes()
        with pytest.raises(ConfigError, match="obsolete or unrelated schema"):
            _decide(world)
        assert world.state_path.read_bytes() == before and model.requests == []
