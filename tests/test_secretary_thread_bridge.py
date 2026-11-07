"""The retained-thread bridge and its guidance, on synthetic data in a private temp directory.

The bridge is copied beside a stand-in gate module (`secretary_thread_gate_stub.py`), the way a host installs it, and
driven only through its own commands, each a real process. The reply form the bridge prints is run through a real
`bash -c`. SECRETARY_THREAD_BRIDGE names another bridge file, for runs against an earlier version or a mutant.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import pty
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from baton.agents.secretary.harness.prompt import RESPONSE_SCHEMA, PromptLayers

TESTS = Path(__file__).resolve().parent
THREAD = TESTS.parent / "integrations" / "secretary" / "thread"
BRIDGE_SOURCE = Path(os.environ.get("SECRETARY_THREAD_BRIDGE", THREAD / "secretary_bridge.py"))
GUIDANCE = THREAD / "GUIDANCE.md"
STUB = TESTS / "secretary_thread_gate_stub.py"
POLICY_TEXT = "Synthetic attribution policy: name who said or asked what."
UNOFFERED_DELIMITER = "BATON_REPLY_JSON"  # the fixed word an earlier proposal used; nothing may rely on it


def message(number: int, text: str = "Synthetic text.", sender: str = "worker") -> dict:
    key = f"msg_{number:016x}"
    return {
        "source_key": f"message:{key}",
        "pos": number,
        "kind": "message",
        "at": "2026-01-01T00:00:00Z",
        "actor": sender,
        "seq": number,
        "message": {"id": key, "conversation_id": "conv_00000000000000aa", "sender": sender, "text": text},
    }


def request(events: list[dict], *, mode: str = "alert", principal: str = "planner", calls: int = 0) -> bytes:
    """A protocol 4 turn request as Baton's harness builds it, with `calls` earlier tool exchanges."""
    context = {
        "principal": principal,
        "instructions": POLICY_TEXT,
        "summary_bytes": 4096,
        "knowledge": "Synthetic contextual knowledge.",
        "mode": mode,
        "trigger": events[-1]["source_key"],
        "since": 0,
        "floor": 0,
        "through_pos": events[-1]["pos"],
        "events": events,
        "reply_context": [],
        "wake_required": False,
        "engine_verdict": None,
    }
    items = PromptLayers(context).messages()
    for number in range(1, calls + 1):
        items.append(
            {"type": "function_call", "call_id": f"call_{number}", "name": "decide_attention", "arguments": "{}"}
        )
        items.append({"type": "function_call_output", "call_id": f"call_{number}", "output": '{"decision":"hold"}'})
    return json.dumps(
        {
            "version": 4,
            "input": items,
            "tools": [{"name": "decide_attention"}],
            "response_schema": RESPONSE_SCHEMA,
            "max_output_tokens": 8192,
        }
    ).encode()


def assessment(header: dict, **changes) -> dict:
    answer = {
        "decision": "wake",
        "reason": "Synthetic reason.",
        "summary": "Synthetic summary of the page.",
        "sources": header["required_sources"],
    }
    return {**answer, **changes}


