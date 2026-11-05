"""Append-only archive of deliverable secretary summaries in Baton's shared database."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from baton import events
from baton.agents.secretary.inbox import sources
from baton.database import Connection, connect, connect_read_only, snapshot, transaction
from baton.database.driver import Row
from baton.errors import Forbidden, InvalidRequest

if TYPE_CHECKING:
    from baton.agents.agent import Agent
    from baton.config import Config

MAX_PAGE_SIZE = 100
MAX_SCAN_ROWS = 500
_DELIVERABLE_FIELDS = (
    "decision",
    "brief",
    "sources",
    "source_refs",
    "kind",
    "mode",
    "engine_verdict",
)


@dataclass(frozen=True)
class SummaryArchive:
    """Store deliverable summaries and page only within an authenticated principal's rows."""

    config: Config

    def save(self, secretary_id: str, result: dict) -> dict | None:
        """Append one deliverable summary; retries for the same secretary and decision are idempotent."""
        if not isinstance(secretary_id, str) or not secretary_id.strip():
            raise InvalidRequest("secretary_id must be a non-empty string")
        principal = result.get("principal") if isinstance(result, dict) else None
        if not isinstance(principal, str) or not principal.strip():
            raise InvalidRequest("secretary summary must name its principal")
        decision_id, brief = result.get("decision_id"), result.get("brief")
        if not isinstance(decision_id, str) or not decision_id or not isinstance(brief, str):
            raise InvalidRequest("secretary summary must have a decision id and brief")
        # Alert holds/keeps carry no summary; digest summaries and notices still enter the archive.
        if not brief.strip():
            return None
        deliverable = {key: result[key] for key in _DELIVERABLE_FIELDS if key in result}
        encoded = json.dumps(deliverable, ensure_ascii=False, separators=(",", ":"))
        with connect(self.config.db_path) as conn, transaction(conn):
            conn.execute(
                "INSERT OR IGNORE INTO secretary_summaries"
                " (secretary_id,principal,decision_id,result,created_at) VALUES (?,?,?,?,?)",
                (secretary_id, principal, decision_id, encoded, events.now()),
            )
            row = conn.execute(
                "SELECT cursor,secretary_id,principal,decision_id,result,created_at FROM secretary_summaries"
                " WHERE secretary_id=? AND decision_id=?",
                (secretary_id, decision_id),
            ).one()
            entry = _entry(row)
            if entry["principal"] != principal:
                raise InvalidRequest("secretary decision id already belongs to a different principal")
            return entry

    def page_for(self, agent: Agent, *, after: int = 0, limit: int = 100) -> dict:
        """Page only the authenticated agent's own principal rows."""
        role = agent.config.roles.get(agent.role)
        if role is None or role.human or role.display_only or agent.role == "guest":
            raise Forbidden("secretary summaries are available only to an authenticated agent principal")
        return self._page(agent.role, after=after, limit=limit)

    def page_for_host(self, principal: str, *, after: int = 0, limit: int = 100) -> dict:
        """Host-local read used after secretary policy validation; callers must supply its validated principal."""
        if not isinstance(principal, str) or not principal.strip():
            raise InvalidRequest("principal must be a non-empty string")
        return self._page(principal, after=after, limit=limit)

    def _page(self, principal: str, *, after: int, limit: int) -> dict:
        _page_args(after, limit)
        with connect_read_only(self.config.db_path) as conn, snapshot(conn):
            rows = conn.execute(
                "SELECT cursor,secretary_id,principal,decision_id,result,created_at FROM secretary_summaries"
                " WHERE principal=? AND cursor>? ORDER BY cursor LIMIT ?",
                (principal, after, MAX_SCAN_ROWS + 1),
            ).fetchall()
            has_unscanned = len(rows) > MAX_SCAN_ROWS
            scanned = rows[:MAX_SCAN_ROWS]
            entries = []
            for row in scanned:
                entry = _entry(row)
                if _references_visible(conn, self.config, principal, entry["summary"]):
                    entries.append(entry)
                    if len(entries) > limit:
                        break
        has_extra_visible = len(entries) > limit
        has_more = has_extra_visible or has_unscanned
        if has_extra_visible:
            entries = entries[:limit]
            next_cursor = entries[-1]["cursor"]
        elif has_unscanned and scanned:
            # An all-hidden SQL batch must still advance so repeated calls cannot loop on the same rows.
            next_cursor = scanned[-1]["cursor"]
        else:
            next_cursor = None
        return {
            "principal": principal,
            "items": entries,
            "next": next_cursor,
            "has_more": has_more,
        }


def _references_visible(conn: Connection, config: Config, principal: str, summary: dict) -> bool:
    """Recheck every archived source identity using the inbox's current visibility rules."""
    refs = summary.get("source_refs", [])
    if not isinstance(refs, list):
        return False
    for ref in refs:
        if not _reference_visible(conn, config, principal, ref):
            return False
    return True


def _reference_visible(conn: Connection, config: Config, principal: str, ref: object) -> bool:
    """Resolve one source at its exact event position and verify its current message copy if present."""
    if not isinstance(ref, dict) or "message_id" not in ref:
        return False
    pos = ref.get("pos")
    message_id = ref.get("message_id")
    if pos is None:
        # Reply-context references predate an event-position link. `_visible_message` is backed by
        # messages_by_id, so this compatibility path remains indexed while rechecking revocation.
        return isinstance(message_id, str) and sources._visible_message(conn, config, principal, message_id) is not None
    if type(pos) is not int or pos < 1:
        return False
    # `_source_rows` applies the principal's event-stream selection; `visible_event` applies any
    # conversation-level restriction for non-message events. The row must be the cited position.
    events = sources._source_rows(conn, principal, pos - 1, 1)
    if not events or events[0]["pos"] != pos or not sources.visible_event(conn, config, principal, events[0]):
        return False
    if events[0]["kind"] != "message":
        return message_id is None
    if not isinstance(message_id, str):
        return False
    try:
        data = json.loads(events[0]["data"])
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    seq = data.get("seq")
    return (
        data.get("message_id") == message_id
        and type(seq) is int
        and sources._event_message(conn, config, principal, message_id, seq, events[0]["audience"]) is not None
    )


def _entry(row: Row) -> dict:
    return {
        "cursor": row[0],
        "secretary_id": row[1],
        "principal": row[2],
        "decision_id": row[3],
        "summary": json.loads(row[4]),
        "created_at": row[5],
    }


def _page_args(after: int, limit: int) -> None:
    if type(after) is not int or after < 0:
        raise InvalidRequest("after must be a non-negative integer")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise InvalidRequest(f"limit must be an integer between 1 and {MAX_PAGE_SIZE}")
