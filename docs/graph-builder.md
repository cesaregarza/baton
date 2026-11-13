# Manual conversation graphs

The graph builder turns a saved Baton thread into a local, incremental graph. Use an
OpenAI-compatible API for generation or let Codex/Claude Code produce the proposal directly.
Both modes consume the same versioned instructions and proposal schema, and commit through
the same validator. They produce the same state format; model interpretations can still differ.
The graph stays local. Host-local export and publication commands connect its completed frontier to
Baton’s existing derived summaries; generation never runs inside the host or an open derived batch.

Run from this checkout with Python 3.12 or newer and Baton's dependencies available:

```sh
python -m baton.graph --help
python -m baton.graph schema
```

An installation of this Baton revision also provides `baton-graph` with identical arguments.
No host configuration or running server is required. The examples below run from the repository
root and keep generated files in a separate local directory:

```sh
mkdir -p /tmp/baton-graph-demo
cp integrations/graph-builder/examples/synthetic-thread.json /tmp/baton-graph-demo/thread.json
```

## Produce with a native skill

Copy the complete folder for your client into a project that can invoke the graph CLI. These
commands show alternative installations; choose the client you use, and preserve any existing skill:

```sh
# Codex: run in the project where the skill should be discovered.
mkdir -p .agents/skills
cp -R /path/to/baton/integrations/graph-builder/codex/baton-graph-builder .agents/skills/

# Claude Code: run in the project where the skill should be discovered.
mkdir -p .claude/skills
cp -R /path/to/baton/integrations/graph-builder/claude-code/baton-graph-builder .claude/skills/
```

