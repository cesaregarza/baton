"""Credential, alias, event subscription and topic grant commands."""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Any

from baton.cli.common import db
from baton.config import Config, ConfigError

GRANT_KEY_NOTE = ("Give the key to the agent only through the owner's own chat with it, never in a Baton thread. "
                  "The agent presents it as grant_token, beside its binding token.")


def add_commands(commands: Any) -> None:
    token = commands.add_parser("token", help="bearer tokens").add_subparsers(dest="token_command", required=True)
    new = token.add_parser("new", help="make a token for a role; prints it once, with the config entry")
    new.add_argument("--name", required=True)
    new.add_argument("--role", dest="token_role", required=True)
    new.set_defaults(handler=new_token)
    revoke = token.add_parser("revoke", help="end a configured token from its next call on, with no restart")
    revoke.add_argument("name", help="the token's auth.tokens name")
    revoke.set_defaults(handler=lambda config, args: revoke_token(config, args.name))

    seat_commands = commands.add_parser("seat", help="the browser seat").add_subparsers(
        dest="seat_command", required=True)
    seat_commands.add_parser("logout-all", help="end every browser seat session at once").set_defaults(
        handler=seat_logout_all)

    add_alias_commands(commands)
    add_topic_grant_commands(commands)
    commands.add_parser("hash-passphrase", help="hash an OAuth consent passphrase for the config")
    oauth = commands.add_parser("oauth", help="OAuth grants").add_subparsers(dest="oauth_command", required=True)
    oauth.add_parser("revoke-all", help="end every OAuth access and refresh grant").set_defaults(handler=revoke_all)


def add_alias_commands(commands: Any) -> None:
    """Owner commands for enrolled aliases and their events targets."""
    alias = commands.add_parser("alias", help="owner commands for enrolled aliases").add_subparsers(
        dest="alias_command", required=True)
    enrol = alias.add_parser("arm-enrol", help="let a role's own credential enrol it once, within 15 minutes")
    enrol.add_argument("enrol_role", metavar="role")
    enrol.set_defaults(handler=lambda config, args: aliases(config, lambda module, conn: module.arm_enrol(
        conn, config, args.enrol_role)))
    rekey = alias.add_parser("arm-rekey", help="let the next claim with a new passphrase reset an alias, "
                             "within 15 minutes")
    rekey.add_argument("name")
    rekey.set_defaults(handler=arm_rekey)
    mapping = alias.add_parser("map", help="make an alias act as an enrolled role; ends both sides' old proofs")
    mapping.add_argument("name")
    mapping.add_argument("map_role", metavar="role")
    mapping.set_defaults(handler=lambda config, args: aliases(config, lambda module, conn: module.map_alias(
        conn, args.name, args.map_role)))
    unmap = alias.add_parser("unmap", help="end an alias's mapping and its proofs; it renews as a guest alias")
    unmap.add_argument("name")
    unmap.set_defaults(handler=lambda config, args: aliases(config, lambda module, conn: module.unmap_alias(
        conn, args.name)))

    events = commands.add_parser("events", help="owner commands for event subscriptions").add_subparsers(
        dest="events_command", required=True)
    events.add_parser("status", help="subscriptions and recent delivery receipts, without URLs or secrets"
                      ).set_defaults(handler=events_status)
    unbind = events.add_parser("unbind", help="drop an OAuth grant's events target, when its alias's token is lost")
    unbind.add_argument("grant")
    unbind.set_defaults(handler=lambda config, args: aliases(config, lambda module, conn: module.unbind_grant(
        conn, args.grant)))


