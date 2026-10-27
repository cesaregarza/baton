from __future__ import annotations

import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from conftest import make_config

from baton import jobs, messages, wakes
from baton.config import Wake
from baton.store import connect


class Recorder:
    """A stand-in deliverer: records each wake and answers with the next scripted outcome."""

    def __init__(self, *outcomes: bool) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def __call__(self, wake: Wake, wake_id: str, body: bytes) -> bool:
        self.calls.append({"id": wake_id, **json.loads(body)})
        return self.outcomes.pop(0) if self.outcomes else True


WORKER_WAKE = '[roles.worker.wake]\ncommand = ["true"]\n'


def test_messages_wake_once_per_unread_batch(tmp_path: Path) -> None:
    host = make_config(tmp_path, extra=WORKER_WAKE)
    recorder = Recorder()
    dispatcher = wakes.Dispatcher(host.config, recorder)
    conn = connect(host.config.db_path)
    for text in ("a", "b", "c"):
        messages.send(conn, sender="planner", recipient="worker", text=text)
    messages.send(conn, sender="worker", recipient="planner", text="planner has no hook")
    dispatcher.pass_once()
    assert [(c["role"], c["reason"], c["first_unread_seq"]) for c in recorder.calls] == [("worker", "message", 1)]
    assert set(recorder.calls[0]) == {"id", "type", "role", "host", "reason", "first_unread_seq"}  # no text

    messages.send(conn, sender="planner", recipient="worker", text="d")
    dispatcher.pass_once()
    assert len(recorder.calls) == 1  # still the same unread batch
    messages.read(conn, "worker")
    messages.send(conn, sender="planner", recipient="worker", text="e")
    dispatcher.pass_once()
    assert [c["first_unread_seq"] for c in recorder.calls] == [1, 5]


def test_a_read_supersedes_a_pending_wake_and_failures_retry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 0.0)
    host = make_config(tmp_path, extra=WORKER_WAKE)
    recorder = Recorder(False, False)
    dispatcher = wakes.Dispatcher(host.config, recorder)
    conn = connect(host.config.db_path)
    job = jobs.create(conn, host.config.jobs, request_id="j", actor="planner", title="t")
    messages.send(conn, sender="planner", recipient="worker", text="hello")

    dispatcher.pass_once()  # both fail
    messages.read(conn, "worker")
    dispatcher.pass_once()  # the job wake is retried; the message wake was superseded by the read
    assert [(c["reason"], c.get("job_id")) for c in recorder.calls] == [
        ("job.created", job["job_id"]), ("message", None), ("job.created", job["job_id"])]
    statuses = {row["key"].split(":")[0]: row["status"] for row in wakes.recent(conn)}
    assert statuses == {"job": "delivered", "messages": "superseded"}


def test_a_wake_gives_up_after_its_attempts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(wakes, "LEASE_SECONDS", 0.0)
    monkeypatch.setattr(wakes, "MAX_ATTEMPTS", 2)
    host = make_config(tmp_path, extra=WORKER_WAKE)
    dispatcher = wakes.Dispatcher(host.config, Recorder(False, False, False))
    conn = connect(host.config.db_path)
    jobs.create(conn, host.config.jobs, request_id="j", actor="planner", title="t")
    for _ in range(3):
        dispatcher.pass_once()
    assert [row["status"] for row in wakes.recent(conn)] == ["failed"]


def test_a_command_hook_gets_the_wake_on_stdin(tmp_path: Path) -> None:
    out = tmp_path / "wake.json"
    script = f"import sys; open({str(out)!r}, 'wb').write(sys.stdin.buffer.read())"
    command = json.dumps([sys.executable, "-c", script])
    host = make_config(tmp_path, extra=f"[roles.worker.wake]\ncommand = {command}\n")
    conn = connect(host.config.db_path)
    messages.send(conn, sender="planner", recipient="worker", text="secret-free wake")
    assert wakes.Dispatcher(host.config).pass_once() == 1
    assert json.loads(out.read_text()) == {"type": "baton.wake", "role": "worker", "host": host.config.public_url,
                                           "reason": "message", "first_unread_seq": 1}


def test_a_webhook_is_signed_with_standard_webhooks(tmp_path: Path, monkeypatch) -> None:
    received: list[tuple[dict, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            received.append((dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    secret = "whsec_" + base64.b64encode(b"example signing key for tests").decode()
    monkeypatch.setenv("EXAMPLE_WEBHOOK_SECRET", secret)
    host = make_config(tmp_path, extra=f'[roles.worker.wake]\nwebhook = "http://127.0.0.1:{server.server_port}/w"\n'
                                       'secret_env = "EXAMPLE_WEBHOOK_SECRET"\n')
    conn = connect(host.config.db_path)
    messages.send(conn, sender="planner", recipient="worker", text="hi")
    try:
        wakes.Dispatcher(host.config).pass_once()
    finally:
        server.shutdown()
    [(headers, body)] = received
    expected = wakes.sign(secret, headers["webhook-id"],
                          int(headers["webhook-timestamp"]), body)
    assert headers["webhook-signature"] == expected
    assert json.loads(body)["reason"] == "message"


def test_a_webhook_with_a_missing_secret_is_not_sent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("EXAMPLE_UNSET_SECRET", raising=False)
    wake = Wake(webhook="http://127.0.0.1:9/never", secret_env="EXAMPLE_UNSET_SECRET")
    assert wakes.deliver(wake, "wake_x", b"{}") is False