The folders are self-contained: they have no links to files outside the copied folder.
They rely on the graph CLI to emit the current extraction contract instead of embedding another
prompt/schema. See the official [Codex skills documentation](https://learn.chatgpt.com/docs/customization/overview#skills)
and [Claude Code skills documentation](https://code.claude.com/docs/en/skills) for discovery and invocation.

Ask Codex:

> Use $baton-graph-builder to process one batch of at most 5 posts from
> /tmp/baton-graph-demo/thread.json into /tmp/baton-graph-demo/native-state.json.
> Use /tmp/baton-graph-demo/native-request.json and /tmp/baton-graph-demo/native-proposal.json
> for the artifacts, then show the compact read and live tail.

Ask Claude Code the same request with `/baton-graph-builder` as the invocation.
The native agent runs the following prepare command, reads the complete emitted request, and
authors the proposal itself before applying it. No separate model API call is involved:

```sh
python -m baton.graph prepare \
  --source /tmp/baton-graph-demo/thread.json \
  --state /tmp/baton-graph-demo/native-state.json \
  --request /tmp/baton-graph-demo/native-request.json \
  --batch-size 5 --max-input-bytes 100000

# Native agent reads native-request.json and writes native-proposal.json here.

python -m baton.graph apply \
  --source /tmp/baton-graph-demo/thread.json \
  --state /tmp/baton-graph-demo/native-state.json \
  --request /tmp/baton-graph-demo/native-request.json \
  --proposal /tmp/baton-graph-demo/native-proposal.json

python -m baton.graph read \
  --source /tmp/baton-graph-demo/thread.json \
  --state /tmp/baton-graph-demo/native-state.json
```

The synthetic thread has six posts. Processing only five leaves the sixth verbatim in the live tail.
It offers multi-target responses, partial and complete supersession, and a resolved discussion
whose source-backed decision still applies to an active strand. These are interpretation examples,
not permission to take actions mentioned in the posts.
The request shows the model the part of the graph chosen for its new posts (see
[Input and limits](#input-and-limits)). Never edit a prepared request: its content is bound to its
request ID.

## Produce with an API

Choose a provider base URL and its exact model ID. The user's preference for Sol depends on
the provider's model mapping; the builder has no hardcoded model alias. Set the credential in
the named environment variable using your normal secret mechanism. Do not put it in JSON artifacts.
This example makes one nonstreaming paid/provider call when executed:

```sh
python -m baton.graph run \
  --source /tmp/baton-graph-demo/thread.json \
  --state /tmp/baton-graph-demo/api-state.json \
  --request /tmp/baton-graph-demo/api-request.json \
  --proposal /tmp/baton-graph-demo/api-proposal.json \
  --base-url https://provider.example/v1 --model PROVIDER_MODEL_ID \
  --api-key-env GRAPH_API_KEY \
  --batch-size 5 --max-input-bytes 100000 \
  --timeout 60 --max-output-tokens 4000 \
  --response-format json_schema --token-parameter max_completion_tokens
```

`run` uses prepare, calls `/chat/completions` once, saves the proposal, and applies the same local
validation. The default structured mode is `json_schema`. Providers without it can be selected
explicitly with `--response-format json_object` or `--response-format plain`; providers using
the older token parameter can use `--token-parameter max_tokens`. See the official
[Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
for the OpenAI request fields; compatible providers may support a subset. There is no automatic fallback
or retry that makes an additional call. An explicitly configured local provider may use
`--allow-no-key` instead of `--api-key-env`. Failed, refused, truncated or invalid output does
not advance the processed frontier. Usage is reported when supplied by the provider; absent
usage is unknown, not an estimated saving or cost. `run` sends one request and has no retrieval loop:
the model reads the graph context that prepare chose and nothing more. The separate `prepare`
plus manual proposal/apply flow can be used when the proposal needs inspection before state advances.

## Read, update and retain artifacts

```sh
python -m baton.graph read --source /tmp/baton-graph-demo/thread.json \
  --state /tmp/baton-graph-demo/native-state.json --strand S1
python -m baton.graph read --source /tmp/baton-graph-demo/thread.json \
  --state /tmp/baton-graph-demo/native-state.json --statements
python -m baton.graph read --source /tmp/baton-graph-demo/thread.json \
  --state /tmp/baton-graph-demo/native-state.json --history
python -m baton.graph raw --source /tmp/baton-graph-demo/thread.json --seq 2 --seq 5
```

Default reads center active strand summaries, open work and applicable shared decisions, deduplicated
across selected strands. Add `--statements` for statement detail; partially superseded statements show
their surviving clause in this working view. `--history` includes resolved strands and original immutable
statement text/quotes. A decision can outlive the discussion that established
it. Semantic edge direction is from a response/supporting/contradicting/superseding/dependent statement
to its referenced parent. Multiple edges may express multiple targets; raw reply metadata remains
separate. Relevance is directly relevant, partially superseded with a surviving clause, or completely
superseded. Accepted statement text, exact source quotes, statement-to-strand memberships, and strand
titles are immutable. Relevance, summaries, open work, and decision status can change only in a normal
batch that advances over new source posts; there is no correction-only revision. Decision text remains
immutable; when a decision changes, supersede its existing D ID and add a new decision.

The read captures source head **H**, processed contiguous frontier **W**, and summary coverage for
each strand. The tail is exactly `W < seq <= H`, with original text and author metadata. A touched
strand whose card the request showed must refresh its summary through the batch before apply advances
W; untouched strands retain their earlier explicit frontier. A strand that gains a statement without
its card shown keeps its older frontier until the next request, which shows the card and asks for the
refresh. A graph summary is derived interpretation, not authenticated
approval, ticket status or action authority.

Append posts to the saved source, preserving its committed prefix, then prepare another batch
against the same state with fresh request/proposal paths. Keep requests, proposals and receipts for
inspection. A request binds the proposal to its conversation, base revision/digest and source batch.
Identical successful replay returns its prior receipt; a different replay or stale proposal fails.
If source grows after prepare, later posts remain in the tail. Edits to prepared/committed posts fail.
Never repair graph state by hand: correct a rejected proposal against the same request, or prepare
again when stale. The native skills allow one correction before reporting a remaining failure.

If an accepted extraction needs correction, preserve its checkpoint and request/proposal artifacts for
audit and rebuild from post 1 against the same immutable source, using a fresh checkpoint and fresh
request/proposal paths. Review the corrected proposal before applying it; do not edit accepted state or
source. IDs belong to a checkpoint/generation, so an S number in a rebuild is not assumed to mean the
same strand as that number in the old checkpoint. A corrected same-W checkpoint cannot replace a
summary already published by this producer/generation: publication requires W to advance beyond the
prior published W, and same-W publication is treated as already up to date. If the corrected graph was
already served, rebuild through a later genuine source post before publishing; never fabricate a post
to force progress. The graph command does not provide same-W replacement. A producer allowlist controls
who may publish future summaries and does not withdraw a summary already served. Do not treat a
single-summary new generation as a way to activate a correction; generation activation covers all
conversations and artifact kinds.

The producer instructions are part of the exact request contract. After this contract changes, discard
old prepared requests for production and regenerate them from the compatible saved checkpoint with new
request/proposal paths; checkpoints need no migration.

Source, state, request and proposal must be distinct paths. The source is read-only input; apply
validates before committing state under a lock with atomic replacement. Inference happens outside
that lock. Artifacts contain conversation data and should be stored with appropriate local access.

## Input and limits

The canonical snapshot is an object with `conversation_id` and `messages`. Every message needs
a contiguous `seq` starting at 1, a stable `message_id`, source `author`/`at`/`text`, and optional raw reply
metadata. The current saved pilot export is also accepted. Use the synthetic fixture as a compact
canonical example. Duplicate IDs, missing posts and incompatible conversation IDs fail explicitly;
permission-filtered or gapped exports are unsupported.

A graph grows larger than the thread it describes, so prepare sends the new raw posts with only the
part of the graph they are likely to touch, as the request's `context`:

- `outline`: every strand on one line, so a new statement can be filed under any of them.
- `entries`: standing statements and active decisions, one line each, as graph readers see them: the
  standing statements of the last 20 processed posts, the active decisions changed in those posts, and
  each new post's 20 best full-text search hits among the older entries.
- `cards`: the summary, open work and evidence of every strand a shown entry belongs to.
- `refresh`: the strands holding statements newer than their summary. Their cards and those statements
  are always sent, and the proposal must refresh each of them.
- `next_ids`: the strand and decision IDs a proposal allocates from.

A proposal may change only what its request showed. A relevance update, an edge, a strand refresh or a
decision change naming anything else is rejected. A new statement may still be filed under any strand
in the outline; a strand that gains one without its card shown falls behind, and the next request sends
its card and asks for the refresh. Completely superseded statements and superseded decisions are never
shown, so neither can be changed again. An older entry the search does not surface can be missed, for
example a decision restated under a new ID, but it is never altered unread.

The input limit measures the serialized request in UTF-8 bytes, not model tokens. Prepare takes as many
whole posts as fit with their full context. When the next post alone does not fit, it drops entries
from the least valuable end (search hits first, the newest statements last) until the request fits. The
outline always stays, and so does what a refresh owed to a strand has to read: its card and its
statements newer than its summary. A post that does not fit even with only those makes prepare fail
instead of splitting or skipping it: increase the explicit budget. Each provider path
holds its own, slightly larger encoding of the request to the same number, so a request within a few
percent of the limit can still be refused there. Output tokens are a separate provider limit.

Choosing the entries needs SQLite FTS5. On a host without it `prepare`, `run` and the background worker
refuse to start; they never fall back to showing the recent entries alone.

V1 runs one bounded batch at a time. The manual host commands below export source and publish the
completed graph. Nothing schedules follow-up work, fetches linked artifacts, installs hooks or changes
global configuration.
The validator enforces references, coverage, quotes, state binding and structural invariants; it
cannot prove that every model interpretation or summary is semantically correct. Review the compact
view and drill down to raw posts for consequential decisions.


## Export, publish and open a real thread manually

These commands run on the host with its local configuration and database access. `index` commands
are host-local; they are unavailable through MCP or the remote CLI. Store graph artifacts outside
Baton's `data_dir`, keep them private, and use distinct source, checkpoint and receipt paths.
No daemon, timer, model credentials or new artifact type is involved in publication.

A later operator rollout can enable one producer and reader access with this minimal configuration.
Apply it only to the intended host configuration; these examples do not install or change anything:

```toml
[search]
enabled = true

[indexer]
producers = ["graph-manual"]
max_artifact_bytes = 32000
max_citations = 200
max_manifest_items = 1000
page_items = 100
```

`max_artifact_bytes` measures the rendered UTF-8 body; `max_citations` bounds deduplicated native
message IDs referenced by rendered evidence. A long graph can exceed either limit and publication
then refuses the entire summary. The full graph remains local. Manifest and provenance bounds still
apply; `max_manifest_items` must accommodate the new interval since this producer's prior summary.
The new batch must reach graph **W** exactly. A smaller manifest never publishes a body claiming a
larger frontier. Increase explicit bounded settings or produce a smaller completed checkpoint.
Only producers named in `indexer.producers` may publish. Readers require search enabled and the
existing core-role artifact visibility policy; derived summaries never grant access to source posts.

```sh
# Export a complete prefix; omit --through-seq to capture the current native head.
baton --config /path/to/baton.toml index graph-export \
  --conversation CONVERSATION_ID --file /private/graphs/source.json \
  --max-posts 10000 --max-bytes 20000000

# Generate outside the host: use the native skill above, or the existing API run command.
# Repeat prepare/apply with fresh request/proposal paths to process more posts.
python -m baton.graph prepare --source /private/graphs/source.json \
  --state /private/graphs/graph.json --request /private/graphs/request-1.json \
  --batch-size 20 --max-input-bytes 100000
# Native agent reads request-1.json and writes proposal-1.json.
python -m baton.graph apply --source /private/graphs/source.json \
  --state /private/graphs/graph.json --request /private/graphs/request-1.json \
  --proposal /private/graphs/proposal-1.json

# Model generation is complete before begin-batch; later native posts may remain beyond W.
baton --config /path/to/baton.toml index graph-publish --producer graph-manual \
  --source /private/graphs/source.json --state /private/graphs/graph.json

# Local core-role consumer, or remote consumer using its ordinary authenticated token.
baton --config /path/to/baton.toml conversation context CONVERSATION_ID --as reviewer --limit 50
baton --host https://baton.example/mcp conversation context CONVERSATION_ID --limit 50
```

`graph-export --through-seq N` exports seqs 1 through N only. Export uses one database snapshot,
deduplicates recipient copies by native seq and message ID, and refuses holes or copies with
inconsistent identity, persisted author attribution, text or raw reply ID. Guest authors use the existing
`guest · alias · code` label; a host notice uses `the host`. Copies naturally have different delivery
timestamps, so the canonical `at` is the earliest retained copy's timestamp. If later retention changes
that canonical timestamp, publication/export refuses the changed prefix rather than silently rewriting
provenance. The export has explicit whole-prefix post and byte limits and never truncates implicitly.
Publication accepts at most 10000 source posts and 20000000 source bytes.

An existing source path can only be extended with its entire earlier normalized prefix unchanged;
shortening or editing it fails. Re-export the same path after more posts arrive, then use new
request/proposal paths and the same graph checkpoint. `graph-publish` loads a stable graph snapshot,
checks its completed W and source binding, and compares every normalized source post through W
with current authoritative SQLite records before creating the batch and immediately before commit.
It records every manifest page. The published body contains active strand IDs, summaries, open work,
individual strand summary frontiers, and active applicable shared decision IDs with deduplicated native
evidence IDs. Resolved graph history and full raw posts stay in local artifacts.

Publication preserves its batch and deterministic commit payload before delivery/commit. Default
attempt files live beside the checkpoint in `graph.json.publications/<identity>.json`; identical retries
reuse that batch and existing commit receipt, creating no additional summary version. Later graph
revisions automatically use another attempt file. A crash after commit but before saving the local
result is recoverable through the existing derived receipt. Keep the attempt files for retries within
configured `receipt_retention_days`; expired or removed batches/receipts may require operator recovery
and are not silently republished. `--receipt /private/graphs/attempt.json` chooses an explicit path; using
it with a different publication identity or altered payload fails. `--generation GEN` targets that
producer's existing generation; select it explicitly when switching generations. An inactive generation
still requires the ordinary generation activation process before consumers can see it. Publication
never changes the graph checkpoint or advertises a new W after a failed commit.

`conversation context` returns summary metadata/body with `covered_through_seq` W, captured
`head_seq` H, and earliest ascending raw messages `W < seq <= H`. It falls back to authorized raw
history with W=0 when search is disabled or no usable summary is accessible. The summary is derived
interpretation; use existing `conversation history`/MCP `read_history` for explicit raw drilldown.
If `continuation` is present, consume its paired bounds together:

```sh
baton --host https://baton.example/mcp conversation context CONVERSATION_ID \
  --after-seq AFTER_SEQ --through-seq CAPTURED_HEAD_SEQ --limit 50
```

Use the returned `continuation.after_seq` and `continuation.through_seq` literally. Continuations
recheck current permissions and return only the next raw page at the fixed H, without introducing a
newer summary. Their `summary` and `covered_through_seq` are null. Inspect `tail_complete` and
`retention_gaps`; never treat a bounded page as acknowledgement of unseen messages. MCP consumers
can use `read_context` with `conversation_id`, optional `producer` and `limit`, and the same paired
`after_seq`/`through_seq` continuation fields.

## Background worker and graph readers

Production generation is a background host service, never an interactive command or an MCP generation
call. The `[graph_worker]` section (see `baton.example.toml`) is off by default; when enabled under
`baton serve`, the worker polls the allowlisted conversations in read snapshots, exports each canonical
native prefix into its private `state_dir`, runs the existing prepare/apply contract one batch at a
time, produces a compact working view, and publishes view and graph together through the derived batch
protocol. `baton index graph-worker status` shows its ledger and meter; `baton index graph-worker reset
--conversation ID` is the only way a blocked record resumes. It works whether or not the service runs:
with the service stopped it applies at once; while the service runs (and holds the ledger lock) the command
leaves a reset request in `state_dir`, says so, and the worker applies it under its own lock at the start of
its next cycle and removes it. A request names one conversation that has a ledger record and carries
nothing else; the command refuses any other, and the worker ignores and removes any other it finds.
The manual export/prepare/apply/publish commands above remain diagnostic and test harnesses.

A thread is graphed when a batch is due: `batch_size` posts (default 20) are waiting past the accepted
watermark, or the oldest of them (the first post after the watermark, dated from the canonical message
table) has waited `max_wait_seconds` (default 21600, six hours; at most 604800). With
`max_wait_seconds = 0` every waiting post is due at once. Deciding needs no export and no model call, and
a new post alone no longer makes a thread pending; `target_head` still tracks the canonical head. After a
batch is applied the thread takes the next due batch at once, and is summarized (the compact phase, then
publication) when no further batch is due or when the published summary is 100 or more posts behind the
graph. A backlog therefore costs one summary call, not one per batch, and one publication covers every
batch accepted since the last. Known limit: a thread that blocks in the middle of a backlog can leave up
to 99 graphed posts without a published summary until it is reset; readers get those posts raw, and the
ledger's `accepted_W` and `published_W` show the gap.

Every model launch is reserved first in one atomically replaced ledger (`ledger.json`) under a
process-lifetime lock, so a crash consumes it and a second worker exits without inference. Extraction
gets at most two calls per base W, compaction two per accepted graph digest, and the daily cap (default
8) and per-cycle cap (default 4) bound launches across every conversation; a correction call carries the
sanitized validation error and rejected output only when they fit `max_input_bytes`. Exhaustion, an
input overflow (one post exceeding `max_input_bytes` with only the graph context it must carry, or a
correction that does not fit), an isolation failure, or an edited source prefix sets the conversation
`blocked`; new posts, restarts and new days never clear it. A daily cap only defers to the next UTC day.
`daily_call_cap = 0` removes only the daily limit, for an authorized catch-up: launches are still
reserved, counted per UTC day and reported, and the per-request attempt and per-cycle caps still apply.

A conversation blocks durably only for a cause that belongs to it, or after calls that may have used the
provider. Every failed call carries one category, fixed where the failure is raised and never read from
message text, and the category alone decides what happens next:

| Category | Raised when | Handling |
|---|---|---|
| `cancelled` | the service stops during the call | spent; nothing returned is applied |
| `input_limit` | serialized input over `max_input_bytes` (any provider), or a prepare overflow | blocked at once |
| `isolation` | a native client recorded a permission denial, a tool use or a disallowed event; a refused environment name | blocked at once, whatever else the call reported |
| `unsafe_source` | prepare, export or load failures | blocked at once |
| `timeout` | the host's wall-time limit ended the call | charged |
| `output_limit` | the response is over the byte limit | charged |
| `malformed` | an answer that is not the expected shape | charged |
| `transport` | the call failed after the request was sent and nothing typed says what happened (an HTTP read failure, a native non-zero exit with no structured result) | charged |
| `request_rejected` | HTTP 4xx other than 401, 403, 408 and 429 | charged |
| `provider_error` | a native client reports an error and its typed fields show work may have been done | charged |
| `authentication` | HTTP 401 or 403; the configured credential variable is missing or empty | refusal |
| `rate_limited` | HTTP 429 | refusal |
| `provider_unavailable` | HTTP 408 or 5xx; the connection could not be made | refusal |
| `provider_refused` | a native client reports an error and its typed fields show no work was done | refusal |
| `unverified_client` | native client verification failed (version, flags, features, introspection) | refusal |

A charged call counts against the request's two calls and the daily cap, the first failure waits 60
seconds, and the second blocks. A refusal is a call the provider declined without doing any work, so it
is never charged and never blocks: its attempt is refunded, the launch is still counted in the ledger's
`calls` for the day and also in `refused`, and the daily cap compares `calls - refused`. The conversation's
consecutive-refusal count `refusals` goes up and it waits 1, 2, 4, 8, 16 and 32 minutes, then hourly, so
refused launches stay bounded (at most about 30 per conversation per UTC day) even with `daily_call_cap =
0`. `graph-worker status` shows the refusal in `error_category` and the next attempt in `not_before`.
The count returns to zero when a call settles any other way except a cancellation, and on `reset`; a new
request does not clear it. A crash between reserving and settling a call stays charged. The per-cycle cap
counts refused launches too.

Native clients are classified from typed fields only, whatever their exit status: an attempted action
first; then an error result, which is a refusal only when the client's own record shows no work. For
Claude Code that is an error result whose every usage counter and cost is zero and whose
`api_error_status` is a refusal status (401, 403, 408, 429 or 5xx) or absent (nothing was sent, as with
no login); any other status or any nonzero counter is `provider_error`. For Codex it is a stream with an
`error` event, a failed turn or an error item, no agent message or reasoning item, and no reported usage.
A subscription usage limit is expected to take the refusal shape (an error status of 429 with zero usage)
but has not been observed directly. Publication never calls a model: it reuses the saved compact view and
the derived receipt, waits 60 seconds between attempts, and blocks after three per graph digest. Native
calls count launches, wall time and bytes; `recent_calls` reports observed turns and usage as the client
reported them, never a dollar figure. Each request carries the part of the graph chosen for its new
posts (see [Input and limits](#input-and-limits)), so its size follows the batch and the number of
strands, not the length of the thread. A saved request written under another producer contract is
deleted and prepared again with a full attempt count, because no answer to it could be applied.
Localized repair is not part of this increment.

The ledger also keeps a meter: calls, input tokens (cached included), cached input tokens, output
tokens and posts accepted, per UTC day for the 30 most recent days and as running totals in each
conversation's record. Calls and tokens are added when a call settles, from the usage its transport
reported (zero where it reported none, as after a timeout or a failed call); a refunded refusal is not a
call here either. Posts are added when a batch is applied. `graph-worker status` prints `meter.days` and
`meter.conversations`, each row with `input_tokens_per_post` and `output_tokens_per_post` (null while no
post was accepted). The meter holds counters only, and the daily cap is still enforced from `calls`, not
from the meter. A ledger written before the meter opens with it empty.

The compact view is checked structurally before publication: it must name exactly the active strands and
the active decisions applicable to them (or to all), cite only evidence that resolves within W, cite at
least one recorded reference for every listed item, bind the exact snapshot digest, and fit
`min(12000, max_artifact_bytes)` UTF-8 bytes and `max_citations` resolved native messages. Every compact
request, including a correction, states those two effective numbers in `limits`, with `max_body_words`
(the byte bound divided by 8, 1500 at the default). The instructions ask for a body within that many
words, say the host measures the encoded size itself and that no tool is available or needed, and never
ask the writer to count bytes. The host still checks bytes exactly, and a rejected body's correction
states its measured size and the limit in bytes. The deterministic rendering is published without a model
call when it already fits. The instructions ask the writer to keep requested next work and its owners,
unresolved contradictions, continuing shared constraints and the exact artifact or test scope the next
action needs, to state repeated rules once, to put historical proof behind references, to attribute
claims to source authors and to separate absence of evidence from requested work; they forbid inventing
any permission, adoption, approval or completion gate. These checks establish coverage, not truth, and a
view may well be longer than the conversation it summarizes.

Three provider modes share one `produce(request, schema, limits)` interface. `http` is one
OpenAI-compatible chat-completions request with a strict JSON schema, run under a single deadline of
`call_timeout_seconds` for connect, headers and body together. A service stop cancels whichever call is in
flight, in every mode, and waits for its connection or process group to be cleaned up before shutdown
completes; the spent attempt stays counted and nothing returned during cancellation is applied or
published. `codex` and `claude` launch the
installed, already-authenticated clients: fixed argv, explicit model, an isolated temporary working
directory, no shell, an allowlisted environment with only runtime basics and the configured
`credential_env` names (never `BATON_*`), bounded stdin/stdout/stderr and wall time, and process-group
cleanup on cancellation.

Which client version is trusted is operator configuration: `client_version` is required for `codex` and
`claude`, refused for `http`, and has no default. Record the exact version you inspected; the worker
launches only a client that reports exactly that version. Pointing `command` at a versioned executable
path keeps an automatic update from changing the client under the worker. The flag and feature profiles
stay in code and are checked against the client's own help output. Every model launch is preceded by a
passing verification of the executable about to run: the worker resolves `command` the way the launch
does (on the child's `PATH`, symlinks followed), verifies the client through that file, and runs that
same file directly. It verifies before every launch, not only when a file changes, because `command`
cannot name every file the client runs: an interpreter runs a script, and the script loads more. That
costs a few short introspection processes per call and never a model run. A failed verification at
service start is logged and leaves the worker running; it is the `unverified_client` refusal, shown in
`status`, and clears without a restart once the client or the configuration is right.

Codex runs `exec --ignore-user-config --ephemeral --sandbox read-only --skip-git-repo-check --json
--output-schema --output-last-message -C TEMP` with `approval_policy="never"`, `web_search="disabled"` and
`features.NAME=false` for `shell_tool`, `apps`, `plugins`, `hooks`, `multi_agent`, `memories`,
`view_image`, `image_generation`, `browser_use`, `computer_use`, `request_permissions_tool` and
`sleep_tool`; the worker verifies the version, the documented flags and the feature list and accepts only
thread/turn status, failure and usage events and assistant text, reasoning or error items. `exec` has no
system-prompt option, so Codex still receives the instructions and the request content in one prompt.
Claude Code runs `--print --output-format json --json-schema SCHEMA --model MODEL --tools ""
--strict-mcp-config --mcp-config EMPTY --safe-mode --disable-slash-commands --no-session-persistence
--system-prompt INSTRUCTIONS` with only the serialized request content on stdin, reading
`structured_output` and refusing any recorded permission denial or tool use; the turn count is reported,
not judged, because structured output may take the SDK more than one turn. `--bare` is never used because
it disables OAuth. `max_input_bytes` bounds the input as the client receives it: instructions plus content,
with the blank line between them for the one-prompt client. Keeping the instructions
in their own channel lowers the chance that thread text steers a summary; it does not remove it, and the
checks establish structure, exact quotes and coverage, not truth. Read-only sandboxing and never-approval
contain whatever built-in surface documented controls cannot remove; a bounded pilot must inspect the
real event stream before enrollment widens.

Readers see the result through five read-only MCP tools. Scoped work starts with `search_graph`, which
returns the standing statements and active decisions matching a few words as one line each (id, strands,
connections, text), and `read_graph_entries`, which loads statements, decisions, strand summaries or a
post's statements whole by id; a reader loads the part of the thread its work needs and nothing else.
The other three carry the full records: `read_graph` opens the working view with strand
cards, applicable decisions and a `tail` naming the raw posts after W, `read_graph_strand` lists a
strand's statements or decisions, and `read_graph_statement` returns one immutable statement with quotes,
evidence and its edges or strands. A graph is served only when the caller may read every source message,
through the same predicate as `read_artifacts`; a guest with `can_view_all_conversations` therefore
reads board graphs, and a guest without it gains nothing. Follow `tail.read_history_args` with
`read_history`, keeping `through_seq` fixed and replacing `after_seq` with `next_seq` until it reaches the
head; a short page is never completion, and `retention_gaps` name what is gone. A newer publication or a
lost input during paging answers `snapshot_unavailable` or `graph_unavailable`; open the graph again.
