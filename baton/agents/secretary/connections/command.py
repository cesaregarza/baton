"""Async process I/O for host-run models and decision engines."""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass

from baton.config.secretary import MAX_COMMAND_RESPONSE_BYTES

_CHUNK_SIZE = 64 * 1024


class OutputLimitExceeded(ValueError):
    """The command answered, but its output exceeded the configured bound."""


@dataclass(frozen=True)
class Command:
    """Run argv with bounded stdout and a reduced environment, not a sandbox.

    The child retains the caller's filesystem and operating-system authority.
    """

    argv: list[str]
    max_output: int = MAX_COMMAND_RESPONSE_BYTES
    env: dict[str, str] | None = None

    async def run(self, payload: bytes, timeout: float) -> tuple[int, bytes]:
        """Run argv without a shell, concurrently streaming stdin and bounded stdout."""
        process: asyncio.subprocess.Process | None = None
        tasks: list[asyncio.Task[object]] = []
        try:
            async with asyncio.timeout(timeout):
                process = await _spawn(self.argv, _command_environment(self.env or {}))
                assert process.stdin is not None and process.stdout is not None
                input_task = asyncio.create_task(_write_input(process.stdin, payload))
                output_task = asyncio.create_task(_read_output(process.stdout, self.max_output))
                wait_task = asyncio.create_task(process.wait())
                tasks = [input_task, output_task, wait_task]
                _, output, returncode = await asyncio.gather(input_task, output_task, wait_task)
                return returncode, output
        except BaseException:
            if process is not None:
                await _cleanup_safely(process, tasks)
            raise


def _command_environment(allowed: dict[str, str]) -> dict[str, str]:
    """Build a small child environment without inheriting host credentials."""
    environment = {"PATH": os.environ.get("PATH", os.defpath)}
    for name in ("HOME", "LANG", "LC_ALL", "LC_CTYPE"):
        if name in os.environ:
            environment[name] = os.environ[name]
    environment.update(allowed)
    return environment


async def _spawn(argv: list[str], environment: dict[str, str]) -> asyncio.subprocess.Process:
    """Shield creation so cancellation cannot orphan a child before it is returned."""
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=(os.name == "posix"),
            env=environment,
        )
    )
    try:
        return await asyncio.shield(spawn)
    except asyncio.CancelledError as cancellation:
        try:
            process = await _settle(spawn)
        except BaseException:
            raise cancellation from None
        await _cleanup_safely(process, [])
        raise cancellation from None


async def _settle(task: asyncio.Task[asyncio.subprocess.Process]) -> asyncio.subprocess.Process:
    """Wait for an owned task despite repeated cancellation of its caller."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _cleanup_safely(process: asyncio.subprocess.Process, tasks: list[asyncio.Task[object]]) -> None:
    """Finish cleanup despite repeated cancellation, then let the caller propagate cancellation."""
    cleanup = asyncio.create_task(_cleanup(process, tasks))
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
    cleanup.result()


async def _write_input(writer: asyncio.StreamWriter, payload: bytes) -> None:
    """Write in bounded chunks so large payloads do not fill the transport buffer."""
    try:
        for offset in range(0, len(payload), _CHUNK_SIZE):
            writer.write(payload[offset : offset + _CHUNK_SIZE])
            await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        # The command has stopped reading. Its eventual status determines success.
        pass
    finally:
        writer.close()


async def _read_output(reader: asyncio.StreamReader, max_output: int) -> bytes:
    """Read at most max_output + 1 bytes, using the extra byte to detect overflow."""
    try:
        output = await reader.readexactly(max_output + 1)
    except asyncio.IncompleteReadError as exc:
        output = exc.partial
    if len(output) > max_output:
        raise OutputLimitExceeded("command output exceeds its bound")
    return output


async def _cleanup(process: asyncio.subprocess.Process, tasks: list[asyncio.Task[object]]) -> None:
    """Kill and reap the owned process group, then stop all pipe readers and writers."""
    wait_task = asyncio.create_task(process.wait())
    await asyncio.sleep(0)  # register its waiter before the transport can finish
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.returncode is None:
        process.kill()

    transport = getattr(process, "_transport", None)
    if transport is not None:
        transport.close()

    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    # Closing stdout first normally wakes the transport wait. On some asyncio loops,
    # wait() can remain pending after all pipes close, even though the child watcher
    # has already reaped the process and set returncode. Bound that wait so cleanup
    # cannot hang on inherited pipes.
    try:
        await asyncio.wait_for(asyncio.shield(wait_task), timeout=1)
    except TimeoutError:
        if process.returncode is None:
            raise
    finally:
        if not wait_task.done():
            wait_task.cancel()
        await asyncio.gather(wait_task, return_exceptions=True)
