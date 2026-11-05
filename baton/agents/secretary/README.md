# Baton secretary (policy version 2)

The secretary is a lightweight model harness for one Baton agent, the **principal**. When visible activity arrives,
it reads the complete pending window and asks a model to assess whether the principal needs attention. A wake
includes a grounded summary and source references for the host to deliver. The secretary never sends that
notification itself or advances the principal's inbox.

## How an assessment works

1. Baton builds the authorized pending window, including visible reply parents and context about the principal's
   own sends. Empty windows skip inference.
2. The harness sends four ordered prompt layers: stable secretary system instructions, operator-authored contextual
   knowledge, principal-specific instructions and summary limit, then the current event window. The first three
   layers are intended to remain stable across assessments; layered caching puts explicit breakpoints on them
   and keeps the provider's implicit window/tool-result boundary for reuse within an assessment.
   Implicit mode omits explicit breakpoints for compatible endpoints or older
   models. Cache hits are not guaranteed: model support, minimum prompt size, cache expiry, and provider reporting
   affect them. The harness does not add padding to chase a cache hit.
3. The model interprets the whole window and either returns the strict assessment (`wake`, `hold`, or `keep`, reason,
   summary, and source keys) or optionally calls `decide_attention` when an independent typed opinion would help.
4. The optional tool sends the full authorized context to a configured decision engine and returns only its typed
   decision. The model remains responsible for its own grounded explanation and summary. An unavailable engine
   returns an error to the model; it never fabricates a `wake` verdict. The model may also finish without calling it.
5. The harness appends each model response and matching tool result to the conversation, under one overall callback
   deadline chosen by the host (at most 60 seconds) and a default limit of two tool calls (configurable up to eight).
   Successful engine verdicts are reused within that assessment. The response is validated and source visibility
   rechecked before delivery.
   Delivery coverage advances only after the host confirms the brief was delivered.

The model weighs semantic relevance and actionability across the pending window; there is no deterministic
keyword fallback. `wake` means attention is useful now, `hold` means useful later, and `keep` means no attention is
needed now. On alerts, hold and keep have empty summaries. Wakes and digests include a concise narrative that
covers material updates, identifies who needs what, and cites relevant supplied sources.

## Setup

Choose a private state path. A new state file is created on first use. Existing model-only v2 state and delivered
coverage remain valid; only state from the retired deterministic/rules-era format is incompatible. When a policy or
prompt change alters the cache fingerprint, prior cached assessments are invalidated while delivery coverage is
preserved. Policy version 2 is the supported format. The default lightweight model is `gpt-6-luna` through the OpenAI
Responses API, using `OPENAI_API_KEY` from the service environment.

```json
{
  "version": 2,
  "principal": "planner",
  "knowledge": "The release window closes at 17:00 UTC.",
  "instructions": "Prefer waking me when another agent is blocked on my decision.",
  "max_tool_calls": 2,
  "model": {
    "provider": "openai",
    "model": "gpt-6-luna",
    "cache_mode": "layered"
  }
}
```

`knowledge` is operator-authored context shared for this policy; `instructions` is static guidance for the named
principal. Each is limited to 8 KiB; the full policy is limited to 16 KiB. `max_tool_calls` defaults to two and may
be set from one to eight. `model.cache_mode` is `layered` by default: three stable breakpoints plus an implicit
boundary for the current window or tool result. This preserves reusable prefixes at different lifetimes and
allows the tool follow-up to reuse its unchanged event input. Use `implicit` for compatible endpoints or older
models without explicit breakpoint support. The harness does not add padding to pursue cache hits.
`result.harness.usage` contains the language model's reported input, output, and cached-token totals when
available, otherwise `null`; these totals exclude decision-engine usage. Reusing a previously cached assessment
reports its historical usage, not usage from a new model call.

The decision tool is optional and is not called automatically. Configure either the TypeSafe JEV choice API:

```json
"decision_engine": {
  "provider": "jev",
  "model": "jev-latest",
  "api_key_env": "TYPESAFE_API_KEY",
  "base_url": "https://api.typesafe.ai/v1",
  "timeout_seconds": 3
}
```

or a local command adapter:

```json
"decision_engine": {
  "provider": "command",
  "command": ["/opt/secretary/decision-adapter"],
  "timeout_seconds": 3
}
```

JEV returns a `wake`, `hold`, or `keep` choice with confidence and a probability distribution. Baton validates that
the distribution is well formed and that the selected choice is the most likely, then exposes only
`{"decision":"..."}` to the model; the engine supplies no rationale. A command engine receives protocol version 1
with task `decide_attention`, the full authorized context, and the three allowed choices; it must return exactly
`{"decision":"wake|hold|keep"}`. The async `DecisionEngine` protocol accepts context and a timeout and returns an
`AttentionDecision`, so another typed engine can replace either adapter. The retired advisor protocol is not
compatible. Result metadata reports `engine_status` as `not_configured`, `not_used`, `available`, or `unavailable`.

The model can instead run as a host command, independent of the optional decision engine. It receives one JSON
request on stdin and returns one JSON object on stdout, without a shell:

```json
"model": {"provider": "command", "command": ["/opt/secretary/model-adapter"], "timeout_seconds": 20}
```

The command request has this envelope:

```json
{"version": 3, "input": [], "tools": [], "response_schema": {}}
```

