"""The seat over a real socket: cookies round-trip, ambiguous framing is refused, and `seat logout-all` works."""

from __future__ import annotations

import http.client
import json
import socket
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest
from conftest import free_port
from seat_helpers import PRE_COOKIE, SEAT_HOST, SEAT_ORIGIN, SESSION_COOKIE, make_seat_host, seat_config_text

from baton import cli

try:
    from baton import seat
except ImportError:  # written before the module: each test then fails on its own instead of the whole run stopping
    seat = None  # type: ignore[assignment]


@pytest.fixture
def served(tmp_path: Path):
    port = free_port()
    host = make_seat_host(tmp_path)
    host.reload(host.path.read_text().replace('origin = "https://seat.example.test"',
                                              f'origin = "https://seat.example.test"\nlisten_port = {port}'))
    bound = seat.server(host.config)
    threading.Thread(target=bound.serve_forever, daemon=True).start()
    try:
        yield host, port
    finally:
        bound.shutdown()
        bound.server_close()


def _request(port: int, method: str, path: str, *, body: bytes = b"", headers: dict[str, str] | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body or None, headers={"Host": SEAT_HOST, **(headers or {})})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response, data


def _cookie(response, name: str) -> str:
    for header in response.headers.get_all("Set-Cookie") or []:
        if header.startswith(name + "="):
            return header.split(";", 1)[0].split("=", 1)[1]
    raise AssertionError(f"no {name} cookie")


def test_a_login_round_trips_over_http(served) -> None:
    host, port = served
    form, page = _request(port, "GET", "/seat/login")
    pre = _cookie(form, PRE_COOKIE)
    body = urlencode({"token": host.seat_token, "pre": pre}).encode()
    response, _ = _request(port, "POST", "/seat/login", body=body, headers={
        "Origin": SEAT_ORIGIN, "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": f"{PRE_COOKIE}={pre}"})
    assert response.status == 303 and response.headers["Location"] == "/seat/"
    session = _cookie(response, SESSION_COOKIE)
    home, page = _request(port, "GET", "/seat/", headers={"Cookie": f"{SESSION_COOKIE}={session}"})
    assert home.status == 200 and b"Your conversations" in page


def test_chunked_framing_is_refused_and_the_connection_closed(served) -> None:
    _, port = served
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        raw.sendall(f"POST /seat/login HTTP/1.1\r\nHost: {SEAT_HOST}\r\nOrigin: {SEAT_ORIGIN}\r\n"
                    "Content-Type: application/x-www-form-urlencoded\r\nTransfer-Encoding: chunked\r\n\r\n"
                    f"5\r\ntoken\r\n0\r\n\r\nGET /seat/login HTTP/1.1\r\nHost: {SEAT_HOST}\r\n\r\n".encode())
        received = b""
        while chunk := raw.recv(65536):
            received += chunk
    assert received.startswith(b"HTTP/1.0 400") or received.startswith(b"HTTP/1.1 400")
    assert received.count(b"HTTP/1.") == 1  # the smuggled second request was never answered


def test_logout_all_ends_every_session(tmp_path: Path, capsys) -> None:
    from seat_helpers import get, log_in

    host = make_seat_host(tmp_path)
    sessions = [log_in(host.config, host.seat_token)[1] for _ in range(2)]
    assert cli.main(["--config", str(host.path), "seat", "logout-all"]) == 0
    assert json.loads(capsys.readouterr().out) == {"sessions_ended": 2}
    assert all(get(host.config, "/seat/", cookies=cookies).status == 303 for cookies in sessions)


def test_the_seat_is_off_unless_enabled(tmp_path: Path) -> None:
    from baton import config as config_module
    from baton.auth import new_token

    text = seat_config_text(tmp_path, new_token()[1], new_token()[1]).replace("[seat]\nenabled = true",
                                                                              "[seat]\nenabled = false")
    path = tmp_path / "off.toml"
    path.write_text(text)
    assert seat.server(config_module.load(path)) is None
