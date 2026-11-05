# Baton secretary

The secretary filters one agent's visible event stream into a small, private attention view. It tracks who owes
whom a result, keeps routine progress from waking an agent on every update, and gives the host a bounded brief or
an hourly digest when useful. The host still owns delivery: the secretary neither sends messages, advances a
Baton inbox cursor, nor verifies claims made in a result.

It runs on demand: the host invokes it for a wake callback or a scheduled digest. Baton remains the source of
truth; the secretary's separate ledger remembers how those records relate to the principal's outstanding work.

```text
Baton event stream (read-only)
          |
          v
 classify messages and job handoffs -----> private expectation ledger
          |                                          |
          +----------> wake / hold / keep <-----------+
                              |
                     bounded brief or digest
                              |
                    host decides delivery
```

## How a request becomes attention

Suppose `planner` posts in a conversation: “Please review `repo#7@abc1234`,” with `attention=["worker"]`.
When planner's secretary sees the post, it opens an **awaited** expectation: requester `planner`, responder
`worker`, artifact `repo#7@abc1234`, expected kind `review`. When worker's secretary sees that addressed post, it
opens an **owed** expectation for worker. The request is context for planner, but a brief for worker, who has the
next action.

If worker replies to the request, “APPROVED review `repo#7@abc1234`,” worker's ledger records completion of its
owed expectation. Planner's ledger records the awaited result as satisfied and raises a brief. An acknowledgment
such as “starting” records evidence but leaves the expectation open. A stale artifact revision is retained as
stale evidence; uncertain artifact matches and conflicting result signals need reconciliation.

“Owed” and “awaited” describe direction relative to the configured principal. Incoming addressed requests remain
actionable even if the ledger is full; the brief explains when an expectation could not be tracked because the
open-expectation limit was reached.

Direct messages have no reply pointer, so their correlation rules differ. A direct delivery to the principal can
open an owed expectation. Word rules can match a direct reply to one unambiguous request from that recipient, but
cannot correlate an untagged direct reply to one of several requests. A principal's outgoing direct request opens
an awaited expectation only when a judge is configured, and only if that message did not already answer another
request. Word rules alone do not infer an awaited request from an outgoing direct message.

## One callback, end to end

1. **Catch up.** Run offline `scan` until no visible events remain. It reads Baton in a consistent, read-only
   snapshot, classifies a bounded page, and commits the page's effects with its source position in the private
   sidecar. The first decision records the activation floor at this caught-up position, so earlier events cannot
   wake the agent.
2. **Decide.** On a host callback, `decide` scans within its time budget, then considers events after the caller's
   `since` position. A configured judge may interpret a bounded number of eligible messages during this callback.
   The result is `wake`, `hold`, or `keep`; a wake can include a brief with a byte limit.
3. **Deliver at the host's discretion.** The result includes `covered_through`. After the host actually delivers
   the brief, it passes that position as `since` on a later callback, together with the saved activation `floor`.
   Rendering alone does not mark anything delivered, so an undelivered brief can be retried. If a brief's size
   limit leaves items out, coverage stops before the first omitted item so it remains eligible later.
4. **Digest when work can wait.** The hourly path returns a bounded digest with counts and visible source IDs or
   labels, not message bodies. It does not invoke the judge. Use `read` when the host needs currently visible
   message text and context.
5. **Replay for inspection.** Replay recomputes from retained envelopes and currently visible source text in
   memory, compares outcomes, and leaves state unchanged. It uses recorded judge verdicts and never reruns the
   command. Sources that are no longer visible cannot be replayed.

Each scan batch is its own atomic sidecar transaction. Cancellation rolls back an uncommitted batch; completed
commits remain. This lets a later callback resume at the saved position without keeping half-applied effects.
Ordered source interpretation stays ordered: later messages can match expectations opened or changed by earlier
messages in the same stream.

## Concepts and why they have separate owners

