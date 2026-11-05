# Baton secretary (policy version 2)

The secretary is a lightweight model-driven attention layer for one agent, the **principal**. The host wakes it
when Baton reports activity. It reads the principal's pending, currently visible messages and events together,
then asks a model whether the more expensive principal needs attention now. A wake result includes a narrative
summary and source references. The host delivers it; the principal can then decide which threads to open.
The secretary itself never sends that notification or advances the principal's Baton inbox.

For a persistent worker and Codex or Claude Code hooks, use the
[secretary integration skills](../../../integrations/secretary/README.md). The integration owns process
supervision and delivery; this package owns model triage and acknowledged coverage.

The model is the interpreter. It weighs meaning and actionability across the whole pending window; there is no
deterministic rule-word fallback. An optional decision engine can provide extra advice to the model. If that
advisory command fails, the model still assesses the inbox without it. Policy version 2 is the only supported
policy format; older policies are unsupported and require a current policy and a new state file.

## What happens on an alert

1. Read all pending visible events, deduplicating recipient copies of the same message. Include visible reply
   parents as context even when they precede the last delivered summary.
2. If configured, ask the decision engine for advisory signals about that window.
3. Give the lightweight model the messages, reply context, operator preferences, and engine advice. The model
   interprets the conversation and chooses an attention decision; Baton does not derive it from words or scores.
4. In the same inference, the model writes a summary when it chooses to wake the principal, or when asked for a
   scheduled digest. It covers the material information across the pending messages, grouping related updates.
5. Validate the response format, size, and cited IDs, and recheck source visibility. Return the assessment to
   the host. Save delivery coverage only after the host confirms delivery.

| Model decision | Meaning on an alert |
| --- | --- |
| `wake` | Interrupt the principal with the generated summary and source references. |
| `hold` | Useful information can wait; retain it for another alert or a scheduled digest. |
| `keep` | No attention is needed now; keep the underlying messages available in Baton. |

For example, the window may contain “Migration finished” from the worker followed by “Health checks regressed;
can you approve a rollback?” from the reviewer. The secretary should explain both the completion and the new
blocker, identify who needs a decision, and cite the messages. It should not wake merely because one message
contains an answer word, or summarize only the last alert. These are model instructions, not hard-coded rules.

## First setup

Choose a private state path; the secretary creates the file on first use. State from the retired rules-based
workflow cannot be reused. A minimal policy names the principal:

```json
{
  "version": 2,
  "principal": "planner"
}
```

The principal must be an existing non-human agent role. The default model is `gpt-6-luna` through the OpenAI
Responses API, with `OPENAI_API_KEY` read from the host process environment. A missing key produces an explicit
`secretary_unavailable` error; there is no silent local or rule-based fallback.

The default model can be customized. For example:

```json
{
  "version": 2,
  "principal": "planner",
  "model": {
    "provider": "openai",
    "model": "gpt-6-luna",
    "api_key_env": "OPENAI_API_KEY",
    "reasoning_effort": "low",
    "timeout_seconds": 20
  },
  "instructions": "Prefer waking me for decisions that block another agent.",
  "max_events": 1000,
  "context_bytes": 1048576,
  "summary_bytes": 8192
}
```

`base_url` can point to another Responses-compatible endpoint; it must use HTTPS, except HTTP on localhost. The
model can also run as a host command. The command receives one JSON request on stdin and writes one JSON object
to stdout; Baton starts an argv list directly, without a shell:

```json
{
  "version": 2,
  "principal": "planner",
  "model": {
    "provider": "command",
    "command": ["/opt/secretary/model-adapter"],
    "timeout_seconds": 20
  }
}
```

The request envelope is `{ "version": 2, "instructions": ..., "context": ..., "response_schema": ... }`.
The adapter returns an assessment object with exactly `decision`, `reason`, `summary`, and `sources`; `decision`
is `wake`, `hold`, or `keep`. The schema is supplied in the request so the host command can enforce the same
contract as the OpenAI structured output path.

## One wake callback

An async host can call the public service directly:

```python
from pathlib import Path
from baton.agents.secretary import Secretary

secretary = Secretary(config, Path("/private/secretary.sqlite"), Path("/private/policy.json"))
result = await secretary.decide(budget_seconds=30, trigger="message:42")
if result["decision"] == "wake":
    await deliver_to_principal(result["brief"])
    await secretary.acknowledge(result["decision_id"])
```

On its first call, the secretary uses its saved activation floor and delivery position (initially zero) and reads
the full pending visible window. For a host that owns positions,
pass `since` and `floor` together on every call. After successfully delivering a non-empty `brief`, either call
`acknowledge(decision_id)` or pass that result's `covered_through` as `since` with the same `floor` next time.
Advance only after delivery succeeds. A `hold`, `keep`, error, or empty inbox does not advance coverage. Retries
with the same policy, prompt, and identical inbox reuse the saved assessment instead of making another model call.

