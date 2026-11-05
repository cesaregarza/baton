"""Bounded batch manifests and the message ranges an artifact builds on.

Plans use explicit generation IDs and artifact storage lookups. Window construction and
revalidation share the same source-message lookup and range operations."""

from __future__ import annotations

import hashlib
from typing import Any

from baton.config import Config
from baton.database import Connection, Row
from baton.errors import BatchSuperseded, ProvenanceLimitExceeded, UpToDate

from . import storage
from .constants import ITEM_REVISION, RECENT


def _kind_plan(
    conn: Connection, producer: str, generation: str | None, conversation_id: str, kind: str
) -> dict[str, Any]:
    """What a kind builds on: its current valid version, or nothing (a first version, or a rebase over an
    invalid one, whose number the commit's compare-and-set still names). An invalid version is never a prior."""
    current = storage._current(conn, producer, generation, conversation_id, kind)
    if current is None:
        return {
            "prior_version_id": None,
            "prior_version": 0,
            "start_seq": 1,
            "prior_inputs": [],
            "rebase": False,
            "reason": None,
        }
    if current["state"] == "invalid":
        return {
            "prior_version_id": None,
            "prior_version": current["version"],
            "start_seq": 1,
            "prior_inputs": [],
            "rebase": True,
            "reason": "prior_invalid",
        }
    return {
        "prior_version_id": current["id"],
        "prior_version": current["version"],
        "start_seq": current["covered_through_seq"] + 1,
        "prior_inputs": storage._inputs(conn, current["id"]),
        "rebase": False,
        "reason": None,
    }


def _recent_plan(
    conn: Connection, config: Config, producer: str, generation: str | None, conversation_id: str, last_seq: int
) -> dict[str, Any]:
    """A recent window builds on nothing and always reaches the newest message.

    It is chosen from the newest message backwards: as many of the latest messages that still have a copy as
    the window size and every bound allow. Built forwards from the oldest of them instead, a fragmented
    history would truncate the window short of the newest and commit the same old prefix every time.
    """
    current = storage._current(conn, producer, generation, conversation_id, RECENT)
    if current is not None and current["state"] == "valid" and current["covered_through_seq"] >= last_seq:
        raise UpToDate(f"{RECENT} already covers the latest messages")
    limits = config.indexer
    most = min(config.search.rolling_window_messages, limits.max_manifest_items)
    ranges_allowed = min(limits.max_manifest_segments, limits.max_input_segments)
    start, ranges, previous = last_seq + 1, 0, None
    for (seq,) in conn.execute(
        "SELECT conversation_seq FROM messages WHERE conversation_id = ? GROUP BY"
        " conversation_seq ORDER BY conversation_seq DESC LIMIT ?",
        (conversation_id, most),
    ):
        joins = previous is not None and seq == previous - 1  # continues the range the newer message began
        if not joins and ranges == ranges_allowed:
            break
        ranges += 0 if joins else 1
        start = previous = seq
    return {
        "prior_version_id": None,
        "prior_version": current["version"] if current else 0,
        "start_seq": start,
        "prior_inputs": [],
        "rebase": False,
        "reason": None,
    }


def _window(
    conn: Connection, config: Config, conversation_id: str, plans: dict[str, dict[str, Any]], last_seq: int
) -> dict[str, Any]:
    """The seqs this batch covers, from the lowest checkpoint asked for, as far as the bounds allow.

    A seq with no copy left is an absent hole: covered, never served, never an input. A run of them is one
    absent range, so there are never more ranges than segments plus one, and at most `max_manifest_items` + 1
    rows are read, however long the history. The window stops before the item that would break a bound; the
    provenance bound is checked for each kind that can commit from here, and if not even one item fits,
    nothing can advance for that kind.
    """
    limits = config.indexer
    from_seq = min(plan["start_seq"] for plan in plans.values())
    if from_seq > last_seq:
        raise UpToDate("every kind asked for already covers the whole conversation")
    eligible = {kind: plan for kind, plan in plans.items() if plan["start_seq"] == from_seq}
    items: list[tuple[int, str]] = []
    ranges: list[list[int]] = []
    absent: list[list[int]] = []
    unaccounted = from_seq  # the first seq neither served nor recorded absent yet
    stop = None  # the item the window couldn't take
    for seq, message_id in _present(conn, conversation_id, from_seq, last_seq, limits.max_manifest_items + 1):
        extended = _extend(ranges, seq)
        if len(items) >= limits.max_manifest_items or len(extended) > limits.max_manifest_segments:
            stop = seq
            break
        over = [
            kind
            for kind, plan in eligible.items()
            if len(_merge(plan["prior_inputs"] + extended)) > limits.max_input_segments
        ]
        if over:
            if not items:
                merged = _merge(eligible[over[0]]["prior_inputs"] + extended)
                raise ProvenanceLimitExceeded(
                    f"{over[0]}'s inputs can't take another range within {limits.max_input_segments}",
                    limit="max_input_segments",
                    conversation_id=conversation_id,
                    kind=over[0],
                    segments=len(merged),
                    items=_count(merged),
                )
            stop = seq
            break
        if seq > unaccounted:
            absent.append([unaccounted, seq - 1])
        items.append((seq, message_id))
        ranges = extended
        unaccounted = seq + 1
    # It covers up to the first item it couldn't take, or to the end, the holes before either included.
    to_seq = last_seq if stop is None else stop - 1
    if to_seq >= unaccounted:
        absent.append([unaccounted, to_seq])
    segments = []
    for start, end in ranges:
        run = [(seq, message_id) for seq, message_id in items if start <= seq <= end]
        segments.append(
            {
                "from_seq": start,
                "to_seq": end,
                "count": len(run),
                "digest": hashlib.sha256(
                    "".join(f"{seq}:{message_id}:{ITEM_REVISION}\n" for seq, message_id in run).encode()
                ).hexdigest(),
                "items": [
                    {"seq": seq, "message_id": message_id, "item_revision": ITEM_REVISION} for seq, message_id in run
                ],
            }
        )
    return {
        "window": {"from_seq": from_seq, "to_seq": to_seq},
        "segments": segments,
        "absent": [{"from_seq": start, "to_seq": end, "reason": "expired"} for start, end in absent],
        "item_count": len(items),
    }


