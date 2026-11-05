"""Bounded JSON I/O shared by the model and decision-engine adapters."""

from __future__ import annotations

import asyncio
import json
import os

import httpx

from baton.config.secretary import MAX_COMMAND_RESPONSE_BYTES, MAX_MODEL_RESPONSE_BYTES

from .command import Command


class ModelUnavailable(Exception):
    """A model or tool transport did not return a usable response."""


async def post_json(
    url: str, api_key: str, body: dict, timeout: float, max_bytes: int = MAX_MODEL_RESPONSE_BYTES
) -> dict:
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            async with client.stream(
                "POST", url, json=body, headers={"Authorization": f"Bearer {api_key}"}
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise ModelUnavailable("model request failed")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ModelUnavailable("model response exceeds its size limit")
                    chunks.append(chunk)
        return json_object(b"".join(chunks))
    except asyncio.CancelledError:
        raise
    except ModelUnavailable:
        raise
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
    except Exception:
        raise ModelUnavailable("model command failed") from None
    if code != 0:
        raise ModelUnavailable("model command failed")
    return json_object(output)


def json_object(raw: str | bytes) -> dict:
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, ValueError, TypeError):
        raise ModelUnavailable("model returned invalid JSON") from None
    if not isinstance(result, dict):
        raise ModelUnavailable("model response is not an object")
    return result
