# Baton secretary (policy version 3)

The secretary is a lightweight model harness for one Baton agent, the **principal**. When visible activity arrives,
it reads bounded, complete pages and asks a model to assess whether the principal needs attention. A wake
includes a grounded summary and source references for the host to deliver. The secretary never sends that
notification itself or advances the principal's inbox.

## How an assessment works

1. Baton builds an authorized, bounded, complete page of visible events, including visible reply parents and
   context about the principal's own sends. Page size also reserves model output space for required citations.
   Pages are never truncated. An `InboxTooLarge` page can be retried and split; an event that still cannot fit
   receives an operational notice. A trigger outside the bounded page is an error.
2. The harness sends four ordered prompt layers: stable secretary system instructions, operator-authored contextual
   knowledge, principal-specific instructions and summary limit, then the current event window. The first three
   layers are intended to remain stable across assessments; layered caching puts explicit breakpoints on them
   and keeps the provider's implicit window/tool-result boundary for reuse within an assessment.
   Implicit mode omits explicit breakpoints for compatible endpoints or older
   models. Cache hits are not guaranteed: model support, minimum prompt size, cache expiry, and provider reporting
   affect them. The harness does not add padding to chase a cache hit.
3. On every alert page, the configured independent decision engine returns a typed decision and wake probability. If it
   returns `wake` with `wake_probability` at or above `wake_threshold`, Baton forces a wake even when model triage holds, keeps, or fails.
4. The model interprets the page and returns the strict assessment (`wake`, `hold`, or `keep`, reason, summary,
   and source keys). It may call `decide_attention` to retrieve the engine's already cached verdict. The tool
   returns only decision and wake probability; the engine cannot explain its choice. The model owns the grounded
   explanation and summary. If model output is unavailable for a forced wake, Baton issues a source-only notice.
5. The harness appends each model response and matching tool result to the conversation, under one overall callback
   deadline chosen by the host (at most 60 seconds) and the policy's required tool-call limit (one to eight).
   The independent verdict is evaluated once for that alert and reused if the model calls the tool. Every summary,
   including a digest, must account for and cite every event in the page; it may cite visible reply parents too.
   The response's source references are checked against offered context and source visibility is rechecked, but
   Baton does not verify the truth of summary claims. The prompt forbids invented assignments or approvals.
6. Scan progress and delivered coverage are separate. Failed pages retry in their original mode with backoff.
   Invalid model output and oversized windows count toward `max_attempts` and may split. Timeouts, rejected
   requests, and failed commands count only when another page has received a valid model answer under the same
   policy since this page last failed. A sustained model outage therefore keeps pages intact.
   Each poll reserves one full budget
   for a fresh alert and one for scheduled work. It runs one eligible scheduled lane per poll, alternating retry
   and digest priority, so a lane never receives a sliver of the budget. Held contiguous pages are batched into a
   bounded digest after `digest_seconds`.
   Pending receipts retain all decision IDs, surface wakes first, and announce new IDs even if older ones remain
   unacknowledged. Acknowledgement is idempotent by summary ID, and delivered ranges merge only when contiguous.

The model weighs semantic relevance and actionability across each page; there is no deterministic keyword
fallback. `wake` means attention is useful now, `hold` means useful later, and `keep` means no attention is
needed now. Every summary, including a digest, must account for and cite all page events. On alert pages, hold and
keep have empty summaries.

## Policy

Policy version 3 requires a principal, stable `secretary_id`, explicit model and decision-engine settings, and
all page, retry, threshold, and digest limits. The model ID and provider URL are explicit policy choices; the
example's `gpt-6-luna` is not a forced default.

```json
{
  "version": 3,
  "principal": "planner",
  "secretary_id": "planner-attention",
  "knowledge": "The release window closes at 17:00 UTC.",
  "instructions": "Prefer waking me when another agent is blocked on my decision.",
  "max_tool_calls": 2,
  "model": {
    "provider": "openai",
    "model": "gpt-6-luna",
    "base_url": "https://api.openai.com/v1",
    "api_key_env": "OPENAI_API_KEY",
    "timeout_seconds": 20,
    "reasoning_effort": "medium",
    "max_output_tokens": 4096,
    "cache_mode": "layered"
  },
  "decision_engine": {
    "provider": "jev",
    "model": "jev-latest",
    "base_url": "https://api.typesafe.ai/v1",
    "api_key_env": "TYPESAFE_API_KEY",
    "timeout_seconds": 3
  },
  "max_events": 1000,
  "context_bytes": 1048576,
  "summary_bytes": 4000,
  "wake_threshold": 0.8,
  "retry_initial_seconds": 5,
  "retry_max_seconds": 300,
  "max_attempts": 3,
  "digest_seconds": 3600
}
```

