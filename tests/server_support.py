"""Bounded startup and cleanup for Uvicorn servers used by tests."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn


@contextmanager
def running_server(
    server: uvicorn.Server, *, startup_timeout: float = 5.0, shutdown_timeout: float = 5.0
) -> Iterator[uvicorn.Server]:
    """Yield a ready server, then request shutdown and wait for its worker to finish."""
    failures: list[BaseException] = []

    def run() -> None:
        try:
            server.run()
        except BaseException as error:  # Uvicorn reports failed startup with SystemExit.
            failures.append(error)

    thread = threading.Thread(target=run, daemon=True, name="test-uvicorn")
    thread.start()
    error: BaseException | None = None
    try:
        deadline = time.monotonic() + startup_timeout
        while not server.started:
            if not thread.is_alive():
                if failures:
                    raise RuntimeError("Test server failed during startup") from failures[0]
                raise RuntimeError("Test server exited before becoming ready")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Test server did not start within {startup_timeout:g} seconds")
            thread.join(timeout=min(0.02, remaining))
        yield server
    except BaseException as caught:
        error = caught
        raise
    finally:
        server.should_exit = True
        thread.join(timeout=shutdown_timeout)
        if thread.is_alive():
            message = (
                f"Test server did not stop within {shutdown_timeout:g} seconds; its daemon thread is still running"
            )
            if error is not None:
                error.add_note(message)
            else:
                raise RuntimeError(message)
        elif failures and error is None:
            raise RuntimeError("Test server worker failed") from failures[0]
