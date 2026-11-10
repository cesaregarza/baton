"""Native producer clients: launch an installed, authenticated Codex or Claude Code once per reserved call.

Native means the operator's own client and subscription authentication, not an API key substituted on the
side. Isolation is preventative first: a fixed argv, an explicit model, an isolated temporary working
directory, no shell, read-only sandboxing and never-approval, every documented retrieval or side-effect
capability family disabled, an allowlisted environment and bounded stdin/stdout/stderr/wall time. Event
checking then supplements that: any action-bearing or unknown event invalidates the attempt. Nothing launches
before a passing verification of the exact executable about to run: it must report the operator's configured
`client_version` and document the flags and features of the inspected profile below. A replaced executable
is verified again, and nothing here runs a model during verification.

A finished process is classified from typed fields only, whatever its exit status: an attempted action first,
then an error the provider refused before doing any work (a refusal), then any other reported error.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from baton.graph.failure import REFUSALS, Category, TransportError, status_category
from baton.graph.source import canonical
from baton.graph.transport import Limits

# Capability families disabled by `-c features.NAME=false`; each must appear in `codex features list`.
CODEX_FEATURES = (
    "shell_tool",
    "apps",
    "plugins",
    "hooks",
    "multi_agent",
    "memories",
    "view_image",
    "image_generation",
    "browser_use",
    "computer_use",
    "request_permissions_tool",
)
CODEX_OVERRIDES = ('approval_policy="never"', 'web_search="disabled"')
CODEX_EXEC_FLAGS = (
    "--ignore-user-config",
    "--ephemeral",
    "--sandbox",
    "--skip-git-repo-check",
    "--model",
    "--json",
    "--output-schema",
    "--output-last-message",
    "--cd",
    "--config",
)
CLAUDE_FLAGS = (
    "--print",
    "--output-format",
    "--json-schema",
    "--model",
    "--tools",
    "--strict-mcp-config",
    "--mcp-config",
    "--safe-mode",
    "--disable-slash-commands",
    "--no-session-persistence",
    "--system-prompt",
)
# Codex JSONL events the host accepts: thread/turn status, usage, failures, and assistant text, reasoning or
# error items only. Agent text and reasoning items are model output, so they show the provider did work.
CODEX_STATUS_EVENTS = frozenset({"thread.started", "turn.started", "turn.completed"})
CODEX_FAILURE_EVENTS = frozenset({"error", "turn.failed"})
CODEX_ITEM_EVENTS = frozenset({"item.started", "item.updated", "item.completed"})
CODEX_OUTPUT_ITEMS = frozenset({"agent_message", "reasoning"})
CODEX_ERROR_ITEM = "error"
# Result fields that would record an attempted tool or MCP action; any nonempty value rejects the attempt.
CLAUDE_ACTION_RECORDS = ("tool_uses", "tool_calls", "mcp_calls")
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM")
FORBIDDEN_ENV_PREFIXES = ("BATON_",)
MAX_STDERR_BYTES = 64 * 1024
VERIFY_TIMEOUT = 30
VERIFY_OUTPUT = 1_000_000
_CHUNK = 64 * 1024


def child_environment(credential_env: list[str]) -> dict[str, str]:
    """Only runtime basics and the configured provider-auth names; Baton's own variables never cross."""
    names = [*ENV_ALLOWLIST, *credential_env]
    if any(name.startswith(FORBIDDEN_ENV_PREFIXES) for name in names):
        raise TransportError(Category.ISOLATION, "a Baton variable was requested for a model process")
    return {name: os.environ[name] for name in names if name in os.environ}


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    elapsed: float
    timed_out: bool
    truncated: bool
    cancelled: bool = False


