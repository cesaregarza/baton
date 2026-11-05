"""The principal's outstanding requests and the evidence that changes them.

The ledger borrows the scan's transaction. Opening, answering and cancelling a
request all pass through the same bounds, including a reserved closure row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from baton.database import Connection, Row

from . import sources, store

MAX_OPEN = 500
MAX_EVIDENCE = 64
EVIDENCE_LIMIT = ("reconcile", "expectation evidence limit reached")
OPEN_LIMIT = ("reconcile", "open expectation limit reached")


def task_ref(meta: dict) -> str | None:
    if meta["job_id"]:
        return f"job:{meta['job_id']}"
    if meta["conversation_id"]:
        return f"conversation:{meta['conversation_id']}"
    return None


def revision_matches(left: str, right: str) -> bool:
    return min(len(left), len(right)) >= 7 and (left.startswith(right) or right.startswith(left))


def artifact_match(expected: Row, observed: list[dict]) -> str:
    key, revision = expected["artifact_key"], expected["artifact_rev"]
    if key is None:
        return "none"
    revisions = [item["rev"] for item in observed if item["key"] == key]
    if not revisions:
        return "none"
    if not any(revision_matches(value, revision) for value in revisions):
        return "stale"
    return "match" if len(revisions) == 1 else "uncertain"


@dataclass(frozen=True)
class Expectations:
    state: Connection
    principal: str

    def evidence(self, expected: Row, meta: dict, kind: str, rule: str,
                 observed_kind: str | None = None) -> bool:
        count = self.state.execute(
            "SELECT COUNT(*) FROM expectation_evidence WHERE principal=? AND expectation_id=?",
            (self.principal, expected["id"])).fetchone()[0]
        limit = MAX_EVIDENCE
        if kind not in {"satisfied", "cancelled", "superseded"}:
            limit -= 1  # Leave the final row for evidence that closes the request.
        if count >= limit:
            return False
        artifact = meta["artifacts"][0] if len(meta["artifacts"]) == 1 else None
        store.add_evidence(self.state, self.principal, expected["id"], meta["source_key"], kind, rule,
                           (artifact["key"], artifact["rev"]) if artifact else (None, None), observed_kind)
        return True

    def record(self, expected: Row, meta: dict, kind: str, rule: str,
               outcome: tuple[str, str], observed_kind: str | None = None) -> tuple[str, str]:
        """Keep the intended disposition only when its evidence fits."""
        if not self.evidence(expected, meta, kind, rule, observed_kind):
            return EVIDENCE_LIMIT
        return outcome

    def close(self, expected: Row, meta: dict, status: str, rule: str) -> bool:
        if not self.evidence(expected, meta, status, rule):
            return False
        self.state.execute(
            "UPDATE expectations SET state=?, closed_source=? WHERE principal=? AND id=? AND state='open'",
            (status, meta["source_key"], self.principal, expected["id"]))
        return True

    def open(self, meta: dict, *, direction: str, requester: str | None, responder: str,
             artifact: dict | None, opening_message_id: str | None, expected_kind: str) -> str | None:
        count = self.state.execute("SELECT COUNT(*) FROM expectations WHERE principal=? AND state='open'",
                                   (self.principal,)).fetchone()[0]
        if count >= MAX_OPEN:
            return None
        key, revision = (artifact["key"], artifact["rev"]) if artifact else (None, None)
        identity = {"direction": direction, "source": meta["source_key"], "responder": responder,
                    "artifact": artifact, "expected_kind": expected_kind}
        expectation_id = store.digest(identity)[:24]
        predecessor = self._predecessor(meta, artifact=artifact, direction=direction, requester=requester,
                                        responder=responder, expected_kind=expected_kind)
        if predecessor is not None:
            self.close(predecessor, meta, "superseded", "explicit-predecessor")
        self.state.execute("INSERT OR IGNORE INTO expectations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL)",
                           (self.principal, expectation_id, direction, requester, responder, task_ref(meta), key,
                            revision, expected_kind, meta["source_key"], opening_message_id))
        return expectation_id

    def _predecessor(self, meta: dict, *, artifact: dict | None, direction: str, requester: str | None,
                     responder: str, expected_kind: str) -> Row | None:
        """Only an explicit, unique request for a different revision can be replaced."""
        previous = meta["supersedes"]
        if previous is None or artifact is None:
            return None
        if artifact["key"] != previous["key"]:
            return None
        if revision_matches(artifact["rev"], previous["rev"]):
            return None
        candidates = self.state.execute(
            "SELECT * FROM expectations WHERE principal=? AND state='open' AND direction=?"
            " AND requester IS ? AND responder=? AND artifact_key=? AND expected_kind=?",
            (self.principal, direction, requester, responder, artifact["key"], expected_kind)).fetchall()
        matching = [row for row in candidates if revision_matches(row["artifact_rev"], previous["rev"])]
        if len(matching) != 1:
            return None
        return matching[0]

    def owe(self, meta: dict, *, requester: str | None, artifact: dict | None,
            opening_message_id: str | None, expected_kind: str, outcome: tuple[str, str]) -> tuple[str, str]:
        """A full ledger loses tracking, never the attention an incoming request needs."""
        opened = self.open(meta, direction="owed", requester=requester, responder=self.principal,
                           artifact=artifact, opening_message_id=opening_message_id, expected_kind=expected_kind)
        if opened is None:
            return outcome[0], f"{outcome[1]}; not tracked: open expectation limit reached"
        return outcome

    def for_responder(self, responder: str | None) -> list[Row]:
        return self.state.execute(
            "SELECT * FROM expectations WHERE principal=? AND state='open' AND responder=? ORDER BY id",
            (self.principal, responder)).fetchall()

    def candidates(self, meta: dict) -> list[Row]:
        """What the author owes in this task, restricted to the parent request for replies."""
        return [row for row in self.for_responder(sources._actor(meta))
                if row["task_ref"] == task_ref(meta)
                and (not meta["reply_to"] or meta["reply_to"] == row["opening_message_id"])]

    def is_direct_request(self, expected: Row) -> bool:
        if expected["direction"] != "awaited" or expected["opening_message_id"] is None:
            return False
        opening = self.state.execute("SELECT envelope FROM items WHERE principal=? AND source_key=?",
                                     (self.principal, expected["opening_source"])).fetchone()
        return opening is not None and json.loads(opening["envelope"])["conversation_id"] is None

    def for_job(self, job_ref: str, direction: str | None = None) -> list[Row]:
        rows = self.state.execute(
            "SELECT * FROM expectations WHERE principal=? AND task_ref=? AND state='open' ORDER BY id",
            (self.principal, job_ref)).fetchall()
        return [row for row in rows if direction is None or row["direction"] == direction]

    def answered(self, meta: dict) -> bool:
        return self.state.execute(
            "SELECT 1 FROM expectation_evidence WHERE principal=? AND source_key=? AND kind='satisfied'",
            (self.principal, meta["source_key"])).fetchone() is not None
