"""Model outages stay observable; other successful pages distinguish oversized work from an outage."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from secretary_support import archived_summary_count, conversation, make_world, say
from test_secretary_observability import _run_until_engine_health
from test_secretary_recovery import _backlog, _step, _worker

from baton.agents.secretary.attention.health import ModelHealth
from baton.config.secretary import load_policy


@contextmanager
def _model_api(world, behavior: dict, requests: list):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            context = json.loads(body["input"][3]["content"][0]["text"])
            requests.append(context)
            failure = behavior.get("failure")
            if len(context["events"]) <= behavior.get("max_events", -1):
                failure = None
            if failure == "timeout":
                time.sleep(2)
                failure = None
            assessment = {
                "decision": "wake",
                "reason": "Review these updates.",
                "summary": "The synthetic updates are ready.",
                "sources": [event["source_key"] for event in context["events"]],
            }
            response = (
                {"error": "sensitive-provider-response-marker"}
                if failure
                else {
                    "status": "completed",
                    "output": [
                        {"type": "message", "content": [{"type": "output_text", "text": json.dumps(assessment)}]}
                    ],
                }
            )
            encoded = json.dumps(response).encode()
            self.send_response(failure or 200)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The timeout case intentionally closes the client before this response.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    policy = json.loads(world.policy_path.read_text())
    policy["model"] = {
        "provider": "openai",
        "model": "synthetic-model",
        "base_url": f"http://127.0.0.1:{server.server_port}",
        "api_key_env": "BATON_SYNTHETIC_MODEL_KEY",
        "reasoning_effort": "minimal",
        "max_output_tokens": 4096,
        "timeout_seconds": 0.5,
        "cache_mode": "implicit",
    }
    policy.update(summary_bytes=4000, retry_initial_seconds=1, retry_max_seconds=1, max_attempts=2)
    world.policy_path.write_text(json.dumps(policy))
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    ("status", "category"),
    [(401, "authentication"), (400, "rejected"), (408, "timeout"), (504, "timeout"), (503, "unavailable")],
)
def test_rejected_or_unavailable_model_is_reported_then_recovers(tmp_path, monkeypatch, caplog, status, category):
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, _ = _worker(world, tmp_path)
        monkeypatch.setenv("BATON_SYNTHETIC_MODEL_KEY", "synthetic-key-marker")
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        behavior, requests = {"failure": status}, []
        with _model_api(world, behavior, requests):
            asyncio.run(worker.instance.secretary.activate())
            say(world, room, "worker", "A synthetic incoming update")
            failed = asyncio.run(_run_until_engine_health(worker))
            assert failed["status"] == "degraded"
            assert failed["model"]["status"] == "unavailable"
            assert failed["model"]["last_failure"] == category
            assert failed["failed_pages"] == 1 and archived_summary_count(world.baton) == 0
            assert f"secretary model unavailable: {category}" in caplog.text
            assert "sensitive-provider-response-marker" not in caplog.text
            assert "synthetic-key-marker" not in caplog.text
            behavior["failure"] = None
            now[0] += 2
            worker.instance.receipts.set("health", None)
            recovered = asyncio.run(_run_until_engine_health(worker))
            assert recovered["status"] == "running"
            assert recovered["model"]["status"] == "available"
            assert recovered["model"]["last_failure"] == category
            assert archived_summary_count(world.baton) == 1 and len(requests) == 2


def test_missing_model_key_is_visible_at_worker_start_without_an_alert(tmp_path, monkeypatch, caplog):
    with make_world(tmp_path) as world:
        worker, _ = _worker(world, tmp_path)
        monkeypatch.delenv("BATON_SYNTHETIC_MODEL_KEY", raising=False)
        requests = []
        with _model_api(world, {}, requests):
            health = asyncio.run(_run_until_engine_health(worker))
        assert health["status"] == "degraded"
        assert health["model"]["last_failure"] == "missing_credential"
        assert "secretary model unavailable: missing_credential" in caplog.text
        assert requests == []


@pytest.mark.parametrize("failure", [400, "timeout"])
def test_other_successful_pages_allow_large_failed_page_to_split(tmp_path, monkeypatch, failure):
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, _ = _worker(world, tmp_path)
        monkeypatch.setenv("BATON_SYNTHETIC_MODEL_KEY", "synthetic-key-marker")
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        requests = []
        with _model_api(world, {"failure": failure, "max_events": 2}, requests):
            asyncio.run(worker.instance.secretary.activate())
            originals = [say(world, room, "worker", f"Large page event {i}") for i in range(4)]
            for _ in range(4):
                asyncio.run(_step(worker))
                now[0] += 900
            backlog = _backlog(world)
            [failed] = backlog.due(now[0], limit=10)
            assert failed.attempts == 4 and failed.content_failures == 0
            assert archived_summary_count(world.baton) == 0
            for index in range(2):
                say(world, room, "reviewer", f"A small healthy page {index}")
                asyncio.run(_step(worker))
                now[0] += 2
            pages = backlog.due(now[0], limit=10)
            assert len(pages) == 2  # Independent successes distinguished this page from a provider outage.
            for _ in pages:
                asyncio.run(_step(worker))
            pending = worker.instance.receipts.get("pending")
            summaries = [asyncio.run(worker.instance.secretary.pending(decision)) for decision in pending]
            assert all(result and result["kind"] == "summary" for result in summaries)
            cited = {source for result in summaries for source in result["sources"]}
            assert {f"message:{message['message_id']}" for message in originals} <= cited
            assert (
                ModelHealth(backlog.log, load_policy(world.config, world.policy_path).digest).read()["status"]
                == "available"
            )
