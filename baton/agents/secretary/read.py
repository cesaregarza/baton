"""Read currently visible messages and job events for a secretary, without changing Baton cursors.

The model workflow and the host use the same visibility checks. Raw authored text and relayed text stay separate.
Pages deduplicate recipient copies by message ID. Read by ID retrieves context for a summary's source references.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from baton import config as config_module
from baton import datadir, messages
from baton.config import Config, ConfigError
from baton.database import Connection, Row, store
from baton.errors import BatonError, InvalidRequest

from . import policy as policy_module
from . import sources

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
DEFAULT_BYTES = 256 * 1024
BYTES = (1024, 1024 * 1024)  # the least and most a page's max_bytes may be
MAX_SCANNED = 5000           # events one page looks through, listed or not
MESSAGE_ID = re.compile(r"msg_[0-9a-f]{8,32}")


def read(config: Config, policy_path: Path, *, after: int | None = None, ids: list[str] | None = None,
         limit: int = DEFAULT_LIMIT, max_bytes: int = DEFAULT_BYTES) -> dict:
    """One page for the policy's principal: after an event position, or for the given message ids."""
    _check(after, ids, limit, max_bytes)
    baton = store.connect_read_only(config.db_path)
    try:
        with store.snapshot(baton):
            principal = policy_module.load_policy(config, policy_path).principal
            if ids is not None:
                return {"principal": principal, **_by_id(baton, config, principal, ids, limit, max_bytes)}
            return {"principal": principal, "after": after,
                    **_after(baton, config, principal, after, limit, max_bytes)}
    finally:
        baton.close()


def _check(after: int | None, ids: list[str] | None, limit: int, max_bytes: int) -> None:
    """Refuse a request outside its bounds before the database is opened."""
    if (after is None) == (ids is None):
        raise InvalidRequest("name either a position (after) or message ids, not both")
    if after is not None and (type(after) is not int or after < 0):
        raise InvalidRequest("after must be an event position, 0 or more")
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise InvalidRequest(f"limit must be 1-{MAX_LIMIT}")
    if type(max_bytes) is not int or not BYTES[0] <= max_bytes <= BYTES[1]:
        raise InvalidRequest(f"max_bytes must be {BYTES[0]}-{BYTES[1]}")
    if ids is not None and (not 1 <= len(ids) <= MAX_LIMIT or len(set(ids)) != len(ids) or not all(
            isinstance(value, str) and MESSAGE_ID.fullmatch(value) for value in ids)):
        raise InvalidRequest(f"ids must be 1-{MAX_LIMIT} distinct message ids")


def _after(baton: Connection, config: Config, principal: str, after: int, limit: int,
           max_bytes: int) -> dict:
    page: dict[str, list[dict]] = {"deliveries": [], "context": []}
    listed: set[str] = set()
    through, size, count, scanned = after, 0, 0, 0
    while scanned < MAX_SCANNED:
        rows = sources._source_rows(baton, principal, through, sources.DEFAULT_BATCH)
        if not rows:
            return {"through_pos": through, "more": False, **page}
        for event in rows:
            entry = _entry(baton, config, principal, event, listed)
            if entry is not None:
                cost = _size(entry)
                if count == limit or (count and size + cost > max_bytes):
                    return {"through_pos": through, "more": True, **page}
                page["deliveries" if sources.delivered_to(principal, event["audience"]) else "context"].append(entry)
                if entry["message"] is not None:
                    listed.add(entry["message"]["id"])
                count, size = count + 1, size + cost
            through = event["pos"]  # a later copy of a listed message, or an event the principal can't see
            scanned += 1
    return {"through_pos": through, "more": True, **page}


