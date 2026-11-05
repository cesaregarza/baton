"""Root parser and local/remote command dispatch."""

from __future__ import annotations

import argparse
import logging
import os

from baton import config as config_module
from baton import datadir, remote, wakes
from baton.cli import conversations, identity, index, jobs, messages, secretary
from baton.cli import remote as remote_cli
from baton.cli.common import db, print_json
from baton.config import ConfigError
from baton.errors import BatonError


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "secretary" and args.state is None and args.secretary_command != "read":
        parser.error("the following arguments are required: --state")
    if args.command == "hash-passphrase":
        return identity.hash_passphrase()
    try:
        if args.command == "secretary" and args.host:
            raise ConfigError("secretary commands are host-local; --host is not supported")
        if args.host:
            result = remote_cli.dispatch(args)
            print_json(result)
            return 0
        config = config_module.load(args.config)
        os.umask(0o077)
        datadir.check(config.data_dir)
        if hasattr(args, "role"):
            args.role = args.role or os.environ.get("BATON_ROLE")
            if not args.role:
                raise ConfigError("name a role with --as or $BATON_ROLE")
            if config.require_role(args.role).display_only:
                raise ConfigError(f"{args.role} is display_only: it only displays, so nothing acts as it")
            if config.require_role(args.role).human:
                raise ConfigError(
                    f"{args.role} is a human role: it acts only with its own token, through "
                    f"`baton --host URL` with ${remote.TOKEN_ENV}, never with --as"
                )
        if args.command == "serve":
            from baton.app import serve  # the MCP SDK is slow to import for one-shot commands

            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
            logging.getLogger("httpx").setLevel(logging.WARNING)
            serve(config)
            return 0
        result = args.handler(config, args)
    except ConfigError as error:
        print_json({"error": getattr(error, "code", "config"), "detail": str(error)})
        return 2
    except BatonError as error:
        print_json({"error": error.code, "detail": str(error), **error.extra})
        return 1
    print_json(result)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baton", description="A small MCP host for agent messages and jobs.")
    parser.add_argument(
        "--config", help=f"config file (default: ${config_module.CONFIG_ENV} or {config_module.DEFAULT_CONFIG})"
    )
    parser.add_argument(
        "--host",
        default=os.environ.get(remote.HOST_ENV),
        help=f"act through this host's URL with the token in ${remote.TOKEN_ENV} or the command in "
        f"${remote.TOKEN_COMMAND_ENV}, instead of a local database (default: ${remote.HOST_ENV})",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the MCP server and the wake dispatcher")
    messages.add_commands(commands)
    jobs.add_commands(commands)
    conversations.add_commands(commands)
    identity.add_commands(commands)
    index.add_commands(commands)
    secretary.add_commands(commands)
    commands.add_parser("wakes", help="show recent wakes and how they ended").set_defaults(
        handler=lambda config, args: db(config, wakes.recent)
    )
    return parser
