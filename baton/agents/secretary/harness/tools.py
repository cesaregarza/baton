"""Tools the secretary may choose to call; each returns data, never principal actions."""

from __future__ import annotations

import asyncio
import math

from ..connections.decisions import AttentionDecision, AttentionVerdict, DecisionEngine, make_engine
from ..connections.model import ToolCall
from ..connections.transport import ModelUnavailable

DECISION_TOOL = {
    "type": "function",
    "name": "decide_attention",
    "strict": True,
    "description": (
        "The harness asks the independent decision model to choose wake, hold, or keep for the "
        "complete pending window before triage. Returns its decision and confidence."
    ),
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
        self._evaluated = False

    @property
    def definitions(self) -> list[dict]:
        return [DECISION_TOOL] if self.engine else []

    async def evaluate(self, context: dict, timeout: float) -> AttentionVerdict | None:
        """Run the independent judgment at most once for this assessment."""
        if self._evaluated:
            if self.verdict is None:
                return None
            return AttentionVerdict(
                decision=AttentionDecision(self.verdict["decision"]),
                confidence=self.verdict["confidence"],
            )
        self._evaluated = True
        if self.engine is None:
            self.status = "unavailable"
            return None
        try:
            async with asyncio.timeout(timeout):
                verdict = await self.engine.decide(context, timeout)
            if (
                not isinstance(verdict, AttentionVerdict)
                or not isinstance(verdict.decision, AttentionDecision)
                or type(verdict.confidence) not in (int, float)
                or not math.isfinite(verdict.confidence)
                or not 0 <= verdict.confidence <= 1
            ):
                raise ModelUnavailable("decision engine violated its typed result contract")
            self.verdict = {"decision": verdict.decision.value, "confidence": float(verdict.confidence)}
            self.status = "available"
            return verdict
        except (ModelUnavailable, TimeoutError):
            self.status = "unavailable"
            return None

    async def call(self, request: ToolCall, context: dict, timeout: float) -> dict:
        if request.name != "decide_attention" or self.engine is None:
            raise ModelUnavailable("model requested an unavailable tool")
        if request.arguments:
            raise ModelUnavailable("decide_attention does not accept arguments; the harness supplies its context")
        cached = self._evaluated
        verdict = await self.evaluate(context, timeout)
        if verdict is None:
            self.calls.append({"name": request.name, "status": "unavailable", "cached": cached})
            return {"error": "decision_engine_unavailable"}
        self.calls.append({"name": request.name, "status": "available", "cached": cached})
        return {"decision": verdict.decision.value, "confidence": verdict.confidence}
