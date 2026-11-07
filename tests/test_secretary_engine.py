"""Contract tests for the secretary's independent probability engine."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from baton.agents.secretary.connections.decisions import (
    AttentionDecision,
    AttentionVerdict,
    EngineUnavailable,
    JevDecisionEngine,
)
from baton.agents.secretary.harness.tools import SecretaryTools


@pytest.mark.parametrize(
    ("wake_probability", "confidence"),
    [(0.85, 0.53), (0.79, 0.99)],
)
def test_jev_wake_probability_comes_from_validated_distribution(wake_probability, confidence, monkeypatch) -> None:
    wire_requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            wire_requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            probabilities = {"wake": wake_probability, "hold": 1 - wake_probability, "keep": 0}
            body = json.dumps(
                {
                    "model": "fake-jev",
                    "answers": {
                        "attention": {
                            "type": "choice",
                            "choice": "wake",
                            "probabilities": probabilities,
                            "confidence": confidence,
                        }
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    monkeypatch.setenv("SECRETARY_JEV_ENGINE_TEST_KEY", "synthetic-secret")
    engine = JevDecisionEngine(
        {
            "model": "configured-nondefault-decision-model",
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "api_key_env": "SECRETARY_JEV_ENGINE_TEST_KEY",
        }
    )
    try:
        verdict = asyncio.run(engine.decide({"events": []}, 2))
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert verdict == AttentionVerdict(AttentionDecision.WAKE, wake_probability)
    assert wire_requests[0]["model"] == "configured-nondefault-decision-model"


def test_engine_failure_is_logged_with_safe_category_and_remains_inspectable(caplog) -> None:
    class BrokenEngine:
        async def decide(self, context: dict, timeout: float) -> AttentionVerdict:
            raise EngineUnavailable(
                "transport", "request failed for https://user:synthetic-sensitive-marker-7c29@example.test/key=secret"
            )

    tools = SecretaryTools({}, engine=BrokenEngine())
    with caplog.at_level(logging.WARNING, logger="baton.agents.secretary.harness.tools"):
        assert asyncio.run(tools.evaluate({"events": []}, 1)) is None

    assert tools.health == {"status": "unavailable", "last_failure": "transport"}
    assert "transport" in caplog.text
    assert "synthetic-sensitive-marker-7c29" not in caplog.text
    assert "example.test" not in caplog.text
