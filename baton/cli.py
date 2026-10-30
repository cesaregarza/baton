"""The `baton` command: run the host, and let local agents message, wait and move jobs.

Local commands open the host's database directly, so they are for agents on
the host machine; `--as` (or `$BATON_ROLE`) names the role to act as, and a
human role is never available that way. With `--host URL` (or `$BATON_HOST`)
the same message, read, wait and conversation commands go through that host's
MCP server instead, as the role of the token in `$BATON_TOKEN` (see
`baton.remote`); that is how a person takes part. Every command prints JSON on
stdout; a refusal prints `{"error": <code>, "detail": ...}` and exits 1, and
a configuration problem exits 2.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import time
from typing import Any

from baton import changes, conversations, datadir, inbox, jobs, messages, remote, wakes
from baton import config as config_module
from baton.config import Config, ConfigError
from baton.errors import BatonError, InvalidRequest
from baton.store import connect, connect_read_only


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "hash-passphrase":
        return _hash_passphrase()
    try:
        if args.host:
            result = _remote(args)
            _print(result)
            return 0
        config = config_module.load(args.config)
        os.umask(0o077)  # SQLite's own files, and anything else created, stay private
        datadir.check(config.data_dir)
        if hasattr(args, "role"):  # a command that acts as a role
            args.role = args.role or os.environ.get("BATON_ROLE")
            if not args.role:
                raise ConfigError("name a role with --as or $BATON_ROLE")
            if config.require_role(args.role).display_only:
                raise ConfigError(f"{args.role} is display_only: it only displays, so nothing acts as it")
            if config.require_role(args.role).human:
                raise ConfigError(f"{args.role} is a human role: it acts only with its own token, through "
                                  f"`baton --host URL` with ${remote.TOKEN_ENV}, never with --as")
        if args.command == "serve":
            from baton.app import serve  # lazily: the MCP SDK is slow to import for one-shot commands

            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
            logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every URL; a webhook's can hold a token
            serve(config)
            return 0
        result = args.handler(config, args)
    except ConfigError as error:
        _print({"error": getattr(error, "code", "config"), "detail": str(error)})
        return 2
    except BatonError as error:
        _print({"error": error.code, "detail": str(error), **error.extra})
        return 1
    _print(result)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baton", description="A small MCP host for agent messages and jobs.")
    parser.add_argument("--config", help=f"config file (default: ${config_module.CONFIG_ENV} or "
                                         f"{config_module.DEFAULT_CONFIG})")
    parser.add_argument("--host", default=os.environ.get(remote.HOST_ENV),
                        help=f"act through this host's URL with the token in ${remote.TOKEN_ENV} or the command in "
                             f"${remote.TOKEN_COMMAND_ENV}, instead of a local database (default: ${remote.HOST_ENV})")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the MCP server and the wake dispatcher")

    message = _with_role(commands.add_parser("message", help="send a message to a role or a conversation"))
    target = message.add_mutually_exclusive_group()
    target.add_argument("--to")
    target.add_argument("--conversation", dest="conversation_id", help="every other participant of a conversation")
    message.add_argument("--text", help="the message; omit to read it from stdin")
    message.add_argument("--job", dest="job_id", help="the job it is about")
    message.add_argument("--relayed-json", help="words passed on from someone else: a JSON list of "
                                                "{source, at, text}, kept apart from --text")
    message.add_argument("--reply-to", help="in a conversation: the message_id this one answers")
    message.add_argument("--attention", help="in a conversation: comma-separated participants to ask to look")
    message.add_argument("--needs-decision", action="store_true",
                         help="in a conversation: ask for a decision. Attention only, never an approval")
    message.add_argument("--request-id", help="reuse a printed request_id to retry safely")
    message.set_defaults(handler=_message)

    read = _with_role(commands.add_parser("read", help="read messages sent to your role"))
    read.add_argument("--after-seq", type=int, help="default: only messages not yet delivered")
    read.add_argument("--limit", type=int, default=50)
    read.set_defaults(handler=lambda config, args: _db(config, lambda conn: messages.read(
        conn, args.role, args.after_seq, args.limit)))

    wait = _with_role(commands.add_parser("wait", help="block until messages or job changes reach your role"))
    wait.add_argument("--timeout", type=float, help="seconds; default: wait indefinitely")
    wait.set_defaults(handler=lambda config, args: _db(config, lambda conn: inbox.wait(
        conn, args.role, timeout_seconds=args.timeout)))

    _add_job_commands(commands)
    _add_conversation_commands(commands)

    token = commands.add_parser("token", help="bearer tokens").add_subparsers(dest="token_command", required=True)
    new = token.add_parser("new", help="make a token for a role; prints it once, with the config entry")
    new.add_argument("--name", required=True)
    new.add_argument("--role", dest="token_role", required=True)
    new.set_defaults(handler=_new_token)
    revoke = token.add_parser("revoke", help="end a configured token from its next call on, with no restart")
    revoke.add_argument("name", help="the token's auth.tokens name")
    revoke.set_defaults(handler=lambda config, args: _revoke_token(config, args.name))

    seat_commands = commands.add_parser("seat", help="the browser seat").add_subparsers(dest="seat_command",
                                                                                       required=True)
    seat_commands.add_parser("logout-all", help="end every browser seat session at once").set_defaults(
        handler=_seat_logout_all)

    commands.add_parser("hash-passphrase", help="hash an OAuth consent passphrase for the config")
    oauth = commands.add_parser("oauth", help="OAuth grants").add_subparsers(dest="oauth_command", required=True)
    oauth.add_parser("revoke-all", help="end every OAuth access and refresh grant").set_defaults(handler=_revoke_all)
    _add_index_commands(commands)
    commands.add_parser("wakes", help="show recent wakes and how they ended").set_defaults(
        handler=lambda config, args: _db(config, wakes.recent))
    return parser


def _add_job_commands(commands: Any) -> None:
    job = commands.add_parser("job", help="create, show, list and move jobs").add_subparsers(dest="job_command",
                                                                                          required=True)
    create = _with_role(job.add_parser("create"))
    create.add_argument("--title", required=True)
    create.add_argument("--body", default="")
    create.add_argument("--request-id")
    create.set_defaults(handler=lambda config, args: _db(config, lambda conn: jobs.create(
        conn, config.jobs, request_id=args.request_id or messages.new_request_id(), actor=args.role,
        title=args.title, body=args.body)))

    show = job.add_parser("show")
    show.add_argument("job_id")
    show.set_defaults(handler=lambda config, args: _db(config, lambda conn: jobs.get(conn, args.job_id)))

    listing = job.add_parser("list")
    listing.add_argument("--state")
    listing.add_argument("--waiting-on")
    listing.set_defaults(handler=lambda config, args: _db(config, lambda conn: jobs.list_jobs(
        conn, state=args.state, waiting_on=args.waiting_on)))

    move = _with_role(job.add_parser("move"))
    move.add_argument("job_id")
    move.add_argument("to_state")
    move.add_argument("--note", default="")
    move.add_argument("--request-id", help="reuse a printed request_id to retry safely")
    move.add_argument("--expected-revision", type=int, help="refuse if the job changed since this revision")
    move.set_defaults(handler=lambda config, args: _db(config, lambda conn: jobs.move(
        conn, config.jobs, args.job_id, args.to_state, actor=args.role, note=args.note,
        request_id=args.request_id, expected_revision=args.expected_revision)))

    history = job.add_parser("events")
    history.add_argument("job_id")
    history.add_argument("--after-seq", type=int, default=0)
    history.set_defaults(handler=lambda config, args: _db(config, lambda conn: jobs.read_events(
        conn, args.job_id, args.after_seq)))


def _add_conversation_commands(commands: Any) -> None:
    conversation = commands.add_parser("conversation", help="group conversations").add_subparsers(
        dest="conversation_command", required=True)
    new = _with_role(conversation.add_parser("new", help="open a conversation; you must be a participant"))
    new.add_argument("--title", required=True)
    new.add_argument("--with", dest="participants", required=True, help="comma-separated roles, yourself included")
    new.add_argument("--topic", help="an optional readable path, such as project or project/subtopic")
    new.add_argument("--request-id")
    new.set_defaults(handler=lambda config, args: _db(config, lambda conn: conversations.create(
        conn, config, actor=args.role, title=args.title, participants=args.participants.split(","),
        topic=args.topic, request_id=args.request_id)))

    listing = _with_role(conversation.add_parser("list", help="the conversations your role takes part in"))
    listing.add_argument("--topic", help="only this topic and what is under it")
    listing.set_defaults(handler=_list_conversations)

    topic = _with_role(conversation.add_parser("topic", help="set, rename or clear a conversation's topic"))
    topic.add_argument("conversation_id")
    change = topic.add_mutually_exclusive_group(required=True)
    change.add_argument("--set", dest="topic", help="the new topic, 1-2 lowercase slug segments")
    change.add_argument("--clear", action="store_true", help="remove the topic")
    topic.set_defaults(handler=lambda config, args: _db(config, lambda conn: conversations.set_topic(
        conn, config, args.conversation_id, None if args.clear else args.topic, actor=args.role)))

    history = _with_role(conversation.add_parser("history", help="a conversation as you saw it; marks nothing read"))
    history.add_argument("conversation_id")
    history.add_argument("--after-seq", type=int, default=0)
    history.add_argument("--limit", type=int, default=50)
    history.set_defaults(handler=_history)

    for name, operation in (("add", conversations.add), ("remove", conversations.remove)):
        change = _with_role(conversation.add_parser(name, help=f"{name} a participant"))
        change.add_argument("conversation_id")
        change.add_argument("participant")
        change.set_defaults(handler=lambda config, args, operation=operation: _db(config, lambda conn: operation(
            conn, config, args.conversation_id, args.participant, actor=args.role)))


def _with_role(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--as", dest="role", help="the role to act as (default: $BATON_ROLE); local mode only")
    return parser


def _db(config: Config, operation: Any) -> Any:
    conn = connect(config.db_path)
    try:
        return operation(conn)
    finally:
        conn.close()


def _attention(args: argparse.Namespace) -> list[str] | None:
    """`--attention a,b` as the list the host expects, or None when it was left out."""
    return args.attention.split(",") if args.attention is not None else None


def _message(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    if args.to is None and args.conversation_id is None:
        raise InvalidRequest("name a recipient with --to or a conversation with --conversation")
    if args.to is not None:
        config.require_role(args.to)
    try:
        relayed = json.loads(args.relayed_json) if args.relayed_json is not None else None
    except json.JSONDecodeError:
        raise InvalidRequest("--relayed-json must be JSON: a list of {source, at, text} entries") from None
    text = args.text if args.text is not None else sys.stdin.read()
    attention = _attention(args)
    if args.conversation_id is not None:
        return _db(config, lambda conn: conversations.send(
            conn, config, sender=args.role, conversation_id=args.conversation_id, text=text, relayed=relayed,
            request_id=args.request_id, job_id=args.job_id, reply_to=args.reply_to, attention=attention,
            needs_decision=args.needs_decision))
    return _db(config, lambda conn: messages.send(conn, sender=args.role, recipient=args.to, text=text,
                                                  request_id=args.request_id, job_id=args.job_id, relayed=relayed,
                                                  author_kind=config.author_kind(args.role),
                                                  reply_to=args.reply_to, attention=attention,
                                                  needs_decision=args.needs_decision))


def _list_conversations(config: Config, args: argparse.Namespace) -> list[dict[str, Any]]:
    conversations.require_enabled(config)
    return _db(config, lambda conn: conversations.list_for(conn, args.role, args.topic))


def _history(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    conversations.require_enabled(config)
    return _db(config, lambda conn: conversations.history(conn, args.role, args.conversation_id, args.after_seq,
                                                          args.limit))


def _new_token(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    from baton.auth import new_token

    if config.require_role(args.token_role).display_only:  # before any secret exists
        raise ConfigError(f"{args.token_role} is display_only: a role that only displays never holds a credential")
    if any(entry.name == args.name for entry in config.tokens):
        raise ConfigError(f"a token named {args.name!r} is already configured; token names must be unique, so "
                          "pick another name (or revoke and remove the old one first)")
    token, digest = new_token()
    entry = f'[[auth.tokens]]\nname = "{args.name}"\nrole = "{args.token_role}"\nsha256 = "{digest}"'
    return {"token": token, "config_entry": entry,
            "note": "Give the token to the client; put only config_entry in the config. It is not shown again."}


def _hash_passphrase() -> int:
    from baton.auth import hash_passphrase

    passphrase = getpass.getpass("Passphrase: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
    if len(passphrase) < 12:
        _print({"error": "invalid_request", "detail": "use a passphrase of at least 12 characters"})
        return 1
    _print({"passphrase_hash": hash_passphrase(passphrase)})
    return 0


def _seat_logout_all(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    from baton import seat

    return {"sessions_ended": seat.logout_all(config)}


def _revoke_token(config: Config, name: str) -> dict[str, Any]:
    from baton.auth import revoke_token

    return revoke_token(config, name)


def _remote(args: argparse.Namespace) -> Any:
    """Run a message, read, wait or conversation list/history command through the host, as the token's role."""
    if getattr(args, "role", None):
        raise ConfigError("--as has no effect with --host: the token decides the role")
    command = " ".join(filter(None, [args.command, getattr(args, "conversation_command", None)]))
    if command == "message":
        if (args.to is None) == (args.conversation_id is None):
            raise InvalidRequest("name a recipient with --to or a conversation with --conversation")
        try:
            relayed = json.loads(args.relayed_json) if args.relayed_json is not None else None
        except json.JSONDecodeError:
            raise InvalidRequest("--relayed-json must be JSON: a list of {source, at, text} entries") from None
        text = args.text if args.text is not None else sys.stdin.read()
        # Every argument the local command takes, or the field is accepted here and never sent.
        arguments = {"text": text, "to": args.to, "conversation_id": args.conversation_id, "job_id": args.job_id,
                     "relayed": relayed, "request_id": args.request_id, "reply_to": args.reply_to,
                     "attention": _attention(args), "needs_decision": True if args.needs_decision else None}
        return _call(args, "send_message", arguments)
    if command == "read":
        return _call(args, "read_messages", {"after_seq": args.after_seq, "limit": args.limit})
    if command == "wait":
        return _remote_wait(args)
    if command == "conversation new":
        return _call(args, "create_conversation", {"title": args.title,
                                                   "participants": args.participants.split(","),
                                                   "topic": args.topic, "request_id": args.request_id})
    if command == "conversation list":
        return _call(args, "list_conversations", {"topic": args.topic})
    if command == "conversation topic":
        # `--clear` means null, which _call strips, and set_topic requires the argument: send it directly.
        return remote.call(args.host, remote.token(), "set_topic",
                           {"conversation_id": args.conversation_id,
                            "topic": None if args.clear else args.topic})
    if command == "conversation history":
        return _call(args, "read_history", {"conversation_id": args.conversation_id, "after_seq": args.after_seq,
                                            "limit": args.limit})
    raise ConfigError(f"`baton {command}` runs on the host only; with --host use message, read, wait, "
                      "conversation new, conversation list, conversation topic or conversation history")