`trigger` is optional and may identify `message:SEQ`, `job:JOB_ID:SEQ`, or `conversation`. A one-hour digest uses
`hourly=True`; it asks the same model to summarize the pending window. An empty inbox skips inference and returns
`keep`. Every callback budget is greater than zero and at most 60 seconds; the default model timeout is 20 seconds.

### Results and source references

The model makes one assessment of the entire alert window and returns its decision and summary together. A wake
has a concise narrative that says what changed, who needs what, and what remains unresolved, so the principal can
choose which thread to open. A digest also has a summary. `hold` and `keep` alert results have an empty `brief`.
The returned `sources` list names the cited source keys, and `source_refs` resolves them to current message and
thread IDs (or event positions). The host can pass those references to its thread-reading UI; the secretary does
not open threads on the principal's behalf.

The model receives authorized message text in `context.events` and visible parent messages in
`context.reply_context`. The prompt treats messages and engine advice as untrusted data, not instructions.
The service checks that every citation was in this supplied context and re-reads it after inference. If content or visibility
changed during the call, it returns a retryable `secretary_unavailable` error without advancing delivery. It does
not independently verify the meaning of a generated summary. The private sidecar stores generated assessments
and references, but does not archive message bodies.

By default the model sees at most 1,000 event/context entries and 1 MiB of their serialized content. `max_events` and `context_bytes` are
configurable. If the complete window exceeds either limit, the call fails with `secretary_context_limit`; it does
not silently truncate the context. `summary_bytes` defaults to 8 KiB. Model timeout, transport, command, or
assessment errors become retryable `secretary_unavailable` errors and leave the delivery position unchanged.

## Optional decision-engine advice

An optional command can provide a separate advisory input before the primary model call:

```json
{
  "version": 2,
  "principal": "planner",
  "decision_engine": {
    "command": ["/opt/secretary/advisor"],
    "timeout_seconds": 3
  }
}
```

It receives `{ "version": 2, "task": "advise_secretary", "context": ... }` and must return a JSON object.
That object is passed to the configured model as `context.engine_advice`; it does not make the final decision. If
the advisory command times out, exits unsuccessfully, or returns unusable JSON, the model proceeds without advice.
An adapter can provide advice in this protocol; Baton does not bundle a decision-engine SDK.

The model command's response is the decision itself, for example (replace the illustrative source IDs):

```json
{
  "decision": "wake",
  "reason": "The reviewer needs the principal's rollback decision.",
  "summary": "The worker reports the migration is complete, but the reviewer reports regressed health checks and asks you to approve a rollback. Open the deployment thread to decide.",
  "sources": ["message:msg_worker_update", "message:msg_reviewer_request"]
}
```

`reason` explains the attention choice. `summary` is delivered as the result's `brief`. Citations must identify
supplied events or reply parents; a wake or nonempty digest needs a summary and at least one citation. A normal
hold/keep alert has an empty summary. The optional engine's JSON is advice, not this final assessment.

## CLI

Run against the principal's policy and private state:

```sh
baton secretary --state /private/secretary.sqlite --policy /private/policy.json decide \
  --budget-seconds 30 --trigger message:42
baton secretary --state /private/secretary.sqlite --policy /private/policy.json show
baton secretary --state /private/secretary.sqlite --policy /private/policy.json acknowledge DECISION_ID
baton secretary --policy /private/policy.json read --ids msg_0123456789abcdef
```

The CLI supports `decide`, `show`, `acknowledge`, and `read`. `show` inspects saved assessments. `read` uses
current visibility rules to return source messages. An older policy is unsupported; create a policy version 2
and a new private state file rather than reusing old state. The short Python modules `baton.secretary_decide` and
`baton.secretary_read` remain aliases for the current model-driven implementation when run with `python -m`, with no deterministic fallback.

## Code map

| Module | Responsibility |
|---|---|
| [service.py](service.py) | Async host API and delivery acknowledgment. |
| [decide.py](decide.py) | Reads the window, enforces the callback budget, caches identical assessments, and rechecks visibility. |
| [inbox.py](inbox.py) | Builds the complete pending context and rejects over-limit windows. |
| [model.py](model.py) | OpenAI Responses and host-command transports; optional advisory command. |
| [prompt.py](prompt.py) | Model instructions and structured response schema. |
| [assessment.py](assessment.py) | Validates decisions, summary bounds, and source references. |
| [policy.py](policy.py) | Validates the supported version 2 configuration. |
| [state.py](state.py) | Private v2 assessment cache, activation/delivery positions, and acknowledgment. |

For the OpenAI transport, see the [Responses API structured outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs)
and [GPT-6 Luna model reference](https://developers.openai.com/api/docs/models/gpt-6-luna).

Principal feedback for the secretary is a future direction; see the [roadmap](../../../docs/roadmap.md).