The actual `input`, `tools`, and `response_schema` values are supplied by the harness. Return either the structured
assessment, or `{"tool_calls":[{"id":"call-1","name":"decide_attention","arguments":{}}]}`. After a tool call,
the next request includes the model's prior output and a `function_call_output` with the matching call ID. The old
version 2 model-command envelope is retired. Unknown tools, non-empty arguments, invalid/reused call IDs, or
exceeding the call limit fail the assessment.

Command adapters should preserve the supplied input order and tool history when constructing their provider
request. They own provider-specific cache settings; the direct OpenAI transport supplies them itself. Migrating
an existing adapter means accepting protocol 3 and replacing any old automatic advisor configuration with an
explicit `decision_engine.provider`. Existing model-only state and acknowledged coverage stay in place.

The OpenAI Responses API is stateless across calls; the harness resends the complete ordered input and append-only
tool history for each turn. It requests strict structured output and encrypted reasoning continuation. See the
[prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching) and
[function calling guide](https://developers.openai.com/api/docs/guides/function-calling). For the optional engine,
see TypeSafe's [System One API](https://docs.typesafe.ai/api/system-one) and
[Choice primitive](https://docs.typesafe.ai/primitives/choice).

## Host API and delivery

An async host can use the service directly:

```python
from pathlib import Path
from baton.agents.secretary import Secretary

secretary = Secretary(config, Path("/private/secretary.sqlite"), Path("/private/policy.json"))
result = await secretary.decide(budget_seconds=30, trigger="message:42")
if result["decision"] == "wake":
    await deliver_to_principal(result["brief"])
    await secretary.acknowledge(result["decision_id"])
```

On first use, the secretary reads from its saved activation floor (initially zero). Hosts that own delivery
positions pass `since` and `floor` together on every call. Acknowledge only after successful delivery. A hold,
keep, error, or empty inbox does not advance coverage. Repeating the same policy, prompt, and inbox can reuse the
saved assessment.

`trigger` may identify `message:SEQ`, `job:JOB_ID:SEQ`, or `conversation`. Use `hourly=True` for a digest; it uses
the same model assessment and can summarize even when no alert decision is pending. The host chooses a callback
budget greater than zero and at most 60 seconds; the same budget bounds the full model/tool conversation. Model
timeout defaults to 20 seconds.

Results contain a `brief`, cited `sources`, and `source_refs` resolving keys to current message/thread IDs or
event positions. The model receives authorized message text in `context.events` and visible parents in
`context.reply_context`; it cannot rewrite or replace that evidence. The service checks every citation against the
supplied context and rereads visibility after inference. If content or visibility changed, it returns a retryable
`secretary_unavailable` error without advancing delivery. It does not independently verify a summary's meaning.
The private sidecar stores assessments and references, not message bodies. A tool transport failure is returned
to the model as `decision_engine_unavailable`, allowing the model to finish its own assessment. Model transport,
invalid protocol/assessment, or exhausted callback budget produces a retryable `secretary_unavailable` error and
does not advance delivery.

By default, at most 1,000 event/context entries and 1 MiB of serialized context are accepted; oversized windows
fail as `secretary_context_limit` instead of being truncated. `summary_bytes` defaults to 8 KiB. Model and
protocol failures and exhausted callback budgets are retryable `secretary_unavailable` errors. A missing model
credential is reported explicitly.

## CLI

```sh
baton secretary --state /private/secretary.sqlite --policy /private/policy.json decide \
  --budget-seconds 30 --trigger message:42
baton secretary --state /private/secretary.sqlite --policy /private/policy.json show
baton secretary --state /private/secretary.sqlite --policy /private/policy.json acknowledge DECISION_ID
baton secretary --policy /private/policy.json read --ids msg_0123456789abcdef
```

The CLI supports `decide`, `show`, `acknowledge`, and `read`. The `show` command inspects saved assessments;
`read` applies current visibility rules. Existing model-only v2 state remains valid; only retired
deterministic/rules-era state requires a new state file. A prompt or policy fingerprint change invalidates cached
assessments while retaining acknowledged delivery coverage.

## Code map

| Module | Responsibility |
|---|---|
| [service.py](service.py) | Async host API and delivery acknowledgment. |
| [decide.py](decide.py) | Reads the authorized window, applies callback budget, caches identical assessments, and rechecks visibility. |
| [inbox.py](inbox.py) | Builds the complete pending context and rejects over-limit windows. |
| [harness.py](harness.py) | Runs bounded model/tool turns and aggregates model usage. |
| [prompt.py](prompt.py) | Stable prompt layers, explicit cache breakpoints, and the strict assessment schema. |
| [model.py](model.py) | OpenAI Responses and protocol version 3 command transports. |
| [tools.py](tools.py) | Optional model-callable decision tool, per-assessment verdict reuse, and engine status. |
| [decisions.py](decisions.py) | Typed decision protocol and JEV/command adapters. |
| [transport.py](transport.py) | Bounded HTTP and command transport helpers. |
| [assessment.py](assessment.py) | Validates decisions, summary bounds, and source references. |
| [policy.py](policy.py) | Validates policy version 2, model/engine settings, and limits. |
| [state.py](state.py) | Private assessment cache, activation/delivery positions, and acknowledgment. |

The Codex and Claude Code integrations use the same secretary harness; principal host choice does not change policy
or model behavior. Principal feedback remains a future direction; see the [roadmap](../../../docs/roadmap.md).
