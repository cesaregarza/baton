"""A webhook wake URL is checked when the config loads, and dispatcher errors never log the exception text.

A token placed in a URL's port field once reached the log through a traceback. The sentinel below stands in
for such a token. Synthetic only: no request leaves the machine.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import httpx
import pytest
from conftest import make_config

from baton import wakes
from baton.config import ConfigError, parse

SENTINEL = "SENTINEL-not-a-real-token-7f3a"


def config_with_webhook(url: str):
    return parse({"host": {"public_url": "http://127.0.0.1:8765"}, "roles": {"worker": {"wake": {"webhook": url}}}})


@pytest.mark.parametrize("url", [
    f"https://hooks.example.test:{SENTINEL}/wake",   # a port that doesn't parse, carrying the token
    f"https://hooks.example.test:99999/{SENTINEL}",  # a port out of range
    f"https://:443/{SENTINEL}",                       # no host
    f"https:///{SENTINEL}",
    f"ftp://hooks.example.test/{SENTINEL}",           # not http(s)
])
def test_a_malformed_webhook_url_is_refused_at_config_load_without_repeating_it(url: str) -> None:
    with pytest.raises(ConfigError) as refused:
        config_with_webhook(url)
    assert "roles.worker.wake.webhook" in str(refused.value)
    assert SENTINEL not in str(refused.value)


@pytest.mark.parametrize("url", ["https://hooks.example.test/wake", "http://127.0.0.1:8080/wake",
                                 "https://hooks.example.test:8443/wake?x=1"])
def test_a_well_formed_webhook_url_still_loads(url: str) -> None:
    assert config_with_webhook(url).roles["worker"].wake.webhook == url


def formatted(records: list[logging.LogRecord]) -> str:
    """Every record as a handler would write it, traceback included."""
    formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
    return "\n".join(formatter.format(record) for record in records)


def test_a_dispatcher_pass_error_logs_no_exception_text(tmp_path: Path, caplog, monkeypatch) -> None:
    host = make_config(tmp_path)
    dispatcher = wakes.Dispatcher(host.config)
    stop = threading.Event()

    def failing_pass() -> int:
        stop.set()
        raise httpx.InvalidURL(f"Invalid port: '{SENTINEL}' in https://hooks.example.test:{SENTINEL}/wake")

    monkeypatch.setattr(dispatcher, "pass_once", failing_pass)
    with caplog.at_level(logging.DEBUG, logger="baton.wakes"):
        dispatcher.run(stop, poll_seconds=0)
    written = formatted(caplog.records)
    assert caplog.records and "InvalidURL" in written
    assert SENTINEL not in written


def test_an_integration_delivery_error_logs_no_exception_text(tmp_path: Path, caplog) -> None:
    host = make_config(tmp_path, extra='[roles.worker.wake]\nintegration = "example"\n'
                                       '[integrations.example]\nenabled = true\n')

    class Failing:
        def deliver(self, role: str, wake_id: str, body: bytes) -> bool:
            raise ValueError(f"callback https://hooks.example.test:{SENTINEL}/ refused")

    dispatcher = wakes.Dispatcher(host.config, integrations={"example": Failing()})
    with caplog.at_level(logging.DEBUG, logger="baton.wakes"):
        assert dispatcher._send("worker", "wake_x", b"{}") is False
    written = formatted(caplog.records)
    assert caplog.records and "ValueError" in written
    assert SENTINEL not in written
