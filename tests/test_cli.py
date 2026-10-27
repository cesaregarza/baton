from __future__ import annotations

import io
import json

import pytest

from baton import cli


def run(capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_local_agents_message_wait_and_move_jobs(host, capsys) -> None:
    config = ["--config", str(host.config.data_dir.parent / "baton.toml")]
    code, sent = run(capsys, *config, "message", "--as", "planner", "--to", "worker", "--text", "start")
    assert (code, sent["seq"], sent["created"]) == (0, 1, True)
    _, again = run(capsys, *config, "message", "--as", "planner", "--to", "worker", "--text", "start",
                   "--request-id", sent["request_id"])
    assert again["created"] is False

    _, waited = run(capsys, *config, "wait", "--as", "worker", "--timeout", "0")
    assert [e["message"]["text"] for e in waited["events"]] == ["start"]

    _, job = run(capsys, *config, "job", "create", "--as", "planner", "--title", "Example")
    _, moved = run(capsys, *config, "job", "move", "--as", "worker", job["job_id"], "working", "--note", "on it")
    assert moved["state"] == "working"
    code, refused = run(capsys, *config, "job", "move", "--as", "worker", job["job_id"], "done")
    assert (code, refused["error"]) == (1, "illegal_transition")


def test_roles_and_config_are_checked(host, capsys, monkeypatch) -> None:
    config = ["--config", str(host.config.data_dir.parent / "baton.toml")]
    monkeypatch.delenv("BATON_ROLE", raising=False)
    assert run(capsys, *config, "read", "--as", "nobody")[0] == 2
    assert run(capsys, *config, "message", "--as", "planner", "--to", "nobody", "--text", "x")[0] == 2
    assert run(capsys, "--config", str(host.config.data_dir / "missing.toml"), "wakes")[0] == 2


def test_message_text_can_come_from_stdin(host, capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin\n"))
    monkeypatch.setenv("BATON_ROLE", "worker")
    config = ["--config", str(host.config.data_dir.parent / "baton.toml")]
    run(capsys, *config, "message", "--to", "planner")
    _, page = run(capsys, *config, "read", "--as", "planner")
    assert page["messages"][0]["text"] == "from stdin\n"


def test_token_new_prints_a_config_entry_with_only_the_digest(host, capsys) -> None:
    config = ["--config", str(host.config.data_dir.parent / "baton.toml")]
    _, made = run(capsys, *config, "token", "new", "--name", "laptop", "--role", "worker")
    assert made["token"] not in made["config_entry"]
    assert 'role = "worker"' in made["config_entry"]


@pytest.mark.parametrize(("line", "code"), [("short\n", 1), ("a sufficiently long phrase\n", 0)])
def test_hash_passphrase_reads_stdin(capsys, monkeypatch, line: str, code: int) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(line))
    assert run(capsys, "hash-passphrase")[0] == code


def test_job_move_takes_a_retry_id_and_an_expected_revision(host, capsys) -> None:
    config = ["--config", str(host.config.data_dir.parent / "baton.toml")]
    _, job = run(capsys, *config, "job", "create", "--as", "planner", "--title", "Example")
    _, moved = run(capsys, *config, "job", "move", "--as", "worker", job["job_id"], "working",
                   "--expected-revision", "1")
    _, again = run(capsys, *config, "job", "move", "--as", "worker", job["job_id"], "working",
                   "--expected-revision", "1", "--request-id", moved["request_id"])
    assert (moved["created"], again["created"], again["revision"]) == (True, False, 2)
    code, stale = run(capsys, *config, "job", "move", "--as", "worker", job["job_id"], "review",
                      "--expected-revision", "1")
    assert (code, stale["error"]) == (1, "stale_revision")
