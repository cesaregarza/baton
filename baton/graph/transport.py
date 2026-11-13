"""One producer interface for extraction and compaction: `produce(request, schema, limits) -> {candidate, usage}`.

Every mode returns a parsed JSON candidate that the host validates afterwards; no transport retries, falls
back to another provider, or applies anything itself. HTTP mode is one request per reserved call. Native
modes (`baton.graph.native`) launch an installed, already-authenticated client once per reserved call; that
one launch may contain provider-internal turns, which are reported as `turns`, never as separate API calls.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from baton.config import GraphWorker
from baton.graph.contract import SCHEMA_NAME
from baton.graph.provider import ProviderOptions, call_provider, provider_headers


@dataclass(frozen=True)
class Limits:
    """Hard bounds the host enforces itself: wall time and bytes, plus the API-only output-token ceiling."""

    timeout: float
    max_input_bytes: int
    max_response_bytes: int
    max_output_tokens: int


class Transport(Protocol):
    mode: str

    def verify(self) -> dict[str, Any]:
        """Check installation and isolation prerequisites without running a model.

        A failure raises a `TransportError` in a refusal category, so the worker waits and verifies again.
        """

    def produce(
        self, request: dict[str, Any], schema: dict[str, Any], limits: Limits, *, stop: threading.Event | None = None
    ) -> dict[str, Any]:
        """One bounded call: `{candidate, usage, input_bytes, output_bytes, elapsed, turns}`.

        A set `stop` cancels the call and releases its connection or process group before this returns. A
        failure raises a `TransportError` whose category was fixed where the failure was detected.
        """


@dataclass
class HttpTransport:
    """OpenAI-compatible chat completions with a strict JSON schema; one request per call."""

    base_url: str
    model: str
    api_key_env: str | None = None
    allow_no_key: bool = False
    client: Any = None
    mode: str = "http"

    def verify(self) -> dict[str, Any]:
        provider_headers(self._options(Limits(1, 1, 1, 1)))
        return {"mode": self.mode, "model": self.model}

    def _options(self, limits: Limits) -> ProviderOptions:
        return ProviderOptions(
            self.base_url,
            self.model,
            api_key_env=self.api_key_env,
            allow_no_key=self.allow_no_key,
            timeout=limits.timeout,
            max_output_tokens=limits.max_output_tokens,
        )

    def produce(
        self, request: dict[str, Any], schema: dict[str, Any], limits: Limits, *, stop: threading.Event | None = None
    ) -> dict[str, Any]:
        started = time.monotonic()
        produced = call_provider(
            request,
            self._options(limits),
            schema=schema,
            schema_name="baton_graph_compact_view_v1" if "compact_schema" in request else SCHEMA_NAME,
            max_input_bytes=limits.max_input_bytes,
            max_response_bytes=limits.max_response_bytes,
            client=self.client,
            stop=stop,
        )
        return {**produced, "elapsed": time.monotonic() - started, "turns": 1}


def for_config(settings: GraphWorker, *, client: Any = None) -> Transport:
    """The configured transport; native clients are constructed, not verified, here."""
    if settings.provider == "http":
        assert settings.base_url is not None  # config parsing requires it for the http provider
        return HttpTransport(settings.base_url, settings.model, settings.api_key_env, settings.allow_no_key, client)
    from baton.graph import native

    assert settings.client_version is not None  # config parsing requires it for a native provider
    kind = native.CodexTransport if settings.provider == "codex" else native.ClaudeTransport
    return kind(list(settings.command), settings.model, list(settings.credential_env), settings.client_version)