def _present(conn: Connection, conversation_id: str, from_seq: int, to_seq: int, limit: int) -> list[tuple[int, str]]:
    """The first `limit` seqs in range that still have a copy, in order, each with its message id."""
    return [
        (row[0], row[1])
        for row in conn.execute(
            "SELECT conversation_seq, MIN(id) FROM messages WHERE conversation_id = ? AND conversation_seq BETWEEN ?"
            " AND ? GROUP BY conversation_seq ORDER BY conversation_seq LIMIT ?",
            (conversation_id, from_seq, to_seq, limit),
        )
    ]


def _revalidate(conn: Connection, manifest: dict[str, Any], kinds: list[str]) -> None:
    """Everything the batch rests on still holds, or the batch is superseded, naming the first thing that failed."""
    for segment in manifest["segments"]:
        for item in segment["items"]:
            if (
                conn.execute(
                    "SELECT 1 FROM messages WHERE id = ? AND conversation_id = ? AND conversation_seq = ? LIMIT 1",
                    (item["message_id"], manifest["conversation_id"], item["seq"]),
                ).fetchone()
                is None
            ):
                raise BatchSuperseded(f"message {item['message_id']} at seq {item['seq']} has no copy left")
    for hole in manifest["absent"]:
        if _present(conn, manifest["conversation_id"], hole["from_seq"], hole["to_seq"], 1):
            raise BatchSuperseded(
                f"seqs {hole['from_seq']}-{hole['to_seq']}, absent when the batch began, now have a message"
            )
    for kind in kinds:
        prior_id = manifest["kinds"][kind]["prior_version_id"]
        if prior_id is not None:
            row = conn.execute("SELECT state FROM artifact_versions WHERE id = ?", (prior_id,)).fetchone()
            if row is None or row["state"] != "valid":
                raise BatchSuperseded(f"{kind} version {prior_id}, which this batch builds on, is no longer valid")


def _check_containment(prior: list[list[int]], merged: list[list[int]]) -> None:
    """A chained version's inputs include all of its prior's, which is what lets one match invalidate a chain."""
    for start, end in prior:
        if not any(low <= start and end <= high for low, high in merged):
            raise BatchSuperseded(f"inputs {start}-{end} of the prior version would be lost from this one")


def _cannot_advance(conn: Connection, config: Config, conversation_id: str, current: Row | None) -> bool:
    """Whether the next item past a valid version's coverage can't be recorded within the provenance bound."""
    prior = storage._inputs(conn, current["id"]) if current is not None else []
    start = current["covered_through_seq"] + 1 if current is not None else 1
    row = conn.execute(
        "SELECT MIN(conversation_seq) FROM messages WHERE conversation_id = ? AND conversation_seq >= ?",
        (conversation_id, start),
    ).one()
    return row[0] is not None and len(_merge(prior + [[row[0], row[0]]])) > config.indexer.max_input_segments


def _extend(ranges: list[list[int]], seq: int) -> list[list[int]]:
    """`ranges` with `seq` added: joined to the last range when it follows it directly, else a range of its own."""
    if ranges and ranges[-1][1] == seq - 1:
        return ranges[:-1] + [[ranges[-1][0], seq]]
    return ranges + [[seq, seq]]


def _merge(ranges: list[list[int]]) -> list[list[int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _count(ranges: list[list[int]]) -> int:
    return sum(end - start + 1 for start, end in ranges)


def _within(seq: int, ranges: list[list[int]]) -> bool:
    return any(start <= seq <= end for start, end in ranges)
