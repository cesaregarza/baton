"""The model's attention decision and summary, checked against the context it actually read."""

from __future__ import annotations

from dataclasses import dataclass

from ..connections.transport import ModelUnavailable


@dataclass(frozen=True)
class Assessment:
    decision: str
    reason: str
    summary: str
    sources: list[str]

    @classmethod
    def parse(
        cls, value: object, *, offered: set[str], required: set[str], summary_bytes: int, digest: bool
    ) -> Assessment:
        fields = {"decision", "reason", "summary", "sources"}
        if not isinstance(value, dict) or value.keys() != fields:
            raise ModelUnavailable("model returned an invalid assessment shape")
        if value["decision"] not in ("wake", "hold", "keep"):
            raise ModelUnavailable("model returned an invalid attention decision")
        if not isinstance(value["reason"], str) or not value["reason"].strip() or len(value["reason"].encode()) > 4096:
            raise ModelUnavailable("model returned an invalid reason")
        summary = value["summary"]
        if not isinstance(summary, str) or len(summary.encode()) > summary_bytes:
            raise ModelUnavailable("model summary exceeds its configured bound")
        refs = value["sources"]
        if (
            not isinstance(refs, list)
            or not all(isinstance(ref, str) and ref in offered for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            raise ModelUnavailable("model cited a source outside its inbox context")
        needs_summary = value["decision"] == "wake" or digest
        if needs_summary and (not summary.strip() or not refs):
            raise ModelUnavailable("model omitted the summary or its source references")
        if needs_summary and not required.issubset(refs):
            raise ModelUnavailable("model summary must account for every event in the page")
        if not needs_summary and (summary or refs):
            raise ModelUnavailable("a held or kept alert must not include a delivery summary or sources")
        return cls(**value)
