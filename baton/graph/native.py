"""Native producer clients: launch an installed, authenticated Codex or Claude Code once per reserved call.

Native means the operator's own client and subscription authentication, not an API key substituted on the
side. Isolation is preventative first: a fixed argv, an explicit model, an isolated temporary working
directory, no shell, read-only sandboxing and never-approval, every documented retrieval or side-effect
capability family disabled, an allowlisted environment and bounded stdin/stdout/stderr/wall time. Event
checking then supplements that: any action-bearing or unknown event invalidates the attempt. A client whose
version, flags or feature list differ from the inspected profile is unavailable until reverified, never
silently accepted. Nothing here runs a model during verification.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from baton.graph.source import canonical
from baton.graph.transport import Limits, TransportError

# Inspected profiles. Codex `codex-cli 0.167.0` and Claude Code `2.1.296 (Claude Code)` on this host.
CODEX_SERIES = "0.167."
CLAUDE_VERSION = "2.1.296"
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
)
# Codex JSONL events the host accepts: thread/turn status, usage, and assistant text or reasoning items only.
CODEX_STATUS_EVENTS = frozenset({"thread.started", "turn.started", "turn.completed"})
CODEX_ITEM_EVENTS = frozenset({"item.started", "item.updated", "item.completed"})
CODEX_INERT_ITEMS = frozenset({"agent_message", "reasoning"})
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
        raise TransportError("isolation", "a Baton variable was requested for a model process")
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
    stdin: bytes,
    env: dict[str, str],
    cwd: str,
    timeout: float,
    max_stdout: int,
    stop: threading.Event | None = None,
) -> ProcessResult:
    """Run argv in its own session with bounded pipes; over-bound output, the deadline or `stop` kills the group.

    Returns only after the group is killed and the client reaped, so no model process outlives the call.
    """
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as error:
        raise TransportError("transport", f"native client could not start ({type(error).__name__})") from None
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


def _introspect(argv: list[str], credential_env: list[str]) -> str:
    """Version, help or feature-list output: a client command that never runs a model."""
    with tempfile.TemporaryDirectory(prefix="baton-graph-verify-") as cwd:
        result = run_process(
            argv,
            stdin=b"",
            env=child_environment(credential_env),
            cwd=cwd,
            timeout=VERIFY_TIMEOUT,
            max_stdout=VERIFY_OUTPUT,
        )
    if result.timed_out or result.returncode != 0:
        raise TransportError("isolation", f"native client introspection failed: {' '.join(argv[1:])}")
    return result.stdout.decode("utf-8", "replace")


def _prompt(request: dict[str, Any], limits: Limits) -> bytes:
    content = {key: value for key, value in request.items() if key != "instructions"}
    prompt = (request["instructions"] + "\n\n" + canonical(content)).encode("utf-8")
    if len(prompt) > limits.max_input_bytes:
        raise TransportError("input_limit", "serialized native prompt exceeds max_input_bytes (UTF-8)")
    return prompt


def _candidate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TransportError("malformed", "native client returned no JSON object")
    return value


@dataclass
class CodexTransport:
    """`codex exec` with read-only sandbox, never-approval and every capability family disabled."""

    command: list[str]
    model: str
    credential_env: list[str]
    mode: str = "codex"
    verified: dict[str, Any] | None = None

    def verify(self) -> dict[str, Any]:
        version = _introspect([*self.command, "--version"], self.credential_env)
        match = re.search(r"codex-cli (\d+\.\d+\.\d+)", version)
        if match is None or not match.group(1).startswith(CODEX_SERIES):
            raise TransportError("isolation", f"codex version is outside the verified {CODEX_SERIES}x profile")
        help_text = _introspect([*self.command, "exec", "--help"], self.credential_env)
        missing = [flag for flag in CODEX_EXEC_FLAGS if flag not in help_text]
        if missing:
            raise TransportError("isolation", f"codex exec does not document {missing[0]}")
        listed = {
            line.split()[0]
            for line in _introspect([*self.command, "features", "list"], self.credential_env).splitlines()
            if line.strip()
        }
        unknown = [name for name in CODEX_FEATURES if name not in listed]
        if unknown:
            raise TransportError("isolation", f"codex does not recognize feature {unknown[0]}; isolation unverified")
        self.verified = {"mode": self.mode, "version": match.group(1), "features_disabled": list(CODEX_FEATURES)}
        return self.verified

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
        if self.verified is None:
            self.verify()
        prompt = _prompt(request, limits)
        with tempfile.TemporaryDirectory(prefix="baton-graph-codex-") as cwd:
            schema_path, output_path = os.path.join(cwd, "schema.json"), os.path.join(cwd, "last-message.json")
            Path(schema_path).write_text(json.dumps(schema), encoding="utf-8")
            result = run_process(
                self.argv(schema_path, output_path, cwd),
                stdin=prompt,
                env=child_environment(self.credential_env),
                cwd=cwd,
                timeout=limits.timeout,
                max_stdout=limits.max_response_bytes,
                stop=stop,
            )
            _check_process(result)
            usage, turns = check_codex_events(result.stdout)
            try:
                size = os.path.getsize(output_path)  # the file is not on stdout's bound, so bound it here
                text = Path(output_path).read_text(encoding="utf-8") if size <= limits.max_response_bytes else None
            except OSError:
                raise TransportError("malformed", "codex wrote no last message") from None
            if text is None:
                raise TransportError("output_limit", "codex last message exceeds max_response_bytes")
            try:
                candidate = _candidate(json.loads(text))
            except ValueError:
                raise TransportError("malformed", "codex wrote no JSON last message") from None
        return {
            "candidate": candidate,
            "usage": usage,
            "input_bytes": len(prompt),
            "output_bytes": len(result.stdout),
            "elapsed": result.elapsed,
            "turns": turns,
        }


def _check_process(result: ProcessResult) -> None:
    if result.cancelled:
        raise TransportError("cancelled", "native client was cancelled by shutdown and its process group killed")
    if result.timed_out:
        raise TransportError("timeout", "native client exceeded its wall-time limit and was killed")
    if result.truncated:
        raise TransportError("output_limit", "native client exceeded its output byte limit and was killed")
    if result.returncode != 0:
        raise TransportError("transport", f"native client exited with status {result.returncode}")


def check_codex_events(stdout: bytes) -> tuple[Any, int]:
    """Accept only status, usage and assistant text/reasoning events; any action or unknown event rejects."""
    usage: Any = "unknown"
    turns = 0
    for line in stdout.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            raise TransportError("malformed", "codex emitted a non-JSON event line") from None
        kind = event.get("type") if isinstance(event, dict) else None
        if kind == "error":
            raise TransportError("rejected", "codex reported an error event")
        if kind in CODEX_STATUS_EVENTS:
            if kind == "turn.completed":
                turns += 1
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
            continue
        item = event.get("item") if kind in CODEX_ITEM_EVENTS and isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") in CODEX_INERT_ITEMS:
            continue
        raise TransportError("isolation", f"codex emitted a disallowed event: {kind or 'unknown'}")
    return usage, turns


@dataclass
class ClaudeTransport:
    """`claude --print` with every built-in tool and MCP server removed, structured output, no persistence."""

    command: list[str]
    model: str
    credential_env: list[str]
    mode: str = "claude"
    verified: dict[str, Any] | None = None

    def verify(self) -> dict[str, Any]:
        version = _introspect([*self.command, "--version"], self.credential_env)
        match = re.search(r"(\d+\.\d+\.\d+)", version)
        if match is None or match.group(1) != CLAUDE_VERSION:
            raise TransportError("isolation", f"claude version is outside the verified {CLAUDE_VERSION} profile")
        help_text = _introspect([*self.command, "--help"], self.credential_env)
        missing = [flag for flag in CLAUDE_FLAGS if flag not in help_text]
        if missing:
            raise TransportError("isolation", f"claude does not document {missing[0]}")
        self.verified = {"mode": self.mode, "version": match.group(1), "flags": list(CLAUDE_FLAGS)}
        return self.verified

    def argv(self, schema: dict[str, Any], empty_config: str) -> list[str]:
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
        ]

    def produce(
        self, request: dict[str, Any], schema: dict[str, Any], limits: Limits, *, stop: threading.Event | None = None
    ) -> dict[str, Any]:
        if self.verified is None:
            self.verify()
        prompt = _prompt(request, limits)
        with tempfile.TemporaryDirectory(prefix="baton-graph-claude-") as cwd:
            empty_config = os.path.join(cwd, "mcp-empty.json")
            Path(empty_config).write_text('{"mcpServers": {}}', encoding="utf-8")
            result = run_process(
                self.argv(schema, empty_config),
                stdin=prompt,
                env=child_environment(self.credential_env),
                cwd=cwd,
                timeout=limits.timeout,
                max_stdout=limits.max_response_bytes,
                stop=stop,
            )
        _check_process(result)
        candidate, usage, turns = check_claude_result(result.stdout)
        return {
            "candidate": candidate,
            "usage": usage,
            "input_bytes": len(prompt),
            "output_bytes": len(result.stdout),
            "elapsed": result.elapsed,
            "turns": turns,
        }


def check_claude_result(stdout: bytes) -> tuple[dict[str, Any], Any, int]:
    """One successful structured result: recorded action attempts reject it, a turn count alone never does.

    Structured output may take the SDK more than one turn when it reprompts on a schema mismatch, so turns are
    reported, not judged; `--tools ""` and the empty MCP config are what keep actions out, and any denial or
    tool record the result does carry is an isolation failure.
    """
    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise TransportError("malformed", "claude emitted no JSON result") from None
    if not isinstance(result, dict) or result.get("type") != "result":
        raise TransportError("malformed", "claude emitted an unexpected result shape")
    if result.get("is_error") or result.get("subtype") != "success":
        raise TransportError("rejected", f"claude result subtype {result.get('subtype') or 'unknown'}")
    if result.get("permission_denials") or any(result.get(key) for key in CLAUDE_ACTION_RECORDS):
        raise TransportError("isolation", "claude recorded an attempted action")
    turns = result.get("num_turns", 1)
    if type(turns) is not int or turns < 1:
        raise TransportError("malformed", "claude reported an invalid turn count")
    usage = result["usage"] if isinstance(result.get("usage"), dict) else "unknown"
    return _candidate(result.get("structured_output")), usage, turns
