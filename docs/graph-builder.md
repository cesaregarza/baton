# Offline conversation graphs

The graph builder turns a saved Baton thread into a local, incremental graph. Use an
OpenAI-compatible API for generation or let Codex/Claude Code produce the proposal directly.
Both modes consume the same versioned instructions and proposal schema, and commit through
the same validator. They produce the same state format; model interpretations can still differ.
This is separate from the live Baton host, inboxes and derived-index publishing.

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
usage is unknown, not an estimated saving or cost.

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
superseded. Extracted text and exact source quotes remain immutable. Strand titles and decision text are stable;
when a decision changes, supersede its existing D ID and add a new decision.

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

Source, state, request and proposal must be distinct paths. The source is read-only input; apply
validates before committing state under a lock with atomic replacement. Inference happens outside
that lock. Artifacts contain conversation data and should be stored with appropriate local access.

## Input and limits

The canonical snapshot is an object with `conversation_id` and `messages`. Every message needs
a contiguous `seq` starting at 1, a stable `message_id`, source `author`/`at`/`text`, and optional raw reply
metadata. The current saved pilot export is also accepted. Use the synthetic fixture as a compact
canonical example. Duplicate IDs, missing posts and incompatible conversation IDs fail explicitly;
permission-filtered or gapped exports are unsupported.

Prepare sends only bounded new raw posts and compact retained graph state, including necessary
resolved context. The input limit measures serialized UTF-8 bytes, not model tokens. If required
context or one post cannot fit, prepare fails instead of dropping context or skipping the post.
Increase the explicit budget or use a smaller batch. Output tokens are a separate provider limit.

V1 runs one bounded batch at a time. It does not fetch linked artifacts, synchronize a live board,
publish to the derived index, schedule follow-up work, install hooks or change global configuration.
The validator enforces references, coverage, quotes, state binding and structural invariants; it
cannot prove that every model interpretation or summary is semantically correct. Review the compact
view and drill down to raw posts for consequential decisions.