def _kill_group(process: subprocess.Popen) -> None:
    """Kill the child's whole process group, which is where its descendants live."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_process(
    argv: list[str],
    *,
    executable: str | None = None,
    stdin: bytes,
    env: dict[str, str],
    cwd: str,
    timeout: float,
    max_stdout: int,
    stop: threading.Event | None = None,
) -> ProcessResult:
    """Run argv in its own session with bounded pipes; over-bound output, the deadline or `stop` kills the group.

    Returns only after the group is killed and the client reaped, so no model process outlives the call. An
    `executable` runs that file in place of argv[0]'s lookup, with argv[0] passed unchanged.
    """
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            executable=executable,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as error:
        raise TransportError(Category.TRANSPORT, f"native client could not start ({type(error).__name__})") from None
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stdin_pipe = process.stdin
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    flags = {"truncated": False}

    def pump(stream: Any, name: str, bound: int) -> None:
        total = 0
        while True:
            data = stream.read(_CHUNK)
            if not data:
                break
            total += len(data)
            if total > bound:
                flags["truncated"] = True
                _kill_group(process)
                continue  # drain to EOF without keeping anything past the bound
            chunks[name].append(data)
        stream.close()

    def feed() -> None:
        try:
            for offset in range(0, len(stdin), _CHUNK):
                stdin_pipe.write(stdin[offset : offset + _CHUNK])
            stdin_pipe.close()
        except (BrokenPipeError, OSError):
            pass

    workers = [
        threading.Thread(target=pump, args=(process.stdout, "stdout", max_stdout), daemon=True),
        threading.Thread(target=pump, args=(process.stderr, "stderr", MAX_STDERR_BYTES), daemon=True),
        threading.Thread(target=feed, daemon=True),
    ]
    for worker in workers:
        worker.start()
    try:
        timed_out, cancelled = _wait(process, started + timeout, stop)
    finally:
        _kill_group(process)  # descendants in the group end with the client, whether it finished or not
        process.wait()
    for worker in workers:
        worker.join(timeout=5)
    return ProcessResult(
        process.returncode,
        b"".join(chunks["stdout"]),
        b"".join(chunks["stderr"]),
        time.monotonic() - started,
        timed_out,
        flags["truncated"],
        cancelled,
    )


def _wait(process: subprocess.Popen, deadline: float, stop: threading.Event | None) -> tuple[bool, bool]:
    """Wait for exit, the deadline or a stop request; returns (timed_out, cancelled)."""
    while process.poll() is None:
        if stop is not None and stop.is_set():
            return False, True
        if time.monotonic() >= deadline:
            return True, False
        time.sleep(0.05)
    return False, False


def resolve_executable(command: list[str], env: dict[str, str]) -> str:
    """The file `command` names: argv[0] looked up on the child's own PATH as exec does, symlinks resolved.

    Launches run this resolved file directly, so a symlink re-pointed after verification is never followed.
    """
    name = command[0]
    found = name if os.sep in name else shutil.which(name, path=env.get("PATH", os.defpath))
    if found is None:
        raise TransportError(Category.UNVERIFIED_CLIENT, "native client executable was not found")
    return os.path.realpath(found)


def file_identity(path: str) -> tuple[int, int, int, int]:
    """What changes when the file at `path` is replaced or rewritten."""
    try:
        stat = os.stat(path)
    except OSError:
        raise TransportError(Category.UNVERIFIED_CLIENT, "native client executable cannot be inspected") from None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _introspect(argv: list[str], executable: str, env: dict[str, str]) -> str:
    """Version, help or feature-list output: a client command that never runs a model."""
    try:
        with tempfile.TemporaryDirectory(prefix="baton-graph-verify-") as cwd:
            result = run_process(
                argv,
                executable=executable,
                stdin=b"",
                env=env,
                cwd=cwd,
                timeout=VERIFY_TIMEOUT,
                max_stdout=VERIFY_OUTPUT,
            )
    except TransportError:
        result = None
    if result is None or result.timed_out or result.returncode != 0:
        raise TransportError(Category.UNVERIFIED_CLIENT, f"native client introspection failed: {' '.join(argv[1:])}")
    return result.stdout.decode("utf-8", "replace")


def _require_flags(help_text: str, flags: tuple[str, ...], client: str) -> None:
    """Each flag must appear as a whole option name, so `--append-system-prompt` never stands in for another."""
    for flag in flags:
        if re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text) is None:
            raise TransportError(Category.UNVERIFIED_CLIENT, f"{client} does not document {flag}")


def _channels(request: dict[str, Any], limits: Limits) -> tuple[str, bytes]:
    """The contract instructions and the serialized content; the input bound covers both together."""
    instructions = request["instructions"]
    content = canonical({key: value for key, value in request.items() if key != "instructions"}).encode("utf-8")
    if len(instructions.encode("utf-8")) + len(content) > limits.max_input_bytes:
        raise TransportError(Category.INPUT_LIMIT, "serialized native prompt exceeds max_input_bytes (UTF-8)")
    return instructions, content


def _candidate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TransportError(Category.MALFORMED, "native client returned no JSON object")
    return value


def _check_process(result: ProcessResult) -> None:
    """Host-imposed endings first; the client's own exit status is judged with its structured output."""
    if result.cancelled:
        raise TransportError(Category.CANCELLED, "native client was cancelled by shutdown and its process group killed")
    if result.timed_out:
        raise TransportError(Category.TIMEOUT, "native client exceeded its wall-time limit and was killed")
    if result.truncated:
        raise TransportError(Category.OUTPUT_LIMIT, "native client exceeded its output byte limit and was killed")


