"""A synthetic `codex` executable for transport tests: no model, scripted behavior from a mode file.

Usage: python graph_fake_codex.py DUMP_DIR [codex arguments...]. Every launch records argv, environment,
cwd and stdin under DUMP_DIR; the file DUMP_DIR/mode selects what `exec` does and DUMP_DIR/version the
reported client version. The `refused` mode prints the event types a real client emits when the provider
refused every request: error events and an error item, a failed turn, no agent item and no usage.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

dump = Path(sys.argv[1])
args = sys.argv[2:]
mode = (dump / "mode").read_text().strip() if (dump / "mode").exists() else "ok"
version = (dump / "version").read_text().strip() if (dump / "version").exists() else "9.8.7"
FEATURES = [
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
]
EXEC_HELP = """Run Codex non-interactively
  -c, --config <key=value>
      --ignore-user-config
      --ephemeral
  -s, --sandbox <SANDBOX_MODE>
      --skip-git-repo-check
  -m, --model <MODEL>
      --json
      --output-schema <FILE>
  -o, --output-last-message <FILE>
  -C, --cd <DIR>
"""

if args == ["--version"]:
    print(f"codex-cli {version}")
    sys.exit(0)
if args == ["exec", "--help"]:
    print(EXEC_HELP.replace("--output-schema <FILE>", "") if mode == "no_schema_flag" else EXEC_HELP)
    sys.exit(0)
if args == ["features", "list"]:
    listed = FEATURES[1:] if mode == "missing_feature" else FEATURES
    print("\n".join(f"{name:40} stable  true" for name in listed))
    sys.exit(0)

stdin = sys.stdin.buffer.read().decode("utf-8")
(dump / "call.json").write_text(json.dumps({"argv": args, "env": dict(os.environ), "cwd": os.getcwd(), "stdin": stdin}))
output = args[args.index("--output-last-message") + 1]


def event(kind, **fields):
    sys.stdout.write(json.dumps({"type": kind, **fields}) + "\n")
    sys.stdout.flush()


event("thread.started", thread_id="t1")
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
candidate = json.loads(stdin.split("\n\n", 1)[1])
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
event("item.completed", item={"type": "agent_message", "text": json.dumps(message)})
event("turn.completed", usage={"input_tokens": 11, "cached_input_tokens": 0, "output_tokens": 5})
sys.exit(3 if mode == "exit" else 0)
