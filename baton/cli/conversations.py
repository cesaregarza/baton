"""Conversation commands."""

from __future__ import annotations

import argparse
from typing import Any

from baton import conversations
from baton.agents import Agent
from baton.agents.thread import Thread
from baton.cli.common import db, with_role
from baton.config import Config


def add_commands(commands: Any) -> None:
    conversation = commands.add_parser("conversation", help="group conversations").add_subparsers(
        dest="conversation_command", required=True
    )
    new = with_role(conversation.add_parser("new", help="open a conversation; you must be a participant"))
    new.add_argument("--title", required=True)
    new.add_argument("--with", dest="participants", required=True, help="comma-separated roles, yourself included")
    new.add_argument("--topic", help="an optional readable path, such as project or project/subtopic")
    new.add_argument("--request-id")
    new.set_defaults(
        handler=lambda config, args: db(
            config,
            lambda conn: Agent(conn, config, args.role).create_thread(
                args.title, participants=args.participants.split(","), topic=args.topic, request_id=args.request_id
            ),
        )
    )

    listing = with_role(conversation.add_parser("list", help="the conversations your role takes part in"))
    listing.add_argument("--topic", help="only this topic and what is under it")
    listing.set_defaults(handler=list_conversations)

    topic = with_role(conversation.add_parser("topic", help="set, rename or clear a conversation's topic"))
    topic.add_argument("conversation_id")
    change = topic.add_mutually_exclusive_group(required=True)
    change.add_argument("--set", dest="topic", help="the new topic, 1-2 lowercase slug segments")
    change.add_argument("--clear", action="store_true", help="remove the topic")
    topic.set_defaults(
        handler=lambda config, args: db(
            config,
            lambda conn: (
                Agent(conn, config, args.role)
                .thread(args.conversation_id)
                .set_topic(None if args.clear else args.topic)
            ),
        )
    )

    history = with_role(conversation.add_parser("history", help="a conversation as you saw it; marks nothing read"))
    history.add_argument("conversation_id")
    history.add_argument("--after-seq", type=int, default=0)
    history.add_argument("--limit", type=int, default=50)
    history.set_defaults(handler=read_history)

    context = with_role(
        conversation.add_parser("context", help="an authorized summary and raw tail; marks nothing read")
    )
    context.add_argument("conversation_id")
    context.add_argument("--producer")
    context.add_argument("--limit", type=int, default=50)
    context.add_argument("--after-seq", type=int, help="continuation only: use with --through-seq")
    context.add_argument("--through-seq", type=int, help="fixed head from continuation; use with --after-seq")
    context.set_defaults(handler=read_context)

    for name, operation in (("add", Thread.add_participant), ("remove", Thread.remove_participant)):
        change = with_role(conversation.add_parser(name, help=f"{name} a participant"))
        change.add_argument("conversation_id")
        change.add_argument("participant")
        change.set_defaults(
            handler=lambda config, args, operation=operation: db(
                config,
                lambda conn: operation(Agent(conn, config, args.role).thread(args.conversation_id), args.participant),
            )
        )


def list_conversations(config: Config, args: argparse.Namespace) -> list[dict[str, Any]]:
    conversations.require_enabled(config)
    return db(config, lambda conn: conversations.list_readable(conn, config, args.role, args.topic))


def read_history(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    conversations.require_enabled(config)
    return db(
        config,
        lambda conn: Agent(conn, config, args.role).thread(args.conversation_id).read(args.after_seq, args.limit),
    )


def read_context(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    return db(
        config,
        lambda conn: (
            Agent(conn, config, args.role)
            .thread(args.conversation_id)
            .read_context(
                producer=args.producer, limit=args.limit, after_seq=args.after_seq, through_seq=args.through_seq
            )
        ),
    )
