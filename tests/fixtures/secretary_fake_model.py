"""Deterministic subprocess transport used by the version 2 secretary integration tests."""

import json
import os
import sys
import time

plan_path, log_path = sys.argv[1:]
request = json.loads(sys.stdin.buffer.read())
with open(log_path, "a", encoding="utf-8") as stream:
    stream.write(json.dumps({**request, "_pid": os.getpid()}) + "\n")
with open(plan_path, encoding="utf-8") as stream:
    plan = json.load(stream)
call = sum(1 for line in open(log_path, encoding="utf-8") if line.strip()) - 1
step = plan.get("steps", [{}])[min(call, len(plan.get("steps", [{}])) - 1)]
time.sleep(step.get("sleep", 0))
if step.get("raw") is not None:
    sys.stdout.write(step["raw"])
else:
    if request.get("task") == "advise_secretary":
        response = step.get("advice", {})
    else:
        events = request["context"]["events"]
        refs = step.get("sources", plan.get("sources", "all"))
        if refs == "all":
            refs = [event["source_key"] for event in events]
        elif refs == "all_context":
            refs = [event["source_key"] for event in [*events, *request["context"].get("reply_context", [])]]
        elif refs == "first":
            refs = [events[0]["source_key"]] if events else []
        response = {"decision": step.get("decision", plan.get("decision", "wake")),
                    "reason": step.get("reason", plan.get("reason", "The incoming context needs attention.")),
                    "summary": step.get("summary", plan.get("summary", "A concise summary of the incoming work.")),
                    "sources": refs}
    sys.stdout.write(json.dumps(response))
sys.stdout.flush()
sys.exit(step.get("exit", 0))