The model and engine objects are both required. The model's OpenAI configuration requires explicit model ID,
base URL, credential variable, timeout, reasoning effort, output-token limit, and cache mode. The JEV engine
requires its model ID, base URL, credential variable, and timeout; do not add model reasoning or output-token
fields to its configuration. `wake_threshold` is the minimum independent-engine wake probability that forces a wake;
the example's `0.8` is configurable, not a hard-coded cutoff.
`model.reasoning_effort` must contain 1–100 printable characters without surrounding whitespace; the provider
determines accepted values, so values such as `minimal` are valid policy inputs.
`retry_initial_seconds` and `retry_max_seconds` bound the retry backoff. `max_attempts` limits failures that are
specific to a page, using the evidence described above, before splitting that page.
`digest_seconds` schedules held-page digests. `knowledge` and `instructions` are operator-authored text, each
limited to 8 KiB; the full policy is limited to 16 KiB. `max_tool_calls` ranges from one to eight.
`max_events` bounds page event/context entries, `context_bytes` bounds serialized input, and `summary_bytes`
bounds each summary. `wake_threshold` ranges from 0 to 1. `model.cache_mode` must be explicitly `layered` or
`implicit`.
Set HTTP credentials in the worker's service environment, using the policy's `api_key_env` names.
`result.harness.usage` contains the language model's reported input, output, and cached-token totals when
available, otherwise `null`; these totals exclude decision-engine usage. Reusing a previously cached assessment
reports its historical usage, not usage from a new model call.

The independent engine runs on each alert page. The model may call `decide_attention` to retrieve that page's
cached verdict; tool use is optional and does not trigger another engine evaluation. The engine can return only
a typed decision and wake probability, with no rationale. Configure either the TypeSafe JEV choice API:

```json
"decision_engine": {
  "provider": "jev",
  "model": "jev-latest",
  "base_url": "https://api.typesafe.ai/v1",
  "api_key_env": "TYPESAFE_API_KEY",
  "timeout_seconds": 3
}
```

or a local command adapter:

```json
"decision_engine": {
  "provider": "command",
  "command": ["/opt/secretary/decision-adapter"],
  "env": ["TYPESAFE_API_KEY"],
  "timeout_seconds": 3,
  "max_output_tokens": 1024
}
```

JEV returns a typed choice and probability distribution, which Baton validates. `wake_threshold` uses
`probabilities["wake"]`; JEV's separate wire `confidence` field is validated but does not affect the threshold.
Its choice API has no text-generation effort or output-token setting; Baton does not invent those parameters.
A command engine receives protocol version 3 with task `decide_attention`, the full authorized context, choices,
and output limit. It must return exactly `{"decision":"wake","wake_probability":0.91}` (with one allowed
decision and wake probability from 0 to 1).
The command's required `env` list is an explicit allowlist of service environment variables forwarded to it.

The model can instead run as a host command, independent of the decision engine. It receives one JSON
request on stdin and returns one JSON object on stdout, without a shell:

```json
"model": {"provider": "command", "command": ["/opt/secretary/model-adapter"], "env": ["MODEL_TOKEN"], "timeout_seconds": 20, "max_output_tokens": 4096}
```

The command request has this envelope:

```json
{"version": 4, "input": [], "tools": [], "response_schema": {}, "max_output_tokens": 4096}
```

The actual `input`, `tools`, and `response_schema` values are supplied by the harness. Return either the structured
assessment, or `{"tool_calls":[{"id":"call-1","name":"decide_attention","arguments":{}}]}`. After a tool call,
the next request includes the model's prior output and a `function_call_output` with the matching call ID. The old
version 3 model-command envelope is retired. Unknown tools, non-empty arguments, invalid/reused call IDs, or
exceeding the call limit fail the assessment.

Command adapters should preserve input order and tool history. Their required `env` list forwards only named
service environment variables; set credentials in the service environment and list their names in the policy.
The required `timeout_seconds` and `max_output_tokens` bound each command. Commands still run as the Baton host
user and have access to that user's files; environment filtering is not a filesystem sandbox. The decision
command's version 3 request uses the exact `{"decision":"wake","wake_probability":0.91}` response shape.

