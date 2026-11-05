"""Empirical tests for the bounded model/tool conversation."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from secretary_support import make_world, table_hashes
from test_secretary_model import Model, _decide, _positions

from baton import messages
from baton.agents.secretary import show
from baton.agents.secretary.decide import SecretaryUnavailable
from baton.agents.secretary.harness import SecretaryHarness
from baton.agents.secretary.policy import load_policy
from baton.agents.secretary.transport import ModelUnavailable


def _context(*, event: str = "one", knowledge: str = "shared knowledge",
             instructions: str = "shared guidance") -> dict:
    return {"principal": "planner", "instructions": instructions, "knowledge": knowledge,
            "summary_bytes": 4000, "mode": "alert",
            "events": [{"source_key": f"message:{event}", "text": "Synthetic incoming event"}],
            "reply_context": []}


def _harness(world) -> SecretaryHarness:
    return SecretaryHarness(load_policy(world.config, world.policy_path))


def _call(call_id: str = "call_1", name: str = "decide_attention", arguments: dict | None = None) -> dict:
    return {"id": call_id, "name": name, "arguments": arguments or {}}


def test_command_decision_adapter_roundtrips_typed_choice_and_reuses_verdict(tmp_path) -> None:
    with make_world(tmp_path) as world:
        model = Model(world, tmp_path, engine=True,
                      plan={"steps": [{"tool_calls": [_call()]}, {"decision": "wake"}],
                            "engine_decision": "hold"})
        harness = _harness(world)
        assessment = asyncio.run(harness.assess(_context(), 5))
        assert assessment["decision"] == "wake"
        assert harness.tools.status == "available"
        assert harness.tools.verdict == {"decision": "hold"}
        assert harness.metrics["model_calls"] == 2 and len(harness.tools.calls) == 1
        [engine_request] = model.engine_requests
        assert engine_request == {
            "version": 1, "task": "decide_attention", "context": _context(),
            "choices": ["wake", "hold", "keep"], "_pid": engine_request["_pid"],
        }


@pytest.mark.parametrize("calls", [
    [_call(name="unknown_tool")],
    [_call(arguments={"explanation": "forbidden"})],
    [_call(), _call()],
])
def test_bad_tool_name_arguments_or_duplicate_batch_ids_fail_before_engine(tmp_path, calls) -> None:
    with make_world(tmp_path) as world:
        model = Model(world, tmp_path, engine=True, plan={"steps": [{"tool_calls": calls}]})
        harness = _harness(world)
        with pytest.raises(ModelUnavailable):
            asyncio.run(harness.assess(_context(), 5))
        assert model.engine_requests == []


def test_repeated_calls_with_distinct_ids_stop_at_policy_limit(tmp_path) -> None:
    with make_world(tmp_path) as world:
        model = Model(world, tmp_path, engine=True, plan={"steps": [
            {"tool_calls": [_call("call_1")]}, {"tool_calls": [_call("call_2")]},
            {"tool_calls": [_call("call_3")]},
        ]})
        messages.send(world.baton, sender="worker", recipient="planner", text="Call limit keeps this pending")
        with pytest.raises(SecretaryUnavailable, match="tool-call limit"):
            _decide(world)
        assert _positions(world) == (0, 0)
        assert show(world.config, world.state_path, world.policy_path)["assessments"] == []
        assert len(model.requests) == 3
        assert len(model.engine_requests) == 1  # The second request reuses the first typed verdict.


def test_model_call_after_tool_is_saved_and_replayed_without_writing_baton(tmp_path) -> None:
    with make_world(tmp_path) as world:
        model = Model(world, tmp_path, engine=True, plan={"steps": [
            {"tool_calls": [_call()]}, {"decision": "wake", "summary": "The event needs a look."}],
            "engine_decision": "hold"})
        sent = messages.send(world.baton, sender="worker", recipient="planner", text="A synthetic incoming event")
        before = table_hashes(world.baton)
        result = _decide(world)
        assert result["decision"] == "wake" and result["brief"]
        assert f"message:{sent['message_id']}" in result["sources"]
        assert result["covered_through"] > 0
        assert result["engine_status"] == "available"
        assert len(show(world.config, world.state_path, world.policy_path)["assessments"]) == 1
        replay = _decide(world)
        assert replay["cached"] is True and replay["decision_id"] == result["decision_id"]
        assert len(model.requests) == 2 and len(model.engine_requests) == 1
        assert table_hashes(world.baton) == before


def test_cancellation_during_command_tool_reaps_child_without_assessment(tmp_path) -> None:
    with make_world(tmp_path) as world:
        model = Model(world, tmp_path, engine=True,
                      plan={"steps": [{"tool_calls": [_call()]}, {"decision": "wake"}]})
        # The engine fixture has an independent plan, so make its step sleep after configuring the command.
        engine_plan = tmp_path / "engine-plan.json"
        engine_plan.write_text(json.dumps({"steps": [{"sleep": 20}], "engine_decision": "hold"}))
        settings = json.loads(world.policy_path.read_text())
        settings["decision_engine"]["command"][4] = str(engine_plan)
        world.policy_path.write_text(json.dumps(settings))
        harness = _harness(world)

        async def run() -> None:
            task = asyncio.create_task(harness.assess(_context(), 5))
            async with asyncio.timeout(3):
                while not model.engine_log_path.exists():
                    await asyncio.sleep(0.01)
            request = model.engine_requests[0]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(ProcessLookupError):
                os.kill(request["_pid"], 0)

        asyncio.run(run())


@pytest.mark.parametrize("bad_response", [
    pytest.param({"model": "", "answers": {"attention": {}}}, id="invalid-envelope"),
    pytest.param({"model": "fake-jev", "answers": {"other": {}}}, id="wrong-answer-key"),
    pytest.param({"model": "fake-jev", "answers": {"attention": {
        "type": "text", "choice": "hold", "probabilities": {"wake": 0, "hold": 1, "keep": 0}, "confidence": 1,
    }}}, id="wrong-answer-type"),
    pytest.param({"model": "fake-jev", "answers": {"attention": {
        "type": "choice", "choice": "hold", "probabilities": {"wake": 0, "hold": 1, "keep": 0},
    }}}, id="missing-answer-field"),
    pytest.param({"model": "fake-jev", "answers": {"attention": {
        "type": "choice", "choice": "ignore", "probabilities": {"wake": 0, "hold": 1, "keep": 0}, "confidence": 1,
    }}}, id="unknown-choice"),
    pytest.param({"model": "fake-jev", "answers": {"attention": {
        "type": "choice", "choice": "hold",
        "probabilities": {"wake": float("nan"), "hold": 0, "keep": 0}, "confidence": 1,
    }}}, id="non-finite-probability"),
    pytest.param({"model": "fake-jev", "answers": {"attention": {
        "type": "choice", "choice": "hold", "probabilities": {"wake": 0.2, "hold": 0.7, "keep": 0}, "confidence": 0.7,
    }}}, id="probabilities-do-not-sum-to-one"),
    pytest.param({"model": "fake-jev", "answers": {"attention": {
        "type": "choice", "choice": "wake", "probabilities": {"wake": 0.2, "hold": 0.8, "keep": 0}, "confidence": 0.8,
    }}}, id="choice-is-not-most-likely"),
    pytest.param({"model": "fake-jev", "answers": {"attention": {
        "type": "choice", "choice": "hold", "probabilities": {"wake": 0, "hold": 1, "keep": 0}, "confidence": 1.1,
    }}}, id="invalid-confidence"),
    pytest.param({"model": "fake-jev", "answers": {"attention": {
        "type": "choice", "choice": "hold", "probabilities": {"wake": 0, "hold": 1, "keep": 0}, "confidence": 1,
    }}, "usage": {"input_tokens": 1}}, id="invalid-optional-usage"),
])
def test_malformed_jev_result_is_unavailable_and_model_can_finish(tmp_path, monkeypatch, bad_response) -> None:
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps(bad_response, allow_nan=True).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass

    with make_world(tmp_path) as world:
        model = Model(world, tmp_path, plan={"steps": [{"tool_calls": [_call()]}, {"decision": "wake"}]})
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setenv("SECRETARY_JEV_TEST_KEY", "synthetic-only")
        value = json.loads(world.policy_path.read_text())
        value["decision_engine"] = {"provider": "jev", "base_url": f"http://127.0.0.1:{server.server_port}",
                                     "api_key_env": "SECRETARY_JEV_TEST_KEY", "timeout_seconds": 2}
        world.policy_path.write_text(json.dumps(value))
        try:
            harness = _harness(world)
            assessment = asyncio.run(harness.assess(_context(), 5))
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        assert assessment["decision"] == "wake"
        assert harness.tools.status == "unavailable"
        assert len(requests) == 1
        [_, model_finish] = model.requests
        assert model_finish["input"][-1]["type"] == "function_call_output"
        assert model_finish["input"][-1]["output"] == '{"error":"decision_engine_unavailable"}'
        assert requests[0]["state"]["instructions"] == "shared guidance"
        assert requests[0]["state"]["knowledge"] == "shared knowledge"
        assert "trust_boundary" not in requests[0]["state"]


def test_responses_keeps_append_only_prefix_reasoning_cache_usage_and_tool_schema_stable(tmp_path, monkeypatch) -> None:
    requests: list[dict] = []
    model_request_count = 0
    event_key = "message:http_event"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal model_request_count
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if self.path == "/jev/systemone":
                response = {"model": "fake-jev", "answers": {"attention": {
                    "type": "choice", "choice": "hold",
                    "probabilities": {"wake": 0, "hold": 1, "keep": 0}, "confidence": 1,
                }}, "usage": {"input_tokens": 4, "output_tokens": 2}}
            else:
                model_request_count += 1
                if model_request_count == 1:
                    response = {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 3,
                        "input_tokens_details": {"cached_tokens": 4}}, "output": [
                        {"type": "reasoning", "id": "rs_opaque", "encrypted_content": "opaque-ciphertext"},
                        {"type": "message", "role": "assistant", "content": [
                            {"type": "output_text", "text": "I will consult the decision model."}]},
                        {"type": "function_call", "id": "fc_opaque", "call_id": "call_1",
                         "name": "decide_attention", "arguments": "{}"},
                    ]}
                else:
                    response = {"status": "completed", "usage": {"input_tokens": 20, "output_tokens": 6,
                        "input_tokens_details": {"cached_tokens": 9}}, "output": [
                        {"type": "message", "content": [{"type": "output_text", "text": json.dumps({
                            "decision": "wake", "reason": "Needs attention", "summary": "A new event arrived.",
                            "sources": [event_key],
                        })}]}
                    ]}
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass

    with make_world(tmp_path) as world:
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setenv("SECRETARY_OPENAI_TEST_KEY", "synthetic-only")
        monkeypatch.setenv("SECRETARY_JEV_TEST_KEY", "synthetic-only")
        policy = json.loads(world.policy_path.read_text())
        policy["model"] = {"provider": "openai", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                           "api_key_env": "SECRETARY_OPENAI_TEST_KEY"}
        policy["decision_engine"] = {"provider": "jev", "base_url": f"http://127.0.0.1:{server.server_port}/jev",
                                     "api_key_env": "SECRETARY_JEV_TEST_KEY"}
        world.policy_path.write_text(json.dumps(policy))
        try:
            harness = _harness(world)
            assessment = asyncio.run(harness.assess(_context(event="http_event"), 5))
            assert harness.metrics["usage"] == {"input_tokens": 30, "output_tokens": 9, "cached_tokens": 13}
            changed_event = _harness(world)
            asyncio.run(changed_event.assess(_context(event="changed"), 5))
            changed_instructions = _harness(world)
            asyncio.run(changed_instructions.assess(
                _context(event="http_event", instructions="new principal instructions"), 5))
            changed_knowledge = _harness(world)
            asyncio.run(changed_knowledge.assess(_context(event="http_event", knowledge="new knowledge"), 5))
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        assert assessment["decision"] == "wake"
        [first, second, third, fourth, fifth] = [request for request in requests if "input" in request]
        first_prefix = first["input"][:4]
        assert first["prompt_cache_options"] == {"mode": "implicit"}
        assert second["input"][:4] == first_prefix
        assert second["input"][4] == {"type": "reasoning", "id": "rs_opaque",
                                      "encrypted_content": "opaque-ciphertext"}
        assert second["input"][5]["content"][0]["text"] == "I will consult the decision model."
        assert second["input"][6]["type"] == "function_call"
        assert second["input"][6]["call_id"] == "call_1"
        assert second["input"][7]["type"] == "function_call_output"
        assert second["input"][7]["call_id"] == "call_1"
        assert second["input"][7]["output"] == '{"decision":"hold"}'
        assert first_prefix[0]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert first_prefix[1]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert first_prefix[2]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert "prompt_cache_breakpoint" not in first_prefix[3]["content"][0]
        assert first["tools"] == second["tools"] and first["tools"][0]["name"] == "decide_attention"
        assert third["input"][:3] == first["input"][:3]
        assert third["input"][3] != first["input"][3]
        assert fourth["input"][:2] == first["input"][:2]
        assert fourth["input"][2] != first["input"][2]
        assert fifth["input"][0] == first["input"][0]
        assert fifth["input"][1] != first["input"][1]
        assert all(request["tools"] == first["tools"] for request in [second, third, fourth, fifth])
