"""Read the principal's pending conversation as model context, without interpreting its meaning."""

from __future__ import annotations

import json
from dataclasses import dataclass

from baton.config import Config
from baton.config.secretary import MAX_COMMAND_RESPONSE_BYTES, Policy, fingerprint
from baton.database import Connection
from baton.database.store import connect_read_only
from baton.errors import BatonError, InvalidRequest

from . import read, sources


class InboxTooLarge(BatonError):
    code = "secretary_context_limit"

    def __init__(self, detail: str, through_pos: int | None = None) -> None:
        super().__init__(detail)
        self.extra = {} if through_pos is None else {"through_pos": through_pos}


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

    def fits_output(self, policy: Policy) -> bool:
        return _json_size(sorted(self.source_keys)) <= _citation_budget(policy)

    @classmethod
    def split_position(cls, config: Config, policy: Policy, since: int, through: int) -> int | None:
        """Return the event cursor that divides a visible reference range as evenly as possible."""
        from .references import ReferenceWindow

        window = ReferenceWindow.read(config, policy, since, through)
        if len(window.refs) <= 1:
            return None
        return window.refs[(len(window.refs) + 1) // 2 - 1]["pos"]

    @classmethod
    def head(cls, config: Config) -> tuple[str, int]:
        """Return the log epoch and event head from one read-only database snapshot."""
        with connect_read_only(config.db_path) as baton, baton.snapshot():
            return sources._epoch(baton), baton.execute("SELECT COALESCE(MAX(pos),0) FROM events").one()[0]

    @classmethod
    def read(cls, config: Config, policy: Policy, since: int, *, through: int | None = None) -> InboxWindow:
        """Read a complete bounded prefix, or the exact requested range when revalidating."""
        with connect_read_only(config.db_path) as baton, baton.snapshot():
            epoch = sources._epoch(baton)
            head = baton.execute("SELECT COALESCE(MAX(pos),0) FROM events").one()[0]
            end = head if through is None else through
            if not 0 <= since <= end <= head:
                raise InvalidRequest("secretary positions must belong to the retained event stream")
            events, parents, scanned = _pending_events(baton, config, policy, since, end, paginate=through is None)
            return cls(policy.principal, epoch, since, scanned, events, parents)


def _pending_events(
    baton: Connection, config: Config, policy: Policy, since: int, end: int, *, paginate: bool
) -> tuple[list[dict], list[dict], int]:
    """Add whole events and their required visible reply parents while they fit the policy."""
    events: list[dict] = []
    parents: list[dict] = []
    listed: set[str] = set()
    size = 0
    citation_limit = _citation_budget(policy)
    scanned = since
    while scanned < end:
        rows = sources._source_rows(baton, policy.principal, scanned, 500)
        if not rows:
            return events, parents, end
        stopped = False
        for row in rows:
            if row["pos"] > end:
                return events, parents, end
            entry = read._entry(baton, config, policy.principal, row, listed)
            if entry is None:
                scanned = row["pos"]
                continue
            candidate_listed = listed.copy()
            candidate_parents = parents.copy()
            entry["source_key"] = _event_source_key(entry)
            parent = _reply_parent(baton, config, policy, entry, candidate_listed)
            if parent is not None:
                candidate_parents.append(parent)
            candidate_events = [*events, entry]
            candidate_size = size + _size(entry) + (_size(parent) if parent is not None else 0)
            candidate_keys = [event["source_key"] for event in [*candidate_events, *candidate_parents]]
            candidate_citation_size = _json_size(candidate_keys)
            exceeds_events = len(candidate_events) + len(candidate_parents) > policy.max_events
            exceeds_bytes = candidate_size > policy.context_bytes
            exceeds_citations = paginate and candidate_citation_size > citation_limit
            if exceeds_events or exceeds_bytes or exceeds_citations:
                if not paginate:
                    raise InboxTooLarge("pending inbox and reply context exceed the configured input bounds")
                if not events:
                    raise InboxTooLarge(
                        "pending inbox exceeds the configured input or citation output bounds", through_pos=row["pos"]
                    )
                stopped = True
                break
            events = candidate_events
            parents = candidate_parents
            listed = candidate_listed
            size = candidate_size
            scanned = row["pos"]
        if stopped:
            return events, parents, scanned
        if len(rows) < 500:
            return events, parents, end
    return events, parents, scanned


def _event_source_key(entry: dict) -> str:
    message = entry["message"]
    return f"message:{message['id']}" if message else f"event:{entry['pos']}"


def _reply_parent(baton: Connection, config: Config, policy: Policy, entry: dict, listed: set[str]) -> dict | None:
    """Resolve one visible reply parent unless its immutable message ID is already present."""
    message = entry["message"]
    parent_id = message["reply_to"] if message else None
    if not parent_id or parent_id in listed:
        if message:
            listed.add(message["id"])
        return None
    listed.add(parent_id)
    if message:
        listed.add(message["id"])
    row = sources._visible_message(baton, config, policy.principal, parent_id)
    if row is None:
        return None
    return {
        "source_key": f"message:{parent_id}",
        "pos": None,
        "job_id": row["job_id"],
        "message": read._shape(row, None),
    }


def _size(value: dict) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _citation_budget(policy: Policy) -> int:
    """Reserve half of the provider's bounded response for source citations."""
    output_tokens = policy.model.get("max_output_tokens")
    if type(output_tokens) is int:
        return max(1, output_tokens // 2)
    return MAX_COMMAND_RESPONSE_BYTES // 2