def _unparsed(returncode: int | None, client: str) -> TransportError:
    if returncode != 0:
        return TransportError(Category.TRANSPORT, f"{client} exited with status {returncode} and no structured result")
    return TransportError(Category.MALFORMED, f"{client} emitted no structured result")


@dataclass
class NativeClient:
    """An installed client that launches only after its executable passed verification against the profile."""

    command: list[str]
    model: str
    credential_env: list[str]
    client_version: str
    verified: dict[str, Any] | None = None
    verified_file: tuple[str, tuple[int, int, int, int]] | None = None
    mode: str = "native"

    def verify(self) -> dict[str, Any]:
        """Check the executable a launch would run now; a failure leaves the client unverified."""
        self.verified = self.verified_file = None
        env = child_environment(self.credential_env)
        executable = resolve_executable(self.command, env)
        identity = file_identity(executable)
        profile = self.profile(executable, env)
        if file_identity(executable) != identity:
            raise TransportError(Category.UNVERIFIED_CLIENT, f"{self.mode} executable changed during verification")
        self.verified, self.verified_file = profile, (executable, identity)
        return profile

    def profile(self, executable: str, env: dict[str, str]) -> dict[str, Any]:
        """Client-specific introspection; raises `unverified_client` when the client is outside the profile."""
        raise NotImplementedError

    def introspect(self, executable: str, env: dict[str, str], *args: str) -> str:
        return _introspect([*self.command, *args], executable, env)

    def launch_executable(self) -> str:
        """The verified file to run, verifying first when the command no longer names the file last verified."""
        executable = resolve_executable(self.command, child_environment(self.credential_env))
        if self.verified_file != (executable, file_identity(executable)):
            self.verify()
        assert self.verified_file is not None  # set by the passing verification above
        return self.verified_file[0]

    def require_version(self, text: str, pattern: str) -> str:
        match = re.search(pattern, text)
        reported = match.group(1) if match else "unknown"
        if reported != self.client_version:
            raise TransportError(
                Category.UNVERIFIED_CLIENT,
                f"{self.mode} reports version {reported}, not the configured client_version {self.client_version}",
            )
        return reported


