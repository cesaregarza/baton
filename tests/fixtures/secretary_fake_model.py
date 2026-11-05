"""Deterministic subprocess transport used by the secretary empirical tests."""

import json
import os
import sys
import time

plan_path, log_path, *mode_arg = sys.argv[1:]
engine_mode = mode_arg == ["engine"]
request = json.loads(sys.stdin.buffer.read())
if engine_mode:
    context = request.get("context", {})
    entry = {**request, "context": context, "_pid": os.getpid()}
else:
    items = request.get("input", [])
    principal = json.loads(items[2]["content"][0]["text"].removeprefix("Principal-specific guidance:\n"))
    context = json.loads(items[3]["content"][0]["text"])
    entry = {
        **request,
        "context": {
            **context,
            **principal,
            "knowledge": items[1]["content"][0]["text"].removeprefix("Contextual knowledge:\n"),
        },
        "_pid": os.getpid(),
    }
with open(log_path, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(entry) + "\n")
with open(plan_path, encoding="utf-8") as stream:
    plan = json.load(stream)
call = sum(1 for line in open(log_path, encoding="utf-8") if line.strip()) - 1
step = plan.get("steps", [{}])[min(call, len(plan.get("steps", [{}])) - 1)]
time.sleep(step.get("sleep", 0))
if step.get("raw") is not None:
    sys.stdout.write(step["raw"])
elif engine_mode:
    response = {"decision": step.get("decision", plan.get("engine_decision", "wake"))}
    sys.stdout.write(json.dumps(response))
elif step.get("tool_calls") is not None:
    sys.stdout.write(json.dumps({"tool_calls": step["tool_calls"]}))
else:
    events = context["events"]
    refs = step.get("sources", plan.get("sources", "all"))
    if refs == "all":
        refs = [event["source_key"] for event in events]
    elif refs == "all_context":
        refs = [event["source_key"] for event in [*events, *context.get("reply_context", [])]]
    elif refs == "first":
        refs = [events[0]["source_key"]] if events else []
    response = {
        "decision": step.get("decision", plan.get("decision", "wake")),
        "reason": step.get("reason", plan.get("reason", "The incoming context needs attention.")),
        "summary": step.get("summary", plan.get("summary", "A concise summary of the incoming work.")),
        "sources": refs,
    }
    sys.stdout.write(json.dumps(response))
sys.stdout.flush()
sys.exit(step.get("exit", 0))
