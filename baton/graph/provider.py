"""Single-call OpenAI-compatible transport; never retries or commits invalid output."""

import asyncio
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from baton.graph.contract import require
from baton.graph.failure import Category, TransportError, status_category
from baton.graph.operations import apply, prepare
from baton.graph.source import canonical
from baton.graph.storage import atomic_json, distinct_paths

MAX_RESPONSE_BYTES = 4_000_000


@dataclass(frozen=True)
class ProviderOptions:
    base_url: str
    model: str
    api_key_env: str | None = None
    allow_no_key: bool = False
    timeout: float = 60
    max_output_tokens: int = 4000
    response_format: str = "json_schema"
    token_parameter: str = "max_completion_tokens"


def provider_payload(request, options, schema=None, schema_name="baton_graph_proposal_v1"):
    require(options.response_format in {"json_schema", "json_object", "plain"}, "unsupported response format")
    require(options.token_parameter in {"max_completion_tokens", "max_tokens"}, "unsupported token parameter")
    content = {key: value for key, value in request.items() if key != "instructions"}
    payload = {
        "model": options.model,
        "stream": False,
        "store": False,
        "messages": [
            {"role": "system", "content": request["instructions"]},
            {"role": "user", "content": canonical(content)},
        ],
        options.token_parameter: options.max_output_tokens,
    }
    if options.response_format == "json_schema":
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": request["proposal_schema"] if schema is None else schema,
            },
        }
    elif options.response_format == "json_object":
        payload["response_format"] = {"type": "json_object"}
    return payload


def provider_headers(options):
    """Request headers, or an `authentication` refusal when the configured credentials cannot form a request."""
    require(bool(options.model.strip()), "explicit model is required")
    require(
        math.isfinite(options.timeout) and options.timeout > 0 and options.max_output_tokens > 0,
        "provider timeout and output token bound must be positive",
    )
    url = urlsplit(options.base_url)
    _expect(
        url.scheme in {"https", "http"}
        and bool(url.netloc)
        and not url.username
        and not url.password
        and not url.query
        and not url.fragment,
        Category.AUTHENTICATION,
        "base URL must be an explicit HTTP(S) API base without credentials",
    )
    _expect(
        bool(options.api_key_env) != options.allow_no_key,
        Category.AUTHENTICATION,
        "choose api-key-env or explicit allow-no-key",
    )
    headers = {"Content-Type": "application/json"}
    if options.api_key_env:
        key = os.environ.get(options.api_key_env)
        _expect(bool(key), Category.AUTHENTICATION, "configured credential environment variable is missing/empty")
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _expect(condition, category, message):
    if not condition:
        raise TransportError(category, message)


def call_provider(
    request,
    options,
    *,
    schema=None,
    schema_name="baton_graph_proposal_v1",
    max_input_bytes=100_000,
    max_response_bytes=MAX_RESPONSE_BYTES,
    client=None,
    stop=None,
):
    """Synchronous wrapper around `call_provider_async` for the CLI harness and the worker thread.

    `client`, when given, is an `httpx.AsyncClient` (tests inject one over a mock transport).
    """
    return asyncio.run(
        call_provider_async(
            request,
            options,
            schema=schema,
            schema_name=schema_name,
            max_input_bytes=max_input_bytes,
            max_response_bytes=max_response_bytes,
            client=client,
            stop=stop,
        )
    )


async def call_provider_async(
    request,
    options,
    *,
    schema=None,
    schema_name="baton_graph_proposal_v1",
    max_input_bytes=100_000,
    max_response_bytes=MAX_RESPONSE_BYTES,
    client=None,
    stop=None,
):
    """One request, one response: `{candidate, usage, input_bytes, output_bytes}` or a categorized TransportError.

    The whole request (connect, write, headers and body) runs under one `asyncio.timeout(options.timeout)`,
    which is also httpx's inactivity timeout, so a stalled or trickling provider ends at the deadline. A
    `stop` event (threading.Event) watched alongside the request cancels it. Either way the request task is
    awaited to completion and the stream and any owned client are closed before this returns.
    """
    headers = provider_headers(options)
    payload = provider_payload(request, options, schema, schema_name)
    # This is an honest UTF-8 bound on serialized input messages, not a token estimate.
    input_bytes = len(canonical(payload["messages"]).encode("utf-8"))
    _expect(
        input_bytes <= max_input_bytes,
        Category.INPUT_LIMIT,
        "serialized provider input exceeds max-input-bytes (UTF-8)",
    )
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=options.timeout, follow_redirects=False)
    url = options.base_url.rstrip("/") + "/chat/completions"
    try:
        body, size = await _bounded_request(client, url, payload, headers, options.timeout, max_response_bytes, stop)
        result = json.loads(body)
    except TransportError:
        raise  # the status, size, deadline or cancellation, categorized where it was detected
    except (httpx.ConnectError, httpx.ConnectTimeout):  # nothing reached the provider
        raise TransportError(Category.PROVIDER_UNAVAILABLE, "provider connection could not be made") from None
    except httpx.HTTPError:  # the request may have been received, and nothing says what happened to it
        raise TransportError(Category.TRANSPORT, "provider transport failed after sending") from None
    except (ValueError, UnicodeError):
        raise TransportError(Category.MALFORMED, "provider response is not JSON") from None
    finally:
        if own_client:
            await client.aclose()
    return {
        "candidate": parse_response(result),
        "usage": result.get("usage") or "unknown",
        "input_bytes": input_bytes,
        "output_bytes": size,
    }


