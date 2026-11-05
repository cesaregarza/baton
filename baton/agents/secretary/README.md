# Baton secretary

The secretary is an attention layer for one agent, called the **principal**. It reads that agent's visible Baton
messages and job events, remembers outstanding requests, and recommends whether the host should interrupt the
agent now or save the information for a later digest. The underlying messages stay in Baton either way.

There are two interpretation paths: **built-in rules**, which need no model, and an **optional external judge**,
which can use an LLM. Both feed the same request ledger and attention selection. The external judge interprets
one message; Baton converts that interpretation into `wake`, `hold`, or `keep` for the outstanding event window.
Judging a reported result does not verify that the work actually happened.

The host supplies callbacks, scheduling, and delivery. This package does not run a background watcher, send a
notification, or advance the principal's Baton inbox cursor.

## From an event to a decision

A normal callback goes through these steps:

1. **Read new visible events in order.** `DecisionRun` asks `ScanBatch` to read pages after the saved scan position.
   `sources.py` supplies message identity, authored text, reply and attention metadata, artifact references, and
   job handoffs. Previously processed sources are not interpreted again.
2. **Find what this event could refer to.** `Expectations` tracks requests that are **owed** by the principal or
   **awaited** from someone else. For a possible answer, it matches the author to the expected responder, the job
   or conversation to the request's context, and an explicit reply to the opening message.
3. **Interpret the event.** Messages normally go through `MessageRules`. During a callback, a configured
   `JudgeSession` can instead send an eligible message and its candidate requests to an external command.
   Job handoffs always use the built-in job rules. The next two sections explain both message paths.
4. **Update the ledger and record a disposition.** Interpretation can open a request, acknowledge it, close it,
   or retain uncertain evidence. It also records `retain`, `route`, `digest`, `brief`, or `reconcile` for this
   source. Each page's ledger changes, dispositions, and scan position commit together.
5. **Choose attention across the outstanding window.** `AttentionWindow` looks at all recorded sources after
   the host's last delivered position, including older undelivered sources. It maps dispositions to
   `wake`/`hold`/`keep`, picks the strongest, then applies the callback's trigger restrictions.
6. **Return a result to the host.** A wake includes a bounded brief. Hold and keep include reasons and counts but
   no brief. The host decides whether to deliver; only successful delivery should advance its saved position.

Interpretation and notification are separate because evidence can change without an interruption. For example,
“starting” acknowledges a request but leaves it open. A later hourly digest can mention that acknowledgment.

## The built-in rule engine

[`MessageRules`](rules.py) uses message structure and configured word lists. It does not ask a model to infer intent.
An addressed message is a direct message to the principal, an attention tag naming it, or a reply to its message.

For a possible answer to an existing request, the rules apply these checks in order:

1. An unrelated artifact does not identify the request. A stale revision is retained as evidence without closing it.
2. An answer with the wrong result kind produces `reconcile`. Kinds come from words such as `check`/`test`/`ci`,
   `review`, and `release`; an untyped answer cannot satisfy a review or release request.
3. An uncertain artifact match or conflicting answer signals produces `reconcile`.
4. A configured answer phrase plus an identifying artifact, or a matching reply for an artifactless request,
   satisfies the request. An awaited result produces `brief`; completing the principal's own owed work produces
   `retain`.
5. An acknowledgment records evidence and leaves the request open: `digest` for an awaited result, `retain` for
   the principal's own owed work. Otherwise an unresolved candidate produces `reconcile`.

Default answer phrases are `PASS`, `FAIL`, `BLOCK`, `CHANGES REQUIRED`, and `APPROVED`; acknowledgments are `claimed`,
`starting`, `received`, and `recorded`. Matching is case-insensitive and uses whole token sequences. Multiple answer
phrases, or an answer together with an acknowledgment, count as conflicting signals.

When a message does not match an existing request, an addressed message from another author opens **owed** work and
produces `brief`. A principal's attention-tagged request opens **awaited** work for the named responders and
produces `retain`. Without semantic interpretation, even an incoming direct “thanks” can be treated as owed work.
An incoming direct message can both answer an awaited request and open a new owed request.