def _entry(baton: Connection, config: Config, principal: str, event: Row,
           listed: set[str]) -> dict | None:
    """One stream event as the thread reads it, whatever its kind; None where the principal can't see it, or for a
    later copy of a message already listed. Message copies and event context use the principal's current visibility."""
    message = None
    if event["kind"] == "message":
        message = _message(baton, config, principal, event)
        if message is None or message["id"] in listed:
            return None
    elif not sources.visible_event(baton, config, principal, event):
        return None
    return {"pos": event["pos"], "kind": event["kind"], "at": event["at"], "actor": event["actor"],
            "audience": event["audience"], "job_id": event["job_id"], "seq": event["seq"],
            "summary": event["summary"], "data": json.loads(event["data"]), "message": message}


def _message(baton: Connection, config: Config, principal: str, event: Row) -> dict | None:
    """Read the event's message copy only while it remains visible to the principal."""
    data = json.loads(event["data"])
    message_id, seq = data.get("message_id"), data.get("seq")
    if not isinstance(message_id, str) or type(seq) is not int:
        raise InvalidRequest("message event has no message_id or seq")
    row = sources._event_message(baton, config, principal, message_id, seq, event["audience"])
    return None if row is None else _shape(row, event["pos"])


def _by_id(baton: Connection, config: Config, principal: str, ids: list[str], limit: int,
           max_bytes: int) -> dict:
    page: list[dict] = []
    missing: list[str] = []
    size = 0
    for index, message_id in enumerate(ids):
        row = sources._visible_message(baton, config, principal, message_id)
        if row is None:
            missing.append(message_id)
            continue
        message = _shape(row, None)
        cost = _size(message)
        if len(page) == limit or (page and size + cost > max_bytes):
            return {"messages": page, "missing": missing, "not_read": ids[index:]}
        page.append(message)
        size += cost
    return {"messages": page, "missing": missing, "not_read": []}


def _shape(row: Row, pos: int | None) -> dict:
    shaped = messages.as_dict(row)
    return {"id": row["id"], "pos": pos, "conversation_id": row["conversation_id"],
            "to": row["recipient"] if row["conversation_id"] is None else None,
            "sender": f"guest:{row['alias_id']}" if row["alias_id"] else row["sender"],
            "from_kind": row["author_kind"],
            "alias": {"name": row["alias"], "code": row["alias_code"]} if row["alias_id"] else None,
            "at": row["at"], "reply_to": row["reply_to"], "job_id": row["job_id"],
            "attention": shaped["attention"] or [], "needs_decision": shaped["needs_decision"],
            "text": row["text"], "relayed": shaped["relayed"] or [], "notice": shaped["notice"]}


def _size(message: dict) -> int:
    return len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """read's own options, shared by `baton secretary read` and `python -m baton.secretary_read`."""
    parser.add_argument("--after", type=int,
                        help="list what was delivered to the principal, and its context, after this event position")
    parser.add_argument("--ids", help="comma-separated message ids to read instead")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"at most this many events or messages (1-{MAX_LIMIT})")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_BYTES,
                        help=f"at most this many bytes of them, at least one ({BYTES[0]}-{BYTES[1]})")


def options(args: argparse.Namespace) -> dict:
    return {"after": args.after, "ids": args.ids.split(",") if args.ids is not None else None, "limit": args.limit,
            "max_bytes": args.max_bytes}


def main(argv: list[str] | None = None) -> int:
    """`python -m baton.secretary_read`: the same options, output and exit codes as `baton secretary read`, without
    the `baton` command's import time."""
    parser = argparse.ArgumentParser(prog="python -m baton.secretary_read",
                                     description="read what the secretary's principal was delivered")
    parser.add_argument("--config", help="config file (default: $BATON_CONFIG or ~/.config/baton/baton.toml)")
    parser.add_argument("--policy", type=Path, required=True, help="versioned secretary model policy")
    add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        config = config_module.load(args.config)
        os.umask(0o077)
        datadir.check(config.data_dir)
        result, code = read(config, args.policy, **options(args)), 0
    except ConfigError as error:
        result, code = {"error": getattr(error, "code", "config"), "detail": str(error)}, 2
    except BatonError as error:
        result, code = {"error": error.code, "detail": str(error), **error.extra}, 1
    print(json.dumps(result), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
