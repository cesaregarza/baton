"""Host-local model secretary: decide, inspect, acknowledge, and read."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from baton.config import Config, ConfigError


def add_commands(commands: Any) -> None:
    group = commands.add_parser("secretary", help="triage an inbox with a lightweight model and inspect its summaries")
    group.add_argument("--state", type=Path, help="private SQLite state file (not required for read or summaries)")
    group.add_argument("--policy", type=Path, help="versioned secretary model policy")
    actions = group.add_subparsers(dest="secretary_command", required=True)
    show = actions.add_parser("show", help="inspect saved assessments whose sources remain visible")
    show.add_argument("--source", dest="source_key")
    show.set_defaults(handler=lambda config, args: run(config, args, "show"))
    summaries = actions.add_parser("summaries", help="page archived deliverable summaries for this principal")
    summaries.add_argument("--after", type=int, default=0, help="resume after this archive cursor")
    summaries.add_argument("--limit", type=int, default=50, help="page size from 1 to 100")
    summaries.set_defaults(handler=lambda config, args: run(config, args, "summaries"))
    acknowledge = actions.add_parser("acknowledge", help="record successful delivery of a saved summary")
    acknowledge.add_argument("decision_id")
    acknowledge.set_defaults(handler=lambda config, args: run(config, args, "acknowledge"))
    from baton.agents.secretary.attention import decide as secretary_decide
    from baton.agents.secretary.inbox import read as secretary_read

    decide = actions.add_parser("decide", help="decide wake, hold or keep for one wake, or digest for the hour")
    secretary_decide.add_arguments(decide)
    decide.set_defaults(handler=lambda config, args: run(config, args, "decide"))
    read = actions.add_parser("read", help="what the principal was delivered, text included: the thread's read")
    secretary_read.add_arguments(read)
    read.set_defaults(handler=lambda config, args: run(config, args, "read"))


def run(config: Config, args: argparse.Namespace, action: str) -> dict[str, Any]:
    from baton.agents import secretary
    from baton.agents.secretary.attention import decide as secretary_decide
    from baton.agents.secretary.inbox import read as secretary_read

    if args.policy is None:
        raise ConfigError("the following arguments are required: --policy")

    if action == "acknowledge":
        import asyncio

        service = secretary.Secretary(config, args.state, args.policy)
        return {"since": asyncio.run(service.acknowledge(args.decision_id))}
    if action == "read":
        return secretary_read.read(config, args.policy, **secretary_read.options(args))
    if action == "summaries":
        from baton.agents.secretary.attention.archive import SummaryArchive
        from baton.config.secretary import load_policy

        policy = load_policy(config, args.policy)
        return SummaryArchive(config).page_for_host(policy.principal, after=args.after, limit=args.limit)
    if action == "decide":
        return secretary_decide.decide(config, args.state, args.policy, **secretary_decide.options(args))
    return secretary.show(config, args.state, args.policy, source_key=args.source_key)
