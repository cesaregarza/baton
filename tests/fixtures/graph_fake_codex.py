"""A synthetic `codex` executable for transport tests: no model, scripted behavior from a mode file.

Usage: python graph_fake_codex.py DUMP_DIR [codex arguments...]. The file DUMP_DIR/mode selects the behavior
and DUMP_DIR/version the reported client version. `debug models` prints a catalog whose entry for `m` adds
tools through its tool fields. An `exec` given a base URL override is the launch check: it records itself in
DUMP_DIR/probe.json, sends what a real client would send there (over a WebSocket, a prewarm and then the turn,
or in `post_probe` mode one HTTP POST) and exits. That request offers a tool while the catalog entry still adds
them, or in `offers_tool` mode. Any other `exec` is the call: it records argv, environment, cwd, stdin, the
instructions and the catalog in DUMP_DIR/call.json, writes its session record under CODEX_HOME and prints
events. The `*_record` modes write a record that is empty, metadata only, another thread's or another call's.
The `refused` mode prints the event types a real client emits when the provider refused every request: error
events and an error item, a failed turn, no agent item and no usage.
"""

import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

sys.stdout.reconfigure(encoding="utf-8")  # raw line separators below are written as UTF-8 under any locale
dump = Path(sys.argv[1])
args = sys.argv[2:]
mode = (dump / "mode").read_text().strip() if (dump / "mode").exists() else "ok"
version = (dump / "version").read_text().strip() if (dump / "version").exists() else "9.8.7"
TOOL_FIELDS = {
    "tool_mode": "code_mode_only",
    "multi_agent_version": "v2",
    "experimental_supported_tools": ["clock"],
    "apply_patch_tool_type": "freeform",
}
NO_TOOL_FIELDS = {
    "tool_mode": "direct",
    "multi_agent_version": None,
    "experimental_supported_tools": [],
    "apply_patch_tool_type": None,
}

if args == ["--version"]:
    print(f"codex-cli {version}")
    sys.exit(0)
if args == ["debug", "models"]:
    slugs = ["other"] if mode == "no_model_entry" else ["m", "other"]
    print(json.dumps({"models": [{"slug": s, "base_instructions": "Built-in.", **TOOL_FIELDS} for s in slugs]}))
    sys.exit(0)

overrides = dict(args[i + 1].split("=", 1) for i, part in enumerate(args) if part == "-c")
stdin = sys.stdin.buffer.read().decode("utf-8")
instructions_file = json.loads(overrides.get("model_instructions_file", "null"))
instructions = Path(instructions_file).read_text(encoding="utf-8") if instructions_file else None
catalog_file = json.loads(overrides.get("model_catalog_json", "null"))
catalog = json.loads(Path(catalog_file).read_text())["models"] if catalog_file else [{}]
base_url = next((json.loads(value) for key, value in overrides.items() if key.endswith("_base_url")), None)


def frame(message):
    """One masked client text frame holding `message` as JSON."""
    data, mask = json.dumps(message).encode(), os.urandom(4)
    size = len(data)
    if size < 126:
        length = bytes([0x80 | size])
    elif size < 1 << 16:
        length = bytes([0x80 | 126]) + size.to_bytes(2, "big")
    else:
        length = bytes([0x80 | 127]) + size.to_bytes(8, "big")
    return bytes([0x81]) + length + mask + bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))


