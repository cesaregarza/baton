"""A saved assessment or operational notice, with the exact sources it covers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from baton.config.secretary import Policy, fingerprint

from ..inbox.references import ReferenceWindow
from ..inbox.window import InboxWindow
from .archive import SummaryArchive
from .assessment import Assessment
from .state import AttentionLog

if TYPE_CHECKING:
    from ..service import Secretary


@dataclass(frozen=True)
class Outcome:
    policy: Policy
    window: InboxWindow | ReferenceWindow
    floor: int
    mode: str
    trigger: str | None = None

    def build(self, assessment: Assessment, *, decision_id: str, kind: str = "summary", **details) -> dict:
        refs = _references(self.window)
        return {
            "principal": self.policy.principal,
            "secretary_id": self.policy.secretary_id,
            "mode": self.mode,
            "trigger": self.trigger,
            "since": self.window.since,
            "floor": self.floor,
            "through_pos": self.window.through,
            "event_count": len(self.window.refs if isinstance(self.window, ReferenceWindow) else self.window.events),
            "covered_through": self.window.through if assessment.summary else self.window.since,
            "coverage": "complete" if assessment.summary else "none",
            "decision": assessment.decision,
            "reason": assessment.reason,
            "brief": assessment.summary,
            "sources": assessment.sources,
            "source_refs": [ref for ref in refs if ref["source_key"] in assessment.sources],
            "decision_id": decision_id,
            "kind": kind,
            "window_digest": self.window.digest,
            "reference_only": isinstance(self.window, ReferenceWindow),
            "policy_digest": self.policy.digest,
            "cached": False,
            "model": {key: self.policy.model[key] for key in ("provider", "model") if key in self.policy.model},
            "scanned_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            **details,
        }

    def notice(self, reason: str, *, kind: str, engine_verdict: dict | None = None) -> dict:
        """Report a workflow failure or an engine-forced wake without inventing a model summary."""
        sources = [ref["source_key"] for ref in _references(self.window)]
        assessment = Assessment(
            "wake", reason, reason + " Read the source references before deciding what to do.", sources
        )
        key = fingerprint(
            {"notice": kind, "window": self.window.digest, "policy": self.policy.digest, "mode": self.mode}
        )
        return self.build(assessment, decision_id=key, kind=kind, engine_verdict=engine_verdict)


def persist(secretary: Secretary, policy: Policy, result: dict) -> dict:
    """Idempotent writes recover a crash between the private ledger and shared archive."""
    log = AttentionLog(secretary.state_path, secretary.config.data_dir, policy.principal)
    stored = log.remember(result)
    SummaryArchive(secretary.config).save(policy.secretary_id, stored)
    return stored


def _references(window: InboxWindow | ReferenceWindow) -> list[dict]:
    if isinstance(window, ReferenceWindow):
        return window.refs
    return [
        {
            "source_key": entry["source_key"],
            "pos": entry["pos"],
            "message_id": entry["message"]["id"] if entry["message"] else None,
            "conversation_id": entry["message"]["conversation_id"] if entry["message"] else None,
            "job_id": entry["job_id"],
        }
        for entry in [*window.events, *window.reply_context]
    ]