Direct messages have no reply pointer. An outgoing direct answer can satisfy one unambiguous owed request from
its recipient, subject to the same task and artifact checks. With a judge configured, an outgoing direct message
opens an awaited item unless it already answered a request; this does not involve asking the judge about the
principal's own message. Word rules alone do not match incoming direct replies to those awaited items; the external
judge supplies that interpretation. Without a judge configured, outgoing direct messages do not open awaited items.

For job events, the configured state determines who owns the next action. A handoff to the principal can open
owed work; waiting on another actor routes the event and tracks an awaited handoff. Actor changes supersede
expectations opened by job events, while preserving message requests. Entering a policy-listed terminal state
cancels open expectations for that job.

### Example without a judge

Suppose `planner` posts “Please review `repo#7@abc1234`” with `attention=["worker"]`. Both agents have a secretary:

| Event | Planner's ledger and attention | Worker's ledger and attention |
| --- | --- | --- |
| Planner sends the request | Open **awaited** review from worker; `retain` → `keep`. | Open **owed** review for planner; `brief` → `wake`. |
| Worker replies “starting” | Record acknowledgment, leave review open; `digest` → `hold`. | No completion yet. |
| Worker replies “APPROVED review `repo#7@abc1234`” | Satisfy awaited review; `brief` → `wake`. | Satisfy owed review; `retain` → `keep`. |

These decisions assume message callbacks, no stronger outstanding item, and successful delivery of earlier wakes.
A reply saying only “PASS `repo#7@abc1234`” would not satisfy the typed review request: it lacks the review kind and
would need reconciliation.

## What the external judge does

The optional judge answers four questions about **one message**:

- Does it answer, acknowledge, change, or cancel an outstanding request?
- Which offered request does it refer to?
- Does it ask the principal to do something new?
- Does it need the principal's attention now?

Baton bundles no LLM adapter, model, or prompt. The plug-in boundary is a host executable configured in the policy.
That adapter owns its prompt/questions, model choice, credentials, model call, and conversion of the model's answer
into the JSON verdict below. It can also be ordinary deterministic code. No Python subclass or model SDK is required.

### Configure the plug-in

A complete policy can look like this; replace the example alias, executable, and placeholder hash:

```json
{
  "version": 1,
  "principal": "planner",
  "secretary_alias_id": "al_existing",
  "judge": {
    "command": ["/opt/baton/judge-adapter"],
    "timeout_seconds": 3,
    "questions_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "owed_digest_below": 0.4,
    "wake_at": 0.8
  }
}
```

The principal must be an existing non-human, non-display agent role; the secretary alias must exist and be unmapped
to a role. Omit `judge` to use built-in rules only. `command` is an argv list whose executable has an absolute path;
Baton starts it without a shell. The timeout must be greater than zero and at most 10 seconds, and is shortened to
fit the callback's remaining budget. The two thresholds shown are defaults and may be omitted.

`questions_sha256` identifies the adapter's question/prompt configuration. Baton validates the 64-character
lowercase hex format, includes it in the policy digest, and passes it to the command. **Baton does not load or
verify a prompt file.** Keeping that hash consistent with the actual prompt is the adapter's responsibility.

### What goes into the command

Baton writes one JSON object to stdin. This example shows a reply to an artifactless request; IDs are illustrative:

```json
{
  "version": 1,
  "principal": "planner",
  "questions_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "timeout_seconds": 3.0,
  "message": {
    "id": "msg_result",
    "from": "worker",
    "from_kind": "agent",
    "at": "2026-11-05T10:00:00Z",
    "channel": "conversation",
    "conversation_id": "conv_example",
    "job_id": null,
    "attention": [],
    "reply_to": "msg_request",
    "replies_to_principal": true,
    "artifacts": [],
    "relayed": false,
    "notice": false,
    "text": "All done on that"
  },
  "items": [
    {
      "id": "0123456789abcdef01234567",
      "direction": "awaited",
      "counterpart": "worker",
      "job_id": null,
      "artifact": null,
      "state": "open",
      "title": "Please run the migration check",
      "relayed": false
    }
  ]
}
```

