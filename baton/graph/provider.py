"""Single-call OpenAI-compatible transport; never retries or commits invalid output."""

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from baton.graph.contract import GraphError, require
from baton.graph.operations import apply, prepare
from baton.graph.source import canonical
from baton.graph.storage import atomic_json, distinct_paths


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


def provider_payload(request, options):
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
            "json_schema": {"name": "baton_graph_proposal_v1", "strict": True, "schema": request["proposal_schema"]},
        }
    elif options.response_format == "json_object":
        payload["response_format"] = {"type": "json_object"}
    return payload


def provider_headers(options):
    url = urlsplit(options.base_url)
    require(
        url.scheme in {"https", "http"}
        and bool(url.netloc)
        and not url.username
        and not url.password
        and not url.query
        and not url.fragment,
        "base URL must be an explicit HTTP(S) API base without credentials",
    )
    require(bool(options.model.strip()), "explicit model is required")
    require(
        math.isfinite(options.timeout) and options.timeout > 0 and options.max_output_tokens > 0,
        "provider timeout and output token bound must be positive",
    )
    require(bool(options.api_key_env) != options.allow_no_key, "choose api-key-env or explicit allow-no-key")
    headers = {"Content-Type": "application/json"}
    if options.api_key_env:
        key = os.environ.get(options.api_key_env)
        require(bool(key), "configured credential environment variable is missing/empty")
        headers["Authorization"] = f"Bearer {key}"
    return headers


def call_provider(request, options, *, max_input_bytes=100_000, client=None):
    headers = provider_headers(options)
    payload = provider_payload(request, options)
    # This is an honest UTF-8 bound on serialized input messages, not a token estimate.
    input_bytes = len(canonical(payload["messages"]).encode("utf-8"))
    require(input_bytes <= max_input_bytes, "serialized provider input exceeds max-input-bytes (UTF-8)")
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=options.timeout, follow_redirects=False)
    try:
        with client.stream(
            "POST",
            options.base_url.rstrip("/") + "/chat/completions",
            json=payload,
            headers=headers,
            timeout=options.timeout,
        ) as response:
            require(200 <= response.status_code < 300, f"provider HTTP failure ({response.status_code})")
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                require(size <= 4_000_000, "provider response exceeds 4000000-byte limit")
                chunks.append(chunk)
        result = json.loads(b"".join(chunks))
    except (httpx.HTTPError, ValueError, UnicodeError) as exc:
        raise GraphError("provider transport or JSON decoding failed") from exc
    finally:
        if own_client:
            client.close()
    return parse_response(result), result.get("usage") or "unknown", input_bytes


def parse_response(result):
    require(
        isinstance(result, dict) and isinstance(result.get("choices"), list) and len(result["choices"]) == 1,
        "provider must return exactly one choice",
    )
    choice = result["choices"][0]
    require(isinstance(choice, dict) and choice.get("finish_reason") == "stop", "provider did not finish with stop")
    message = choice.get("message")
    require(isinstance(message, dict) and not message.get("refusal"), "provider refused or omitted message")
    require(isinstance(message.get("content"), str) and bool(message["content"].strip()), "provider content is absent")
    try:
        proposal = json.loads(message["content"])
    except ValueError as exc:
        raise GraphError("provider content is not JSON") from exc
    require(isinstance(proposal, dict), "provider proposal must be an object")
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
    client=None,
):
    distinct_paths(source=source_path, state=state_path, request=request_path, proposal=proposal_path)
    require(not Path(proposal_path).exists(), "proposal artifact already exists; choose a new path")
    # Validate credentials/options before creating any request artifact.
    provider_headers(options)
    request = prepare(source_path, state_path, request_path, batch_size=batch_size, max_input_bytes=max_input_bytes)
    proposal, usage, input_bytes = call_provider(request, options, max_input_bytes=max_input_bytes, client=client)
    atomic_json(proposal_path, proposal, replace=False)
    receipt = apply(source_path, state_path, request_path, proposal_path)
    return {"receipt": receipt, "usage": usage, "input_message_utf8_bytes": input_bytes, "provider_calls": 1}
