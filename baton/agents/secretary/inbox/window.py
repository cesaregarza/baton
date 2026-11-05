"""Read the principal's pending conversation as model context, without interpreting its meaning."""

from __future__ import annotations

import json
from dataclasses import dataclass

from baton.config import Config
from baton.config.secretary import Policy, fingerprint
from baton.database import Connection
from baton.database.store import connect_read_only
from baton.errors import BatonError, InvalidRequest

from . import read, sources


class InboxTooLarge(BatonError):
    code = "secretary_context_limit"


@dataclass(frozen=True)
class InboxWindow:
    principal: str
    epoch: str
    since: int
    through: int
    events: list[dict]
    reply_context: list[dict]

    @property
    def digest(self) -> str:
        return fingerprint(
            {
                "principal": self.principal,
                "epoch": self.epoch,
                "since": self.since,
                "through": self.through,
                "events": self.events,
                "reply_context": self.reply_context,
            }
        )

    @property
    def source_keys(self) -> set[str]:
        return {event["source_key"] for event in [*self.events, *self.reply_context]}

    @classmethod
    def read(cls, config: Config, policy: Policy, since: int, *, through: int | None = None) -> InboxWindow:
        with connect_read_only(config.db_path) as baton, baton.snapshot():
            epoch = sources._epoch(baton)
            head = baton.execute("SELECT COALESCE(MAX(pos),0) FROM events").one()[0]
            end = head if through is None else through
            if not 0 <= since <= end <= head:
                raise InvalidRequest("secretary positions must belong to the retained event stream")
            events, listed, size = _pending_events(baton, config, policy, since, end)
            parents = _reply_context(baton, config, policy, events, listed, size)
            return cls(policy.principal, epoch, since, end, events, parents)


def _pending_events(
    baton: Connection, config: Config, policy: Policy, since: int, end: int
) -> tuple[list[dict], set[str], int]:
    """Read and deduplicate the bounded pending event window inside the caller's snapshot."""
    events, listed = [], set()
    cursor, size = since, 0
    while cursor < end:
        rows = sources._source_rows(baton, policy.principal, cursor, 500)
        if not rows:
            break
        for row in rows:
            cursor = row["pos"]
            if cursor > end:
                break
            entry = read._entry(baton, config, policy.principal, row, listed)
            if entry is None:
                continue
            message = entry["message"]
            entry["source_key"] = f"message:{message['id']}" if message else f"event:{entry['pos']}"
            if message:
                listed.add(message["id"])
            size += len(json.dumps(entry, ensure_ascii=False).encode())
            if len(events) >= policy.max_events or size > policy.context_bytes:
                raise InboxTooLarge(
                    "pending inbox exceeds max_events/context_bytes; increase the model "
                    "context allowance or handle the backlog before retrying; "
                    "nothing was consumed"
                )
            events.append(entry)
    return events, listed, size


def _reply_context(
    baton: Connection, config: Config, policy: Policy, events: list[dict], listed: set[str], size: int
) -> list[dict]:
    """Fill in visible reply parents using the remaining event and byte allowance."""
    parents = []
    for entry in events:
        parent_id = entry["message"]["reply_to"] if entry["message"] else None
        if not parent_id or parent_id in listed:
            continue
        listed.add(parent_id)
        row = sources._visible_message(baton, config, policy.principal, parent_id)
        if row is None:
            continue
        parent = {
            "source_key": f"message:{parent_id}",
            "pos": None,
            "job_id": row["job_id"],
            "message": read._shape(row, None),
        }
        size += len(json.dumps(parent, ensure_ascii=False).encode())
        if len(events) + len(parents) >= policy.max_events or size > policy.context_bytes:
            raise InboxTooLarge("pending inbox and reply context exceed the configured input bounds")
        parents.append(parent)
    return parents