async def _bounded_request(client, url, payload, headers, timeout, max_response_bytes, stop):
    """Run the request as a task beside a stop watcher; the deadline or the stop cancels and awaits it."""

    async def fetch():
        async with client.stream("POST", url, json=payload, headers=headers, timeout=timeout) as response:
            status = response.status_code
            _expect(200 <= status < 300, status_category(status), f"provider HTTP failure ({status})")
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                _expect(
                    size <= max_response_bytes,
                    Category.OUTPUT_LIMIT,
                    f"provider response exceeds {max_response_bytes}-byte limit",
                )
                chunks.append(chunk)
            return b"".join(chunks), size

    async def watch():
        while stop is None or not stop.is_set():
            await asyncio.sleep(0.05)

    fetching = asyncio.create_task(fetch())
    watching = asyncio.create_task(watch())
    ended = None
    try:
        async with asyncio.timeout(timeout):
            await asyncio.wait({fetching, watching}, return_when=asyncio.FIRST_COMPLETED)
        if not fetching.done():
            ended = TransportError(Category.CANCELLED, "provider call cancelled by shutdown")
    except TimeoutError:
        ended = TransportError(Category.TIMEOUT, "provider call exceeded its total deadline")
    finally:
        for task in (watching, fetching):
            if not task.done():
                task.cancel()
        await asyncio.gather(watching, return_exceptions=True)
    if ended is not None:
        await asyncio.gather(fetching, return_exceptions=True)  # the stream closes with the cancelled task
        raise ended
    return fetching.result()


def parse_response(result):
    """The proposal object from one completed choice; any other shape is a `malformed` answer."""

    def malformed_unless(condition, message):
        _expect(condition, Category.MALFORMED, message)

    malformed_unless(
        isinstance(result, dict) and isinstance(result.get("choices"), list) and len(result["choices"]) == 1,
        "provider must return exactly one choice",
    )
    choice = result["choices"][0]
    malformed_unless(
        isinstance(choice, dict) and choice.get("finish_reason") == "stop", "provider did not finish with stop"
    )
    message = choice.get("message")
    malformed_unless(isinstance(message, dict) and not message.get("refusal"), "provider refused or omitted message")
    malformed_unless(
        isinstance(message.get("content"), str) and bool(message["content"].strip()), "provider content is absent"
    )
    try:
        proposal = json.loads(message["content"])
    except ValueError:
        raise TransportError(Category.MALFORMED, "provider content is not JSON") from None
    malformed_unless(isinstance(proposal, dict), "provider proposal must be an object")
    return proposal


def run(
    source_path,
    state_path,
    request_path,
    proposal_path,
    options,
    *,
    batch_size=20,
    max_input_bytes=100_000,
    recall_statements=(),
    client=None,
):
    distinct_paths(source=source_path, state=state_path, request=request_path, proposal=proposal_path)
    require(not Path(proposal_path).exists(), "proposal artifact already exists; choose a new path")
    # Validate credentials/options before creating any request artifact.
    provider_headers(options)
    request = prepare(
        source_path,
        state_path,
        request_path,
        batch_size=batch_size,
        max_input_bytes=max_input_bytes,
        recall_statements=recall_statements,
    )
    produced = call_provider(request, options, max_input_bytes=max_input_bytes, client=client)
    atomic_json(proposal_path, produced["candidate"], replace=False)
    receipt = apply(source_path, state_path, request_path, proposal_path)
    return {
        "receipt": receipt,
        "usage": produced["usage"],
        "input_message_utf8_bytes": produced["input_bytes"],
        "provider_calls": 1,
    }
