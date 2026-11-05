"""Check the installed process keeps HTTP client request details off stderr."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from secretary_support import explicit_policy, make_world

from baton import messages

ROOT = Path(__file__).resolve().parents[1]


def test_http_client_request_url_does_not_reach_process_stderr(tmp_path) -> None:
    received = threading.Event()
    marker = "secretary-log-marker"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            body = {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "decision": "wake",
                                        "reason": "A synthetic message arrived",
                                        "summary": "Review the synthetic update.",
                                        "sources": [f"message:{message_id}"],
                                    }
                                ),
                            }
                        ],
                    }
                ],
            }
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            received.set()

        def log_message(self, *_args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        with make_world(tmp_path) as world:
            policy = explicit_policy("planner")
            policy["summary_bytes"] = 4000
            policy["model"] = {
                "provider": "openai",
                "model": "gpt-6-luna",
                "base_url": f"http://127.0.0.1:{server.server_port}/{marker}/v1",
                "api_key_env": "SECRETARY_LOG_TEST_KEY",
                "timeout_seconds": 5,
                "max_output_tokens": 4096,
                "reasoning_effort": "none",
                "cache_mode": "implicit",
            }
            world.policy_path.write_text(json.dumps(policy))
            project = tmp_path / "project"
            project.mkdir()
            destination = tmp_path / "instance"
            env = {**os.environ, "PYTHONPATH": str(ROOT), "SECRETARY_LOG_TEST_KEY": "synthetic-key"}
            install = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "baton.integrations.secretary",
                    "install",
                    "--product",
                    "claude-code",
                    "--destination",
                    str(destination),
                    "--project",
                    str(project),
                    "--config",
                    str(world.config_path),
                    "--policy",
                    str(world.policy_path),
                    "--poll-seconds",
                    "0.05",
                    "--budget-seconds",
                    "5",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=15,
            )
            assert install.returncode == 0, install.stderr
            instance = json.loads(install.stdout)["instance"]
            sent = messages.send(world.baton, sender="worker", recipient="planner", text="Synthetic local HTTP test")
            message_id = sent["message_id"]

            process = subprocess.Popen(
                [sys.executable, "-m", "baton.integrations.secretary", "--instance", instance, "run"],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                assert received.wait(10), "the local fake HTTP endpoint did not receive a request"
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=10)

            assert process.returncode == 0, stderr
            assert marker not in stderr
            assert "WARNING httpx" not in stderr and "WARNING httpcore" not in stderr
            assert stdout == ""
    finally:
        server.shutdown()
        server_thread.join(timeout=5)
        server.server_close()