def add_topic_grant_commands(commands: Any) -> None:
    """Owner topic grant records; only `authorize` mints a key."""
    grant = commands.add_parser("topic-grant", help="owner commands for topic grants").add_subparsers(
        dest="topic_grant_command", required=True)
    propose = grant.add_parser("propose", help="propose a grant for an alias on a topic; it grants nothing yet")
    propose.add_argument("alias")
    propose.add_argument("topic")
    propose.add_argument("--capability", action="append", required=True,
                         help="reference.edit, thread.organise or topic.create_sub; repeat for more")
    propose.add_argument("--inherit", action="store_true", help="also cover the topic's direct subtopics")
    propose.add_argument("--days", type=int, required=True, help="how long the grant lasts once authorized: 1-30")
    propose.set_defaults(handler=topic_grant_propose)
    authorize = grant.add_parser("authorize", help="authorize a proposal by its digest; prints the key once")
    authorize.add_argument("grant_id")
    authorize.add_argument("digest")
    authorize.set_defaults(handler=topic_grant_authorize)
    revoke = grant.add_parser("revoke", help="end a grant in any state, with a reason")
    revoke.add_argument("grant_id")
    revoke.add_argument("--reason", required=True)
    revoke.set_defaults(handler=topic_grant_revoke)
    listing = grant.add_parser("list", help="pending and active grants")
    listing.add_argument("--all", action="store_true", help="every grant, including those no longer in force")
    listing.set_defaults(handler=topic_grant_list)


def aliases(config: Config, operation: Any) -> Any:
    from baton import aliases as aliases_module  # imports the MCP SDK, so keep this lazy

    return db(config, lambda conn: operation(aliases_module, conn))


def arm_rekey(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    from baton import aliases as aliases_module
    from baton.topics import grants as topic_grants

    def arm(conn: Any) -> dict[str, Any]:
        armed = aliases_module.arm_rekey(conn, args.name)
        grants = topic_grants.active_grant_ids(conn, armed["target"])
        return {**armed, "active_topic_grants": grants} if grants else armed

    return db(config, arm)


def topic_grant_propose(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    from baton.topics import grants as topic_grants

    return db(config, lambda conn: topic_grants.propose(conn, args.alias, args.topic, args.capability,
                                                        args.inherit, args.days))


def topic_grant_authorize(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    from baton.topics import grants as topic_grants

    granted = db(config, lambda conn: topic_grants.authorize(conn, args.grant_id, args.digest))
    return {**granted, "note": GRANT_KEY_NOTE}


def topic_grant_revoke(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    from baton.topics import grants as topic_grants

    return db(config, lambda conn: topic_grants.revoke(conn, args.grant_id, args.reason))


def topic_grant_list(config: Config, args: argparse.Namespace) -> list[dict[str, Any]]:
    from baton.topics import grants as topic_grants

    return db(config, lambda conn: topic_grants.list_grants(conn, include_all=args.all))


def events_status(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    from baton.integrations import openai_mcp_events  # lazy, like aliases

    return openai_mcp_events.status(config)


def new_token(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    from baton.auth import new_token as create_token

    if config.require_role(args.token_role).display_only:
        raise ConfigError(f"{args.token_role} is display_only: a role that only displays never holds a credential")
    if any(entry.name == args.name for entry in config.tokens):
        raise ConfigError(f"a token named {args.name!r} is already configured; token names must be unique, so "
                          "pick another name (or revoke and remove the old one first)")
    token_value, digest = create_token()
    entry = f'[[auth.tokens]]\nname = "{args.name}"\nrole = "{args.token_role}"\nsha256 = "{digest}"'
    return {"token": token_value, "config_entry": entry,
            "note": "Give the token to the client; put only config_entry in the config. It is not shown again."}


def hash_passphrase() -> int:
    from baton.auth import hash_passphrase as hash_value

    passphrase = getpass.getpass("Passphrase: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
    if len(passphrase) < 12:
        from baton.cli.common import print_json

        print_json({"error": "invalid_request", "detail": "use a passphrase of at least 12 characters"})
        return 1
    from baton.cli.common import print_json

    print_json({"passphrase_hash": hash_value(passphrase)})
    return 0


def seat_logout_all(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    from baton import seat

    return {"sessions_ended": seat.logout_all(config)}


def revoke_token(config: Config, name: str) -> dict[str, Any]:
    from baton.auth import revoke_token as revoke

    return revoke(config, name)


def revoke_all(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    from baton.auth import revoke_all as revoke

    return {"revoked": revoke(config.oauth_state_path)}