The OpenAI Responses API is stateless across calls; the harness resends the complete ordered input and append-only
tool history for each turn. It requests strict structured output and encrypted reasoning continuation. See the
[prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching) and
[function calling guide](https://developers.openai.com/api/docs/guides/function-calling). For the JEV decision engine,
see TypeSafe's [System One API](https://docs.typesafe.ai/api/system-one) and
[Choice primitive](https://docs.typesafe.ai/primitives/choice).

## Host API and delivery

An async host can use the service directly:

```python
from pathlib import Path
from baton.agents.secretary import Secretary

secretary = Secretary(config, Path("/private/secretary.sqlite"), Path("/private/policy.json"))
result = await secretary.decide(budget_seconds=30, trigger="message:42")
if result["brief"]:  # Deliver and acknowledge digest summaries and operational notices too.
    await deliver_to_principal(result["brief"])
    await secretary.acknowledge(result["decision_id"])
```

Installation binds the activation floor to the current database head. Standalone implicit decisions use the
durable scan position; passing both `since` and `floor` requests an exact replay. The scan position advances
independently of the contiguous delivered watermark. Acknowledge each validated summary
ID after successful delivery; retries with the same ID are safe. A later acknowledgement cannot advance across an
earlier unacknowledged gap.

`trigger` may identify `message:SEQ`, `job:JOB_ID:SEQ`, or `conversation`. It must fall within the bounded page.
Use `hourly=True` for a digest of contiguous due held pages. The host chooses a callback budget greater than zero
and at most 60 seconds; the engine timeout must be shorter than the assessment budget. Installed integrations check
this at install and startup. Integration delivery has a separate required `delivery_timeout_seconds` setting; it
does not inherit the engine timeout. Standalone decisions process one due retry before scanning new work, even
when called with a newer trigger. In that case the returned page has no newer trigger attached; handle its result,
then call again for the newer event. An untriggered call while failed work is in backoff raises retryable
`secretary_unavailable`. Calls for a different new event may still assess that fresh page during the backoff.

Results contain a `brief`, cited `sources`, and `source_refs` resolving keys to current message/thread IDs or
event positions. Every summary, including a digest, must account for and cite every event on its page; visible
parents may also be cited.
The model receives authorized message text in `context.events` and visible parents in
`context.reply_context`; it cannot rewrite or replace that evidence. The service checks every citation against the
supplied context and rereads visibility after inference. If content or visibility changed, it returns a retryable
`secretary_unavailable` error without advancing delivery. It does not independently verify a summary's meaning.
The private sidecar stores full assessment results, including the reason, brief, and source references, but not
source message bodies. It is protected by the private-file checks and remains until the operator removes it; there
is no automatic assessment cleanup. Deliverable summaries are also written to the append-only `secretary_summaries`
table in `baton.db`, tagged with stable secretary ID and principal. `baton secretary --policy FILE summaries`
pages this shared archive without `--state`; it accepts `--after` and `--limit` and returns `items`, `next`, and
`has_more`.
Authorized message context is sent to the configured model provider or host command, and the configured decision
engine receives context for each alert page. Host commands run as the Baton host user;
their environment filtering does not sandbox access to files available to that user. A decision-engine transport
or timeout failure is returned to the model as `decision_engine_unavailable`, allowing it to finish its own
assessment. Sanitized engine and model failures are logged and exposed independently in worker health, including
`missing_credential`, `authentication`, `rejected`, `timeout`, and `unavailable` model categories. The worker checks
for a missing model key at startup, even before an alert, and reports degraded health while a component is failing.
Successful model answers clear its current failure status while retaining the last failure. If a wake verdict
forces attention but the model cannot summarize it, the worker
emits a forced-wake notice; a model outage without a forced wake remains an outage and does not become an alert.
Digest failures retain digest mode and eventually produce a keep notice, never an alert or wake. Transport failures
and timeouts back off in their original mode. Invalid model protocol/content and oversized windows count toward
`max_attempts` and may split. A timeout, rejected request, or failed command also counts when another page has
received a valid model answer under the same policy since this page last failed. Without that evidence, the page
continues backing off; elapsed time alone never turns a model outage into per-event wake notices.

Before retrying, the queue checks for an already persisted result for the same policy, page, mode, activation floor,
and current source content. It finishes that result without invoking the engine or model again. This closes the
restart window between saving a summary or notice and recording queue completion.

Pages use the configured `max_events`, `context_bytes`, `summary_bytes`, and citation budget. They are complete
and never silently truncated. Retried and split pages preserve their original alert or digest mode.
No deterministic semantic fallback is used. Worker health is tracked separately from page retries. A held page
does not produce an immediate alert; contiguous due held pages are combined into one bounded hourly digest.

## CLI

```sh
baton secretary --state /private/secretary.sqlite --policy /private/policy.json decide \
  --budget-seconds 30 --trigger message:42
baton secretary --state /private/secretary.sqlite --policy /private/policy.json show
baton secretary --state /private/secretary.sqlite --policy /private/policy.json acknowledge DECISION_ID
baton secretary --policy /private/policy.json read --ids msg_0123456789abcdef
baton secretary --policy /private/policy.json summaries --after 0 --limit 50
baton --host https://baton.example secretary summaries --after 0 --limit 50
```

The CLI supports `decide`, `show`, `acknowledge`, `read`, and `summaries`. Local summaries use the policy principal;
remote summaries use the authenticated token's role and need no host-side policy file. The MCP tool
`read_secretary_summaries` offers the same role-scoped archive read. Other secretary actions remain host-local.
The `show` command inspects saved
assessments; `read` applies current visibility rules. Private state version 1 is refused; use a new file. State
version 2 is preserved, concurrent initialization is serialized, and harness version 4 invalidates older
unchecked cache entries. The shared database uses schema version 22; version 21 added the append-only summary
archive, and version 22 indexes message identities for source visibility checks. `summaries` pages archive entries
for the policy's principal without a private state file.

## Code map

| Module | Responsibility |
|---|---|
| [service.py](service.py) | Async public host API and delivery acknowledgment. |
| [attention/decide.py](attention/decide.py) | Assessment lifecycle: authorized window, callback budget, cache, and visibility recheck. |
| [attention/assessment.py](attention/assessment.py) | Validates decisions, summary bounds, and source references. |
| [attention/state.py](attention/state.py) | Private assessment cache, activation/delivery positions, and acknowledgment. |
| [attention/backlog.py](attention/backlog.py) | Durable page scan, retry, digest, and pending-delivery work. |
| [attention/archive.py](attention/archive.py) | Append-only delivery summaries shared through the Baton database. |
| [inbox/window.py](inbox/window.py) | Builds the complete pending context and rejects over-limit windows. |
| [inbox/read.py](inbox/read.py), [inbox/sources.py](inbox/sources.py) | Read-only source access and current visibility checks. |
| [harness/run.py](harness/run.py) | Runs bounded model/tool turns and aggregates model usage. |
| [harness/prompt.py](harness/prompt.py) | Stable prompt layers, explicit cache breakpoints, and strict assessment schema. |
| [harness/tools.py](harness/tools.py) | Model-callable access to the per-alert independent decision verdict. |
| [connections/model.py](connections/model.py) | OpenAI Responses and protocol version 4 command model transports. |
| [connections/decisions.py](connections/decisions.py) | Typed decision protocol and JEV/command adapters. |
| [connections/transport.py](connections/transport.py), [connections/command.py](connections/command.py) | Bounded HTTP and command transport helpers. |
| [../../config/secretary.py](../../config/secretary.py) | Central policy validation and installed-instance configuration (`Policy`, `load_policy`, `fingerprint`, `IntegrationConfig`). |

The secretary package is divided by lifecycle and responsibility: `attention` owns assessment state and validation,
`inbox` owns authorized source context, `harness` owns prompts and bounded model/tool turns, and `connections`
owns external transports. Child package `__init__.py` files are docstrings only; import from canonical modules,
such as `from baton.agents.secretary.harness.run import SecretaryHarness`. The root package continues to expose
`Secretary`, `show`, `Policy`, and `load_policy`. Policy and installed-instance configuration code is centralized
in `baton.config.secretary`. The Baton host TOML format is unchanged. Secretary policy version 3 is required;
older policy versions are unsupported. Private state version 1 is refused and needs a new file; version 2 is
preserved. The installed integration JSON remains a separate version 1 worker configuration.

The Codex and Claude Code integrations use the same secretary harness; principal host choice does not change policy
or model behavior. `python -m baton.secretary_decide` and `python -m baton.secretary_read` are compatibility
module wrappers. There is no `baton.secretary_read` executable. Canonical module commands are
`python -m baton.agents.secretary.attention.decide` and `python -m baton.agents.secretary.inbox.read`; callers
should import from canonical modules or use the documented root API.
Principal feedback remains a future direction; see the [roadmap](../../../docs/roadmap.md).
