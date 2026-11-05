# Secretary package

The secretary is an optional, offline attention projection for one configured agent role. It reads the events and
messages that role can see, keeps a separate private ledger of outstanding requests, and decides whether a wake
needs action. It does not send messages, advance a Baton inbox cursor, or mark a brief as delivered.

## Package map

| Module | Responsibility |
| --- | --- |
| `policy.py` | Load, validate and normalize the versioned JSON policy. Its digest identifies the rules used. |
| `sources.py` | Read visible Baton events and produce metadata envelopes plus transient message text. |
| `tracking.py` | Public scan, replay, inspect and cancel operations; owns source progress and scan transactions. |
| `expectations.py` | Open, match, record evidence for, and close the principal's owed or awaited requests. |
| `rules.py` | Word-list message matching and job handoffs. |
| `judge.py` | Optional host-command interpretation of eligible messages and stored verdicts used by replay. |
| `decide.py` | Scan within a wake budget and turn dispositions into wake, hold or keep. |
| `digest.py` | Render bounded briefs and hourly digests with a covered position. |
| `read.py` | Read the principal's delivered messages and context from Baton, including authored text. |
| `store.py` | Private sidecar schema, append-only evidence records and deterministic state export. |

For changes, start in `rules.py` for word matching or handoffs, `expectations.py` for ledger lifecycle and limits,
`tracking.py` for scan/replay behavior, `decide.py` for decision policy, `digest.py` for rendered text, and `read.py`
for read output. Policy validation belongs in `policy.py`; source visibility and envelopes belong in `sources.py`.
The package entrypoint exports `scan`, `show`, `cancel`, `replay`, `load_policy`, `Policy`, and version constants.
The decision and read functions also have dedicated modules and CLI commands.

## Owed and awaited: a concrete example

After both roles have completed catch-up and activation, suppose `planner` posts in a conversation:
“Please review `repo#7@abc1234`,” with `attention=["worker"]`. When
`planner` scans it, the ledger opens an **awaited** expectation: requester `planner`, responder `worker`, artifact
`repo#7@abc1234`, expected kind `review`. When `worker` scans the same addressed post, its own ledger opens an
**owed** expectation from `planner`; the request needs `worker` to act. The conversation post is retained as context
for planner, but is a brief/wake for worker.

Now `worker` replies in that conversation, replying to the request: “APPROVED review repo#7@abc1234.” On worker's
scan, the reply matches and satisfies its owed expectation, so it is retained as completion of its own action. On
planner's scan, it matches and satisfies the awaited expectation; a newly satisfied awaited result is a brief/wake
for planner. An acknowledgment such as “starting” records evidence but leaves the expectation open. A stale artifact
revision is recorded as stale evidence and retained; uncertain artifact matches or conflicting result signals are
reconciliation cases.

Word rules open awaited expectations for conversation requests tagged with an attention recipient. An outgoing
direct message opens one only when a judge is configured and it did not already answer another request.

“Owed” and “awaited” describe direction relative to the principal. A full ledger still gives an incoming request
its attention disposition, with an explanation that it was not tracked because the open-expectation limit was reached.

## Lifecycle

1. **Scan.** `tracking.scan()` opens Baton read-only in a consistent snapshot, validates the policy, reads the next
   bounded page of visible events, and applies effects in one sidecar transaction. It stores source metadata and
   hashes, not authored message text. Its sidecar cursor advances with those effects; a failed transaction leaves
   both uncommitted.
2. **Interpret.** Message rules match existing requests and record evidence before opening new requests from the
   message. Job events use their own handoff rules. A disposition is stored for each new source. Repeated message
   deliveries with the same message ID are canonicalized to one secretary item.
3. **Decide.** `decide.decide()` scans until caught up or its time budget expires, then considers items after the
   caller's `since` position. On the first decision it records an activation floor; older items are catch-up context
   and cannot wake. A normal decision returns wake, hold or keep. An hourly decision renders a digest and never
   invokes the judge.
4. **Render and deliver.** A wake may include a bounded brief; hourly mode returns a bounded digest. Rendering
   returns `covered_through`, the position before the first omitted item, or the scan position if everything fit.
   The caller must pass that position as
   `since` on a later call only after actually delivering the text. Rendering alone does not mark coverage as
   delivered, so an undelivered brief can be retried; omitted lines remain eligible for a later brief or digest.
5. **Replay.** `tracking.replay()` recomputes from stored envelopes and currently visible source text in memory,
   compares results, and leaves saved state untouched. It uses the recorded judge verdicts, never calls the judge
   again. It reports sources that are no longer visible and cannot be replayed.

## Matching, order and decisions

Word rules tokenize case-insensitively and match configured answer and acknowledgment words or phrases as whole
token sequences. Defaults include answers such as `PASS`, `FAIL`, `BLOCK`, `CHANGES REQUIRED`, `APPROVED`, and
acknowledgments such as `claimed`, `starting`, `received`, `recorded`. These are lexical signals, not semantic
understanding: an answer word alone does not prove which request it answers. The rules also require a suitable
reply, task or artifact relationship and compatible expected result kind. Without a unique match, a message can
be marked uncertain for reconciliation.

