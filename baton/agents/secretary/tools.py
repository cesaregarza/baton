"""Tools the secretary may choose to call; each returns data, never principal actions."""

from __future__ import annotations

import asyncio

from .decisions import AttentionDecision, DecisionEngine, make_engine
from .model import ToolCall
from .transport import ModelUnavailable

DECISION_TOOL = {
    "type": "function", "name": "decide_attention", "strict": True,
    "description": ("Ask an independent decision model to choose wake, hold, or keep for the complete pending "
                    "window. Returns a typed decision only, with no explanation or text. Use when an "
                    "independent judgment would help; you still write the summary and final assessment."),
    "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
}


class SecretaryTools:
    """Own the configured decision tool and reuse a successful verdict within this one assessment."""

    def __init__(self, settings: dict | None, *, engine: DecisionEngine | None = None):
        self.settings = settings
        self.engine = engine or (make_engine(settings) if settings else None)
        self.status = "not_used" if self.engine else "not_configured"
        self.verdict: dict | None = None
        self.calls: list[dict] = []

    @property
    def definitions(self) -> list[dict]:
        return [DECISION_TOOL] if self.engine else []

    async def call(self, request: ToolCall, context: dict, timeout: float) -> dict:
        if request.name != "decide_attention" or self.engine is None:
            raise ModelUnavailable("model requested an unavailable tool")
        if request.arguments:
            raise ModelUnavailable("decide_attention does not accept arguments; the harness supplies its context")
        cached = self.verdict is not None
        if not cached:
            try:
                async with asyncio.timeout(timeout):
                    decision = await self.engine.decide(context, timeout)
                if not isinstance(decision, AttentionDecision):
                    raise ModelUnavailable("decision engine violated its typed result contract")
                self.verdict = {"decision": decision.value}
            except (ModelUnavailable, TimeoutError):
                self.status = "unavailable"
                self.calls.append({"name": request.name, "status": self.status, "cached": False})
                return {"error": "decision_engine_unavailable"}
        self.status = "available"
        self.calls.append({"name": request.name, "status": self.status, "cached": cached})
        return self.verdict
