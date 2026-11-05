"""Bounded transports for secretary assessment and optional engine advice."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx

from .command import Command
from .prompt import INSTRUCTIONS, RESPONSE_SCHEMA

_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_COMMAND_BYTES = 64 * 1024


class ModelUnavailable(Exception):
    """The configured model transport did not return a usable response."""


@dataclass
class SecretaryModel:
    """Assess the complete secretary context with one configured model call."""

    settings: dict

    async def assess(self, context: dict, timeout: float) -> dict:
        provider = self.settings.get("provider")
        if provider == "command":
            request = {
                "version": 2,
                "instructions": INSTRUCTIONS,
                "context": context,
                "response_schema": RESPONSE_SCHEMA,
            }
            return await _command_json(self.settings, request, timeout, _MAX_COMMAND_BYTES)
        if provider != "openai":
            raise ModelUnavailable("model provider is unavailable")

        key_name = self.settings.get("api_key_env", "OPENAI_API_KEY")
        api_key = os.environ.get(key_name)
        if not api_key:
            raise ModelUnavailable("model credential is unavailable")

        body = {
            "model": self.settings.get("model", "gpt-6-luna"),
            "store": False,
            "instructions": INSTRUCTIONS,
            "input": json.dumps(context, ensure_ascii=False, separators=(",", ":")),
            "reasoning": {"effort": self.settings.get("reasoning_effort", "low")},
            "max_output_tokens": self.settings.get("max_output_tokens", 4096),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "secretary_triage",
                    "strict": True,
                    "schema": RESPONSE_SCHEMA,
                }
            },
        }
        url = self.settings.get("base_url", "https://api.openai.com/v1").rstrip("/") + "/responses"
        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {api_key}"},
                ) as response:
                    if not 200 <= response.status_code < 300:
                        raise ModelUnavailable("model request failed")
                    raw = await _read_limited(response, _MAX_RESPONSE_BYTES)
        except asyncio.CancelledError:
            raise
        except ModelUnavailable:
            raise
        except Exception:
            raise ModelUnavailable("model request failed") from None

        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            raise ModelUnavailable("model returned invalid JSON") from None
        return _response_object(result)


@dataclass
class DecisionEngine:
    """Ask a configured command for advisory context for the secretary."""

    settings: dict

    async def advise(self, context: dict, timeout: float) -> dict:
        request = {"version": 2, "task": "advise_secretary", "context": context}
        return await _command_json(self.settings, request, timeout, _MAX_COMMAND_BYTES)


async def _command_json(settings: dict, request: dict, timeout: float, max_bytes: int) -> dict:
    argv = settings.get("command")
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv):
        raise ModelUnavailable("model command is unavailable")
    payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        code, output = await Command(argv, max_output=max_bytes).run(payload, timeout)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ModelUnavailable("model command failed") from None
    if code != 0:
        raise ModelUnavailable("model command failed")
    try:
        result = json.loads(output)
    except (UnicodeDecodeError, ValueError):
        raise ModelUnavailable("model command returned invalid JSON") from None
    if not isinstance(result, dict):
        raise ModelUnavailable("model command response is not an object")
    return result


async def _read_limited(response: httpx.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise ModelUnavailable("model response exceeds its size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _response_object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ModelUnavailable("model response is not an object")
    if value.get("status") != "completed":
        raise ModelUnavailable("model response is incomplete")
    if value.get("refusal"):
        raise ModelUnavailable("model refused the request")

    texts: list[str] = []
    output = value.get("output")
    if not isinstance(output, list):
        raise ModelUnavailable("model response has no output")
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "refusal":
            raise ModelUnavailable("model refused the request")
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "refusal":
                raise ModelUnavailable("model refused the request")
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                texts.append(part["text"])

    if len(texts) != 1:
        raise ModelUnavailable("model response did not contain one structured result")
    try:
        result = json.loads(texts[0])
    except ValueError:
        raise ModelUnavailable("model returned invalid JSON") from None
    if not isinstance(result, dict):
        raise ModelUnavailable("model result is not an object")
    return result
