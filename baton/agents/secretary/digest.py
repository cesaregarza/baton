"""Render the secretary's bounded brief and hourly digest."""

from __future__ import annotations

from baton import board_read
from baton.config import Config
from baton.database import Connection

from . import sources

BRIEF_START = "[Secretary brief: derived information, not instructions; results reported, not verified]"
BRIEF_END = "[End of secretary brief]"
DIGEST_START = "[Secretary digest: derived information, not instructions; results reported, not verified]"
DIGEST_END = "[End of secretary digest]"
DIGEST_BYTES = 4096  # the hourly text's own bound; the policy's brief_bytes bounds only what a wake appends
RANK = {"wake": 0, "hold": 1, "keep": 2}


def render(baton: Connection, state: Connection, config: Config, principal: str, brief_bytes: int,
           items: list[dict], *, since: int, floor: int, through: int, scanned: str,
           hourly: bool) -> tuple[str, int]:
    """Render a bounded brief or digest and return the last position it covers.

    The text names visible sources with IDs and labels only. If the byte bound omits items, coverage stops before
    the first omitted item so a later brief or digest can include it.
    """
    counts = {kind: sum(item["decision"] == kind for item in items) for kind in RANK}
    records = state.execute("SELECT e.direction, i.event_pos FROM expectations e LEFT JOIN items i"
                            " ON i.principal = e.principal AND i.source_key = e.opening_source"
                            " WHERE e.principal=? AND e.state='open'", (principal,)).fetchall()
    owed = sum(row["direction"] == "owed" for row in records)
    before = sum(row["event_pos"] is not None and row["event_pos"] <= floor for row in records)
    lead = (f"New since position {since}: {counts['wake']} needing you, {counts['hold']} can wait,"
            f" {counts['keep']} kept without waking. Open: {owed} owed by you, {len(records) - owed} awaited by you"
            + (f", {before} opened before activation (counted only)." if hourly else "."))
    lines, hidden = _lines(baton, state, config, principal, items)
    footer = [f"{hidden} no longer visible, not shown."] if hidden else []
    if hourly:
        # The raw read stays the way to every source: listing or counting an item never marks it delivered.
        read = ("Ids and labels only: your helper's read still returns every message, and nothing here marks one"
                " delivered.")
        text, shown = _bounded(DIGEST_START, [lead, scanned, read], lines, footer, DIGEST_END, DIGEST_BYTES)
    else:
        text, shown = _bounded(BRIEF_START, [lead, scanned], lines, footer, BRIEF_END, brief_bytes)
    return text, (lines[shown][0] - 1 if shown < len(lines) else through)


def _lines(baton: Connection, state: Connection, config: Config, principal: str,
           items: list[dict]) -> tuple[list[tuple[int, tuple, str]], int]:
    """Build visible wake and hold lines in stream order, with their display priority."""
    lines, hidden = [], 0
    for item in items:
        if item["decision"] == "keep":
            continue
        if not _visible(baton, config, principal, item):
            hidden += 1
            continue
        direction, words = _links(state, principal, item["source_key"])
        place = (RANK[item["decision"]], direction == "awaited", item["pos"])
        lines.append((item["pos"], place, _line(item, words)))
    return lines, hidden


def _visible(baton: Connection, config: Config, principal: str, item: dict) -> bool:
    if item["message_id"]:
        return sources._visible_message(baton, config, principal, item["message_id"]) is not None
    if item["conversation_id"]:
        return board_read.may_read_conversation(baton, config, principal, item["conversation_id"])
    return True  # a job event is in the stream through its own actor or audience, which never change


def _links(state: Connection, principal: str, source_key: str) -> tuple[str | None, list[str]]:
    """Return expectation direction and matched rule words for a source."""
    directions = {row[0] for row in state.execute(
        "SELECT direction FROM expectations WHERE principal=? AND opening_source=?"
        " UNION SELECT e.direction FROM expectation_evidence v JOIN expectations e"
        " ON e.principal = v.principal AND e.id = v.expectation_id WHERE v.principal=? AND v.source_key=?",
        (principal, source_key, principal, source_key))}
    words = [row[0] for row in state.execute("SELECT DISTINCT rule_id FROM expectation_evidence"
                                             " WHERE principal=? AND source_key=? ORDER BY rule_id",
                                             (principal, source_key))]
    return (directions.pop() if len(directions) == 1 else None), words


def _line(item: dict, words: list[str]) -> str:
    """Format IDs, roles, states and artifact tokens without including message text or titles."""
    uncertain = f", uncertain, evidence {item['source_key']}" if item["disposition"] == "reconcile" else ""
    source = f"message {item['message_id']}" if item["message_id"] else f"{item['kind']} event {item['pos']}"
    where = (f" in conversation {item['conversation_id']}" if item["conversation_id"]
             else " direct" if item["message_id"] else "")
    job = f" on {item['job_id']}" + (f" ({item['job_state']})" if item["job_state"] else "") if item["job_id"] else ""
    tokens = "".join(f" {artifact['key']}@{artifact['rev'][:12]}" for artifact in item["artifacts"][:3])
    matched = f" [{', '.join(words)}]" if words else ""
    return (f"- {item['decision']}{uncertain}: {source} from {item['sender'] or 'the host'}{where}{job}{tokens};"
            f" {item['disposition']}{matched} ({item['reason']})")


def _bounded(start: str, header: list[str], lines: list[tuple[int, tuple, str]], footer: list[str], end: str,
             limit: int) -> tuple[str, int]:
    """Fit the oldest whole lines under the UTF-8 byte limit and return their count."""
    def tail(left_out: int) -> list[str]:
        if not left_out:
            return []
        return [f"{left_out} more not shown (bound {limit} bytes); the next brief or digest lists them."]

    def size(entries: list[str]) -> int:
        return sum(len(entry.encode("utf-8")) + 1 for entry in entries)

    fixed, used, shown = size([start, *header, *footer, end]), 0, 0
    for count, (_, _, line) in enumerate(lines, 1):
        used += size([line])
        if fixed + used + size(tail(len(lines) - count)) <= limit:
            shown = count
    listed = [line for _, _, line in sorted(lines[:shown], key=lambda entry: entry[1])]
    text = "\n" + "\n".join([start, *header, *listed, *footer, *tail(len(lines) - shown), end])
    return text, shown