def websocket(url, messages, headers):
    """Send each message as a text frame, waiting after a prewarm for its completed response."""
    parts = urlsplit(url)
    key = base64.b64encode(os.urandom(16)).decode()
    lines = [f"GET {parts.path} HTTP/1.1", f"Host: {parts.hostname}:{parts.port}", "Upgrade: websocket"]
    lines += ["Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
    lines += [f"{name}: {value}" for name, value in headers.items()]
    with socket.create_connection((parts.hostname, parts.port), timeout=10) as connection:
        connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        reader = connection.makefile("rb")
        if b" 101 " not in reader.readline():
            return
        while reader.readline() not in (b"\r\n", b""):
            pass
        for message in messages:
            connection.sendall(frame(message))
            while message.get("generate") is False:
                head = reader.read(2)
                size = head[1] & 0x7F if len(head) == 2 else 0
                if size >= 126:
                    size = int.from_bytes(reader.read(2 if size == 126 else 8), "big")
                if len(head) < 2 or b"response.completed" in reader.read(size):
                    break


if base_url is not None:  # the launch check: send what a real client would, then stop
    developer = {"type": "input_text", "text": instructions.strip() if instructions else "Built-in."}
    user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": stdin}]}
    turn = {
        "type": "response.create",
        "model": args[args.index("--model") + 1],
        "stream": True,
        "store": False,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": {"effort": "low"},
        "input": [{"type": "message", "role": "developer", "content": [developer]}, user],
    }
    if mode == "offers_tool" or any(catalog[0].get(key) != value for key, value in NO_TOOL_FIELDS.items()):
        turn["tools"] = [{"type": "function", "name": "exec"}]
    headers = {"Authorization": "Bearer synthetic"} if mode == "probe_credential" else {}
    (dump / "probe.json").write_text(json.dumps({"argv": args, "env": dict(os.environ), "stdin": stdin}))
    try:
        if mode == "post_probe":
            post = urllib.request.Request(f"{base_url}/responses", json.dumps(turn).encode(), headers, method="POST")
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(post, timeout=10)
        else:
            prewarm = {**turn, "generate": False, "input": turn["input"][:1]}
            websocket(base_url.replace("http://", "ws://") + "/responses", [prewarm, turn], headers)
    except OSError:
        pass  # the capture answers every turn with an error
    sys.exit(1)

(dump / "call.json").write_text(
    json.dumps(
        {
            "argv": args,
            "env": dict(os.environ),
            "cwd": os.getcwd(),
            "stdin": stdin,
            "instructions": instructions,
            "catalog": catalog,
        }
    )
)
output = args[args.index("--output-last-message") + 1]
thread_id = str(uuid.uuid4())


def event(kind, **fields):
    sys.stdout.write(json.dumps({"type": kind, **fields}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def entry(kind, **payload):
    return json.dumps({"timestamp": "2026-11-13T00:00:00Z", "type": kind, "payload": payload})


if mode != "no_record":  # the client's own record of the turn, shaped like the installed client's
    name = f"rollout-2026-11-13T00-00-00-{thread_id}.jsonl"
    record = Path(os.environ["CODEX_HOME"], "sessions", "2026", "11", "13", name)
    record.parent.mkdir(parents=True, exist_ok=True)
    session = str(uuid.uuid4()) if mode == "other_thread_record" else thread_id
    user = "another call's content" if mode == "other_content_record" else stdin
    entries = [
        entry("session_meta", id=session, cli_version=version),
        entry("event_msg", type="task_started", turn_id="turn-1"),
        entry("turn_context", cwd=os.getcwd()),
        entry("response_item", type="message", role="user", content=[{"type": "input_text", "text": user}]),
        entry("event_msg", type="item_completed", item={"type": "UserMessage", "id": "u1"}),
        entry("response_item", type="reasoning", summary=[]),
        entry("event_msg", type="item_completed", item={"type": "Reasoning", "id": "r1"}),
    ]
    if mode == "record_tool":  # a code-mode call: it shows here, never in the event stream
        entries.append(entry("response_item", type="custom_tool_call", name="exec", input="clock()", call_id="c1"))
    if mode not in ("refused", "error", "failed_action"):  # a failed turn records no answer and no completion
        entries.append(entry("response_item", type="message", role="assistant", content=[]))
        entries.append(entry("event_msg", type="task_complete", turn_id="turn-1"))
    if mode == "meta_only_record":
        entries = entries[:3]
    record.write_text("" if mode == "empty_record" else "\n".join(entries) + "\n", encoding="utf-8")

event("thread.started", thread_id=thread_id)
event("turn.started")
if mode == "refused":
    for attempt in (1, 2):
        event("error", message=f"Reconnecting... {attempt}/2 (synthetic refusal)")
    event("item.completed", item={"id": "item_0", "type": "error", "message": "synthetic transport fallback"})
    event("turn.failed", error={"message": "synthetic refusal"})
    sys.exit(1)
if mode == "garbage_exit":
    sys.stdout.write("not an event\n")
    sys.exit(1)
if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    (dump / "descendant.pid").write_text(str(child.pid))
    time.sleep(600)
if mode == "flood":
    while True:
        sys.stdout.write(
            json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "x" * 1000}}) + "\n"
        )
if mode in ("action", "failed_action"):
    event("item.completed", item={"type": "command_execution", "command": "cat /etc/passwd"})
if mode == "mcp":
    event("item.started", item={"type": "mcp_tool_call", "server": "baton", "tool": "read_history"})
if mode == "unknown":
    event("item.completed", item={"type": "web_search", "query": "x"})
if mode in ("error", "failed_action"):
    event("error", message="provider rejected the request")
event("item.completed", item={"type": "reasoning", "text": "thinking"})
candidate = json.loads(stdin)
if "compact_schema" in candidate:
    message = {
        "schema_version": 1,
        "graph_digest": candidate["graph_digest"],
        "body": "Synthetic compact view.",
        "strand_ids": [s["id"] for s in candidate["strands"]],
        "decision_ids": [d["id"] for d in candidate["decisions"]],
        "evidence": sorted(
            {ref for item in [*candidate["strands"], *candidate["decisions"]] for ref in item["evidence"]}
        ),
    }
else:
    message = {"echo": candidate.get("binding", {}).get("request_id", "none")}
if mode == "malformed":
    Path(output).write_text("not json")
elif mode == "bigfile":  # a quiet stdout with an oversized last-message file
    Path(output).write_text(json.dumps({**message, "padding": "x" * 150_000}))
else:
    Path(output).write_text(json.dumps(message))
if mode == "line_separators":  # JSON allows both raw inside a string
    event("item.completed", item={"type": "agent_message", "text": "one\u2028two\u0085three"})
event("item.completed", item={"type": "agent_message", "text": json.dumps(message)})
event("turn.completed", usage={"input_tokens": 11, "cached_input_tokens": 0, "output_tokens": 5})
sys.exit(3 if mode == "exit" else 0)
