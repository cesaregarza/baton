# MCP and CLI reference

This page is for people connecting a client or operating Baton. For a first working example, start with
[Getting started](getting-started.md). The [example config](../baton.example.toml) lists configuration
settings; [Architecture and design](design.md) explains storage, delivery, and retry behavior.

## MCP tools

An MCP credential normally selects the caller's role. An enrolled alias can select a mapped role on a
shared guest login; see [Guest aliases](aliases.md). The following tools act within the caller's permissions.
Arguments ending in `?` are optional. Some tools are available only when their feature is enabled.

### Identity and messaging

| Tool | Purpose |
|---|---|
| `whoami` | Return the effective role, credential role, any supplied alias and code, configured roles, and job states. |
| `alias_exists(name)` | Check whether an exact normalized alias is taken. It does not return its owner or code. |
| `claim_alias(name, passphrase)` | Claim or renew an alias and receive an expiring `binding_token`. A fixed role can use it only to enroll itself. |
| `bind_events(token)`, `unbind_events(token)` | Bind or unbind a guest OAuth grant's event subscriptions to an enrolled alias. See [MCP Events](integrations/openai-mcp-events.md#shared-guest-logins). |
| `send_message(text, to? \| conversation_id?, job_id?, relayed?, reply_to?, attention?, needs_decision?, request_id?, token?)` | Send to a role or conversation. Reply and attention fields apply only to conversations. |
| `read_messages(after_seq?, limit?)` | Read messages addressed to the caller. Without `after_seq`, return undelivered messages. |
| `wait(timeout_seconds?)` | Wait up to 30 seconds for messages or job changes addressed to the caller. |

A plain guest has a smaller tool allowlist and must supply an alias proof to post; an enrolled alias can
act as its mapped role. [Guest aliases](aliases.md) describes the distinction and its limits.

### Jobs

| Tool | Purpose |
|---|---|
| `create_job(title, body?, request_id?)` | Create a job in the configured initial state. |
| `move_job(job_id, to_state, note?, request_id?, expected_revision?)` | Move along a configured transition. An invalid edge returns `illegal_transition`; a changed revision returns `stale_revision`. |
| `get_job`, `list_jobs`, `read_job_events` | Read jobs and their history. |

Set `job_authority = false` on a role to deny MCP calls to `create_job` and `move_job`, including an
idempotent retry. The default is `true`. This setting does not remove the role's other permissions. The
trusted local CLI accesses jobs directly and does not enforce `job_authority`.

### Conversations

These tools are present when `[conversations] enabled = true`.

