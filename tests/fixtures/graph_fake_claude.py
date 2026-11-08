"""A synthetic `claude` executable for transport tests: no model, scripted behavior from a mode file.

Usage: python graph_fake_claude.py DUMP_DIR [claude arguments...]. Every launch records argv, environment,
cwd and stdin under DUMP_DIR; the file DUMP_DIR/mode selects the printed result.
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
FLAGS = [
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
    "--bare",
]

if args == ["--version"]:
    print("2.1.295 (Claude Code)" if mode == "old_version" else "2.1.296 (Claude Code)")
    sys.exit(0)
if args == ["--help"]:
    shown = [flag for flag in FLAGS if not (mode == "no_tools_flag" and flag == "--tools")]
    print("Usage: claude [options] [command] [prompt]\n" + "\n".join(f"  {flag} <value>" for flag in shown))
    sys.exit(0)

stdin = sys.stdin.buffer.read().decode("utf-8")
(dump / "call.json").write_text(json.dumps({"argv": args, "env": dict(os.environ), "cwd": os.getcwd(), "stdin": stdin}))
if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    (dump / "descendant.pid").write_text(str(child.pid))
    time.sleep(600)
if mode == "flood":
    while True:
        sys.stdout.write("x" * 4096)
request = json.loads(stdin.split("\n\n", 1)[1])
if "compact_schema" in request:
    structured = {
        "schema_version": 1,
        "graph_digest": request["graph_digest"],
        "body": "Synthetic compact view.",
        "strand_ids": [s["id"] for s in request["strands"]],
        "decision_ids": [d["id"] for d in request["decisions"]],
        "evidence": sorted({ref for item in [*request["strands"], *request["decisions"]] for ref in item["evidence"]}),
    }
else:
    structured = {"echo": request.get("binding", {}).get("request_id", "none")}
result = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 1,
    "structured_output": structured,
    "usage": {"input_tokens": 9, "output_tokens": 4},
    "permission_denials": [],
}
if mode == "denied":
    result["permission_denials"] = [{"tool_name": "Bash"}]
if mode == "turns":
    result["num_turns"] = 3
if mode == "tooluse":
    result["tool_uses"] = [{"name": "Bash", "input": {"command": "id"}}]
if mode == "error":
    result.update(subtype="error_during_execution", is_error=True)
if mode == "prose":
    result.pop("structured_output")
    result["result"] = "plain prose instead of structured output"
print("garbage" if mode == "malformed" else json.dumps(result))
sys.exit(2 if mode == "exit" else 0)
