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
Use `--recall-statement ID` once per selected ID when older original wording is needed. First inspect
`read --history` and `raw --seq` to identify the IDs, then prepare a fresh request; never edit a
prepared request because its content is bound to its request ID.

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
usage is unknown, not an estimated saving or cost. The operator must inspect history and select recall
IDs before `run`; it sends one request and has no automatic retrieval loop. The separate `prepare`
plus manual proposal/apply flow can be used when the proposal needs inspection before state advances.
The API proposal schema has no missing-context outcome, so a single call cannot reliably report that
an archived meaning was unavailable; the operator must select necessary IDs before calling it.

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
strand must refresh its summary through the batch before apply advances W; untouched strands
retain their earlier explicit frontier. A graph summary is derived interpretation, not authenticated
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

Prepare sends only bounded new raw posts and compact retained graph state. Active and partially
superseded statements include their host-supplied source metadata, while compact text still uses the
surviving clause. Completely superseded statements appear as IDs only unless explicitly recalled.
Recall includes the original text, exact quotes, source metadata, relevance, surviving text, memberships,
and evidence for each requested statement, including archived statements. Recall is empty by default
and is included in both request and provider UTF-8 bounds. The input limit measures serialized UTF-8
bytes, not model tokens. If required context or one post cannot fit, prepare fails instead of dropping
context or skipping the post. Archived IDs alone do not recover meaning: native producers should stop
and report missing IDs for operator recall; API callers must select IDs before the single call. This is
producer guidance, not validator-enforced semantic fidelity; the API schema has no missing-context
response.
Increase the explicit budget or use a smaller batch. Output tokens are a separate provider limit.

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