There are five internal **dispositions**: `retain` (no new action), `route` (a job waits on its existing actor),
`digest` (an acknowledgment or item that can wait), `brief` (action requested or awaited result received), and
`reconcile` (uncertain, conflicting or over a bound). When several rules apply, precedence is `reconcile`, `brief`,
`digest`/`route`, then `retain`; the strongest applicable outcome wins.

For a matched message result, word rules check these conditions in order:

| Check | Ledger effect |
| --- | --- |
| Stale artifact revision | Record stale evidence; retain the item open. |
| Answer with the wrong result kind | Record a kind mismatch and reconcile. |
| Uncertain artifact match or conflicting result signals | Record uncertainty and reconcile. |
| Identifying answer signal | Record satisfaction and close the expectation. |
| Acknowledgment signal | Record acknowledgment; leave the expectation open. |
| No decisive signal | Record uncertainty and reconcile. |

The caller sees three **decisions**, derived from the disposition and source kind:

| Decision | Meaning |
| --- | --- |
| `wake` | A message brief needs attention; an eligible job brief wakes only when policy enables job-event wakes. |
| `hold` | A digest or reconciliation item can wait for the hourly turn; also a job brief when job-event wakes are disabled. |
| `keep` | Retained and routed context is recorded without waking. |

The strongest item in the window determines the decision. A job trigger can wake only when job-event wakes are
enabled. A conversation/rate-limit trigger never wakes, even with that opt-in; a message trigger can wake normally.
The hourly digest reports counts and visible source IDs/labels, not message bodies. Use `read` to fetch the
principal's actual deliveries and context.

## Optional judge

A policy may name a host command by an absolute argv, timeout, question-file SHA-256 and thresholds. The command
receives one bounded JSON request on stdin and returns a JSON verdict on stdout. Its authored message text is sent
to that command for the call but is not stored in the secretary sidecar. The judge can correlate an answer without
an answer word, so judge matching is semantic relative to the command's verdict; it still uses the kernel's
structural candidate set and artifact checks. Word rules alone use lexical signals and cannot correlate an
untagged direct reply in the same way.

The callback path can invoke the judge while scanning, before the final wake/hold/keep decision is known; it judges
at most two new eligible messages from the wake's own message onward, within that callback's time budget. Catch-up
scans, public `scan` calls and hourly digests use word rules. If the budget or call count is spent through
the trigger event, that event falls back to word rules; after the trigger, scanning stops before the next eligible
message the judge would read. Timeout, command error, malformed or declined verdict also falls back. If more
candidates exist than were offered, fallback applies only when the verdict relates to none or selects no item; a
verdict tied to an offered candidate can stand. Fallbacks are recorded alongside the word-rule outcome.
Conversation triggers have no event position, so budget exhaustion uses word rules rather than stopping the scan.
For a judged message, the sidecar stores the verdict fields and what word rules would have decided, not the text.
Replay consumes that stored verdict and does not execute the command.

## State and privacy boundaries

Baton is opened read-only. The secretary writes only to its separate private SQLite sidecar; it does not migrate or
modify Baton's database. The sidecar is private to the configured data directory rules and bound to the Baton log
epoch, so a different epoch requires a new state file. Scan effects and the scan position commit together.
Expectation openings, evidence and closures are bounded; closure evidence has reserved capacity so ordinary
evidence cannot consume the final closure slot. The source envelope keeps identifiers, visibility-related
metadata, artifact references and a hash of authored text, not the message body. Brief records contain bounded
derived metadata and hashes, not copied authored text. `read` is the separate interface for current visible text.

## Minimal API and CLI

For trusted host-side Python code, use the exported tracking API:

```python
from baton.agents import secretary

page = secretary.scan(config, state_path, policy_path, limit=100)
inspection = secretary.show(config, state_path, policy_path)
comparison = secretary.replay(config, state_path, policy_path)
```

`state_path` is the private sidecar file and `policy_path` is the required versioned JSON policy. `scan` initializes
or advances the sidecar; `show` inspects it; `replay` requires an existing scan state. `cancel(config, state_path,
policy_path, expectation_id, reason)` explicitly cancels one expectation and records the reason. The public
`secretary.scan()` uses word rules; normal applications should use `decide` for the activation floor, wake budget
and optional judge behavior.

Finish the offline catch-up before the first `decide`: repeat `scan` until `events_seen` is zero. The first
decision uses that saved position as its activation floor. CLI state and policy options go before the action name:

```sh
baton secretary --state /private/secretary.sqlite --policy /private/policy.json scan --limit 100
baton secretary --state /private/secretary.sqlite --policy /private/policy.json decide --budget-seconds 5
baton secretary --state /private/secretary.sqlite --policy /private/policy.json decide \
  --budget-seconds 5 --since 420 --floor 300 --trigger message:17
baton secretary --state /private/secretary.sqlite --policy /private/policy.json decide \
  --budget-seconds 10 --since 420 --floor 300 --hourly
baton secretary --policy /private/policy.json read --after 420 --limit 100 --max-bytes 65536
```

`decide` requires `--budget-seconds`; after the initial decision, pass both `--since` (the last delivered brief's
`covered_through`) and the saved `--floor`. `--trigger` accepts `message:SEQ`, `job:JOB_ID:SEQ` or `conversation`.
`read` requires exactly one of `--after POSITION` or `--ids ID,ID` (comma-separated message IDs); it returns message text and does not change
secretary state. The command also provides `show`, `replay`, and explicit `cancel` actions.