Recording evidence and choosing an interruption are different jobs. An acknowledgment changes the ledger even
when it does not wake anyone. Replay needs to rebuild that evidence without delivering a brief or asking a model
again. Likewise, interpreting a judge's answer is separate from starting its process and managing its deadline.

| Concept | What it owns | Why it is separate |
| --- | --- | --- |
| [`Secretary`](service.py) | Long-lived config and service access for `decide(...)` and `scan(...)`. | Reuses setup across calls without making the callback state itself long-lived. |
| [`DecisionRun`](decide.py) | One callback's scan budget, activation/delivery cursor, and response. | Keeps one wake's progress and coverage together. |
| [`AttentionWindow`](decide.py) | Classified items, selection, counts, report, and rendering through `digest.py`. | Separates attention selection from scanning and ledger changes. |
| [`Expectations`](expectations.py) | Outstanding requests and evidence for one principal. | Word rules and judgments share the same opening and closure limits. |
| [`JudgeSession`](judge.py) | Per-callback judge call and time budget. | Bounds how much interpretation one callback can request. |
| [`Judgment`](judge.py) | One message's candidate context, question, verdict application, and trace. | Keeps the interpretation and its auditable effect together. |
| [`RecordedJudgments`](judge.py) | Verdicts supplied during replay. | Makes replay deterministic without invoking the external command again. |
| [`JudgeCommand`](command.py) | Async subprocess input/output, timeout, and cancellation. | Keeps external process I/O awaitable and bounded. |
| [`ScanBatch` and `Projection`](tracking.py) | Source snapshot, per-source interpretation and atomic sidecar progress. | Commits source effects and cursor advancement together. |
| [`DatabaseWorker`](runtime.py) | Existing synchronous database operations, run off the event loop. | Gives connections one thread owner and keeps SQLite waits off the caller's loop. |

`Secretary.decide()` creates a `DecisionRun`. It scans pages through `ScanBatch`, awaits `JudgeSession` when a
message needs interpretation, then asks `AttentionWindow` to select and present the result. The synchronous CLI
enters this workflow through the module-level `decide()` function.

The external judge uses asyncio subprocess I/O. Database work stays synchronous on one worker thread per run;
source interpretation remains ordered. Pagination awaits real work on every iteration rather than polling.
Cancellation lets an issued database operation settle before rollback and connection cleanup, so it can wait for
a database lock without blocking the caller's event loop. Standalone CLI/read/replay functions remain synchronous.

The supporting files keep distinct policies apart: `sources.py` determines what the principal can see, `rules.py`
interprets words and job transitions, and `policy.py` validates configuration. `store.py` persists derived records;
`digest.py` presents their attention summary. `read.py` fetches original visible text when the host needs more detail.

## Outcomes and matching

There are five internal **dispositions**:

- `retain`: record context without a new action;
- `route`: a job still waits on its existing actor;
- `digest`: an acknowledgment or other item that can wait;
- `brief`: action requested or an awaited result received;
- `reconcile`: uncertain, conflicting, or over a bound.

When several rules apply, the strongest disposition wins in this order: `reconcile`, `brief`, `digest` or `route`,
then `retain`.

The caller sees three **decisions**, derived from disposition and source kind:

| Decision | Meaning |
| --- | --- |
| `wake` | A message brief needs attention. A job brief wakes only when policy enables job-event wakes. |
| `hold` | A digest or reconciliation item can wait for the hourly turn; job briefs also hold when job wakes are disabled. |
| `keep` | Retained and routed context is recorded without waking. |

The strongest item in the window determines the decision. A job trigger wakes only with the job-event opt-in. A
conversation or rate-limit trigger never wakes by itself, even with that opt-in; a message trigger can wake
normally. The trigger does not establish that an item is covered if the scan has not reached its event yet.