def _remote_wait(args: argparse.Namespace) -> Any:
    """Wait in rounds the host allows, until something arrives or `--timeout` passes."""
    from baton.app import MAX_WAIT_SECONDS

    deadline = None if args.timeout is None else time.monotonic() + max(0.0, args.timeout)
    while True:
        left = MAX_WAIT_SECONDS if deadline is None else max(0.0, min(MAX_WAIT_SECONDS, deadline - time.monotonic()))
        taken = _call(args, "wait", {"timeout_seconds": left})
        if taken["events"] or (deadline is not None and time.monotonic() >= deadline):
            return taken


def _call(args: argparse.Namespace, tool: str, arguments: dict[str, Any]) -> Any:
    return remote.call(args.host, remote.token(), tool, {key: value for key, value in arguments.items()
                                                         if value is not None})


def _revoke_all(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    from baton.auth import revoke_all

    return {"revoked": revoke_all(config.oauth_state_path)}


def _add_index_commands(commands: Any) -> None:
    """Local, read-only commands for the host owner's indexing worker: no role, no token, no MCP.

    They open the database read-only, follow the change log through a cursor the worker keeps, and print
    change metadata only, never message text. They write nothing at all.
    """
    index = commands.add_parser("index", help="read-only change feed for a local indexing worker").add_subparsers(
        dest="index_command", required=True)
    changes_parser = index.add_parser("changes", help="changes after a cursor, oldest first, metadata only")
    changes_parser.add_argument("--after", help="the next_cursor of the last call; omit to start at the oldest kept")
    changes_parser.add_argument("--limit", type=int, default=100)
    changes_parser.set_defaults(handler=lambda config, args: _index(config, lambda conn: changes.read(
        conn, changes.Scope.ALL, cursor=args.after, limit=args.limit)))
    wait_parser = index.add_parser("wait", help="block until something changes after a cursor; prints only its seq")
    wait_parser.add_argument("--after", required=True)
    wait_parser.add_argument("--timeout", type=float, default=30.0, help="seconds")
    wait_parser.set_defaults(handler=lambda config, args: _index(config, lambda conn: changes.wait(
        conn, changes.Scope.ALL, cursor=args.after, timeout_seconds=args.timeout)))


def _index(config: Config, operation: Any) -> Any:
    conn = connect_read_only(config.db_path)
    try:
        return operation(conn)
    finally:
        conn.close()


def _print(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    sys.exit(main())
