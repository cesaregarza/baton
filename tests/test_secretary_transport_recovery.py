"""Empirical queue behavior for transport failures and page-specific command failures."""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest
from secretary_support import conversation, make_world, say
from test_secretary_recovery import _backlog, _step, _worker

from baton.agents.secretary.attention.decide import SecretaryUnavailable
from baton.agents.secretary.attention.health import ModelHealth
from baton.agents.secretary.attention.state import AttentionLog
from baton.config.secretary import load_policy


def _openai_model(base_url: str = "https://secretary-test.invalid") -> dict:
    return {
        "provider": "openai",
        "model": "synthetic-model",
        "base_url": base_url,
        "api_key_env": "BATON_SYNTHETIC_MODEL_KEY",
        "reasoning_effort": "minimal",
        "max_output_tokens": 4096,
        "timeout_seconds": 0.5,
        "cache_mode": "implicit",
    }


@pytest.mark.parametrize("timeout_type", [httpx.ConnectTimeout, httpx.PoolTimeout])
def test_connect_and_pool_timeouts_remain_outages_through_queue_retries(tmp_path, monkeypatch, timeout_type) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, _ = _worker(world, tmp_path)
        policy_value = json.loads(world.policy_path.read_text())
        policy_value.update(summary_bytes=4000, max_attempts=2, retry_initial_seconds=1, retry_max_seconds=1)
        policy_value["model"] = _openai_model()
        world.policy_path.write_text(json.dumps(policy_value))
        monkeypatch.setenv("BATON_SYNTHETIC_MODEL_KEY", "synthetic-key-marker")
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])

        request_sizes: list[int] = []

        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            context = json.loads(body["input"][3]["content"][0]["text"])
            size = len(context["events"])
            request_sizes.append(size)
            if size > 2:
                raise timeout_type("synthetic transport failure", request=request)
            assessment = {
                "decision": "wake",
                "reason": "Review these updates.",
                "summary": "The synthetic updates are ready.",
                "sources": [event["source_key"] for event in context["events"]],
            }
            response = {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(assessment)}]}],
            }
            return httpx.Response(200, json=response, request=request)

        real_client = httpx.AsyncClient

        def client_with_fault_boundary(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(
            "baton.agents.secretary.connections.transport.httpx.AsyncClient",
            client_with_fault_boundary,
        )

        asyncio.run(worker.instance.secretary.activate())
        for index in range(4):
            say(world, room, "worker", f"Large page event {index}")

        for index in range(4):
            asyncio.run(_step(worker))
            now[0] += 2
            say(world, room, "reviewer", f"Independent healthy page {index}")

        asyncio.run(_step(worker))
        failed_calls = [size for size in request_sizes if size > 2]
        healthy_calls = [size for size in request_sizes if size <= 2]
        assert len(failed_calls) >= 3
        assert len(healthy_calls) >= 2
        assert worker.model_health["last_failure"] == "unavailable"
        pending = worker.instance.receipts.get("pending") or []
        results = [asyncio.run(worker.instance.secretary.pending(decision)) for decision in pending]
        assert all(result["kind"] != "failure_notice" for result in results if result)
        # Every attempted large request still contains the original page, proving it was not split.
        assert set(failed_calls) == {4}


def test_command_failure_can_become_page_specific_after_another_page_answers(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, _ = _worker(world, tmp_path)
        command = tmp_path / "synthetic-model.py"
        command.write_text(
            "import json,sys\n"
            "request=json.load(sys.stdin)\n"
            "context=json.loads(request['input'][3]['content'][0]['text'])\n"
            "events=context['events']\n"
            "if len(events)>2: sys.exit(7)\n"
            "result={'decision':'wake','reason':'Review these updates.',"
            "'summary':'The synthetic updates are ready.',"
            "'sources':[event['source_key'] for event in events]}\n"
            "print(json.dumps(result))\n"
        )
        policy = json.loads(world.policy_path.read_text())
        policy.update(summary_bytes=4000, max_attempts=2, retry_initial_seconds=1, retry_max_seconds=1)
        policy["model"].update(provider="command", command=[sys.executable, "-I", "-S", str(command)])
        world.policy_path.write_text(json.dumps(policy))
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(worker.instance.secretary.activate())
        for index in range(4):
            say(world, room, "worker", f"Command-failing large page event {index}")

        asyncio.run(_step(worker))
        now[0] += 2
        for index in range(2):
            say(world, room, "reviewer", f"Healthy command page event {index}")
        asyncio.run(_step(worker))  # The new page answers; the retained large page fails again.
        now[0] += 2
        for index in range(2):
            say(world, room, "reviewer", f"Another healthy command page event {index}")
        asyncio.run(_step(worker))  # A second independent answer lets the failed page split.

        pages = _backlog(world).due(now[0], limit=10)
        assert len(pages) == 2


def test_secretary_api_records_key_removed_after_activation(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, _ = _worker(world, tmp_path)
        policy_value = json.loads(world.policy_path.read_text())
        policy_value["model"] = _openai_model()
        world.policy_path.write_text(json.dumps(policy_value))
        monkeypatch.setenv("BATON_SYNTHETIC_MODEL_KEY", "synthetic-key-marker")
        _, head = asyncio.run(worker.instance.secretary.activate())
        sent = say(world, room, "worker", "The key disappears after activation")
        monkeypatch.delenv("BATON_SYNTHETIC_MODEL_KEY")

        with pytest.raises(SecretaryUnavailable) as error:
            asyncio.run(
                worker.instance.secretary.decide(
                    budget_seconds=5,
                    since=head,
                    floor=head,
                    trigger=f"message:{sent['seqs']['planner']}",
                )
            )

        policy = load_policy(world.config, world.policy_path)
        log = AttentionLog(world.state_path, world.config.data_dir, "planner")
        health = ModelHealth(log, policy.digest).read()
        assert error.value.category == "missing_credential"
        assert health["status"] == "unavailable"
        assert health["last_failure"] == "missing_credential"
