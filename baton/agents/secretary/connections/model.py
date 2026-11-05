"""One model turn over the harness's ordered prompt and append-only tool history."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from ..harness.prompt import RESPONSE_SCHEMA
from .transport import ModelUnavailable, command_json, json_object, post_json


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict

    @classmethod
    def parse(cls, value: object) -> ToolCall:
        if not isinstance(value, dict) or value.keys() != {"id", "name", "arguments"}:
            raise ModelUnavailable("invalid tool call shape")
        if not all(isinstance(value[key], str) and 0 < len(value[key]) <= 200 for key in ("id", "name")):
            raise ModelUnavailable("invalid tool call identity")
        if not isinstance(value["arguments"], dict):
            raise ModelUnavailable("tool arguments must be an object")
        return cls(**value)

    def item(self) -> dict:
        return {
            "type": "function_call",
            "call_id": self.id,
            "name": self.name,
            "arguments": json.dumps(self.arguments, sort_keys=True, separators=(",", ":")),
        }


@dataclass(frozen=True)
class ModelTurn:
    answer: dict | None
    calls: list[ToolCall] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    usage: dict | None = None

    @classmethod
    def command(cls, value: dict) -> ModelTurn:
        if "tool_calls" not in value:
            return cls(answer=value)
        if value.keys() != {"tool_calls"} or not isinstance(value["tool_calls"], list) or not value["tool_calls"]:
            raise ModelUnavailable("invalid command tool response")
        calls = [ToolCall.parse(call) for call in value["tool_calls"]]
        return cls(None, calls, [call.item() for call in calls])

    @classmethod
    def responses(cls, value: dict) -> ModelTurn:
        if value.get("status") != "completed" or value.get("refusal"):
            raise ModelUnavailable("model response is incomplete or refused")
        output = value.get("output")
        if not isinstance(output, list):
            raise ModelUnavailable("model response has no output")
        texts, calls = [], []
        for item in output:
            if not isinstance(item, dict):
                raise ModelUnavailable("model output item is invalid")
            match item.get("type"):
                case "reasoning":
                    pass  # Preserve the encrypted reasoning item for stateless continuation.
                case "function_call":
                    calls.append(
                        ToolCall.parse(
                            {
                                "id": item.get("call_id"),
                                "name": item.get("name"),
                                "arguments": json_object(item.get("arguments", "")),
                            }
                        )
                    )
                case "message":
                    content = item.get("content")
                    if not isinstance(content, list):
                        raise ModelUnavailable("model message has invalid content")
                    for part in content:
                        if (
                            not isinstance(part, dict)
                            or part.get("type") != "output_text"
                            or not isinstance(part.get("text"), str)
                        ):
                            raise ModelUnavailable("model message was refused or invalid")
                        texts.append(part["text"])
                case _:
                    raise ModelUnavailable("model returned an unsupported output item")
        if not calls and len(texts) != 1:
            raise ModelUnavailable("model must return tool calls or one structured assessment")
        # A tool turn may include a preamble. Preserve it in history; only a call-free turn is final.
        return cls(None if calls else json_object(texts[0]), calls, output, _usage(value.get("usage")))


def _usage(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    details = value.get("input_tokens_details")
    counts = {
        "input_tokens": value.get("input_tokens"),
        "output_tokens": value.get("output_tokens"),
        "cached_tokens": details.get("cached_tokens") if isinstance(details, dict) else None,
    }
    return counts if all(type(n) is int and n >= 0 for n in counts.values()) else None


@dataclass
class SecretaryModel:
    settings: dict

    @property
    def layered_cache(self) -> bool:
        return self.settings["provider"] == "openai" and self.settings["cache_mode"] == "layered"

    async def turn(self, messages: list[dict], tools: list[dict], timeout: float) -> ModelTurn:
        if self.settings["provider"] == "command":
            value = await command_json(
                self.settings,
                {
                    "version": 4,
                    "input": messages,
                    "tools": tools,
                    "response_schema": RESPONSE_SCHEMA,
                    "max_output_tokens": self.settings["max_output_tokens"],
                },
                timeout,
            )
            return ModelTurn.command(value)
        api_key = os.environ.get(self.settings["api_key_env"])
        if not api_key:
            raise ModelUnavailable("model credential is unavailable")
        body = {
            "model": self.settings["model"],
            "store": False,
            "input": messages,
            "tools": tools,
            "parallel_tool_calls": False,
            "include": ["reasoning.encrypted_content"],
            "reasoning": {"effort": self.settings["reasoning_effort"]},
            "max_output_tokens": self.settings["max_output_tokens"],
            "text": {
                "format": {"type": "json_schema", "name": "secretary_triage", "strict": True, "schema": RESPONSE_SCHEMA}
            },
        }
        if self.layered_cache:
            # Three stable breakpoints plus the implicit window/tool-result boundary (four writes maximum).
            body["prompt_cache_options"] = {"mode": "implicit"}
        result = await post_json(self.settings["base_url"] + "/responses", api_key, body, timeout)
        return ModelTurn.responses(result)
