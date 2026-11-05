"""Host-only offline attention record commands."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from baton.config import Config


def add_commands(commands: Any) -> None:
    group = commands.add_parser("secretary", help="scan, replay and inspect offline attention records")
    group.add_argument("--state", type=Path, help="private SQLite state file (every action but read)")
    group.add_argument("--policy", type=Path, required=True, help="versioned JSON relationship and rules")
    actions = group.add_subparsers(dest="secretary_command", required=True)
    scan = actions.add_parser("scan", help="read a bounded page of Baton events without advancing Baton cursors")
    scan.add_argument("--limit", type=int, default=100)
    scan.set_defaults(handler=lambda config, args: run(config, args, "scan"))
    actions.add_parser("replay", help="compare a recomputation without changing saved records").set_defaults(
        handler=lambda config, args: run(config, args, "replay"))
    show = actions.add_parser("show", help="inspect one principal and render visible source excerpts")
    show.add_argument("--source", dest="source_key")
    show.set_defaults(handler=lambda config, args: run(config, args, "show"))
    cancel = actions.add_parser("cancel", help="explicitly cancel an expectation with a reason")
    cancel.add_argument("expectation_id")
    cancel.add_argument("--reason", required=True)
    cancel.set_defaults(handler=lambda config, args: run(config, args, "cancel"))
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
