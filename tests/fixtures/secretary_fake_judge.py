"""A stand-in judge command for the secretary tests: never a model, and never anything but test use.

It appends each request it is given to a log, then answers from a plan file: the first step whose `when` occurs in
the message text decides. A step may sleep, print raw output, exit with a code, or reply with a verdict whose
`item` is an index into the items it was offered.
"""

import json
import sys
import time

plan_path, log_path = sys.argv[1], sys.argv[2]
request = json.loads(sys.stdin.buffer.read())
with open(log_path, "a", encoding="utf-8") as log:
    log.write(json.dumps(request) + "\n")
with open(plan_path, encoding="utf-8") as source:
    plan = json.load(source)
step = next((step for step in plan if step.get("when", "") in request["message"]["text"]), {})
time.sleep(step.get("sleep", 0))
reply = step.get("reply", {"relation": "none", "item": None, "opens_owed": 0.0, "needs_now": 0.0})
if isinstance(reply.get("item"), int):
    reply = {**reply, "item": request["items"][reply["item"]]["id"]}
sys.stdout.write(step["raw"] if "raw" in step else json.dumps(reply))
sys.stdout.flush()
sys.exit(step.get("exit", 0))