@dataclass
class CodexTransport(NativeClient):
    """`codex exec` with read-only sandbox, never-approval and every capability family disabled."""

    mode: str = "codex"

    def profile(self, executable: str, env: dict[str, str]) -> dict[str, Any]:
        version = self.require_version(self.introspect(executable, env, "--version"), r"codex-cli (\d+\.\d+\.\d+)")
        _require_flags(self.introspect(executable, env, "exec", "--help"), CODEX_EXEC_FLAGS, "codex exec")
        features = self.introspect(executable, env, "features", "list")
        listed = {line.split()[0] for line in features.splitlines() if line.strip()}
        unknown = [name for name in CODEX_FEATURES if name not in listed]
        if unknown:
            raise TransportError(
                Category.UNVERIFIED_CLIENT, f"codex does not recognize feature {unknown[0]}; isolation unverified"
            )
        return {"mode": self.mode, "version": version, "features_disabled": list(CODEX_FEATURES)}

    def argv(self, schema_path: str, output_path: str, cwd: str) -> list[str]:
        overrides = [*CODEX_OVERRIDES, *(f"features.{name}=false" for name in CODEX_FEATURES)]
        return [
            *self.command,
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--model",
            self.model,
            "--json",
            "--output-schema",
            schema_path,
            "--output-last-message",
            output_path,
            "-C",
            cwd,
            *(part for override in overrides for part in ("-c", override)),
            "-",
        ]

    def produce(
        self, request: dict[str, Any], schema: dict[str, Any], limits: Limits, *, stop: threading.Event | None = None
    ) -> dict[str, Any]:
        instructions, content = _channels(request, limits)
        prompt = instructions.encode("utf-8") + b"\n\n" + content  # `exec` has no separate instruction channel
        executable = self.launch_executable()
        with tempfile.TemporaryDirectory(prefix="baton-graph-codex-") as cwd:
            schema_path, output_path = os.path.join(cwd, "schema.json"), os.path.join(cwd, "last-message.json")
            Path(schema_path).write_text(json.dumps(schema), encoding="utf-8")
            result = run_process(
                self.argv(schema_path, output_path, cwd),
                executable=executable,
                stdin=prompt,
                env=child_environment(self.credential_env),
                cwd=cwd,
                timeout=limits.timeout,
                max_stdout=limits.max_response_bytes,
                stop=stop,
            )
            _check_process(result)
            usage, turns = check_codex_events(result.stdout, result.returncode)
            candidate = _last_message(output_path, limits.max_response_bytes)
        return {
            "candidate": candidate,
            "usage": usage,
            "input_bytes": len(prompt),
            "output_bytes": len(result.stdout),
            "elapsed": result.elapsed,
            "turns": turns,
        }


def _last_message(path: str, bound: int) -> dict[str, Any]:
    try:
        size = os.path.getsize(path)  # the file is not on stdout's bound, so bound it here
        text = Path(path).read_text(encoding="utf-8") if size <= bound else None
    except OSError:
        raise TransportError(Category.MALFORMED, "codex wrote no last message") from None
    if text is None:
        raise TransportError(Category.OUTPUT_LIMIT, "codex last message exceeds max_response_bytes")
    try:
        return _candidate(json.loads(text))
    except ValueError:
        raise TransportError(Category.MALFORMED, "codex wrote no JSON last message") from None


def check_codex_events(stdout: bytes, returncode: int | None) -> tuple[Any, int]:
    """Classify the whole event stream: an action or unknown event outranks any reported failure.

    A failure (an error event, a failed turn or an error item) is a refusal only when no model output item
    and no usage appear anywhere in the stream; otherwise the provider may have done work and it is charged.
    """
    events, unparsable = [], False
    for line in stdout.decode("utf-8", "replace").split("\n"):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            unparsable = True
    for event in events:
        if not _codex_allowed(event):
            kind = event.get("type") if isinstance(event, dict) else None
            raise TransportError(Category.ISOLATION, f"codex emitted a disallowed event: {kind or 'unknown'}")
    if unparsable or not events:
        raise _unparsed(returncode, "codex")
    if any(_codex_item_type(event) == CODEX_ERROR_ITEM or event["type"] in CODEX_FAILURE_EVENTS for event in events):
        worked = any(_codex_item_type(e) in CODEX_OUTPUT_ITEMS or isinstance(e.get("usage"), dict) for e in events)
        raise TransportError(
            Category.PROVIDER_ERROR if worked else Category.PROVIDER_REFUSED, "codex reported a failed turn"
        )
    if returncode != 0:
        raise TransportError(Category.TRANSPORT, f"codex exited with status {returncode}")
    completed = [event for event in events if event["type"] == "turn.completed"]
    usages = [event["usage"] for event in completed if isinstance(event.get("usage"), dict)]
    return (usages[-1] if usages else "unknown"), len(completed)


def _codex_item_type(event: dict[str, Any]) -> Any:
    item = event.get("item") if event.get("type") in CODEX_ITEM_EVENTS else None
    return item.get("type") if isinstance(item, dict) else None


