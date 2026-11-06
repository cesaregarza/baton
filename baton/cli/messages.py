"""Message send, inbox read and wait commands."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from baton.agents import Agent
from baton.cli.common import db, with_role
from baton.config import Config
from baton.errors import InvalidRequest


def add_commands(commands: Any) -> None:
    message = with_role(commands.add_parser("message", help="send a message to a role or a conversation"))
    target = message.add_mutually_exclusive_group()
    target.add_argument("--to")
    target.add_argument("--conversation", dest="conversation_id", help="every other participant of a conversation")
    message.add_argument("--text", help="the message; omit to read it from stdin")
    message.add_argument("--job", dest="job_id", help="the job it is about")
    message.add_argument(
        "--relayed-json",
        help="words passed on from someone else: a JSON list of {source, at, text}, kept apart from --text",
    )
    message.add_argument("--reply-to", help="in a conversation: the message_id this one answers")
    message.add_argument("--attention", help="in a conversation: comma-separated participants to ask to look")
    message.add_argument(
        "--needs-decision",
        action="store_true",
        help="in a conversation: ask for a decision. Attention only, never an approval",
    )
    message.add_argument("--request-id", help="reuse a printed request_id to retry safely")
    message.set_defaults(handler=send)

    read = with_role(commands.add_parser("read", help="read messages sent to your role"))
    read.add_argument("--after-seq", type=int, help="default: only messages not yet delivered")
    read.add_argument("--limit", type=int, default=50)
    read.set_defaults(
        handler=lambda config, args: db(
            config, lambda conn: Agent(conn, config, args.role).inbox.read(args.after_seq, args.limit)
        )
    )

    wait = with_role(commands.add_parser("wait", help="block until messages or job changes reach your role"))
    wait.add_argument("--timeout", type=float, help="seconds; default: wait indefinitely")
    wait.set_defaults(
        handler=lambda config, args: db(
            config, lambda conn: Agent(conn, config, args.role).inbox.wait(timeout_seconds=args.timeout)
        )
    )


def attention(args: argparse.Namespace) -> list[str] | None:
    """`--attention a,b` as the list the host expects, or None when it was left out."""
    return args.attention.split(",") if args.attention is not None else None


def message_arguments(args: argparse.Namespace) -> dict[str, Any]:
    """Normalize message input once for local and remote sends."""
    try:
        relayed = json.loads(args.relayed_json) if args.relayed_json is not None else None
    except json.JSONDecodeError:
        raise InvalidRequest("--relayed-json must be JSON: a list of {source, at, text} entries") from None
    return {
        "text": args.text if args.text is not None else sys.stdin.read(),
        "job_id": args.job_id,
        "relayed": relayed,
        "request_id": args.request_id,
        "reply_to": args.reply_to,
        "attention": attention(args),
        "needs_decision": args.needs_decision,
    }


def send(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    if args.to is None and args.conversation_id is None:
        raise InvalidRequest("name a recipient with --to or a conversation with --conversation")
    if args.to is not None:
        config.require_role(args.to)
    message_args = message_arguments(args)
    return db(
        config,
        lambda conn: Agent(conn, config, args.role).send(
            to=args.to, conversation_id=args.conversation_id, **message_args
        ),
    )