The command gets the message's authored text, not relayed text, and at most four structurally matched, currently
visible candidate requests. Their titles contain at most the first 30 words of the opening message. The most
recent candidates are selected and presented oldest first. `items` may be empty. The command cannot select an
arbitrary request outside this list.

### What comes back, and how Baton uses it

The command exits successfully and writes exactly one JSON object to stdout, with exactly these four fields:

```json
{
  "relation": "answers",
  "item": "0123456789abcdef01234567",
  "opens_owed": 0.0,
  "needs_now": 0.2
}
```

| Field | Meaning |
| --- | --- |
| `relation` | One of `answers`, `acknowledges`, `changes_or_cancels`, or `none`. |
| `item` | The ID of an offered request, or `null`. A relation only affects a request when an item is selected. |
| `opens_owed` | Score from 0 to 1 for whether the message asks the principal to do something new. |
| `needs_now` | Score from 0 to 1 for whether the principal needs to attend now. |

The scores must be finite numbers, not booleans. An adapter that cannot interpret a message can instead return
exactly `{"declined":"reason"}`. Extra fields, surrounding prose, and Markdown fences are not accepted.

[`Judgment`](judge.py) applies a valid verdict in this order:

| Step | Condition | Ledger effect and disposition |
| --- | --- | --- |
| 1. Relate to an offered request | Artifact revision is stale or uncertain | Keep stale evidence without closing, or `reconcile` uncertainty. The claimed relation cannot bypass this check. |
| 1. Relate to an offered request | `answers`, with an acceptable artifact match | Satisfy the request; `brief`. |
| 1. Relate to an offered request | `acknowledges` | Record acknowledgment, leave request open; `digest`. |
| 1. Relate to an offered request | `changes_or_cancels` | Record changed evidence, **leave request open**; `digest`. |
| 2. Check for new work, if addressed to the principal | `opens_owed >= 0.5` | Open owed work; `digest` if `needs_now < owed_digest_below`, otherwise `brief`. |
| 2. Check for new work, if addressed to the principal | `opens_owed < 0.5` | No new owed work; `digest`. |
| 3. Apply urgency | `needs_now >= wake_at` and outcome so far is `retain` or `digest` | Promote to `brief`. |
| Otherwise | No relation, new work, or urgency applies | `retain`. |

The `0.5` request threshold is fixed; `owed_digest_below` and `wake_at` are policy settings. Outcomes from the
steps combine using `reconcile > brief > digest/route > retain`. Ledger or evidence limits can require
reconciliation; urgency cannot override it. A full request ledger preserves an incoming request's attention
level and records that it could not be tracked.

In the JSON example, “All done on that” has no built-in answer phrase. Word rules would leave the request open
and require reconciliation. The judge's `answers` verdict identifies the awaited request, so Baton closes it and
records `brief`. That yields `wake` on a normal message callback even though `needs_now` is only `0.2`: receiving
an awaited result is itself an attention rule, independent of the urgency threshold.

For an addressed message with `relation="none"`, default thresholds give these examples:

| `opens_owed` | `needs_now` | Result for this message |
| --- | --- | --- |
| 0.1 | 0.1 | No new request; `digest` → `hold`. |
| 0.9 | 0.2 | Open owed work that can wait; `digest` → `hold`. |
| 0.9 | 0.6 | Open owed work needing attention; `brief` → `wake`. |
| 0.1 | 0.9 | No new request, but urgent information; `brief` → `wake`. |

### When the judge runs, and what happens on failure

Only new messages with a named author other than the principal are eligible. On a message callback, judging
starts at the triggering message; earlier messages use word rules. Catch-up/public `scan`, hourly digests,
job events, and the principal's own messages never invoke the command. Already recorded messages are not judged
again on retry. This is an optional message interpreter, not a second pass over every recorded disposition.