Word rules use case-insensitive whole-token phrases such as `PASS`, `FAIL`, `BLOCK`, `CHANGES REQUIRED`, and
`APPROVED`; acknowledgment defaults include `claimed`, `starting`, `received`, and `recorded`. These are lexical
signals, not proof of what a result means. Matching also checks reply, task, artifact, and expected result kind;
without a unique relationship, the item may need reconciliation.

For a candidate result, checks apply in this order:

1. A stale artifact revision records stale evidence and leaves the expectation open.
2. An answer with the wrong result kind records a mismatch and reconciles.
3. An uncertain artifact match or conflicting answer signals record uncertainty and reconcile.
4. An identifying answer signal satisfies and closes the expectation.
5. An acknowledgment records evidence but leaves the expectation open.
6. No decisive signal records uncertainty and reconciles.

## Optional judge

A versioned policy may configure a host command using an absolute argv, timeout, question-file SHA-256, and
thresholds. The command receives one bounded JSON request on stdin and returns a verdict on stdout. Authored
message text is passed for that call but is not stored in the secretary sidecar. The judge can correlate a result
without an answer word, but only within the kernel's structurally matched candidate set and artifact checks.

Each callback owns one judge budget. It can judge at most two new eligible messages, starting at the triggering
message when the trigger names one. Once spent, scanning stops before the next eligible message past the trigger;
through the trigger itself, word rules supply the fallback. A conversation trigger has no event position, so it
uses word rules after the budget is spent rather than stopping its scan.
Catch-up scans, public `scan` calls, and hourly digests use word rules only. A timeout, command error, malformed or
declined verdict falls back to word rules. A verdict that refers to none of the offered candidates also falls back
when other candidates were not offered. Fallback and the word-rule outcome are recorded. Replay uses that recorded
verdict instead of starting the command again.

## State and privacy

Baton is opened read-only. Only the separate private SQLite sidecar is written; it is bound to the Baton log epoch,
so a different epoch needs a new state file. Stored envelopes keep identifiers, visibility metadata, artifact
references, and hashes of authored text, not message bodies. Brief records contain bounded derived metadata and
hashes, not copied authored text. The separate `read` operation returns currently visible message text and does
not change secretary state.

Expectation openings, evidence, and closures are bounded. Closure evidence has reserved capacity so ordinary
evidence cannot use the final closure slot. The judge receives only the bounded candidate context and authored
message for its call; relayed text is not treated as authored text.

## Using the secretary

For asynchronous host code, keep a `Secretary(config, state_path, policy_path)` and await its `scan(...)` and
`decide(...)` methods. It owns configuration and service access; each call owns its own work and budget. The
synchronous module-level `scan` and `decide` remain available at CLI and compatibility boundaries. Public scan
uses word rules; normal callback handling uses `decide` for the activation floor, wake budget, and optional judge.

The offline catch-up comes before the first decision. Repeat scan until `events_seen` is zero. At the first
decision, catch-up position becomes the activation floor. Later calls pass `since` as the `covered_through` of the
last brief actually delivered and keep the saved `floor`.

```python
from baton.agents.secretary import Secretary

secretary = Secretary(config, state_path, policy_path)
page = await secretary.scan(limit=100)
result = await secretary.decide(budget_seconds=5, trigger="message:17")
```

The CLI remains available for setup and operations; state and policy options go before the action:

```sh
baton secretary --state /private/secretary.sqlite --policy /private/policy.json scan --limit 100
baton secretary --state /private/secretary.sqlite --policy /private/policy.json decide --budget-seconds 5
baton secretary --state /private/secretary.sqlite --policy /private/policy.json decide \
  --budget-seconds 10 --since 420 --floor 300 --hourly
baton secretary --policy /private/policy.json read --after 420 --limit 100 --max-bytes 65536
```

`decide` requires `--budget-seconds`. Its trigger may be `message:SEQ`, `job:JOB_ID:SEQ`, or `conversation`.
`read` requires exactly one of `--after POSITION` or `--ids ID,ID`; it returns message text without changing
secretary state. The CLI also provides `show`, `replay`, and explicit expectation `cancel`.