def _codex_allowed(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    if event.get("type") in CODEX_STATUS_EVENTS | CODEX_FAILURE_EVENTS:
        return True
    return _codex_item_type(event) in CODEX_OUTPUT_ITEMS | {CODEX_ERROR_ITEM}


@dataclass
class ClaudeTransport(NativeClient):
    """`claude --print` with every built-in tool and MCP server removed, structured output, no persistence.

    The contract instructions go in `--system-prompt`; stdin carries only the serialized request content.
    """

    mode: str = "claude"

    def profile(self, executable: str, env: dict[str, str]) -> dict[str, Any]:
        version = self.require_version(self.introspect(executable, env, "--version"), r"(\d+\.\d+\.\d+)")
        _require_flags(self.introspect(executable, env, "--help"), CLAUDE_FLAGS, "claude")
        return {"mode": self.mode, "version": version, "flags": list(CLAUDE_FLAGS)}

    def argv(self, schema: dict[str, Any], empty_config: str, instructions: str) -> list[str]:
        return [
            *self.command,
            "--print",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--model",
            self.model,
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            empty_config,
            "--safe-mode",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt",
            instructions,
        ]

    def produce(
        self, request: dict[str, Any], schema: dict[str, Any], limits: Limits, *, stop: threading.Event | None = None
    ) -> dict[str, Any]:
        instructions, content = _channels(request, limits)
        executable = self.launch_executable()
        with tempfile.TemporaryDirectory(prefix="baton-graph-claude-") as cwd:
            empty_config = os.path.join(cwd, "mcp-empty.json")
            Path(empty_config).write_text('{"mcpServers": {}}', encoding="utf-8")
            result = run_process(
                self.argv(schema, empty_config, instructions),
                executable=executable,
                stdin=content,
                env=child_environment(self.credential_env),
                cwd=cwd,
                timeout=limits.timeout,
                max_stdout=limits.max_response_bytes,
                stop=stop,
            )
        _check_process(result)
        candidate, usage, turns = check_claude_result(result.stdout, result.returncode)
        return {
            "candidate": candidate,
            "usage": usage,
            "input_bytes": len(instructions.encode("utf-8")) + len(content),
            "output_bytes": len(result.stdout),
            "elapsed": result.elapsed,
            "turns": turns,
        }


def check_claude_result(stdout: bytes, returncode: int | None) -> tuple[dict[str, Any], Any, int]:
    """One successful structured result: a recorded action rejects it first, a turn count alone never does.

    Structured output may take the SDK more than one turn when it reprompts on a schema mismatch, so turns are
    reported, not judged; `--tools ""` and the empty MCP config are what keep actions out, and any denial or
    tool record the result does carry is an isolation failure, even on an error result.
    """
    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        result = None
    if not isinstance(result, dict) or result.get("type") != "result":
        raise _unparsed(returncode, "claude")
    if result.get("permission_denials") or any(result.get(key) for key in CLAUDE_ACTION_RECORDS):
        raise TransportError(Category.ISOLATION, "claude recorded an attempted action")
    if result.get("is_error") or result.get("subtype") != "success":
        category = Category.PROVIDER_REFUSED if claude_did_no_work(result) else Category.PROVIDER_ERROR
        raise TransportError(category, f"claude reported an error result ({result.get('subtype') or 'unknown'})")
    if returncode != 0:
        raise TransportError(Category.TRANSPORT, f"claude exited with status {returncode}")
    turns = result.get("num_turns", 1)
    if type(turns) is not int or turns < 1:
        raise TransportError(Category.MALFORMED, "claude reported an invalid turn count")
    usage = result["usage"] if isinstance(result.get("usage"), dict) else "unknown"
    return _candidate(result.get("structured_output")), usage, turns


def claude_did_no_work(result: dict[str, Any]) -> bool:
    """Typed evidence that an error result cost nothing: zero usage, and a refusal status or none at all.

    The status is absent when no request was sent (no login, for example). A status outside the refusal
    statuses, missing token counters, or any nonzero counter or cost means the call may have used the provider.
    """
    status = result.get("api_error_status")
    if status is not None and (type(status) is not int or status_category(status) not in REFUSALS):
        return False
    usage = result.get("usage")
    if not isinstance(usage, dict) or not all(type(usage.get(key)) is int for key in ("input_tokens", "output_tokens")):
        return False
    counters = [*_numbers(usage), *_numbers(result.get("modelUsage")), *_numbers(result.get("total_cost_usd"))]
    return all(value == 0 for value in counters)


def _numbers(value: Any) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int | float):
        return [value]
    if isinstance(value, dict):
        return [number for item in value.values() for number in _numbers(item)]
    if isinstance(value, list):
        return [number for item in value for number in _numbers(item)]
    return []
