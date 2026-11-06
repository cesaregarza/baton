"""An assessment's bounded model/tool conversation, with stable leading instruction layers."""

from __future__ import annotations

import asyncio
import json
import time

from baton.config.secretary import Policy

from ..connections.model import SecretaryModel
from ..connections.transport import ModelResponseInvalid
from .prompt import PromptLayers
from .tools import SecretaryTools


class SecretaryHarness:
    def __init__(self, policy: Policy):
        self.policy = policy
        self.model = SecretaryModel(policy.model)
        self.tools = SecretaryTools(policy.decision_engine)
        self.model_calls = 0
        self.usage: list[dict | None] = []
        self.started = False

    @property
    def metrics(self) -> dict:
        totals = None
        recorded = [usage for usage in self.usage if usage is not None]
        if recorded and len(recorded) == len(self.usage):
            totals = {
                key: sum(usage[key] for usage in recorded) for key in ("input_tokens", "output_tokens", "cached_tokens")
            }
        return {"model_calls": self.model_calls, "tool_calls": self.tools.calls, "usage": totals}

    async def assess(self, context: dict, timeout: float) -> dict:
        if self.started:
            raise ModelResponseInvalid(
                "a secretary harness owns one assessment; create a new harness for another window"
            )
        self.started = True
        messages = PromptLayers(context).messages(cache=self.model.layered_cache)
        used_ids: set[str] = set()
        deadline = time.monotonic() + timeout
        async with asyncio.timeout(timeout):
            for _ in range(self.policy.max_tool_calls + 1):
                turn = await self.model.turn(
                    messages,
                    self.tools.definitions,
                    min(self.policy.model["timeout_seconds"], deadline - time.monotonic()),
                )
                self.model_calls += 1
                self.usage.append(turn.usage)
                if turn.answer is not None:
                    return turn.answer
                if len(used_ids) + len(turn.calls) > self.policy.max_tool_calls:
                    raise ModelResponseInvalid("secretary tool-call limit reached")
                # Validate all IDs before executing any part of a batch. Retain reasoning and calls verbatim.
                ids = [call.id for call in turn.calls]
                if len(set(ids)) != len(ids) or used_ids.intersection(ids):
                    raise ModelResponseInvalid("model reused a tool call id")
                used_ids.update(ids)
                messages.extend(turn.items)
                for call in turn.calls:
                    tool_timeout = min(self.policy.decision_timeout, (deadline - time.monotonic()) / 2)
                    result = await self.tools.call(call, context, tool_timeout)
                    messages.append(
                        {
                            "type": "function_call_output",
                            "call_id": call.id,
                            "output": json.dumps(result, sort_keys=True, separators=(",", ":")),
                        }
                    )
        raise ModelResponseInvalid("secretary did not finish an assessment")