A callback can make at most two judge calls, and a call needs at least one second of decision budget remaining.
After that, scanning stops before the next eligible message past a known trigger position and reports itself
behind. Up through the trigger, word rules provide the fallback. A `conversation` trigger names no event position,
so scanning continues with word rules after the judge budget is spent.

Timeout, nonzero exit, invalid JSON, wrong shape, explicit decline, or lack of judge budget invokes the word
rules for that message. If some candidates were omitted from the request, a verdict with `relation="none"` or
`item=null` also falls back: seeing four candidates cannot rule out a fifth. Output is limited to 64 KiB. A failure
neither automatically wakes the principal nor discards the event; the built-in rules determine its effect.

For a valid verdict, Baton also computes the word-rule alternative in a rolled-back savepoint, then applies only
the judge's effects. It records the verdict, the alternative, and any fallback reason. Replay uses these stored
verdicts without executing the command or asking a model again.

## How dispositions become wake, hold, or keep

The five dispositions describe an individual source; the three decisions describe attention for the outstanding
window. [`AttentionWindow`](decide.py) first maps each source as follows:

| Disposition | Meaning | Attention |
| --- | --- | --- |
| `brief` | New action or an awaited result | `wake` for a message; a job brief wakes only with `job_events_wake: true`, otherwise `hold`. |
| `digest` | An acknowledgment or information that can wait | `hold`. |
| `reconcile` | Uncertain, conflicting, or bounded evidence needing inspection | `hold`. |
| `retain` | Context without a new principal action | `keep`. |
| `route` | A job waits on its existing actor | `keep`. |

It then selects the strongest attention level: **wake before hold before keep**, with the oldest event breaking
a tie. An empty window returns `keep`. This is different from disposition precedence within a single message:
an uncertain message can hold while a separate actionable message makes the overall window wake.

The callback trigger applies one final gate. A message trigger, or a call without a trigger, can wake. A job
trigger can wake only with `job_events_wake: true`. A `conversation` trigger always reduces a would-be wake to
hold. The result is about the whole undelivered window, so an older brief can determine it even when the trigger
itself is routine.

| Final decision | What the host receives and can do |
| --- | --- |
| `wake` | A brief plus its evidence source and rule. The host can deliver it and interrupt the principal. |
| `hold` | Reasons and counts, with an empty brief. Keep the delivery position; a later wake or hourly digest can include it. |
| `keep` | Reasons and counts, with an empty brief. Record context without interrupting; it remains available in Baton. |

Keep items contribute counts but do not get individual lines in a brief or digest. Neither hold nor keep schedules
anything: the host must invoke a later callback or digest to reconsider the outstanding window.

An hourly call skips this selection and renders a digest instead; its `decision`, `rule`, and `evidence` are `null`.
It can render previously recorded judged items but interprets newly scanned messages using word rules.

## Host lifecycle and delivery positions

There are three distinct positions:

- `floor`: the activation boundary; older sources may populate the request ledger but cannot cause a new wake.
- `through_pos`: how far the secretary has scanned, whether delivered or not.
- `since`: the host's last successfully delivered coverage position; the attention window starts after it.

Before activation, run `scan` until `events_seen` is zero. Then make the first `decide` call without `since`/`floor`;
it records the catch-up position as the activation floor **before** scanning any new events. Save the returned
`floor`, and initialize the host's `since` from the returned `since`. A retry of this first call reuses the stored
floor even if scanning progressed before an interruption.

For an async host, the entry point is [`Secretary`](service.py). After catch-up and activation, a callback can use
the saved `since`/`floor` as below. `config` is the loaded Baton configuration; `state_path` and `policy_path` are
private `Path` objects. `deliver_to_principal` and `save_delivery_position` are functions supplied by the host:

```python
from baton.agents.secretary import Secretary

secretary = Secretary(config, state_path, policy_path)

# message_seq is the principal's mailbox delivery sequence.
result = await secretary.decide(
    budget_seconds=5, since=since, floor=floor, trigger=f"message:{message_seq}"
)
if result["decision"] == "wake":
    await deliver_to_principal(result["brief"])
    await save_delivery_position(since=result["covered_through"], floor=result["floor"])
```

