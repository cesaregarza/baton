"""Immutable source references for a secretary event range, without authored message bodies."""

from __future__ import annotations

from dataclasses import dataclass

from baton.config import Config
from baton.config.secretary import Policy, fingerprint
from baton.database.store import connect_read_only
from baton.errors import InvalidRequest

from . import read, sources


@dataclass(frozen=True)
class ReferenceWindow:
    principal: str
    epoch: str
    since: int
    through: int
    refs: list[dict]

    @property
    def digest(self) -> str:
        return fingerprint(
            {
                "principal": self.principal,
                "epoch": self.epoch,
                "since": self.since,
                "through": self.through,
                "refs": self.refs,
            }
        )

    @classmethod
    def read(cls, config: Config, policy: Policy, since: int, through: int) -> ReferenceWindow:
        """Snapshot source identities even when a range no longer fits the model's input limits."""
        with connect_read_only(config.db_path) as baton, baton.snapshot():
            epoch = sources._epoch(baton)
            head = baton.execute("SELECT COALESCE(MAX(pos),0) FROM events").one()[0]
            if not 0 <= since <= through <= head:
                raise InvalidRequest("secretary positions must belong to the retained event stream")

            refs: list[dict] = []
            listed: set[str] = set()
            scanned = since
            while scanned < through:
                rows = sources._source_rows(baton, policy.principal, scanned, 500)
                if not rows:
                    break
                for row in rows:
                    if row["pos"] > through:
                        break
                    entry = read._entry(baton, config, policy.principal, row, listed)
                    scanned = row["pos"]
                    if entry is None:
                        continue
                    message = entry["message"]
                    conversation_id = message["conversation_id"] if message else entry["data"].get("conversation_id")
                    if message is not None:
                        listed.add(message["id"])
                    ref = {
                        "source_key": f"message:{message['id']}" if message else f"event:{entry['pos']}",
                        "pos": entry["pos"],
                        "message_id": message["id"] if message else None,
                        "conversation_id": conversation_id,
                        "job_id": entry["job_id"],
                    }
                    refs.append(ref)
                if rows[-1]["pos"] >= through or len(rows) < 500:
                    break
                scanned = rows[-1]["pos"]
            return cls(policy.principal, epoch, since, through, refs)
