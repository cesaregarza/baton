"""Host-local model secretary and legacy attention records."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from baton.config import Config


def add_commands(commands: Any) -> None:
    group = commands.add_parser("secretary", help="triage an inbox with a lightweight model and inspect its summaries")
    group.add_argument("--state", type=Path, help="private SQLite state file (every action but read)")
    group.add_argument("--policy", type=Path, required=True, help="versioned secretary model policy")
    actions = group.add_subparsers(dest="secretary_command", required=True)
    scan = actions.add_parser("scan", help="version-1 compatibility catch-up (version 2 needs no scan)")
    scan.add_argument("--limit", type=int, default=100)
    scan.set_defaults(handler=lambda config, args: run(config, args, "scan"))
    actions.add_parser("replay", help="inspect saved assessments, or recompute legacy rules").set_defaults(
        handler=lambda config, args: run(config, args, "replay"))
    show = actions.add_parser("show", help="inspect current-visible assessments or legacy source records")
    show.add_argument("--source", dest="source_key")
    show.set_defaults(handler=lambda config, args: run(config, args, "show"))
    cancel = actions.add_parser("cancel", help="explicitly cancel an expectation with a reason")
    cancel.add_argument("expectation_id")
    cancel.add_argument("--reason", required=True)
    cancel.set_defaults(handler=lambda config, args: run(config, args, "cancel"))
    acknowledge = actions.add_parser("acknowledge", help="record successful delivery of a version-2 summary")
    acknowledge.add_argument("decision_id")
    acknowledge.set_defaults(handler=lambda config, args: run(config, args, "acknowledge"))
    from baton.agents.secretary import decide as secretary_decide
    from baton.agents.secretary import read as secretary_read

    decide = actions.add_parser("decide", help="decide wake, hold or keep for one wake, or digest for the hour")
    secretary_decide.add_arguments(decide)
    decide.set_defaults(handler=lambda config, args: run(config, args, "decide"))
    read = actions.add_parser("read", help="what the principal was delivered, text included: the thread's read")
    secretary_read.add_arguments(read)
    read.set_defaults(handler=lambda config, args: run(config, args, "read"))


def run(config: Config, args: argparse.Namespace, action: str) -> dict[str, Any]:
    from baton.agents import secretary
    from baton.agents.secretary import decide as secretary_decide
    from baton.agents.secretary import read as secretary_read

    if action == "acknowledge":
        import asyncio
        service = secretary.Secretary(config, args.state, args.policy)
        return {"since": asyncio.run(service.acknowledge(args.decision_id))}
    if action == "read":
        return secretary_read.read(config, args.policy, **secretary_read.options(args))
    if action == "decide":
        return secretary_decide.decide(config, args.state, args.policy, **secretary_decide.options(args))
    if action == "scan":
        return secretary.scan(config, args.state, args.policy, limit=args.limit)
    if action == "replay":
        return secretary.replay(config, args.state, args.policy)
    if action == "show":
        return secretary.show(config, args.state, args.policy, source_key=args.source_key)
    return secretary.cancel(config, args.state, args.policy, args.expectation_id, args.reason)
