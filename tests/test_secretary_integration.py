"""Empirical process checks for the installed secretary lifecycle."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from secretary_support import conversation, make_world, say, table_hashes
from test_secretary_model import Model

from baton.agents.secretary.attention.state import AttentionLog

ROOT = Path(__file__).resolve().parents[1]
SESSION = "synthetic_session"


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(ROOT)}


def _model(world, tmp_path: Path, *, plan: dict | None = None) -> Model:
    model = Model(world, tmp_path, plan=plan)
    policy = json.loads(world.policy_path.read_text())
    policy["summary_bytes"] = 4000
    world.policy_path.write_text(json.dumps(policy))
    return model


def _cli(instance: Path, action: str, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "baton.integrations.secretary", "--instance", str(instance),
                           action, *args], cwd=ROOT, env=_env(), input=input_text, text=True,
                          capture_output=True, check=False, timeout=15)


def _install(world, tmp_path: Path, product: str, *, project_name: str = "project"):
    project = tmp_path / project_name
    project.mkdir()
    codex = tmp_path / "fake-codex"
    codex_log = tmp_path / "codex-argv.jsonl"
    codex.write_text("#!/usr/bin/env python3\nimport json,sys\n"
                     f"open({str(codex_log)!r}, 'a').write(json.dumps(sys.argv[1:])+'\\n')\n")
    codex.chmod(0o700)
    destination = tmp_path / "instance"
    initial_hooks = project / (".codex/hooks.json" if product == "codex" else ".claude/settings.local.json")
    initial_hooks.parent.mkdir(parents=True)
    original = b'{"userMarker":{"preserve":true},"hooks":{"Stop":[{"matcher":"existing"}]}}\n'
    initial_hooks.write_bytes(original)
    _model(world, tmp_path)
    installed = subprocess.run([sys.executable, "-m", "baton.integrations.secretary", "install",
                                "--product", product, "--destination", str(destination), "--project", str(project),
                                "--config", str(world.config_path), "--policy", str(world.policy_path),
                                "--codex", str(codex), "--apply-hooks"], cwd=ROOT, env=_env(), text=True,
                               capture_output=True,
                               check=False, timeout=15)
    assert installed.returncode == 0, installed.stderr
    result = json.loads(installed.stdout)
    instance = Path(result["instance"])
    value = json.loads(instance.read_text())
    value.update(poll_seconds=0.05, budget_seconds=5)
    instance.write_text(json.dumps(value))
    return project, instance, codex_log, initial_hooks, original, result


def _hook(project: Path, instance: Path, event: dict) -> tuple[subprocess.CompletedProcess, dict | None]:
    spec = json.loads(instance.with_name("hooks.json").read_text())
    command = spec["hooks"][event["hook_event_name"]][0]["hooks"][0]["command"]
    run = subprocess.run(["/bin/sh", "-c", command], cwd=project, env=_env(),
                         input=json.dumps(event), text=True, capture_output=True, check=False, timeout=15)
    output = json.loads(run.stdout) if run.stdout.strip() else None
    return run, output


def _wait(predicate, *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    assert predicate(), "timed out waiting for secretary process state"


def _start_worker(instance: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "baton.integrations.secretary", "--instance", str(instance),
                             "run"], cwd=ROOT, env=_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop_worker(process: subprocess.Popen, *, force: bool = False) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGKILL if force else signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _status(instance: Path) -> dict:
    run = _cli(instance, "status")
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd service parser is not installed")
def test_generated_service_accepts_project_paths_with_spaces_and_specifier_characters(tmp_path) -> None:
    with make_world(tmp_path) as world:
        *_, result = _install(world, tmp_path, "claude-code", project_name="project $percent% with spaces")
        parsed = subprocess.run(["systemd-analyze", "--user", "verify", result["unit"]],
                                text=True, capture_output=True, check=False, timeout=15)
        assert parsed.returncode == 0, parsed.stderr
        assert parsed.stderr == "", parsed.stderr  # Invalid EnvironmentFile directives only emit warnings.


def _positions(world) -> tuple[int, int]:
    return AttentionLog(world.state_path, world.config.data_dir, "planner").positions()


@pytest.mark.parametrize("product", ["codex", "claude-code"])
def test_install_generated_hook_worker_receive_and_explicit_ack_lifecycle(tmp_path, product) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        project, instance, codex_log, hooks_path, original, result = _install(world, tmp_path, product)
        assert result["hook_events"] == (["SessionStart", "Stop"] if product == "codex"
                                         else ["SessionStart", "Stop", "FileChanged"])
        merged = json.loads(hooks_path.read_text())
        assert merged["userMarker"] == {"preserve": True} and len(merged["hooks"]["Stop"]) == 2
        applied = _cli(instance, "apply-hooks")
        assert applied.returncode == 0 and json.loads(applied.stdout)["changed"] is False
        assert hooks_path.read_bytes() != original
        assert result["applied"]["changed"] is True
        assert sorted(path.read_bytes() for path in hooks_path.parent.glob(
            hooks_path.name + ".baton-secretary-backup-*")) == [original]

        session_event = {"hook_event_name": "SessionStart", "session_id": SESSION, "cwd": str(project)}
        first, output = _hook(project, instance, session_event)
        assert first.returncode == 0 and output is not None
        refused, refused_output = _hook(project, instance, {**session_event, "session_id": "other_session"})
        assert refused.returncode == 0 and refused_output is None
        assert _status(instance)["session"] == SESSION

        sent = say(world, room, "worker", "A private synthetic progress note")
        model = _model(world, tmp_path)
        worker = _start_worker(instance)
        try:
            _wait(lambda: bool(_status(instance)["pending"]))
            received = _cli(instance, "receive")
            summary = json.loads(received.stdout)
            assert received.returncode == 0 and f"message:{sent['message_id']}" in summary["sources"]
            assert _positions(world) == (0, 0)
            if product == "codex":
                stop_event = {"hook_event_name": "Stop", "session_id": SESSION, "cwd": str(project)}
                stop, stop_output = _hook(project, instance, stop_event)
                duplicate, duplicate_output = _hook(project, instance, stop_event)
                assert stop.returncode == 0 and stop_output["decision"] == "block"
                assert duplicate.returncode == 0 and duplicate_output is None
            ack = _cli(instance, "acknowledge", summary["decision_id"])
            assert ack.returncode == 0
            second = say(world, room, "reviewer", "A new follow-up after acknowledgment")
            source_before = table_hashes(world.baton)
            _wait(lambda: len(model.requests) >= 2
                  and _status(instance)["pending"] not in (None, summary["decision_id"]))
            after = json.loads(_cli(instance, "receive").stdout)
            assert f"message:{second['message_id']}" in after["sources"]
            assert f"message:{sent['message_id']}" not in after["sources"]
            assert table_hashes(world.baton) == source_before
        finally:
            _stop_worker(worker)


def test_pending_result_survives_worker_kill_restart_and_duplicate_lock(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        project, instance, codex_log, *_ = _install(world, tmp_path, "codex")
        _hook(project, instance, {"hook_event_name": "SessionStart", "session_id": SESSION, "cwd": str(project)})
        sent = say(world, room, "worker", "Survive a worker restart")
        model = _model(world, tmp_path)
        before = table_hashes(world.baton)
        first = _start_worker(instance)
        second = None
        try:
            _wait(lambda: bool(_status(instance)["pending"]) and codex_log.exists()
                  and _status(instance)["health"] is not None)
            duplicate = _start_worker(instance)
            try:
                _wait(lambda: duplicate.poll() is not None)
                assert duplicate.returncode == 1
            finally:
                _stop_worker(duplicate)
            last_health = _status(instance)["health"]["checked_at"]
            _stop_worker(first, force=True)
            first = None
            restart = _start_worker(instance)
            second = restart
            _wait(lambda: _status(instance)["health"]["checked_at"] > last_health)
            received = json.loads(_cli(instance, "receive").stdout)
            assert f"message:{sent['message_id']}" in received["sources"]
            assert len(model.requests) == 1
            assert len(codex_log.read_text().splitlines()) == 1
            assert table_hashes(world.baton) == before
        finally:
            if first is not None:
                _stop_worker(first)
            if second is not None:
                _stop_worker(second)


def test_hold_delivers_nothing_then_new_message_wakes_with_held_context(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        project, instance, codex_log, *_ = _install(world, tmp_path, "codex")
        _hook(project, instance, {"hook_event_name": "SessionStart", "session_id": SESSION, "cwd": str(project)})
        first = say(world, room, "worker", "Earlier held context")
        model = _model(world, tmp_path, plan={"steps": [{"decision": "hold", "summary": "", "sources": []}, {}]})
        worker = _start_worker(instance)
        try:
            _wait(lambda: len(model.requests) >= 1)
            assert _status(instance)["pending"] is None
            assert not codex_log.exists()
            second = say(world, room, "reviewer", "This new note needs attention")
            before = table_hashes(world.baton)
            _wait(lambda: len(model.requests) >= 2 and bool(_status(instance)["pending"]))
            result = json.loads(_cli(instance, "receive").stdout)
            assert f"message:{first['message_id']}" in result["sources"]
            assert f"message:{second['message_id']}" in result["sources"]
            assert table_hashes(world.baton) == before
        finally:
            _stop_worker(worker)


def test_claude_file_changed_wake_and_stop_loop_guard_are_session_scoped(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        project, instance, _, _, _, result = _install(world, tmp_path, "claude-code")
        fragment = json.loads(Path(result["hooks"]).read_text())
        file_hook = fragment["hooks"]["FileChanged"][0]["hooks"][0]
        assert file_hook["asyncRewake"] is True
        sent = say(world, room, "worker", "Claude file notification")
        model = _model(world, tmp_path)
        before = table_hashes(world.baton)
        event = {"hook_event_name": "SessionStart", "session_id": SESSION, "cwd": str(project)}
        _, initial_output = _hook(project, instance, event)
        assert initial_output is not None
        worker = _start_worker(instance)
        try:
            _wait(lambda: bool(_status(instance)["pending"]))
            assert initial_output["hookSpecificOutput"]["watchPaths"] == [str(instance.parent / "pending.json")]
            changed = {"hook_event_name": "FileChanged", "session_id": SESSION, "cwd": str(project),
                       "file_path": str(instance.parent / "pending.json"), "event": "change"}
            first, _ = _hook(project, instance, changed)
            assert first.returncode == 2 and "Baton secretary attention summary" in first.stderr
            again, _ = _hook(project, instance, changed)
            assert again.returncode == 0 and again.stderr == ""
            wrong, _ = _hook(project, instance, {**changed, "file_path": str(project / "other")})
            assert wrong.returncode == 0 and wrong.stderr == ""
            stop_guard, output = _hook(project, instance, {"hook_event_name": "Stop", "session_id": SESSION,
                                                          "cwd": str(project), "stop_hook_active": True})
            assert stop_guard.returncode == 0 and output is None and stop_guard.stderr == ""
            received = json.loads(_cli(instance, "receive").stdout)
            assert len(model.requests) == 1 and f"message:{sent['message_id']}" in received["sources"]
            assert table_hashes(world.baton) == before
        finally:
            _stop_worker(worker)


def test_visibility_revocation_before_receive_discards_brief_and_rejects_ack(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        project, instance, *_ = _install(world, tmp_path, "claude-code")
        _model(world, tmp_path)
        say(world, room, "worker", "Visibility must be checked at delivery")
        worker = _start_worker(instance)
        try:
            _wait(lambda: bool(_status(instance)["pending"]))
            decision_id = _status(instance)["pending"]
            from baton import conversations
            conversations.remove(world.baton, world.config, room, "planner", actor="planner")
            before_runner = table_hashes(world.baton)
            receive = _cli(instance, "receive")
            assert receive.returncode == 0 and receive.stdout.strip() == "null"
            acknowledge = _cli(instance, "acknowledge", decision_id)
            assert acknowledge.returncode == 1
            assert table_hashes(world.baton) == before_runner
        finally:
            _stop_worker(worker)
