"""Bounded JSON I/O shared by the model and decision-engine adapters."""

from __future__ import annotations

import asyncio
import json
import os

import httpx

from baton.config.secretary import MAX_COMMAND_RESPONSE_BYTES, MAX_MODEL_RESPONSE_BYTES

from .command import Command, OutputLimitExceeded


class ModelUnavailable(Exception):
    """A model or tool transport did not return a usable response."""

    category = "unavailable"

    def __init__(self, message: str, *, category: str | None = None):
        super().__init__(message)
        self.category = category or type(self).category


class ModelResponseInvalid(ModelUnavailable):
    """A received response was invalid or truncated; retrying a smaller page may help."""

    category = "invalid_response"


async def post_json(
    url: str, api_key: str, body: dict, timeout: float, max_bytes: int = MAX_MODEL_RESPONSE_BYTES
) -> dict:
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            async with client.stream(
                "POST", url, json=body, headers={"Authorization": f"Bearer {api_key}"}
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise ModelUnavailable("model request failed", category=_http_failure(response.status_code))
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ModelResponseInvalid("model response exceeds its size limit")
                    chunks.append(chunk)
        return json_object(b"".join(chunks))
    except asyncio.CancelledError:
        raise
    except ModelUnavailable:
        raise
    except httpx.TimeoutException:
        raise ModelUnavailable("model request timed out", category="timeout") from None
    except Exception:
        raise ModelUnavailable("model request failed") from None


async def command_json(
    settings: dict, request: dict, timeout: float, max_bytes: int = MAX_COMMAND_RESPONSE_BYTES
) -> dict:
    argv = settings.get("command")
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv):
        raise ModelUnavailable("model command is unavailable")
    payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    names = settings.get("env", [])
    allowed_environment = {name: os.environ[name] for name in names if name in os.environ}
    try:
        code, output = await Command(argv, max_output=max_bytes, env=allowed_environment).run(payload, timeout)
    except asyncio.CancelledError:
        raise
    except OutputLimitExceeded:
        raise ModelResponseInvalid("model response exceeds its size limit") from None
    except TimeoutError:
        raise ModelUnavailable("model command timed out", category="timeout") from None
    except Exception:
        raise ModelUnavailable("model command failed") from None
    if code != 0:
        raise ModelUnavailable("model command failed", category="command_failed")
    return json_object(output)


def _http_failure(status: int) -> str:
    """Classify the status without copying provider errors, URLs, or credentials."""
    if status == 408:
        return "timeout"
    if status in {401, 403}:
        return "authentication"
    if 400 <= status < 500 and status != 429:
        return "rejected"
    return "unavailable"


def json_object(raw: str | bytes) -> dict:
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, ValueError, TypeError):
        raise ModelResponseInvalid("model returned invalid JSON") from None
    if not isinstance(result, dict):
        raise ModelResponseInvalid("model response is not an object")
    return result
