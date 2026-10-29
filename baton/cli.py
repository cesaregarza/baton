"""The `baton` command: run the host, and let local agents message, wait and move jobs.

Local commands open the host's database directly, so they are for agents on
the host machine; a remote agent connects to the MCP server instead. `--as`
(or `$BATON_ROLE`) names the role to act as. Every command prints JSON on
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
from typing import Any

from baton import config as config_module
from baton import conversations, datadir, inbox, jobs, messages, wakes
from baton.config import Config, ConfigError
from baton.errors import BatonError, InvalidRequest
from baton.store import connect


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "hash-passphrase":
        return _hash_passphrase()
    try:
        config = config_module.load(args.config)
        os.umask(0o077)  # SQLite's own files, and anything else created, stay private
        datadir.check(config.data_dir)
        if hasattr(args, "role"):  # a command that acts as a role
            if not args.role:
                raise ConfigError("name a role with --as or $BATON_ROLE")
            config.require_role(args.role)
        if args.command == "serve":
            from baton.app import serve  # lazily: the MCP SDK is slow to import for one-shot commands

            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
            serve(config)
            return 0
        result = args.handler(config, args)
    except ConfigError as error:
        _print({"error": getattr(error, "code", "config"), "detail": str(error)})
        return 2
    except BatonError as error:
        _print({"error": error.code, "detail": str(error)})
        return 1
    _print(result)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baton", description="A small MCP host for agent messages and jobs.")
    parser.add_argument("--config", help=f"config file (default: ${config_module.CONFIG_ENV} or "
                                         f"{config_module.DEFAULT_CONFIG})")
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

    commands.add_parser("hash-passphrase", help="hash an OAuth consent passphrase for the config")
    oauth = commands.add_parser("oauth", help="OAuth grants").add_subparsers(dest="oauth_command", required=True)
    oauth.add_parser("revoke-all", help="end every OAuth access and refresh grant").set_defaults(handler=_revoke_all)
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
    new.add_argument("--request-id")
    new.set_defaults(handler=lambda config, args: _db(config, lambda conn: conversations.create(
        conn, config, actor=args.role, title=args.title, participants=args.participants.split(","),
        request_id=args.request_id)))

    listing = _with_role(conversation.add_parser("list", help="the conversations your role takes part in"))
    listing.set_defaults(handler=_list_conversations)

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
    parser.add_argument("--as", dest="role", default=os.environ.get("BATON_ROLE"),
                        help="the role to act as (default: $BATON_ROLE)")
    return parser


def _db(config: Config, operation: Any) -> Any:
    conn = connect(config.db_path)
    try:
        return operation(conn)
    finally:
        conn.close()


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
    attention = args.attention.split(",") if args.attention is not None else None
    if args.conversation_id is not None:
        return _db(config, lambda conn: conversations.send(
            conn, config, sender=args.role, conversation_id=args.conversation_id, text=text, relayed=relayed,
            request_id=args.request_id, job_id=args.job_id, reply_to=args.reply_to, attention=attention,
            needs_decision=args.needs_decision))
    return _db(config, lambda conn: messages.send(conn, sender=args.role, recipient=args.to, text=text,
                                                  request_id=args.request_id, job_id=args.job_id, relayed=relayed,
                                                  reply_to=args.reply_to, attention=attention,
                                                  needs_decision=args.needs_decision))


def _list_conversations(config: Config, args: argparse.Namespace) -> list[dict[str, Any]]:
    conversations.require_enabled(config)
    return _db(config, lambda conn: conversations.list_for(conn, args.role))


def _history(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    conversations.require_enabled(config)
    return _db(config, lambda conn: conversations.history(conn, args.role, args.conversation_id, args.after_seq,
                                                          args.limit))


def _new_token(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    from baton.auth import new_token

    config.require_role(args.token_role)
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


def _revoke_all(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    from baton.auth import revoke_all

    return {"revoked": revoke_all(config.oauth_state_path)}


def _print(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    sys.exit(main())
