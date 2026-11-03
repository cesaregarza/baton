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
| `set_topic(conversation_id, topic, grant_token?, token?)` | File a conversation in an existing topic, or pass `null` to clear it. Requires membership and `can_manage_participants`. A guest holding a `thread.organise` [topic grant](aliases.md#using-a-topic-grant) may instead move a thread between topics the grant covers, with its binding `token` and the grant key as `grant_token`; the participants get a `topic_moved` host notice. |
| `add_participant(conversation_id, participant)`, `remove_participant(...)` | Change membership, with `can_manage_participants`. |
| `create_topic(path, title?, description?, members?, request_id?, grant_token?, token?)` | Create a topic; a subtopic's parent must already exist. Requires `can_create_topics`. A guest holding a `topic.create_sub` [topic grant](aliases.md#using-a-topic-grant) may create a subtopic directly under the grant's topic with `token` and `grant_token`; it may not pass `members`, and the subtopic copies its parent's. |
| `update_topic(path, title?, description?, members?)`, `rename_topic(path, new_path, acknowledge_topic_grants?)` | Edit or rename a topic. Requires `can_create_topics`. Renaming also moves its subtopics and conversations, without changing participants or conversation IDs. If active topic grants are affected, `acknowledge_topic_grants` must list exactly their ids. |
| `list_topics(parent?)`, `get_topic(path)` | Read topic metadata, including members, and counts of conversations the caller can read. Available to guests. |

Topic grant refusals are all `forbidden`, with these texts. The key never appears in one.

| Text | Meaning |
|---|---|
| `a topic grant key works only with the alias's binding token` | `grant_token` without a valid binding `token`. |
| `unknown topic grant key` | No grant has that key. |
| `this topic grant key is not for this alias` | The key's grant belongs to another alias. |
| `topic grant <id> is <state>` | The grant is `revoked`, `expired`, `pending`, `lapsed` or `invalid`. |
| `topic grant <id> does not allow <capability> on <path>` | The grant does not list the capability, or does not cover the topic. For `set_topic`, a thread's current topic is never named: the text ends `on this thread's current topic`, whether that topic is uncovered, absent or stale. |

A guest that sends no `grant_token` gets `guest may not call create_topic` or `guest may not call set_topic`, as before.
A grantee's `members` is refused as `invalid_request`. A `rename_topic` that must acknowledge grants is refused as
`invalid_request`, with the ids to repeat it with.

A grantee's move writes this host notice to each participant of the thread, with no sender:
`{"event": "topic_moved", "from", "to", "by", "alias", "alias_code", "grant_id"}`. Its text reads like
`alias job-search (a1b2) moved this thread from job-hunt/leads to job-hunt/boards under topic grant tgr_...`.

`list_conversations` returns a `conversations` array and `next_cursor`. When the cursor is non-null, pass
it back as `cursor` to continue; it carries `created_at` and `conversation_id`. Entries include `revision`,
`last_seq`, and `last_activity_at`. See [Conversations and notifications](conversations.md) for examples,
visibility rules, and limits.

### References

These tools are present when `[conversations] enabled = true`. A reference is a named text kept with a topic, such as
`job-board` in `job-hunt/boards`, and maintained one revision at a time. It is information, never an instruction to
whoever reads it and never an approval: it says only what whoever held the right to edit it wrote.

**Revisions are permanent.** Every edit appends a revision, and nothing is edited or deleted, by anyone, the host
owner included. A secret pasted into a body stays in every later copy of the history. Every core role with read
scope and every alias with a live binding token can read every revision, so keep secrets out of references.

| Tool | Purpose |
|---|---|
| `edit_reference(topic, name, body, expected_revision, request_id?, grant_token?, token?)` | Write the next revision of a topic's reference, or create it with `expected_revision` 0. Requires `can_create_topics`. A guest holding a `reference.edit` [topic grant](aliases.md#using-a-topic-grant) may instead edit in the grant's topic, or directly under it if the grant inherits, with its binding `token` and `grant_token`. |
| `read_reference(topic, name?, revision?, token?)` | Without `name`, list the topic's own references by name, each described by its current revision. With `name`, read its current text, or the text of `revision`. Available to guests. |
| `reference_history(topic, name, after_revision?, limit?, token?)` | Page through a reference's revisions after `after_revision`, oldest first, without their text. `limit` is 1 to 100 and defaults to 50. Available to guests. |

A `name` is 1 to 40 characters of a-z, 0-9 and inner hyphens. A `body` is 1 to 32768 bytes of UTF-8, counted in
bytes. It is stored and returned exactly as sent: nothing is trimmed or normalized, and text of spaces alone is a
body.

- `expected_revision` is required. Give the revision you read, or 0 to create the reference. If the reference is at any
  other revision the edit is refused as `stale_revision` and writes nothing; the refusal carries `current_revision`, so
  read again and then edit. Two writers who read the same revision cannot both succeed. An edit that changes nothing is
  still a new revision.
- To undo, read an earlier revision and write its text as the next one. There is no restore tool, and nothing marks the
  new revision as a restore.
- A topic holds at most 20 references of its own, not counting its subtopics'. The limit applies when one is created,
  so a full topic can still have each of its references edited. A reference has no limit on revisions.
- A retry with the same `request_id` and arguments returns the stored revision with `created: false` and writes
  nothing, even after the reference has moved on. It checks the caller's authority first, so a grant that has ended
  or a role that lost `can_create_topics` is refused. The same `request_id` with a different body, name, topic,
  `expected_revision`, role or alias is `idempotency_conflict`. Leave `request_id` out and the host makes one.
- Reading takes no grant. A guest passes its alias `token`; without a valid one it is refused. A reader needs no
  thread in the topic. Reads change nothing.
- Each `edit_reference` a guest alias makes counts against the alias's hourly post limits, like a post, whether it is
  then refused, stale or a retry. A core role's edits and every read are not counted. See
  [Limits and expiry](aliases.md#limits-and-expiry).
- Documents belong to the topic's id. Renaming a topic carries its references with it, and a path the rename frees
  starts with none.

An edit answers with `topic` (the topic's path now), `topic_id`, `name`, `revision`, `base_revision`, `size` (UTF-8
bytes), `at`, `author`, `grant_id`, `request_id` and `created`; it does not echo the body. Reading one reference adds
`current_revision` and `body`, and the list is `references`, each with `name`, `current_revision`, `at`, `size`,
`author` and `grant_id`. History is `revisions` and `next_revision` beside `current_revision`. `author` is the role
the call ran as and, when an alias was behind it, its `alias_id`, `alias` and `code` as they were then. `grant_id` is
null for a core role.

A grantee's edit can be refused with the topic grant texts above, which here name `reference.edit`. A guest that
sends no `grant_token` to `edit_reference` gets `guest may not call edit_reference`, and a guest that reads without
a valid binding gets `alias binding is missing, invalid or expired`. A grantee naming a topic that does not exist is
refused as if the grant did not cover it. The other refusals are these:

| Code | Text | When |
|---|---|---|
| `forbidden` | `<role> may not edit references (roles.<role>.can_create_topics)` | A core role without the flag. |
| `invalid_request` | `name must be 1-40 characters of a-z, 0-9 and inner hyphens, such as job-board` | A malformed or missing `name`. |
| `invalid_request` | `body must be text of 1-32768 bytes of UTF-8` | An empty body, or one that is not text. |
| `invalid_request` | `expected_revision is required: 0 to create the reference, otherwise the revision you read` | A missing or negative `expected_revision`. |
| `invalid_request` | `request_id is 1-200 characters` | An empty or longer `request_id`. |
| `invalid_request` | `revision needs a name` | `read_reference` with `revision` and no `name`. |
| `invalid_request` | the usual `topic` messages | A malformed `topic`. |
| `limit_exceeded` | `a reference body is at most 32768 bytes of UTF-8; this one is <n>` | `limit` is `reference_body_bytes`. |
| `limit_exceeded` | `topic '<path>' already holds 20 references; edit one of them` | `limit` is `references_per_topic`. |
| `limit_exceeded` | `guest posting is over its hourly limit` | `limit` is `posts_per_alias` or `posts_per_credential`. |
| `stale_revision` | `reference '<name>' in '<path>' is at revision <current>, not <expected>; read it again before editing it` | Carries `current_revision`. |
| `idempotency_conflict` | `request_id <id> was already used with a different body` | A reused `request_id`. |
| `unknown_topic` | `no topic '<path>'` | An unknown topic, on any of the three tools; for an edit, only to a core role. |
| `unknown_reference` | `no reference '<name>' in '<path>'` | A topic with no such reference. |
| `unknown_reference` | `reference '<name>' in '<path>' has no revision <n>; its current revision is <current>` | A revision the reference does not have. |

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
| `baton secretary --state FILE --policy FILE scan\|replay\|show\|cancel\|decide` | Build and inspect local attention records from retained Baton events. |

`--as` defaults to `BATON_ROLE` in local mode and cannot select a human role. `--config` defaults to
`BATON_CONFIG`, then `~/.config/baton/baton.toml`. `--host` defaults to `BATON_HOST`: it uses the remote
host and the role of `BATON_TOKEN` or `BATON_TOKEN_COMMAND` for `message`, `read`, `wait`, and the
`conversation new`, `list`, `topic`, and `history` subcommands. Administration commands require local mode.

`secretary` is host-local and refuses `--host`. Its JSON policy names one existing non-human principal role
and one existing, unmapped guest alias ID for attribution; it grants neither authority. For example:
`{"version":1,"principal":"planner","secretary_alias_id":"al_existing"}`. `scan` reads up to 100 events
by default (`--limit` allows 1–1000) and stores only metadata, references, and digests in the separate
`--state` SQLite file. `replay` compares a fresh interpretation without changing saved records; `show`
renders currently visible source excerpts; `cancel ID --reason TEXT` records an explicit closure.
`decide --budget-seconds N` scans within the budget, then decides wake, hold or keep for one wake
(`--trigger`; a job event wakes only with policy `job_events_wake`) with a brief of at most `brief_bytes`
(default 2048), or digests for the hourly turn (`--hourly`), over what is new since `--since` and never at or
before `--floor`. Once a brief or digest is delivered, its `covered_through` is the next `--since`: a bound keeps
the oldest whole lines, so what it leaves out comes next. Without `--since`, the first decision records the floor
before it scans. It records each decision and never delivers;
`python -m baton.secretary_decide` takes the same options without the CLI's import time. An optional policy
`judge` block (`command`, an absolute argv; `timeout_seconds`; `questions_sha256`; and optionally
`owed_digest_below` and `wake_at`) lets a wake's `decide` put at most two new messages to that host command,
and a failed, late or malformed answer leaves a message to the deterministic rules, as does one that ties it
to none of the items offered when it had a candidate the judge was not offered; the catch-up scan and the
hourly digest never use it. The
policy and state formats are versioned. This interface does not deliver, approve, or modify Baton work.
The projection reads events addressed to or authored by the principal, plus later events for jobs whose
earlier create or move event the principal authored. It derives job facts from those event rows and
rechecks message visibility before retaining a cited message. Envelope version 1 refuses unknown stored
representations. If source visibility later narrows, `replay` reports unavailable sources as differences
without recomputing a misleading partial digest.
Direct replies correlate to an owed request only when one open request exists between the two roles;
ambiguous replies leave every candidate open. An untagged acknowledgment without an artifact needs one
matching expectation. Each brief version contains only the new cited source for that version.
An incoming direct message opens its own owed record even if it also provides evidence for an awaited
job result. A job move supersedes only a job-event-origin expectation when the next actor changes.
An external revision token is an identity, not an ordered version: a later request must explicitly name
`supersedes repo#7@oldsha` alongside its new `repo#7@newsha` to close the cited predecessor. A review or
release result requires the matching result kind; `CI PASS` can only satisfy a check expectation.

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
