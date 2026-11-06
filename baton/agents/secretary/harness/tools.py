"""Tools the secretary may choose to call; each returns data, never principal actions."""

from __future__ import annotations

import asyncio
import logging
import math

from ..connections.decisions import AttentionDecision, AttentionVerdict, DecisionEngine, EngineUnavailable, make_engine
from ..connections.model import ToolCall
from ..connections.transport import ModelResponseInvalid, ModelUnavailable

DECISION_TOOL = {
    "type": "function",
    "name": "decide_attention",
    "strict": True,
    "description": (
        "The harness asks the independent decision model to choose wake, hold, or keep for the "
        "complete pending window before triage. Returns its decision and wake_probability."
    ),
    "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
}
_LOGGER = logging.getLogger(__name__)


class SecretaryTools:
    """Own the configured decision tool and reuse a successful verdict within this one assessment."""

    def __init__(self, settings: dict | None, *, engine: DecisionEngine | None = None):
        self.settings = settings
        self.engine = engine or (make_engine(settings) if settings else None)
        self.status = "not_used" if self.engine else "not_configured"
        self.verdict: dict | None = None
        self.calls: list[dict] = []
        self._evaluated = False
        self._last_failure: str | None = None

    @property
    def health(self) -> dict:
        """Inspectable, sanitized state for this assessment's decision engine."""
        state = self.status if self.status in {"available", "unavailable"} else "not_used"
        return {"status": state, "last_failure": self._last_failure}

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
                wake_probability=self.verdict["wake_probability"],
            )
        self._evaluated = True
        if self.engine is None:
            return None
        try:
            async with asyncio.timeout(timeout):
                verdict = await self.engine.decide(context, timeout)
            if (
                not isinstance(verdict, AttentionVerdict)
                or not isinstance(verdict.decision, AttentionDecision)
                or type(verdict.wake_probability) not in (int, float)
                or not math.isfinite(verdict.wake_probability)
                or not 0 <= verdict.wake_probability <= 1
            ):
                raise EngineUnavailable("invalid_response", "decision engine violated its typed result contract")
            self.verdict = {
                "decision": verdict.decision.value,
                "wake_probability": float(verdict.wake_probability),
            }
            self.status = "available"
            self._last_failure = None
            return verdict
        except TimeoutError:
            self.status = "unavailable"
            self._last_failure = "timeout"
            _LOGGER.warning("secretary decision engine unavailable: %s", self._last_failure)
            return None
        except ModelUnavailable as exc:
            self.status = "unavailable"
            self._last_failure = exc.category if isinstance(exc, EngineUnavailable) else "transport"
            _LOGGER.warning("secretary decision engine unavailable: %s", self._last_failure)
            return None

    async def call(self, request: ToolCall, context: dict, timeout: float) -> dict:
        if request.name != "decide_attention" or self.engine is None:
            raise ModelResponseInvalid("model requested an unavailable tool")
        if request.arguments:
            raise ModelResponseInvalid("decide_attention does not accept arguments; the harness supplies its context")
        cached = self._evaluated
        verdict = await self.evaluate(context, timeout)
        if verdict is None:
            self.calls.append({"name": request.name, "status": "unavailable", "cached": cached})
            return {"error": "decision_engine_unavailable"}
        self.calls.append({"name": request.name, "status": "available", "cached": cached})
        return {"decision": verdict.decision.value, "wake_probability": verdict.wake_probability}
