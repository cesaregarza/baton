"""Dispatch the commands supported by a remote Baton host."""

from __future__ import annotations

import argparse
import time
from typing import Any

from baton import remote
from baton.cli import messages
from baton.config import ConfigError
from baton.errors import InvalidRequest


def dispatch(args: argparse.Namespace) -> Any:
    """Run a message, read, wait or conversation command through the host."""
    if getattr(args, "role", None):
        raise ConfigError("--as has no effect with --host: the token decides the role")
    command = " ".join(filter(None, [args.command, getattr(args, "conversation_command", None)]))
    if command == "message":
        if (args.to is None) == (args.conversation_id is None):
            raise InvalidRequest("name a recipient with --to or a conversation with --conversation")
        arguments = {**messages.message_arguments(args), "to": args.to, "conversation_id": args.conversation_id}
        return call(args, "send_message", arguments)
    if command == "read":
        return call(args, "read_messages", {"after_seq": args.after_seq, "limit": args.limit})
    if command == "wait":
        return wait(args)
    if command == "conversation new":
        return call(
            args,
            "create_conversation",
            {
                "title": args.title,
                "participants": args.participants.split(","),
                "topic": args.topic,
                "request_id": args.request_id,
            },
        )
    if command == "conversation list":
        return conversation_list_result(call(args, "list_conversations", {"topic": args.topic}))
    if command == "conversation topic":
        return call(
            args, "set_topic", {"conversation_id": args.conversation_id, "topic": None if args.clear else args.topic}
        )
    if command == "conversation history":
        return call(
            args,
            "read_history",
            {"conversation_id": args.conversation_id, "after_seq": args.after_seq, "limit": args.limit},
        )
    raise ConfigError(
        f"`baton {command}` runs on the host only; with --host use message, read, wait, "
        "conversation new, conversation list, conversation topic or conversation history"
    )


def wait(args: argparse.Namespace) -> Any:
    """Wait in rounds the host allows, until something arrives or `--timeout` passes."""
    from baton.app import MAX_WAIT_SECONDS

    deadline = None if args.timeout is None else time.monotonic() + max(0.0, args.timeout)
    while True:
        left = MAX_WAIT_SECONDS if deadline is None else max(0.0, min(MAX_WAIT_SECONDS, deadline - time.monotonic()))
        taken = call(args, "wait", {"timeout_seconds": left})
        if taken["events"] or (deadline is not None and time.monotonic() >= deadline):
            return taken


def call(args: argparse.Namespace, tool: str, arguments: dict[str, Any]) -> Any:
    return remote.call(args.host, remote.token(), tool, arguments)


def conversation_list_result(result: Any) -> Any:
    """Keep the CLI's bare-list output across hosts with either list response shape."""
    if isinstance(result, dict) and isinstance(result.get("conversations"), list):
        return result["conversations"]
    return result
