"""The retained-thread bridge and its guidance, on synthetic data in a private temp directory.

The bridge is copied beside a stand-in gate module (`secretary_thread_gate_stub.py`), the way a host installs it, and
driven only through its own commands, each a real process. The reply form the bridge prints is run through a real
`bash -c`. SECRETARY_THREAD_BRIDGE names another bridge file, for runs against an earlier version or a mutant.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import math
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
import secretary_thread_fixtures as fixtures

from baton.agents.secretary.harness.prompt import RESPONSE_SCHEMA, PromptLayers
from baton.config import ROLE_NAME, ConfigError, Conversations, parse

TESTS = Path(__file__).resolve().parent
THREAD = TESTS.parent / "integrations" / "secretary" / "thread"
BRIDGE_SOURCE = Path(os.environ.get("SECRETARY_THREAD_BRIDGE", THREAD / "secretary_bridge.py"))
GUIDANCE = THREAD / "GUIDANCE.md"
RESUME_MESSAGE = THREAD / "resume_message.py"
STUB = TESTS / "secretary_thread_gate_stub.py"
POLICY_TEXT = "Synthetic attribution policy: name who said or asked what."
UNOFFERED_DELIMITER = "BATON_REPLY_JSON"  # the fixed word an earlier proposal used; nothing may rely on it
TOOL_TOKENS = 10_000  # the default output budget of each of the thread's two tool layers
MARGIN = 0.85  # the share of that budget a page may use: the rest is the tools' wrapping and a tokenizer's excess
# What printed text costs, by kind, as the bridge must count it at least: words and spaces, other runs of letters and
# digits (ids, digests, encodings), other ASCII characters, and every other byte a token.
WORD_BYTES_PER_TOKEN, DENSE_BYTES_PER_TOKEN, SYMBOL_BYTES_PER_TOKEN, WORD_LETTERS = 3.0, 1.15, 1.25, 12
# A command cell of the guidance: its command, its output budget, its sandbox permission and its justification.
COMMAND_CELL = re.compile(
    r"(?m)^    const out = await tools\.exec_command\(\{ cmd: (.+?), yield_time_ms: \d+, max_output_tokens: (\d+), "
    r'sandbox_permissions: "([^"]*)", justification: "([^"]*)" \}\);$'
)
# Each command cell's command and the justification it carries, in the guidance's order.
CELL_JUSTIFICATIONS = [
    ('"BRIDGE receive --wait 600"', "Run the single receive loop for the existing secretary bridge."),
    ('"BRIDGE read REQUEST_ID --page N"', "Read every page of the newly offered protocol-4 turn before deciding."),
    (
        'form.command + "\\n" + JSON.stringify(answer) + "\\n" + form.closing_line',
        "Submit the assessment using the turn's supplied reply form.",
    ),
    ('"BRIDGE status"', "Read bridge status as required to acknowledge the updated protocol."),
]
# The most participants `conversations.max_participants` may allow; a message's attention names distinct participants.
MAX_PARTICIPANTS = 1000
# An index line that leaves attention names out: the names it shows, how many it left out and how many there are.
ABRIDGED = re.compile(
    r" attention (?P<shown>(?:[a-z][a-z0-9_-]*, )*)(?P<left>\d+) more not shown here; "
    r"the event's own attention list names all (?P<count>\d+)(?: \(part \d+ of \d+\))?$"
)


def message(number: int, text: str = "Synthetic text.", sender: str = "worker", attention: list | None = None) -> dict:
    key = f"msg_{number:016x}"
    return {
        "source_key": f"message:{key}",
        "pos": number,
        "kind": "message",
        "at": "2026-01-01T00:00:00Z",
        "actor": sender,
        "seq": number,
        "message": {
            "id": key,
            "conversation_id": "conv_00000000000000aa",
            "sender": sender,
            "text": text,
            **({"attention": attention} if attention else {}),
        },
    }


def longest_roles(count: int) -> list[str]:
    """`count` distinct role names, each as long as a role name may be and in the costliest shape: one dense run."""
    names = [f"r{number:031d}" for number in range(count)]
    assert all(ROLE_NAME.fullmatch(name) and not ROLE_NAME.fullmatch(name + "0") for name in names)
    return names


def request(
    events: list[dict],
    *,
    mode: str = "alert",
    principal: str = "planner",
    calls: int = 0,
    instructions: str = POLICY_TEXT,
    knowledge: str = "Synthetic contextual knowledge.",
    reply_context: list[dict] | None = None,
) -> bytes:
    """A protocol 4 turn request as Baton's harness builds it, with `calls` earlier tool exchanges."""
    context = {
        "principal": principal,
        "instructions": instructions,
        "summary_bytes": 4096,
        "knowledge": knowledge,
        "mode": mode,
        "trigger": events[-1]["source_key"],
        "since": 0,
        "floor": 0,
        "through_pos": events[-1]["pos"],
        "events": events,
        "reply_context": reply_context or [],
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
        assert code == 0 and out and out["status"] == "offered", f"{out}; model exit {self.process.poll()}"
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
        code, out = self.printed(*args, stdin=stdin, timeout=timeout)
        return code, json.loads(out) if out.strip() else None

    def printed(self, *args: str, stdin: bytes = b"", timeout: float = 20) -> tuple[int, str]:
        """A command's exit code and its stdout exactly as the thread's tools would get it."""
        done = subprocess.run(
            [sys.executable, "-I", str(self.bridge), *args],
            input=stdin,
            capture_output=True,
            timeout=timeout,
            env=self.env(),
        )
        return done.returncode, done.stdout.decode()

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


def tool_tokens(line: str) -> float:
    """What a printed line costs the thread's tools at worst: as the cell prints a result object, escaped once more
    inside its JSON, then counted run by run. A word is letters of one case shape, at most WORD_LETTERS, never four
    consonants in a row, and a vowel in any of three letters or more."""
    total = 0.0
    for run in re.findall(r"[A-Za-z0-9]+|[^A-Za-z0-9]", json.dumps(line, ensure_ascii=False)[1:-1]):
        if run.isascii() and run.isalnum():
            lower = run.lower()
            word = (
                len(run) <= WORD_LETTERS
                and re.fullmatch(r"[A-Z]?[a-z]+|[A-Z]+", run) is not None
                and re.search(r"[^aeiouy]{4}", lower) is None
                and (len(run) < 3 or re.fullmatch(r"[^aeiouy]+", lower) is None)
            )
            total += len(run) / (WORD_BYTES_PER_TOKEN if word else DENSE_BYTES_PER_TOKEN)
        elif run == " ":
            total += 1 / WORD_BYTES_PER_TOKEN
        elif run.isascii():
            total += 1 / SYMBOL_BYTES_PER_TOKEN
        else:
            total += len(run.encode("utf-8", "surrogatepass"))
    return total


def read_all(host: Host, header: dict) -> list[tuple[str, dict]]:
    """Every page the header counts, each read once, in order: its printed line and its value."""
    count = header.get("pages")
    assert isinstance(count, int) and count >= 1, f"the header counts no pages: {count!r}"
    read = []
    for number in range(1, count + 1):
        code, line = host.printed("read", header["request_id"], "--page", str(number))
        assert code == 0 and line.strip(), (number, code, line[:200])
        read.append((line, json.loads(line)))
    return read


def delivered(read: list[tuple[str, dict]]) -> list[str]:
    """Each event as the pages deliver it, in order, printed the way a whole page prints it: a part's shares joined."""
    events, parts = [], None
    for _, page in read:
        for item in page["events"]:
            if "part" not in item:
                assert parts is None, "an event arrived inside another's parts"
                events.append(json.dumps(item, sort_keys=True, ensure_ascii=False))
                continue
            if item["part"] == 1:
                assert parts is None, "a split event began before the last one ended"
                parts = (item["source_key"], item["parts"], [])
            assert parts is not None and (item["source_key"], item["parts"]) == parts[:2], item
            assert item["part"] == len(parts[2]) + 1, "parts out of order"
            parts[2].append(item["json"])
            if item["part"] == item["parts"]:
                events.append("".join(parts[2]))
                parts = None
    assert parts is None, "a split event never finished"
    return events


def printed_as_a_page(events: list[dict]) -> list[str]:
    return [json.dumps(event, sort_keys=True, ensure_ascii=False) for event in events]


def page_bound() -> float:
    bound = module_constant("PAGE_TOKENS")
    assert isinstance(bound, int | float) and 0 < bound <= MARGIN * TOOL_TOKENS, f"no margin under the tools: {bound!r}"
    return bound


def assert_within_the_tools(read: list[tuple[str, dict]]) -> None:
    bound = page_bound()
    for number, (line, _) in enumerate(read, 1):
        assert tool_tokens(line) <= bound, f"page {number} costs {tool_tokens(line):.0f} tokens against {bound}"


def index_room() -> float:
    """The most of a page one event's index line may take: a twentieth, which INDEX_TOKENS must stay within."""
    return page_bound() / 20


def index_bound() -> float:
    bound = module_constant("INDEX_TOKENS")
    assert isinstance(bound, int | float) and 0 < bound <= index_room(), f"no small index bound: {bound!r}"
    return bound


def first_page_holding(host: Host, header: dict, source_key: str) -> tuple[int, dict]:
    """The number and value of the first page holding the event `source_key`, reading the pages in order up to it."""
    for number in range(1, header["pages"] + 1):
        code, line = host.printed("read", header["request_id"], "--page", str(number))
        assert code == 0 and line.strip(), (number, code, line[:200])
        page = json.loads(line)
        if any(item["source_key"] == source_key for item in page["events"]):
            return number, page
    raise AssertionError(f"none of the {header['pages']} pages holds {source_key}")


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


def test_every_read_page_carries_the_turns_form(host):
    _, header = host.start(fixtures.forty_one_events())
    form = form_of(header)
    read = read_all(host, header)
    assert len(read) > 1
    assert all(form_of(page) == form for _, page in read)


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
    page = fixtures.forty_one_events()
    wanted = [item["content"][0]["text"] for item in json.loads(request(page))["input"][:3]]
    for calls in (0, 1):
        turn, header = host.start(page, calls=calls)
        read = read_all(host, header)
        first = read[0][1]
        assert "layers" in first, "the first page holds no layers"
        assert [layer["text"] for layer in first["layers"]] == wanted
        assert [layer["role"] for layer in first["layers"]] == ["developer"] * 3
        assert all(not page.get("layers") for _, page in read[1:])
        turn.process.kill()
        turn.process.communicate()


def test_the_header_says_citing_every_source_is_required_and_is_not_coverage(host):
    _, header = host.start()
    note = header.get("coverage")
    assert isinstance(note, str) and "not coverage" in note and "every event" in note


# --- pages the thread's tools show whole ---------------------------------------------------------------------


def test_guard_the_41_event_fixture_reproduces_the_two_oversized_pages_of_the_earlier_bound():
    """Regression guard: the fixture is the observed shape, two pages each far past what the tools show."""
    old = fixtures.old_pages(fixtures.forty_one_events())
    assert [len(page) for page in old] == [16, 25]
    for page in old:
        assert tool_tokens(", ".join(json.dumps(event, ensure_ascii=False) for event in page)) > TOOL_TOKENS


def test_every_printed_line_of_the_41_event_turn_fits_the_tools(host):
    _, header = host.start(fixtures.forty_one_events(), instructions=fixtures.ATTRIBUTION_POLICY)
    code, first = host.printed("read", header["request_id"])  # page 1, the same command in every version
    assert code == 0 and tool_tokens(first) <= TOOL_TOKENS, f"page 1 costs {tool_tokens(first):.0f} tokens"
    code, line = host.printed("receive", "--wait", "10")
    assert code == 0 and tool_tokens(line) <= page_bound()
    read = read_all(host, header)
    assert read[0][0] == first or {**json.loads(first), "seconds_left": 0} == {**read[0][1], "seconds_left": 0}
    assert_within_the_tools(read)
    assert len(read) > 2


@pytest.mark.parametrize("window", fixtures.DENSE_WINDOWS)
def test_pages_of_digests_ids_an_encoding_and_escapes_fit_the_tools_as_printed_and_arrive_whole(host, window):
    """The text that costs the most per byte, counted as the cell prints a result object at worst."""
    events = getattr(fixtures, window)()
    _, header = host.start(events)
    read = read_all(host, header)
    assert_within_the_tools(read)
    assert delivered(read) == printed_as_a_page(events)


def test_a_header_too_wide_for_the_tools_is_never_offered_and_the_operator_sees_why(host):
    turn = Turn(host, request([message(number) for number in range(1, 801)]), 30)
    host.turns.append(turn)
    try:
        ended = turn.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        ended = None
    assert ended == 1, "the turn was offered"
    assert turn.finish() == (1, None)
    assert host.run("receive", "--wait", "1") == (0, {"status": "none"})
    code, out = host.run("timing")
    assert code == 0 and out.get("oversized") == 1 and out["requests"] == [], out


@pytest.mark.parametrize("window", ["forty_one_events", "nine_events"])
def test_every_event_and_reply_parent_arrives_once_and_byte_identical(host, window):
    events = getattr(fixtures, window)()
    parents = [{"source_key": f"message:msg_{number:016x}", "pos": None} for number in (7, 8)]
    _, header = host.start(events, instructions=fixtures.ATTRIBUTION_POLICY, reply_context=parents)
    read = read_all(host, header)
    assert_within_the_tools(read)
    assert delivered(read) == printed_as_a_page(events)
    shown = [entry["source_key"] for _, page in read for entry in page.get("reply_context", [])]
    assert shown == [parent["source_key"] for parent in parents]


def test_an_event_larger_than_a_page_arrives_whole_in_numbered_parts_on_consecutive_pages(host):
    hazards = 'quote " backslash \\ newline\n tab\t caf\u00e9 \u65e5\u672c \U0001f680 \u2028 '
    events = [message(1), message(2, hazards * 2000), message(3)]
    _, header = host.start(events)
    read = read_all(host, header)
    assert_within_the_tools(read)
    assert delivered(read) == printed_as_a_page(events)
    parts = [(number, item) for number, (_, page) in enumerate(read, 1) for item in page["events"] if "part" in item]
    assert len(parts) > 2, "the large event was not split"
    assert [item["part"] for _, item in parts] == list(range(1, len(parts) + 1))
    assert {item["parts"] for _, item in parts} == {len(parts)}
    pages_holding = [number for number, _ in parts]
    assert pages_holding == list(range(pages_holding[0], pages_holding[0] + len(parts))), "parts not consecutive"
    lines = [line for _, page in read for line in page["index"] if "2/3 " in line]
    assert lines == [f"2/3 pos 2 message from worker (part {k} of {len(parts)})" for k in range(1, len(parts) + 1)]


def test_long_layers_count_inside_the_bound_and_come_before_every_event(host):
    policy = "Synthetic policy line, with caf\u00e9 and \u65e5\u672c in it. " * 600
    events = fixtures.forty_one_events()
    wanted = [item["content"][0]["text"] for item in json.loads(request(events, instructions=policy))["input"][:3]]
    _, header = host.start(events, instructions=policy)
    read = read_all(host, header)
    assert_within_the_tools(read)
    order = [field for _, page in read for field in ("layers", "events") for _ in page.get(field, [])]
    assert order == sorted(order, key=lambda field: field != "layers"), "an event came before a layer"
    texts: dict[int, str] = {}
    for _, page in read:
        for layer in page.get("layers", []):
            texts[layer["index"]] = texts.get(layer["index"], "") + layer["text"]
    assert [texts[index] for index in sorted(texts)] == wanted
    assert delivered(read) == printed_as_a_page(events)


def test_a_layer_of_raw_line_breaks_is_cut_where_its_printed_runs_end_and_arrives_whole(host):
    """A layer's text is raw: a line break prints as an escape whose last letter joins the word after it."""
    knowledge = "\nOk" * 15000
    wanted = json.loads(request([message(1)], knowledge=knowledge))["input"][1]["content"][0]["text"]
    _, header = host.start([message(1)], knowledge=knowledge)
    read = read_all(host, header)
    assert_within_the_tools(read)
    shares = [layer for _, page in read for layer in page.get("layers", []) if layer["index"] == 1]
    assert len(shares) > 1 and "".join(layer["text"] for layer in shares) == wanted


def test_the_header_counts_the_pages_and_every_read_agrees(host):
    _, header = host.start(fixtures.forty_one_events())
    read = read_all(host, header)
    count = header["pages"]
    assert [page["page"] for _, page in read] == list(range(1, count + 1))
    assert {page["pages"] for _, page in read} == {count}
    assert [page["more"] for _, page in read] == [True] * (count - 1) + [False]
    for beyond in (0, count + 1):
        code, out = host.run("read", header["request_id"], "--page", str(beyond))
        assert (code, out["status"], out["detail"], out["pages"]) == (1, "invalid", "no_such_page", count)
    again = host.run("read", header["request_id"], "--page", "1")[1]
    assert {**again, "seconds_left": None} == {**read[0][1], "seconds_left": None}, "the same page changed"


def test_every_page_carries_status_and_its_own_seconds_left(host):
    _, header = host.start(fixtures.forty_one_events())
    read = read_all(host, header)
    for _, page in read:
        assert page.get("status") == "page"
        left = page.get("seconds_left")
        assert isinstance(left, float) and 0 < left <= header["seconds_left"], left


def test_the_index_lists_every_event_once_in_order_with_its_author_and_attention(host):
    events = fixtures.forty_one_events()
    _, header = host.start(events)
    read = read_all(host, header)
    lines = [line for _, page in read for line in page["index"]]
    wanted = []
    for number, event in enumerate(events, 1):
        attention = event["message"]["attention"]
        line = f"{number}/41 pos {event['pos']} message from {event['actor']}"
        wanted.append(line + (f" attention {', '.join(attention)}" if attention else ""))
    assert lines == wanted
    for _, page in read:
        assert len(page["index"]) == len(page["events"])


def test_guard_an_ordinary_attention_list_prints_whole_on_its_index_line(host):
    """Regression guard: a short list, and a conversation of Baton's default size with every name at the longest role
    name, print whole, as before the index was bounded."""
    eight = longest_roles(Conversations().max_participants)
    _, header = host.start([message(1, attention=["reviewer", "coordinator"]), message(2, attention=eight)])
    lines = [line for _, page in read_all(host, header) for line in page["index"]]
    assert lines == [
        "1/2 pos 1 message from worker attention reviewer, coordinator",
        f"2/2 pos 2 message from worker attention {', '.join(eight)}",
    ]


def test_guard_the_widest_supported_attention_list_is_a_thousand_names_of_32_characters(tmp_path):
    """Regression guard: the boundary below is Baton's. Attention names distinct participants of the conversation,
    which config allows at most MAX_PARTICIPANTS of, and a role name is at most 32 characters."""
    site = {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")}
    raw = {"host": site, "roles": {"reviewer": {}}}
    allowed = parse({**raw, "conversations": {"enabled": True, "max_participants": MAX_PARTICIPANTS}})
    assert allowed.conversations.max_participants == MAX_PARTICIPANTS
    with pytest.raises(ConfigError):
        parse({**raw, "conversations": {"enabled": True, "max_participants": MAX_PARTICIPANTS + 1}})
    assert len(longest_roles(1)[0]) == 32


@pytest.mark.parametrize("count", [275, 280, MAX_PARTICIPANTS], ids=["275_roles", "280_roles", "boundary"])
def test_a_long_attention_list_keeps_every_index_line_bounded_and_every_name_in_the_event(host, count):
    """One message whose attention names `count` roles at the longest role name. Its index line, repeated on every
    part, shows what fits under INDEX_TOKENS and counts the rest; the event's JSON keeps every name, and the event
    takes no more pages than its own size needs at what a page leaves it after the bounded overhead (the page's frame
    and an index line of at most `index_room`), plus one. The header's count is checked first, from the event's first
    page, so an inflated turn fails on its count without being read through."""
    names = longest_roles(count)
    event = message(2, attention=names)
    events = [message(1), event, message(3)]
    _, header = host.start(events)
    first, opening = first_page_holding(host, header, event["source_key"])
    part = next(item for item in opening["events"] if item["source_key"] == event["source_key"])
    empty = {**opening, "events": [{**part, "json": ""}], "index": []}  # the page with no share and no index line
    frame = tool_tokens(json.dumps(empty, sort_keys=True, ensure_ascii=False))
    need = tool_tokens(json.dumps(printed_as_a_page([event])[0], ensure_ascii=False)[1:-1])  # as a part's share prints
    most = math.ceil(need / (page_bound() - frame - index_room())) + 1
    # From its first page on, the turn's pages hold the event's parts and, on at most one page more, the last message.
    assert header["pages"] - first <= most, (
        f"the header counts {header['pages']} pages, so pages {first} to {header['pages'] - 1} at least hold an event "
        f"that needs at most {most}"
    )
    read = read_all(host, header)
    assert_within_the_tools(read)
    assert delivered(read) == printed_as_a_page(events)
    assert json.loads(delivered(read)[1])["message"]["attention"] == names
    holding = [page for _, page in read if any(item["source_key"] == event["source_key"] for item in page["events"])]
    assert len(holding) <= most, f"{len(holding)} pages hold an event that needs at most {most}"
    lines = [line for _, page in read for line in page["index"]]
    assert all(tool_tokens(json.dumps(line, ensure_ascii=False)) <= index_bound() for line in lines)
    for line in (line for line in lines if line.startswith("2/3 ")):
        abridged = ABRIDGED.search(line)
        assert abridged is not None and line.startswith("2/3 pos 2 message from worker attention "), line
        shown = abridged["shown"].split(", ")[:-1]
        assert shown and shown == names[: len(shown)], line
        assert (int(abridged["left"]), int(abridged["count"])) == (count - len(shown), count), line


def test_the_nine_event_window_keeps_the_refusal_the_approval_and_both_requests_in_view(host):
    events = fixtures.nine_events()
    _, header = host.start(events, instructions=fixtures.ATTRIBUTION_POLICY)
    read = read_all(host, header)
    assert delivered(read) == printed_as_a_page(events)
    lines = [line for _, page in read for line in page["index"]]
    assert len(lines) == 9 and len(set(lines)) == 9
    refusal, approval = (lines[index] for index in (fixtures.REFUSAL, fixtures.APPROVAL))
    assert refusal.startswith(f"{fixtures.REFUSAL + 1}/9 ") and approval.startswith(f"{fixtures.APPROVAL + 1}/9 ")
    assert "from operator" in refusal and "from operator" in approval
    assert lines[fixtures.TWO_ACTORS].endswith("attention reviewer, coordinator")


def test_a_summary_over_its_bound_is_refused_with_its_size_and_the_bound(host):
    turn, header = host.start()
    code, out = submit(host, form_of(header), serialize(assessment(header, summary="x" * 5000)))
    assert (code, out["status"], out["detail"]) == (1, "invalid", "summary_bytes")
    assert (out.get("summary_size"), out.get("summary_bytes")) == (5000, 4096), out
    assert isinstance(out.get("seconds_left"), float)
    assert_still_open(host, turn, header)


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
    assert (code, out["status"]) == (1, "not_outstanding")
    assert reply_on_terminal(host, other)["status"] == "not_outstanding"


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
        code, out = host.run("reply", bad, stdin=answer)
        assert (code, out["status"]) == (1, "not_outstanding")
    assert not (host.work / "SENTINEL").exists()
    assert host.run("reply", header["request_id"], stdin=answer) == (0, {"status": "accepted"})
    turn.finish()
    code, out = host.run("reply", header["request_id"], stdin=answer)
    assert (code, out["status"]) == (1, "not_outstanding")
    assert [row["outcome"] for row in host.timing() if row["step"] == "replied"][-1] == "not_outstanding"


def test_guard_a_reply_inside_the_return_margin_is_late_and_a_second_identical_one_is_late_again(host):
    turn, header = host.start(seconds=3)
    answer = serialize(assessment(header)).encode()
    time.sleep(max(0.0, turn.deadline - 0.3 - time.time()))
    first = host.run("reply", header["request_id"], stdin=answer)
    second = host.run("reply", header["request_id"], stdin=answer)
    assert (first[0], first[1]["status"]) == (1, "late") and (second[0], second[1]["status"]) == (1, "late")
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
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_the_form_is_built_only_for_a_request_id(in_process):
    reply_form = getattr(in_process, "reply_form", None)
    assert callable(reply_form), "the bridge builds no reply form"
    for bad in ("thr_x' ; touch PLANTED ; '", "thr_" + "a" * 31, "thr_" + "a" * 32 + "\n"):
        with pytest.raises(in_process.Invalid):
            reply_form(bad)
    assert reply_form("thr_" + "a" * 32)["closing_line"] == "END_thr_" + "a" * 32


def test_read_of_a_finished_turn_or_another_id_is_not_outstanding_and_says_to_go_back_to_receive(host):
    turn, header = host.start()
    answer = serialize(assessment(header)).encode()
    assert host.run("reply", header["request_id"], stdin=answer) == (0, {"status": "accepted"})
    turn.finish()
    for request_id in (header["request_id"], "thr_" + "0" * 32):
        code, out = host.run("read", request_id)
        assert code == 1 and out is not None
        assert out.get("status") == "not_outstanding", out
        assert "receive" in out.get("next", "") and "events" not in out


def test_a_read_and_a_reply_once_the_turns_time_is_gone_are_late_and_say_to_go_back_to_receive(host):
    turn, header = host.start(seconds=3)
    time.sleep(max(0.0, turn.deadline - 0.3 - time.time()))
    code, page = host.run("read", header["request_id"], "--page", "1")
    assert code == 1 and page is not None and page.get("status") == "late", page
    assert "receive" in page.get("next", "") and "events" not in page
    code, out = host.run("reply", header["request_id"], stdin=serialize(assessment(header)).encode())
    assert (code, out["status"]) == (1, "late") and "receive" in out.get("next", "")


def test_a_late_or_not_outstanding_reply_says_the_turn_is_over(host):
    other = "thr_" + "0" * 32
    turn, header = host.start()
    code, out = host.run("reply", other, stdin=serialize(assessment(header)).encode())
    assert out["status"] == "not_outstanding" and "receive" in out.get("next", ""), out


@pytest.mark.parametrize(
    "args",
    [
        ("read", "thr_" + "0" * 32, "--page", "x"),
        ("read", "thr_" + "0" * 32, "--after", "0"),
        ("receive", "--wait", "0"),
    ],
)
def test_a_command_the_bridge_does_not_take_prints_a_usage_status(host, args):
    assert host.run(*args) == (2, {"status": "invalid", "detail": "usage"})


def test_half_of_a_pair_in_an_event_still_gives_the_header_and_its_read_fails_with_a_status(host):
    _, header = host.start([message(1, "a\ud800b")])
    assert header.get("pages") == 1, header
    fault = {"status": "fault", "type": "UnicodeEncodeError"}
    assert host.run("read", header["request_id"], "--page", "1") == (1, fault)


def test_a_fault_carries_a_status(host):
    turn, header = host.start()
    (host.gate / "thread" / "protocol4" / "request.json").write_text("{")
    code, out = host.run("read", header["request_id"])
    assert code == 1 and out is not None and out.get("status") == "fault", out


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


def test_the_guidance_holds_the_cells_the_deadline_and_the_drafting_rule():
    text = GUIDANCE.read_text(encoding="utf-8")
    budget = '// @exec: {"max_output_tokens": 12000, "yield_time_ms": 60000}'
    cells = [(command, tokens) for command, tokens, _, _ in COMMAND_CELL.findall(text)]
    assert cells == [(command, "12000") for command, _ in CELL_JUSTIFICATIONS]
    assert text.count(budget) == 5, "a cell asks the cell layer for no budget"
    assert "max_output_tokens: 12000 });" in text.split("tools.write_stdin(", 1)[1].split("\n", 1)[0]
    assert "max_tokens: 12000" in text
    assert text.count("text(typeof out") == 5 and "console." not in text, "a cell prints other than once"
    assert "TextEncoder" not in text
    for needle in (
        "never measure bytes",
        "Never reread a page",
        "seconds_left",
        "about 44",
        "stop work on it",
        "one clause",
        "A refusal and a later approval are two events",
        "who asked whom",
        "makes two requests: state both",
        "merging only one author's continuing point",
        "approval step stays",
        "never to\n  merge events of different authors",
        "detail `usage`",
        "summary_size",
        "no_such_page",
        '"status": "fault"',
    ):
        assert needle in text, needle


def test_every_command_cell_asks_for_escalation_with_its_own_justification_and_no_placeholder_remains():
    text = GUIDANCE.read_text(encoding="utf-8")
    cells = COMMAND_CELL.findall(text)
    assert text.count("tools.exec_command(") == len(cells), "a command cell lacks the literal sandbox fields"
    assert [permission for _, _, permission, _ in cells] == ["require_escalated"] * len(CELL_JUSTIFICATIONS)
    assert [(command, justification) for command, _, _, justification in cells] == CELL_JUSTIFICATIONS
    assert "SANDBOX" not in text
    for command, justification in (CELL_JUSTIFICATIONS[0], CELL_JUSTIFICATIONS[2]):  # receive and reply
        assert not re.search(r"\d", justification) and "version" not in justification.lower(), command


def render_resume_message(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(RESUME_MESSAGE), *args], capture_output=True, timeout=20, cwd=cwd)


def test_the_resume_message_is_the_bridge_line_a_blank_line_and_the_guidance_bytes(tmp_path):
    command = "python3 -I 'synthetic bridge dir/secretary_bridge.py'"
    done = render_resume_message(tmp_path, command)
    assert done.returncode == 0 and done.stderr == b""
    assert done.stdout == b"BRIDGE: " + command.encode() + b"\n\n" + GUIDANCE.read_bytes()


@pytest.mark.parametrize("command", ["", "python3 -I secretary_bridge.py\nstatus", "python3 -I bridge.py\n"])
def test_the_resume_message_refuses_an_empty_or_multi_line_bridge_command(tmp_path, command):
    done = render_resume_message(tmp_path, command)
    assert done.returncode != 0 and done.stdout == b""
    assert done.stderr.endswith(b"\n") and done.stderr.count(b"\n") == 1 and done.stderr.strip()


def test_the_guidance_covers_the_loop_the_replies_and_the_coverage_rule():
    text = GUIDANCE.read_text(encoding="utf-8")
    for needle in (
        "BRIDGE receive --wait",
        "--page N",
        "reply_form",
        "closing_line",
        "summary_bytes",
        "4096",
        "not coverage",
        "late",
        "not_outstanding",
        "stdin_idle",
        "unpaired_surrogate",
        "unresolved",
        "author",
    ):
        assert needle in text, needle