def serialize(value: object) -> str:
    """What a thread's JSON serializer emits: one line, characters kept as they are."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def module_constant(name: str):
    for node in ast.parse(BRIDGE_SOURCE.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and [t.id for t in node.targets if isinstance(t, ast.Name)] == [name]:
            return eval(compile(ast.Expression(node.value), "<constant>", "eval"), {"__builtins__": {}})
    return None


class Turn:
    """One live `model` process: Baton's side of a turn, holding the lease until it is answered or killed."""

    def __init__(self, host: Host, raw: bytes, seconds: float) -> None:
        self.host = host
        self.deadline = time.time() + seconds
        env = {**host.env(), "SECRETARY_DEADLINE_AT": repr(self.deadline)}
        self.process = subprocess.Popen(
            [sys.executable, "-I", str(host.bridge), "model"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        assert self.process.stdin is not None
        self.process.stdin.write(raw)
        self.process.stdin.close()
        self.process.stdin = None  # closed: communicate() would flush it

    def header(self) -> dict:
        code, out = self.host.run("receive", "--wait", "10")
        assert code == 0 and out and out["status"] == "offered", out
        return out["request"]

    def finish(self, timeout: float = 10) -> tuple[int, object]:
        stdout, _ = self.process.communicate(timeout=timeout)
        return self.process.returncode, json.loads(stdout) if stdout.strip() else None


class Host:
    """The bridge installed beside a stand-in gate, with a private spool, and a working directory for commands."""

    def __init__(self, root: Path) -> None:
        root.mkdir(mode=0o700)
        self.dir, self.gate, self.work = root / "host", root / "gate", root / "work"
        for directory in (self.dir, self.gate, self.work):
            directory.mkdir(mode=0o700)
        self.bridge = self.dir / "secretary_bridge.py"
        shutil.copy(BRIDGE_SOURCE, self.bridge)
        shutil.copy(STUB, self.dir / "secretary_gate.py")
        (self.gate / "policy-v3.json").write_text('{"max_tool_calls": 1}\n')
        (self.gate / "policy-v3.json").chmod(0o600)
        self.turns: list[Turn] = []

    def env(self) -> dict:
        return {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "HOME": str(self.work)}

    def run(self, *args: str, stdin: bytes = b"", timeout: float = 20) -> tuple[int, dict | None]:
        done = subprocess.run(
            [sys.executable, "-I", str(self.bridge), *args],
            input=stdin,
            capture_output=True,
            timeout=timeout,
            env=self.env(),
        )
        return done.returncode, json.loads(done.stdout) if done.stdout.strip() else None

    def start(self, events: list[dict] | None = None, *, seconds: float = 30, **options) -> tuple[Turn, dict]:
        turn = Turn(self, request(events or [message(1)], **options), seconds)
        self.turns.append(turn)
        return turn, turn.header()

    def timing(self) -> list[dict]:
        text = (self.gate / "thread" / "protocol4" / "timing.jsonl").read_text()
        return [json.loads(line) for line in text.split("\n") if line]

    def stop(self) -> None:
        for turn in self.turns:
            if turn.process.poll() is None:
                turn.process.kill()
            turn.process.communicate()


@pytest.fixture
def host(tmp_path):
    installed = Host(tmp_path / "install")
    yield installed
    installed.stop()


def submit(host: Host, form: dict, body: str) -> tuple[int, dict | None]:
    """What a thread's shell tool runs: the printed command, a newline, the serialized answer, a newline and the
    closing line, in one `bash -c`."""
    script = f"{form['command']}\n{body}\n{form['closing_line']}".encode()
    done = subprocess.run(["bash", "-c", script], cwd=host.work, capture_output=True, timeout=20, env=host.env())
    return done.returncode, json.loads(done.stdout) if done.stdout.strip() else None


def form_of(output: dict | None) -> dict:
    assert output is not None and "reply_form" in output, f"no reply form in {output}"
    return output["reply_form"]


def reply_on_terminal(host: Host, request_id: str, timeout: float = 8) -> dict:
    master, slave = pty.openpty()
    try:
        done = subprocess.run(
            [sys.executable, "-I", str(host.bridge), "reply", request_id],
            stdin=slave,
            capture_output=True,
            timeout=timeout,
            env=host.env(),
        )
    except subprocess.TimeoutExpired:
        raise AssertionError("reply did not return within its bound while stdin was a terminal") from None
    finally:
        os.close(master)
        os.close(slave)
    return json.loads(done.stdout)


def accepted(host: Host, turn: Turn, header: dict, answer: dict | None = None) -> object:
    code, out = submit(host, form_of(header), serialize(answer or assessment(header)))
    assert (code, out) == (0, {"status": "accepted"})
    returned, printed = turn.finish()
    assert returned == 0
    return printed


def assert_still_open(host: Host, turn: Turn, header: dict) -> None:
    """A refused reply leaves the turn outstanding, and a correct one is then accepted."""
    code, status = host.run("status")
    assert status and status["spool"] == "live" and status["request_id"] == header["request_id"]
    answer = assessment(header)
    assert accepted(host, turn, header, answer) == answer


# --- the reply form the bridge prints ---------------------------------------------------------------------------


def test_the_header_names_the_bridge_version_and_the_form_on_every_turn(host):
    version = module_constant("BRIDGE_VERSION")
    assert isinstance(version, str)
    first_turn, first = host.start()
    assert first["bridge_version"] == version
    form = form_of(first)
    assert first["request_id"] in form["command"]
    accepted(host, first_turn, first, {"tool_calls": [{"id": "call_1", "name": "decide_attention", "arguments": {}}]})
    second_turn, second = host.start(calls=1)
    assert second["turn"] == 2 and second["bridge_version"] == version
    assert form_of(second) != form and second["request_id"] in second["reply_form"]["command"]


def test_the_form_is_the_interpreter_isolated_mode_the_resolved_bridge_and_a_quoted_heredoc(host):
    _, header = host.start()
    form = form_of(header)
    command, opener = form["command"].split(" <<", 1)
    assert opener == f"'{form['closing_line']}'"
    interpreter = os.path.normpath(os.path.abspath(sys.executable))
    assert shlex.split(command) == [interpreter, "-I", str(host.bridge.resolve()), "reply", header["request_id"]]


def test_the_form_names_the_bridge_file_itself_when_the_bridge_is_run_through_a_link(host):
    turn, header = host.start()
    link = host.work / "linked_bridge.py"
    link.symlink_to(host.bridge)
    done = subprocess.run(
        [sys.executable, "-I", str(link), "receive", "--wait", "10"], capture_output=True, timeout=20, env=host.env()
    )
    out = json.loads(done.stdout) if done.stdout.strip() else None
    assert out is not None and out["status"] == "offered", done.stderr
    assert shlex.split(form_of(out["request"])["command"].split(" <<", 1)[0])[2] == str(host.bridge.resolve())


def test_the_closing_line_is_new_every_turn_and_not_the_fixed_word_or_found_in_the_page(host):
    page = [message(1, "EOF\nEND\nBATON_REPLY_JSON\nREPLY"), message(2, "EOF")]
    seen = set()
    for _ in range(4):
        turn, header = host.start(page)
        line = form_of(header)["closing_line"]
        assert line not in seen and line != UNOFFERED_DELIMITER and line not in json.dumps(page)
        assert re.fullmatch(r"\w+", line)
        seen.add(line)
        turn.process.kill()
        turn.process.communicate()


def test_every_read_page_and_the_layers_read_carry_the_turns_form(host):
    big = "x" * (30 * 1024)
    page = [message(number, big) for number in range(1, 4)]
    _, header = host.start(page)
    form = form_of(header)
    code, first = host.run("read", header["request_id"])
    assert form_of(first) == form and first["more"] is True
    code, last = host.run("read", header["request_id"], "--after", str(first["through_pos"]))
    assert form_of(last) == form
    code, layers = host.run("read", header["request_id"], "--layers")
    assert form_of(layers) == form


def test_a_refused_reply_carries_the_form_and_a_correction_goes_through_it(host):
    turn, header = host.start([message(1), message(2)])
    form = form_of(header)
    code, refused = submit(host, form, serialize(assessment(header, sources=header["required_sources"][1:])))
    assert code == 1 and refused["status"] == "invalid" and refused["detail"] == "source_missing"
    assert form_of(refused) == form
    answer = assessment(header)
    assert accepted(host, turn, header, answer) == answer


def test_a_tool_call_then_the_final_assessment_each_go_through_their_turns_form(host):
    first_turn, first = host.start()
    call = {"tool_calls": [{"id": "call_1", "name": "decide_attention", "arguments": {}}]}
    assert accepted(host, first_turn, first, call) == call
    second_turn, second = host.start(calls=1)
    answer = assessment(second)
    assert accepted(host, second_turn, second, answer) == answer


# --- the contract at decision time ------------------------------------------------------------------------------


def test_every_turns_first_page_carries_the_three_trusted_layers_and_later_pages_do_not(host):
    big = "x" * (30 * 1024)
    page = [message(number, big) for number in range(1, 4)]
    wanted = [item["content"][0]["text"] for item in json.loads(request(page))["input"][:3]]
    for calls in (0, 1):
        turn, header = host.start(page, calls=calls)
        code, first = host.run("read", header["request_id"])
        assert "layers" in first, "the first page holds no layers"
        assert [layer["text"] for layer in first["layers"]] == wanted
        assert [layer["role"] for layer in first["layers"]] == ["developer"] * 3
        code, later = host.run("read", header["request_id"], "--after", str(first["through_pos"]))
        assert "layers" not in later
        turn.process.kill()
        turn.process.communicate()


def test_the_header_says_citing_every_source_is_required_and_is_not_coverage(host):
    _, header = host.start()
    note = header.get("coverage")
    assert isinstance(note, str) and "not coverage" in note and "every event" in note


def test_the_event_page_bound_is_unchanged(host):
    assert module_constant("PAGE_BYTES") == 48 * 1024
    big = "x" * (30 * 1024)
    _, header = host.start([message(number, big) for number in range(1, 4)])
    code, first = host.run("read", header["request_id"])
    assert len(first["events"]) == 1 and first["more"] is True


# --- stdin that is not an answer --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stdin", "detail"), [(b"", "stdin_empty"), (b" \n\t \n", "stdin_empty")], ids=["empty", "spaces"]
)
def test_empty_and_whitespace_stdin_are_refused_by_name_and_leave_the_turn_open(host, stdin, detail):
    turn, header = host.start()
    code, out = host.run("reply", header["request_id"], stdin=stdin, timeout=8)
    assert (code, out["status"], out.get("detail")) == (1, "invalid", detail)
    refused = [row for row in host.timing() if row["step"] == "replied"]
    assert [(row["outcome"], row["detail"]) for row in refused] == [("invalid", detail)]
    assert_still_open(host, turn, header)


def test_a_terminal_stdin_is_refused_at_once_and_leaves_the_turn_open(host):
    turn, header = host.start()
    out = reply_on_terminal(host, header["request_id"])
    assert (out["status"], out.get("detail")) == ("invalid", "stdin_terminal")
    assert [row["detail"] for row in host.timing() if row["step"] == "replied"] == ["stdin_terminal"]
    assert_still_open(host, turn, header)


def test_a_pipe_held_open_and_silent_is_refused_within_its_bound_and_leaves_the_turn_open(host):
    turn, header = host.start()
    process = subprocess.Popen(
        [sys.executable, "-I", str(host.bridge), "reply", header["request_id"]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=host.env(),
    )
    try:
        code = process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        raise AssertionError("reply did not return within its bound while stdin stayed open") from None
    finally:
        process.stdin.close()
        process.stdin = None  # closed: communicate() would flush it
        stdout, _ = process.communicate()
    out = json.loads(stdout)
    assert (code, out["status"], out.get("detail")) == (1, "invalid", "stdin_idle")
    assert form_of(out) == form_of(header)
    assert_still_open(host, turn, header)


def test_a_wrong_id_is_not_outstanding_before_stdin_is_judged(host):
    _, header = host.start()
    other = "thr_" + "0" * 32
    code, out = host.run("reply", other, stdin=b"")
    assert (code, out) == (1, {"status": "not_outstanding"})
    assert reply_on_terminal(host, other) == {"status": "not_outstanding"}


# --- text that must arrive intact --------------------------------------------------------------------------------

HAZARDS = {
    "apostrophes": {"summary": "Worker's note: it's the owner's call, isn't it?"},
    "double_quotes": {"summary": 'The worker said "ship it" and then "wait".'},
    "dollar": {"summary": "Cost is $HOME and ${PATH} and $1 and $$."},
    "command_substitution": {"summary": "Run $(touch SENTINEL) now."},
    "backticks": {"summary": "Run `touch SENTINEL` now."},
    "backslashes": {"summary": "Path C:\\temp\\new and a trailing backslash \\"},
    "escaped_newline": {"summary": "Line one.\nLine two with a literal backslash-n: \\n."},
    "unicode": {
        "summary": "Caf\u00e9 \u65e5\u672c\u8a9e \U0001f680 done",
        "reason": "Separator \u2028 and \u2029 kept.",
    },
    "history_and_pipes": {"summary": "a | b; c && d || e > f < g # h ! i * j ? k ~ l [m] {n}"},
    "heredoc_words": {"reason": "EOF\nBATON_REPLY_JSON <<EOF <<'END' END"},
}


@pytest.mark.parametrize("case", list(HAZARDS))
def test_text_arrives_intact_and_nothing_is_evaluated(host, case):
    turn, header = host.start()
    answer = assessment(header, **HAZARDS[case])
    assert accepted(host, turn, header, answer) == answer
    assert not (host.work / "SENTINEL").exists()


@pytest.mark.parametrize("field", ["reason", "summary"])
def test_half_of_a_surrogate_pair_is_refused_with_the_form_and_leaves_the_turn_open(host, field):
    turn, header = host.start()
    body = serialize(assessment(header, **{field: "trimmed CUT"})).replace("CUT", "\\ud83d")  # a JSON escape
    code, out = submit(host, form_of(header), body)
    assert (code, out["status"], out.get("detail")) == (1, "invalid", "unpaired_surrogate")
    assert form_of(out) == form_of(header)
    assert [row["detail"] for row in host.timing() if row["step"] == "replied"] == ["unpaired_surrogate"]
    assert_still_open(host, turn, header)


def test_the_unique_closing_line_inside_a_json_string_is_just_text(host):
    turn, header = host.start()
    line = form_of(header)["closing_line"]
    answer = assessment(header, reason=f"before\n{line}\ntouch SENTINEL")  # an escaped newline, not a physical one
    assert accepted(host, turn, header, answer) == answer
    assert not (host.work / "SENTINEL").exists()


def test_a_body_with_a_raw_newline_and_the_old_fixed_word_runs_nothing_and_is_refused(host):
    turn, header = host.start()
    body = '{"decision":"wake","reason":"x\n' + UNOFFERED_DELIMITER + '\ntouch SENTINEL\n","summary":"s","sources":[]}'
    code, out = submit(host, form_of(header), body)
    assert not (host.work / "SENTINEL").exists()
    assert (code, out["status"], out["detail"]) == (1, "invalid", "not_json")
    assert_still_open(host, turn, header)


# --- the existing replies and deadlines ---------------------------------------------------------------------------


def test_guard_a_hand_built_quoted_heredoc_preserves_every_hazard(host):
    """Regression guard: the mechanism already works whenever the form is right, with a bridge that prints none."""
    turn, header = host.start()
    summary = 'It\'s $(touch SENTINEL) `touch SENTINEL` "q" \\ \\n \U0001f680'
    answer = assessment(header, summary=summary, reason="sep \u2028")
    command = shlex.join([sys.executable, "-I", str(host.bridge), "reply", header["request_id"]])
    script = f"{command} <<'HAND_BUILT_END_1f3a'\n{serialize(answer)}\nHAND_BUILT_END_1f3a".encode()
    done = subprocess.run(["bash", "-c", script], cwd=host.work, capture_output=True, timeout=20, env=host.env())
    assert json.loads(done.stdout) == {"status": "accepted"}
    assert turn.finish() == (0, answer)
    assert not (host.work / "SENTINEL").exists()


def test_guard_a_wrong_malformed_or_finished_id_is_not_outstanding(host):
    turn, header = host.start()
    answer = serialize(assessment(header)).encode()
    for bad in ("thr_" + "f" * 32, "thr_xyz", "thr_", "not-an-id", "'; touch SENTINEL; '", "thr_" + "F" * 32):
        assert host.run("reply", bad, stdin=answer) == (1, {"status": "not_outstanding"})
    assert not (host.work / "SENTINEL").exists()
    assert host.run("reply", header["request_id"], stdin=answer) == (0, {"status": "accepted"})
    turn.finish()
    assert host.run("reply", header["request_id"], stdin=answer) == (1, {"status": "not_outstanding"})
    assert [row["outcome"] for row in host.timing() if row["step"] == "replied"][-1] == "not_outstanding"


def test_guard_a_reply_inside_the_return_margin_is_late_and_a_second_identical_one_is_late_again(host):
    turn, header = host.start(seconds=3)
    answer = serialize(assessment(header)).encode()
    time.sleep(max(0.0, turn.deadline - 0.3 - time.time()))
    first = host.run("reply", header["request_id"], stdin=answer)
    second = host.run("reply", header["request_id"], stdin=answer)
    assert first == (1, {"status": "late"}) and second == (1, {"status": "late"})
    code, refused = host.run("reply", header["request_id"], stdin=b"{")
    assert (code, refused["status"], refused["detail"]) == (1, "invalid", "not_json")  # invalid is judged before late
    assert [row["outcome"] for row in host.timing() if row["step"] == "replied"] == ["late", "late", "invalid"]


@pytest.fixture
def in_process(host, monkeypatch):
    """The installed bridge imported here with a fake clock, to place a reply exactly at the return margin."""
    monkeypatch.setattr(sys, "path", [str(host.dir), *sys.path])
    monkeypatch.delitem(sys.modules, "secretary_gate", raising=False)
    spec = importlib.util.spec_from_file_location("secretary_bridge_in_process", host.bridge)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    umask = os.umask(0o077)
    yield module
    os.umask(umask)
    sys.modules.pop("secretary_gate", None)


@pytest.mark.parametrize(("seconds_before_deadline", "status"), [(0.51, "accepted"), (0.49, "late")])
def test_guard_the_return_margin_is_half_a_second(
    host, in_process, monkeypatch, capsys, seconds_before_deadline, status
):
    turn, header = host.start()
    stamp = turn.deadline - seconds_before_deadline
    monkeypatch.setattr(
        in_process,
        "time",
        type("Clock", (), {"time": staticmethod(lambda: stamp), "monotonic": time.monotonic, "sleep": time.sleep}),
    )
    answer = host.work / "answer.json"
    answer.write_text(serialize(assessment(header)), encoding="utf-8")
    with answer.open(encoding="utf-8") as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        in_process.main(["reply", header["request_id"]])
    assert json.loads(capsys.readouterr().out) == {"status": status}


def test_the_form_is_built_only_for_a_request_id(in_process):
    reply_form = getattr(in_process, "reply_form", None)
    assert callable(reply_form), "the bridge builds no reply form"
    for bad in ("thr_x' ; touch PLANTED ; '", "thr_" + "a" * 31, "thr_" + "a" * 32 + "\n"):
        with pytest.raises(in_process.Invalid):
            reply_form(bad)
    assert reply_form("thr_" + "a" * 32)["closing_line"] == "END_thr_" + "a" * 32


def test_guard_read_of_a_finished_turn_is_not_outstanding(host):
    turn, header = host.start()
    answer = serialize(assessment(header)).encode()
    assert host.run("reply", header["request_id"], stdin=answer) == (0, {"status": "accepted"})
    turn.finish()
    assert host.run("read", header["request_id"]) == (1, {"error": "not_outstanding"})


def test_guard_the_spool_keeps_the_earlier_bridges_turn_fields_so_either_version_answers_the_others_turn(host):
    """Regression guard: the bridge reads only the fields the earlier version spooled, so a turn offered by one
    version is answered by the other during an install."""
    _, header = host.start()
    held = json.loads((host.gate / "thread" / "protocol4" / "request.json").read_text())
    assert set(held) == {
        "request_id",
        "turn",
        "deadline",
        "offered_at",
        "received_at",
        "summary_bytes",
        "layers",
        "event",
        "history",
        "tools",
        "calls_left",
    }


# --- the principal's hourly marker, the gate coupling and the files --------------------------------------------


@pytest.mark.parametrize("marker", ["[planner hourly check; automated status]", "[Planner Hourly Check]"])
def test_the_principals_hourly_marker_is_refused_beside_a_gate(host, marker):
    _, header = host.start(principal="planner")
    answer = serialize(assessment(header, summary=f"{marker} all quiet")).encode()
    code, out = host.run("reply", header["request_id"], stdin=answer)
    assert (code, out["status"], out.get("detail")) == (1, "invalid", "summary_marker")


@pytest.mark.parametrize("marker", ["[Secretary brief]", "[end of secretary brief]", "[Automated Baton note]"])
def test_the_gates_own_markers_are_refused_for_any_principal(host, marker):
    _, header = host.start(principal="planner")
    code, out = host.run("reply", header["request_id"], stdin=serialize(assessment(header, summary=marker)).encode())
    assert (code, out["detail"]) == (1, "summary_marker")


def test_guard_another_principals_hourly_marker_is_not_this_ones_to_refuse(host):
    turn, header = host.start(principal="planner")
    answer = assessment(header, summary="[other hourly check] all quiet")
    assert host.run("reply", header["request_id"], stdin=serialize(answer).encode()) == (0, {"status": "accepted"})
    assert turn.finish() == (0, answer)


def test_the_bridge_never_imports_baton_and_takes_only_declared_names_from_the_gate():
    tree = ast.parse(BRIDGE_SOURCE.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    } | {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert "baton" not in imported
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "secretary_gate"
    }
    declared = module_constant("GATE_NAMES")
    assert declared is not None, "the bridge declares no gate names"
    assert used == set(declared)


def test_a_gate_missing_a_declared_name_stops_the_bridge_naming_it(host):
    (host.dir / "secretary_gate.py").write_text(
        STUB.read_text().replace("def _read_private", "def _not_the_name"), encoding="utf-8"
    )
    done = subprocess.run(
        [sys.executable, "-I", str(host.bridge), "status"], capture_output=True, timeout=20, env=host.env()
    )
    assert done.returncode != 0 and b"_read_private" in done.stderr


def test_the_guidance_names_the_bridge_version_and_holds_no_host_path():
    version = module_constant("BRIDGE_VERSION")
    assert isinstance(version, str)
    text = GUIDANCE.read_text(encoding="utf-8")
    assert f"bridge version {version}" in text
    paths = r"(?<![\w.:])/(?:home|mnt|usr|root|opt|var|etc|srv|tmp|run)\b"
    assert not re.search(paths, text) and not re.search(paths, BRIDGE_SOURCE.read_text(encoding="utf-8"))


def test_the_guidance_covers_the_loop_the_replies_and_the_coverage_rule():
    text = GUIDANCE.read_text(encoding="utf-8")
    for needle in (
        "BRIDGE receive --wait",
        "--after THROUGH_POS",
        "reply_form",
        "closing_line",
        "summary_bytes",
        "4096",
        "not coverage",
        "late",
        "not_outstanding",
        '{"error": "not_outstanding"}',
        "stdin_idle",
        "unpaired_surrogate",
        "unresolved",
        "author",
    ):
        assert needle in text, needle
