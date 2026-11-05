"""Test-host startup failures and cleanup must finish instead of hanging pytest."""

from __future__ import annotations

import threading

import httpx
import pytest
import uvicorn
from server_support import running_server


def make_server(*, fail_startup: bool = False):
    observed = {"worker": None, "shutdown": False}

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            observed["worker"] = threading.current_thread()
            assert (await receive())["type"] == "lifespan.startup"
            if fail_startup:
                await send({"type": "lifespan.startup.failed", "message": "synthetic startup failure"})
                return
            await send({"type": "lifespan.startup.complete"})
            assert (await receive())["type"] == "lifespan.shutdown"
            observed["shutdown"] = True
            await send({"type": "lifespan.shutdown.complete"})
        else:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ready"})

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="on", log_level="warning"))
    return server, observed


def test_a_running_server_answers_http_and_finishes_shutdown():
    server, observed = make_server()
    with running_server(server) as ready:
        assert ready is server and server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        response = httpx.get(f"http://127.0.0.1:{port}", timeout=1)
        assert response.status_code == 200 and response.text == "ready"
    assert observed["shutdown"]
    assert not observed["worker"].is_alive()
    assert not server.servers[0].is_serving()


def test_a_test_body_failure_still_finishes_server_shutdown():
    server, observed = make_server()
    with pytest.raises(ValueError, match="synthetic test failure"):
        with running_server(server):
            raise ValueError("synthetic test failure")
    assert observed["shutdown"]
    assert not observed["worker"].is_alive()


def test_a_real_uvicorn_startup_failure_reaches_the_test(capsys):
    server, observed = make_server(fail_startup=True)
    with pytest.raises(RuntimeError, match="failed during startup") as raised:
        with running_server(server):
            pytest.fail("startup failed, so the server must not be yielded")
    assert isinstance(raised.value.__cause__, SystemExit)
    assert "synthetic startup failure" in capsys.readouterr().err
    assert not server.started and server.should_exit
    assert not observed["worker"].is_alive()


@pytest.mark.parametrize("failure", [None, ValueError("synthetic worker failure")])
def test_a_worker_exiting_before_readiness_does_not_leave_a_waiter(monkeypatch, failure):
    server, _ = make_server()

    def run():
        if failure is not None:
            raise failure

    monkeypatch.setattr(server, "run", run)
    with pytest.raises(RuntimeError, match="failed during startup|exited before becoming ready") as raised:
        with running_server(server):
            pytest.fail("an exited worker must not be yielded")
    assert raised.value.__cause__ is failure
    assert server.should_exit


@pytest.mark.parametrize(
    ("started", "body_failure"),
    [(False, False), (True, False), (True, True)],
    ids=["startup-stalls", "shutdown-stalls", "test-fails-and-shutdown-stalls"],
)
def test_a_worker_ignoring_shutdown_is_reported_within_a_bound(monkeypatch, started, body_failure):
    server, _ = make_server()
    server.started = started
    release = threading.Event()
    workers = []
    failures = []

    def stalled_worker():
        workers.append(threading.current_thread())
        release.wait()

    def use_server():
        try:
            with running_server(server, startup_timeout=0.05, shutdown_timeout=0.05):
                if body_failure:
                    raise ValueError("synthetic test failure")
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(server, "run", stalled_worker)
    # Bound the test too: a broken readiness loop must not hang the test for bounded waits.
    caller = threading.Thread(target=use_server, daemon=True)
    caller.start()
    try:
        caller.join(timeout=2)
        assert not caller.is_alive(), "the lifecycle helper did not return within the test's deadline"
        [failure] = failures
        assert server.should_exit
        if body_failure:
            assert isinstance(failure, ValueError)
            assert str(failure) == "synthetic test failure"
            assert any("did not stop" in note for note in failure.__notes__)
        elif started:
            assert isinstance(failure, RuntimeError)
            assert "did not stop" in str(failure)
        else:
            assert isinstance(failure, TimeoutError)
            assert "did not start" in str(failure)
            assert any("did not stop" in note for note in failure.__notes__)
        assert workers[0].is_alive(), "the helper must report, not claim to kill, an uncooperative thread"
    finally:
        release.set()
        caller.join(timeout=1)
        for worker in workers:
            worker.join(timeout=1)
    assert not workers[0].is_alive()
