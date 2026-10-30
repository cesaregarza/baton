"""A webhook wake's URL may carry a token, so no log record under `baton serve`'s own logging may contain it."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from conftest import free_port, make_config

from baton import cli, messages, wakes
from baton.store import connect

SENTINEL = "sentinelwaketoken7f3a"


class Capture(logging.Handler):
    """Every record that reaches the root logger, formatted with any traceback, as the journal would get it."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@contextmanager
def bare_logging() -> Iterator[None]:
    """Run with no root handlers, as a fresh `baton serve` process starts, and put pytest's back afterwards."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    loggers = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    root.handlers = []
    try:
        yield
    finally:
        root.handlers, root.level = handlers, level
        for name, saved in loggers.items():
            logging.getLogger(name).setLevel(saved)


def _receiver(status: int) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("outcome", ["delivered", "refused", "unreachable"])
def test_a_webhook_wake_url_reaches_no_log_record(tmp_path: Path, monkeypatch, outcome: str) -> None:
    server = _receiver(204 if outcome == "delivered" else 500) if outcome != "unreachable" else None
    port = server.server_port if server else free_port()
    url = f"http://127.0.0.1:{port}/hooks/{SENTINEL}?token={SENTINEL}"
    make_config(tmp_path, extra=f'[roles.worker.wake]\nwebhook = "{url}"\n')
    capture = Capture()
    tried: list[int] = []

    def one_wake(config) -> None:
        logging.getLogger().addHandler(capture)  # after the server's own setup, so it sees what the journal would
        conn = connect(config.db_path)
        messages.send(conn, sender="planner", recipient="worker", text="hi")
        conn.close()
        tried.append(wakes.Dispatcher(config).pass_once())

    monkeypatch.setattr("baton.app.serve", one_wake)
    try:
        with bare_logging():
            assert cli.main(["--config", str(tmp_path / "baton.toml"), "serve"]) == 0
            level = logging.getLogger().getEffectiveLevel()
    finally:
        if server:
            server.shutdown()
    assert tried == [1]
    assert level == logging.INFO  # the server's real level was in force
    assert [line for line in capture.lines if SENTINEL in line] == []
    if outcome != "delivered":
        assert any(line.startswith("WARNING baton.wakes") for line in capture.lines)  # failures are still logged