| Tool | Purpose |
|---|---|
| `create_conversation(title, participants?, topic?, request_id?, token?)` | A permitted role opens a conversation containing itself. An identified guest names an existing topic instead of participants; see [Guest aliases](aliases.md#use-a-guest-alias). |
| `list_conversations(topic?, changed_since?, limit?, cursor?)` | List conversations the caller may read. |
| `read_history(conversation_id, after_seq?, before_seq?, limit?)` | Read retained history without advancing the mailbox. `before_seq` pages backwards; `retention_gaps` reports missing accessible ranges. |
| `set_topic(conversation_id, topic)` | File a conversation in an existing topic, or pass `null` to clear it. Requires membership and `can_manage_participants`. |
| `add_participant(conversation_id, participant)`, `remove_participant(...)` | Change membership, with `can_manage_participants`. |
| `create_topic(path, title?, description?, members?, request_id?)` | Create a topic; a subtopic's parent must already exist. Requires `can_create_topics`. |
| `update_topic(path, title?, description?, members?)`, `rename_topic(path, new_path)` | Edit or rename a topic. Requires `can_create_topics`. Renaming also moves its subtopics and conversations, without changing participants or conversation IDs. |
| `list_topics(parent?)`, `get_topic(path)` | Read topic metadata, including members, and counts of conversations the caller can read. Available to guests. |

`list_conversations` returns a `conversations` array and `next_cursor`. When the cursor is non-null, pass
it back as `cursor` to continue; it carries `created_at` and `conversation_id`. Entries include `revision`,
`last_seq`, and `last_activity_at`. See [Conversations and notifications](conversations.md) for examples,
visibility rules, and limits.

### Search and derived artifacts

These tools are present when `[search] enabled = true`.

| Tool | Purpose |
|---|---|
| `search(query, conversation_id?, topic?, kinds?, limit?, before?, producer?, query_vector?, model?)` | Search readable messages by keyword. Optional kinds include derived artifacts; an optional vector can reorder keyword matches when embeddings are enabled. |
| `get_message(message_id)` | Read one message the caller may see. |
| `read_artifacts(conversation_id, producer?)` | Read permitted derived summaries, decisions, and open questions, labelled `valid` or `behind`. |

A query may contain at most eight words and 200 characters; longer queries are refused. Every word must
match. `before` is an ISO 8601 timestamp with a UTC offset. Baton uses SQLite FTS5 when available and a
slower scan otherwise; `keyword_index` reports which. If embeddings are enabled, `query_vector` and
`model` reorder keyword matches; they do not add new matches. `producer` narrows derived artifacts.

Search, message lookup, history, conversation listing, and reply references apply the same board-read rule.
`can_view_all_conversations` grants access to retained conversations; other roles need membership and
retained copies. Direct messages remain visible only to their sender and recipient. Search filters by
access before ranking, and an artifact is visible only if the caller can read all its source messages.
These reads do not mark mailbox messages as delivered. Derived artifacts are information supplied by a
local producer, not approvals or automatically generated summaries.

## CLI

Run `baton --help` or `baton <command> --help` for the arguments supported by the installed version. Most
commands print JSON. A Baton refusal prints `{"error": code, "detail": ...}` and exits with status 1; a
configuration problem exits with status 2.

| Command | Purpose |
|---|---|
| `baton serve` | Run the MCP server and wake dispatcher. |
| `baton message --as R --to R --text T` | Send a local direct message. Omit `--text` to read it from stdin. |
| `baton message --as R --conversation C ...` | Send to the other conversation participants. Supports `--reply-to`, `--attention`, and `--needs-decision`. |
| `baton read --as R [--after-seq N]` | Read a role's mailbox. |
| `baton wait --as R [--timeout S]` | Wait for work addressed to a role. |
| `baton conversation new\|list\|history\|add\|remove` | Create, list, read, and manage conversations. `new` and `list` take `--topic`; `conversation topic C --set T\|--clear` files or unfiles one. Create topic containers with MCP first. |
| `baton job create\|show\|list\|move\|events` | Create and inspect jobs or move them through their configured states. |
| `baton token new --name N --role R` | Generate a bearer token and its config entry. |
| `baton token revoke N` | Revoke a named bearer token without restarting the server. |
| `baton hash-passphrase`, `baton oauth revoke-all` | Create an OAuth passphrase hash or revoke all OAuth grants. |
| `baton wakes` | Show recent wake outcomes. |
| `baton alias arm-enrol\|arm-rekey\|map\|unmap` | Manage enrolled aliases on the host. |
| `baton events status\|unbind` | Inspect event subscriptions and receipts, or unbind a guest grant's event target. |
| `baton seat logout-all` | End all browser-seat sessions. |

`--as` defaults to `BATON_ROLE` in local mode and cannot select a human role. `--config` defaults to
`BATON_CONFIG`, then `~/.config/baton/baton.toml`. `--host` defaults to `BATON_HOST`: it uses the remote
host and the role of `BATON_TOKEN` or `BATON_TOKEN_COMMAND` for `message`, `read`, `wait`, and the
`conversation new`, `list`, `topic`, and `history` subcommands. Administration commands require local mode.

## Local indexing interface

A host owner can run a separate indexing worker to submit derived data. Baton stores and serves the
worker's output; it does not run a summarizer or calculate embeddings. Configure producer names under
`[indexer] producers`. These names are not roles. An empty producer list disables the writer; read-only
change-feed commands do not need a configured producer.

| Command | Purpose |
|---|---|
| `baton index changes [--after CURSOR] [--limit N]` | Read conversation changes after a cursor, oldest first; metadata only. |
| `baton index wait --after CURSOR [--timeout S]` | Wait for a change after a cursor. |
| `baton index dirty --producer P` | List work without a current artifact for a producer. |
| `baton index begin-batch --producer P --conversation C --kinds K1,K2` | Create a manifest of the messages a batch covers. |
| `baton index read-batch B --page N --producer P` | Read a page from that manifest. |
| `baton index commit-batch B --file F --producer P` | Submit the batch's derived artifacts. |
| `baton index generation create\|activate\|drop` | Build, switch to, or discard a producer's generation. |

Each new conversation message and membership change updates the conversation revision and appends to a change
log, which is kept. A cursor from another database, or older than the oldest change still kept, is refused with
`cursor_expired` and a `reset_cursor` for rebuilding.

A batch manifest fixes which messages it covers. The worker reads every page, then submits artifacts such
as `summary`, `decisions`, or `open_questions`. Baton records their inputs. Baton deletes no messages, so an
artifact's sources stay. An artifact that an earlier version invalidated, when a source message expired from every
mailbox, is rebuilt from the start by the next batch.
With `[search] rolling_window_messages` enabled, a producer can also write `summary_recent` over the latest
messages. Retrying an identical commit with the same `request_id` returns its receipt; changing the payload
under that ID is refused as `idempotency_conflict`.

A producer can build an inactive generation while its active generation keeps serving reads. Activation
switches readers in one step only after the new generation covers each conversation with retained messages
through its latest message. The default limit is three generations per producer; dropping an inactive one
frees its slot.

### Optional vectors

With `[search] embeddings = true`, a generation may declare an embedding profile as `--profile` JSON:

```json
{"embedding": {"model": "example-model", "dim": 256, "normalization": "unit", "distance": "cosine"}}
```

`normalization` is `unit` or `none`; `distance` is `cosine` or `inner_product`. The producer supplies the
vectors and records any skips (`empty_text`, `over_token_limit`, or `excluded_kind`) in its commit. Each
kind written and each served message without a vector needs a vector or a skip; otherwise the entire
commit is refused. `max_vectors_per_generation` defaults to 1,000,000.

A vector search reranks a wider keyword pool: up to four times the requested limit of message matches and,
when requested, four times the limit of artifact matches. All active generations for the requested model
must match the query's dimension. A model name also identifies one embedding signature—model, dimension,
normalization, and distance—across active producers. Activation refuses a conflicting signature with
`embedding_profile_conflict`; use a different model name for a different embedding.
