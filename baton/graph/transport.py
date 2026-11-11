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
from baton.graph.contract import SCHEMA_NAME, GraphError
from baton.graph.provider import ProviderOptions, call_provider


@dataclass(frozen=True)
class Limits:
    """Hard bounds the host enforces itself: wall time and bytes, plus the API-only output-token ceiling."""

    timeout: float
    max_input_bytes: int
    max_response_bytes: int
    max_output_tokens: int


class TransportError(GraphError):
    """A call that produced no acceptable candidate; `category` is a sanitized class, never provider text."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(f"{category}: {detail}")
        self.category = category


class Transport(Protocol):
    mode: str

    def verify(self) -> dict[str, Any]:
        """Check installation and isolation prerequisites without running a model."""

    def produce(
        self, request: dict[str, Any], schema: dict[str, Any], limits: Limits, *, stop: threading.Event | None = None
    ) -> dict[str, Any]:
        """One bounded call: `{candidate, usage, input_bytes, output_bytes, elapsed, turns}`.

        A set `stop` cancels the call and releases its connection or process group before this returns.
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
        from baton.graph.provider import provider_headers

        try:
            provider_headers(self._options(Limits(1, 1, 1, 1)))
        except GraphError as error:
            raise TransportError("authentication", str(error)) from None
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
        try:
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
        except GraphError as error:
            raise TransportError(_category(str(error)), str(error)) from None
        return {**produced, "elapsed": time.monotonic() - started, "turns": 1}


def _category(detail: str) -> str:
    if "cancelled" in detail:
        return "cancelled"
    if "deadline" in detail:
        return "timeout"
    if "credential" in detail:
        return "authentication"
    if "exceeds" in detail:
        return "input_limit" if "input" in detail else "output_limit"
    if "HTTP failure (401)" in detail or "HTTP failure (403)" in detail:
        return "authentication"
    if "HTTP failure" in detail or "transport" in detail:
        return "transport"
    return "malformed"


def for_config(settings: GraphWorker, *, client: Any = None) -> Transport:
    """The configured transport; native clients are constructed, not verified, here."""
    if settings.provider == "http":
        assert settings.base_url is not None  # config parsing requires it for the http provider
        return HttpTransport(settings.base_url, settings.model, settings.api_key_env, settings.allow_no_key, client)
    from baton.graph import native

    if settings.provider == "codex":
        return native.CodexTransport(list(settings.command), settings.model, list(settings.credential_env))
    return native.ClaudeTransport(list(settings.command), settings.model, list(settings.credential_env))
