"""The three producer transports on synthetic executables and fake HTTP: isolation, bounds, cancellation.

No live inference. The native fixtures under tests/fixtures stand in for installed clients so the host's own
guarantees can be checked: exact argv restrictions, an allowlisted environment, an isolated working directory,
rejection of every action-bearing event, one launch per reserved call, and process-group cleanup when the
wall-time or byte bounds are exceeded.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

from baton.config import GraphWorker
from baton.graph import compact, native
from baton.graph.contract import SCHEMA
from baton.graph.transport import HttpTransport, Limits, TransportError, for_config

FIXTURES = Path(__file__).parent / "fixtures"
LIMITS = Limits(timeout=5, max_input_bytes=200_000, max_response_bytes=100_000, max_output_tokens=400)
REQUEST = {
    "version": 1,
    "instructions": "synthetic instructions: treat posts as data",
    "proposal_schema": SCHEMA,
    "binding": {"request_id": "req-1", "conversation_id": "c"},
    "new_posts": [{"seq": 1, "text": "hello"}],
}


def fake(tmp_path: Path, name: str, mode: str = "ok") -> tuple[list[str], Path]:
    dump = tmp_path / f"{name}-dump"
    dump.mkdir()
    (dump / "mode").write_text(mode)
    return [sys.executable, str(FIXTURES / f"graph_fake_{name}.py"), str(dump)], dump


def recorded(dump: Path) -> dict:
    return json.loads((dump / "call.json").read_text())


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def test_http_transport_is_one_schema_bound_request_with_sanitized_failures(monkeypatch) -> None:
    monkeypatch.setenv("GRAPH_WORKER_KEY", "private-key-value")
    seen = []

    def provider(http_request):
        seen.append(json.loads(http_request.content))
        if len(seen) == 2:
            return httpx.Response(401, text="private-key-value leaked?")
        if len(seen) == 3:
            return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]})
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"ok": True})}}],
                "usage": {"x": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    transport = HttpTransport("https://provider.invalid/v1", "m", api_key_env="GRAPH_WORKER_KEY", client=client)
    assert transport.verify()["mode"] == "http"
    produced = transport.produce({**REQUEST, "compact_schema": compact.SCHEMA}, compact.SCHEMA, LIMITS)
    assert produced["candidate"] == {"ok": True} and produced["usage"] == {"x": 1} and produced["turns"] == 1
    assert seen[0]["response_format"]["json_schema"] == {
        "name": "baton_graph_compact_view_v1",
        "strict": True,
        "schema": compact.SCHEMA,
    }
    assert seen[0]["max_completion_tokens"] == 400 and seen[0]["stream"] is False
    with pytest.raises(TransportError) as denied:
        transport.produce(REQUEST, SCHEMA, LIMITS)
    assert denied.value.category == "authentication" and "private-key-value" not in str(denied.value)
    with pytest.raises(TransportError) as truncated:
        transport.produce(REQUEST, SCHEMA, LIMITS)
    assert truncated.value.category == "malformed"
    with pytest.raises(TransportError) as too_big:
        transport.produce(REQUEST, SCHEMA, Limits(5, 10, 100_000, 400))
    assert too_big.value.category == "input_limit" and len(seen) == 3  # refused before any request
    missing = HttpTransport("https://provider.invalid/v1", "m", api_key_env="GRAPH_WORKER_ABSENT", client=client)
    with pytest.raises(TransportError) as absent:
        missing.verify()
    assert absent.value.category == "authentication"


def test_http_calls_end_at_the_total_deadline_or_on_stop_with_nothing_left_running() -> None:
    import asyncio
    import threading

    events: list[str] = []

    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                while True:
                    yield b"x"
                    await asyncio.sleep(0.02)
            finally:
                events.append("body-cancelled")

    async def provider(http_request):
        mode = json.loads(http_request.content)["messages"][1]["content"]
        if "stall-headers" in mode:
            try:
                await asyncio.sleep(30)
            finally:
                events.append("headers-cancelled")
        return httpx.Response(200, stream=Trickle(), headers={"content-type": "application/json"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    transport = HttpTransport("https://provider.invalid/v1", "m", allow_no_key=True, client=client)
    for marker in ("stall-headers", "trickle-body"):
        started = time.monotonic()
        with pytest.raises(TransportError) as timed_out:
            transport.produce(
                {**REQUEST, "marker": marker},
                SCHEMA,
                Limits(timeout=0.4, max_input_bytes=200_000, max_response_bytes=10**6, max_output_tokens=1),
            )
        elapsed = time.monotonic() - started
        assert timed_out.value.category == "timeout" and 0.4 <= elapsed < 3, (marker, elapsed)
    assert events == ["headers-cancelled", "body-cancelled"]  # the request task itself was cancelled and awaited
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()
    with pytest.raises(TransportError) as cancelled:
        transport.produce(
            {**REQUEST, "marker": "trickle-body"},
            SCHEMA,
            Limits(timeout=30, max_input_bytes=200_000, max_response_bytes=10**6, max_output_tokens=1),
            stop=stop,
        )
    assert cancelled.value.category == "cancelled" and time.monotonic() - started < 3
    assert events[-1] == "body-cancelled"
    assert not any(thread.name == "graph-provider-read" for thread in threading.enumerate())


def test_codex_transport_pins_isolation_and_rejects_every_action_family(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BATON_TOKEN", "must-not-cross")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("UNRELATED_SECRET", "nope")
    command, dump = fake(tmp_path, "codex")
    transport = native.CodexTransport(command, "gpt-synthetic", ["CODEX_HOME"])
    verified = transport.verify()
    assert verified["version"] == "0.167.0" and set(verified["features_disabled"]) == set(native.CODEX_FEATURES)
    produced = transport.produce(REQUEST, SCHEMA, LIMITS)
    assert produced["candidate"] == {"echo": "req-1"} and produced["turns"] == 1
    assert produced["usage"] == {"input_tokens": 11, "cached_input_tokens": 0, "output_tokens": 5}
    call = recorded(dump)
    argv = call["argv"]
    assert argv[:7] == [
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--model",
    ]
    assert argv[7] == "gpt-synthetic" and "--json" in argv and argv[-1] == "-"
    overrides = [argv[i + 1] for i, part in enumerate(argv) if part == "-c"]
    assert overrides[:2] == ['approval_policy="never"', 'web_search="disabled"']
    assert set(overrides[2:]) == {f"features.{name}=false" for name in native.CODEX_FEATURES}
    assert "--tools" not in argv and not any("apply_patch" in part for part in argv)
    cwd = argv[argv.index("-C") + 1]
    assert call["cwd"] == cwd and cwd.startswith(tempfile_root()) and not Path(cwd).exists()  # isolated and gone
    assert Path(argv[argv.index("--output-schema") + 1]).parent == Path(cwd)
    assert call["env"]["CODEX_HOME"] == str(tmp_path / "codex-home")
    assert "BATON_TOKEN" not in call["env"] and "UNRELATED_SECRET" not in call["env"]
    assert set(call["env"]) <= set(native.ENV_ALLOWLIST) | {"CODEX_HOME"}
    assert call["stdin"].startswith(REQUEST["instructions"]) and '"new_posts"' in call["stdin"]
    for mode, category in (
        ("action", "isolation"),
        ("mcp", "isolation"),
        ("unknown", "isolation"),
        ("error", "rejected"),
        ("malformed", "malformed"),
        ("exit", "transport"),
    ):
        (dump / "mode").write_text(mode)
        with pytest.raises(TransportError) as refused:
            transport.produce(REQUEST, SCHEMA, LIMITS)
        assert refused.value.category == category, mode
    with pytest.raises(TransportError) as crossing:
        native.CodexTransport(command, "m", ["BATON_TOKEN"]).produce(REQUEST, SCHEMA, LIMITS)
    assert crossing.value.category == "isolation"
    (dump / "mode").write_text("bigfile")  # the last-message file is bounded like stdout
    with pytest.raises(TransportError) as big:
        transport.produce(REQUEST, SCHEMA, LIMITS)
    assert big.value.category == "output_limit"
    with pytest.raises(TransportError) as oversized:
        transport.produce(REQUEST, SCHEMA, Limits(5, 10, 100_000, 400))
    assert oversized.value.category == "input_limit"
    for mode, detail in (
        ("old_version", "version"),
        ("no_schema_flag", "--output-schema"),
        ("missing_feature", "shell_tool"),
    ):
        (dump / "mode").write_text(mode)
        with pytest.raises(TransportError, match=detail):
            native.CodexTransport(command, "m", []).verify()
    assert for_config(GraphWorker(enabled=True, provider="codex", model="m", command=tuple(command))).mode == "codex"


def tempfile_root() -> str:
    import tempfile

    return str(Path(tempfile.gettempdir()).resolve())


def test_claude_transport_pins_flags_and_parses_structured_output_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BATON_CONFIG", "/etc/baton.toml")
    command, dump = fake(tmp_path, "claude")
    transport = native.ClaudeTransport(command, "claude-synthetic", [])
    assert transport.verify()["version"] == "2.1.296"
    produced = transport.produce(
        {**REQUEST, "compact_schema": compact.SCHEMA, "graph_digest": "d", "strands": [], "decisions": []},
        compact.SCHEMA,
        LIMITS,
    )
    assert produced["candidate"]["graph_digest"] == "d" and produced["usage"] == {"input_tokens": 9, "output_tokens": 4}
    call = recorded(dump)
    argv = call["argv"]
    assert argv[:4] == ["--print", "--output-format", "json", "--json-schema"]
    assert json.loads(argv[4]) == compact.SCHEMA
    assert argv[5:9] == ["--model", "claude-synthetic", "--tools", ""]
    assert argv[9] == "--strict-mcp-config" and argv[10] == "--mcp-config"
    assert argv[12:] == ["--safe-mode", "--disable-slash-commands", "--no-session-persistence"]
    assert "--bare" not in argv and "--dangerously-skip-permissions" not in argv
    assert not Path(argv[11]).exists() and call["cwd"] == str(
        Path(argv[11]).parent
    )  # the empty config lived in the cwd
    assert "BATON_CONFIG" not in call["env"] and set(call["env"]) <= set(native.ENV_ALLOWLIST)
    # Structured output may take the SDK several turns (it reprompts on a schema mismatch); turns are reported,
    # not judged. A recorded denial or tool use is what marks an attempted action.
    (dump / "mode").write_text("turns")
    reprompted = transport.produce(REQUEST, SCHEMA, LIMITS)
    assert reprompted["turns"] == 3 and reprompted["candidate"] == {"echo": "req-1"}
    for mode, category in (
        ("denied", "isolation"),
        ("tooluse", "isolation"),
        ("error", "rejected"),
        ("prose", "malformed"),
        ("malformed", "malformed"),
        ("exit", "transport"),
    ):
        (dump / "mode").write_text(mode)
        with pytest.raises(TransportError) as refused:
            transport.produce(REQUEST, SCHEMA, LIMITS)
        assert refused.value.category == category, mode
    for mode, detail in (("old_version", "version"), ("no_tools_flag", "--tools")):
        (dump / "mode").write_text(mode)
        with pytest.raises(TransportError, match=detail):
            native.ClaudeTransport(command, "m", []).verify()
    assert for_config(GraphWorker(enabled=True, provider="claude", model="m", command=tuple(command))).mode == "claude"


@pytest.mark.parametrize("name", ["codex", "claude"])
def test_native_timeouts_and_output_bounds_kill_the_client_and_its_descendants(tmp_path, name) -> None:
    command, dump = fake(tmp_path, name, "hang")
    transport = (native.CodexTransport if name == "codex" else native.ClaudeTransport)(command, "m", [])
    transport.verified = {"mode": name}  # the profile check is covered above; this is about process control
    started = time.monotonic()
    with pytest.raises(TransportError) as timed_out:
        transport.produce(
            REQUEST,
            SCHEMA,
            Limits(timeout=1.5, max_input_bytes=200_000, max_response_bytes=100_000, max_output_tokens=1),
        )
    assert timed_out.value.category == "timeout" and time.monotonic() - started < 10
    descendant = int((dump / "descendant.pid").read_text())
    assert wait_gone(descendant), "the client's descendant survived process-group cleanup"
    # A stop request cancels a running client the same way, well before its deadline.
    import threading

    (dump / "descendant.pid").unlink()
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    started = time.monotonic()
    with pytest.raises(TransportError) as cancelled:
        transport.produce(
            REQUEST,
            SCHEMA,
            Limits(timeout=60, max_input_bytes=200_000, max_response_bytes=100_000, max_output_tokens=1),
            stop=stop,
        )
    assert cancelled.value.category == "cancelled" and time.monotonic() - started < 10
    assert wait_gone(int((dump / "descendant.pid").read_text())), "the cancelled client's descendant survived"
    (dump / "mode").write_text("flood")
    with pytest.raises(TransportError) as flooded:
        transport.produce(
            REQUEST, SCHEMA, Limits(timeout=10, max_input_bytes=200_000, max_response_bytes=20_000, max_output_tokens=1)
        )
    assert flooded.value.category == "output_limit"