`coverage` says whether scanning is `current` or `behind`. `covers_trigger` says whether this result covers the
trigger. If false, the host must not treat the result as a completed decision about that trigger; schedule another
call using the last successfully delivered position. `trigger_item`, `rule`, `evidence`, `counts`, and `judge`
explain what was considered, what won, and how many external calls fell back.

A wake brief defaults to 2048 bytes (`brief_bytes`); an hourly digest is bounded to 4096 bytes. Both contain IDs,
roles, artifacts, and derived reasons, not message bodies. `covered_through` stops before the first omitted line
when the size bound leaves items out. **After actual delivery**, save it as the next `since`; a failed delivery
retries with the same `since` and `floor`. Hold/keep leave `covered_through` equal to `since`.

The host schedules hourly calls with `hourly=True`, the saved `since`/`floor`, and no trigger. Handle digest delivery
and coverage the same way. Use `read` to fetch currently visible original text when the principal needs details.

## Where the behavior lives

| Owner | Responsibility and caller |
| --- | --- |
| [`Secretary`](service.py) / [`DecisionRun`](decide.py) | Host API / one callback's activation, scan budget, and response. |
| [`ScanBatch` and `Projection`](tracking.py) | Called by scan/decide; interpret ordered sources and commit ledger effects with progress. |
| [`Expectations`](expectations.py) | Request matching, openings, evidence, closures, and limits, shared by both interpreters. |
| [`MessageRules`](rules.py) / [`Judgment`](judge.py) | Built-in interpretation / external verdict application to that ledger. |
| [`JudgeSession`](judge.py) / [`JudgeCommand`](command.py) | Callback call budget / asynchronous subprocess transport. |
| [`AttentionWindow`](decide.py) / [`digest.py`](digest.py) | Select attention from recorded dispositions / render bounded output. |
| [`RecordedJudgments`](judge.py) | Supplies saved interpretations during replay. |

Supporting modules have narrower jobs: `sources.py` controls visibility and event envelopes, `policy.py` validates
configuration, `store.py` persists the private state, and `read.py` fetches original visible text.

External process I/O uses asyncio. [`DatabaseWorker`](runtime.py) keeps the existing synchronous database driver
on one worker thread per decision, so database waits do not block the host's event loop. Source interpretation
stays sequential so later messages can match requests opened earlier. Cancellation rolls back an unfinished
page and preserves completed pages; an already-issued database operation settles before connection cleanup.

## State, inspection, and CLI

The secretary opens Baton read-only and writes only a separate private SQLite sidecar, bound to the Baton log
epoch. The sidecar stores identifiers, visibility metadata, artifacts, text hashes, dispositions, request evidence,
and judge verdicts, not authored message bodies. Text reaches the external adapter transiently; the adapter owns
any further logging or transmission. `read` returns visible text without changing secretary state.

`show` exposes the ledger, source excerpts, and recorded dispositions. `replay` rebuilds from retained envelopes,
currently visible source text, and saved verdicts in memory, then compares outcomes without changing either
database. Sources no longer visible cannot be replayed. Explicit `cancel` closes an expectation when the operator
chooses to do so.

The synchronous CLI remains available; state and policy options go before the action:

```sh
baton secretary --state /private/secretary.sqlite --policy /private/policy.json scan --limit 100
baton secretary --state /private/secretary.sqlite --policy /private/policy.json decide --budget-seconds 5
baton secretary --state /private/secretary.sqlite --policy /private/policy.json decide \
  --budget-seconds 10 --since 420 --floor 300 --hourly
baton secretary --policy /private/policy.json read --after 420 --limit 100 --max-bytes 65536
```

Use `baton --config /path/to/baton.toml ...` when the host's default configuration is not appropriate.
`decide` requires `--budget-seconds` (greater than zero, at most 60). Its optional trigger is `message:SEQ`,
`job:JOB_ID:SEQ`, or `conversation`. `read` requires exactly one of `--after POSITION` or `--ids ID,ID`.
