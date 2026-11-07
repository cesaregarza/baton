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
| `read_secretary_summaries(after?, limit?)` | Page the authenticated role's shared secretary summary archive; rechecks current visibility of every cited source. |
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
| `baton secretary --state FILE --policy FILE decide\|show\|acknowledge` | Run model triage, acknowledge delivered summaries, and inspect private assessments. |
| `baton secretary --policy FILE summaries [--after N] [--limit N]` | Page the local principal's shared archive; no private `--state` file is required. |
| `baton --host URL secretary summaries [--after N] [--limit N]` | Page the authenticated token role's archive remotely; no host-side secretary policy file is required. |
| `baton secretary --policy FILE read --after POS\|--ids IDS` | Read what the principal was delivered, of any kind, text included, for a secretary thread. |

`--as` defaults to `BATON_ROLE` in local mode and cannot select a human role. `--config` defaults to
`BATON_CONFIG`, then `~/.config/baton/baton.toml`. `--host` defaults to `BATON_HOST`: it uses the remote
host and the role of `BATON_TOKEN` or `BATON_TOKEN_COMMAND` for `message`, `read`, `wait`, and the
`conversation new`, `list`, `topic`, and `history` subcommands. Administration commands require local mode.

Secretary assessment, worker, hook, and acknowledgement actions are host-local. `secretary summaries` also supports
an authenticated remote read through `--host`; the token role selects the archive principal, and this read needs no
host-side policy file or policy configuration. The MCP tool `read_secretary_summaries` provides the same role-scoped
read. Local secretary actions require a version 3 JSON policy with `principal`, stable
`secretary_id`, explicit `model` and `decision_engine` objects, `max_tool_calls`, `max_events`, `context_bytes`,
`summary_bytes`, `wake_threshold`, `retry_initial_seconds`, `retry_max_seconds`, `max_attempts`, and
`digest_seconds`. The [secretary guide](../baton/agents/secretary/README.md#policy) has a complete example.
`decide` writes private assessment/queue state and appends deliverable summaries to `baton.db`, so the local
database must be writable. `read` reads source messages from a read-only snapshot; `summaries` only reads the archive.
Model and engine IDs and HTTP endpoints are selected in the policy; Baton does not supply model, URL, or
transport defaults. Command providers require `command`, `env`, `timeout_seconds`, and `max_output_tokens`.
Model commands use protocol 4; decision-engine commands use protocol 3 and return exactly `decision` and
`wake_probability`. The independent engine runs on each alert page. JEV validates its probability distribution
and uses `probabilities["wake"]` for `wake_threshold`; its separate wire `confidence` is not used as the threshold.
A wake verdict at or above `wake_threshold` forces a wake even if model triage holds, keeps, or fails. The model
may call a tool to retrieve the cached verdict, which contains no rationale.

`decide --budget-seconds N` reads bounded, complete pages of the principal's visible events, including visible
reply parents, and asks the independent engine and model for triage. The engine timeout must be shorter than the
assessment budget; install, worker startup, and standalone decisions validate that relationship. Integration
delivery uses a separate required `delivery_timeout_seconds`, independent of engine timeout. Standalone implicit
mode handles one due retry before scanning new work, even for a newer trigger. Handle that returned page first,
then call again for the new event. An untriggered call while failed work is in backoff raises retryable
`secretary_unavailable`; a different new event can still be assessed during that backoff. Passing both `--since`
and `--floor` requests an exact replay. A trigger that is not a visible event fails the call, on every path. A
visible trigger that is not on the bounded page makes that page historical catch-up: the result has `trigger` null
and `catch_up` true, and never covers the trigger. One beyond the page stays pending; one already behind it was
covered by an earlier page, or waits in a retry page. A trigger is on a page when its event's position is after the
page's start and at or before its end. A due digest retry that a triggered call resumes is catch-up
too. Page size also reserves a citation budget from model output
capacity. Pages are never silently truncated. Transport failures back off in their original mode. Invalid model
content/output and `InboxTooLarge` count toward `max_attempts`. A stored range that is `InboxTooLarge` and can split
splits on its first refusal before any engine or model call; a refusal from the re-read after inference keeps the
counted path. A single event over the bounds gets its notice after `max_attempts`. Timeouts, rejected requests, and
failed commands also count when another page has received a valid answer under the same policy since this page
last failed; elapsed time alone never causes splitting. Each poll reserves one configured budget
for a fresh alert and another for scheduled work. It runs one eligible scheduled lane per poll, alternating retry
and digest priority, so a lane receives the full reserved budget rather than leftover time. An hourly host that
runs the queue's digest lane whenever a held page is due gets the same alternation from stored state: a digest marks
the earliest due retry it passes over, and the next digest lane takes that retry first, clearing the mark as it
does. A callback that attempts the marked retry in between clears the mark too, so the next lane digests.

The worker batches contiguous due held pages into one bounded hourly digest at `digest_seconds` intervals.
Hold, keep, and model-error pages remain eligible while scanning continues. Pending wake IDs stay queued, with
wakes surfaced first; new IDs are announced even when older ones remain unacknowledged. Installation binds the
activation floor to the current database head. Scan progress is separate from the contiguous delivered watermark.

Every summary, including a digest, must account for and cite all events in its page; citing visible reply parents
is optional. Source checks validate references against supplied context, not the truth of summary claims, and the
prompt forbids invented assignments or approvals. `acknowledge DECISION_ID` is idempotent. Delivered ranges
merge only when contiguous, so acknowledging a later wake cannot skip a held gap. Deliver and acknowledge every
nonempty brief, including digest results with decision `keep`. Sanitized engine, model, and delivery failures
are reported separately in worker health/status and logged without provider response bodies or credentials. A model outage
without a forced engine wake remains an outage, while an unavailable model after a forced wake produces a notice.
Digest failures retain digest mode and eventually produce a keep notice, never an alert or wake. After a crash,
the queue reuses a saved result for the same policy and current source window before invoking either provider,
and returns it only to a call with its trigger and catch-up label; for a call of another kind it finishes that page
and returns nothing from it. A split, combine, failure, finish or empty-page discard changes a stored page only as
its caller read it.
Model failures distinguish missing credentials, authentication rejection, invalid requests, timeouts, and outages.
Connection/pool timeouts remain outages; read/write timeouts and HTTP 408/504 responses can count toward
splitting only after another page answers. A supplied key clears a current missing-credential failure to
`not_used` at startup while preserving its history. Delivery runs in one supervised task alongside assessment;
its retries double from `retry_initial_seconds` to `retry_max_seconds`, with warnings on failure-state changes.
Hourly host calls wait only on digest retries, and acknowledged saved results are not returned for delivery again.

`show` inspects saved assessments. Policy version 3 is required. Private state version 1 is refused and requires
a new file; state version 2 is preserved. `--state` is a private SQLite sidecar for assessment work and positions.
Deliverable summaries are also appended to shared `baton.db` table `secretary_summaries`, tagged with
`secretary_id` and principal and uniquely recorded by decision ID. `summaries` pages that archive without `--state`.
Archive reads budget 256 citation checks per call, completing at least the first row even if it exceeds that
budget. Follow `next` while `has_more` is true, including when a page returns no visible entries.

`read` returns currently visible source text for the principal. After an event position (`--after`), it pages
events addressed to or authored by the principal and relevant job events in `deliveries` and `context`; with
`--ids`, it reads the named messages and reports inaccessible messages as missing. Message rows include their
IDs and conversation IDs for opening the cited thread. A page holds at most `--limit` events or messages (default
20, maximum 100) and aims for `--max-bytes` (default 256 KiB); one oversized entry may be returned alone.
Position-based reads return `through_pos` for the next page; ID-based reads report any `not_read` IDs. `read` opens
the database read-only, needs no `--state`, and writes nothing; the canonical module command is
`python -m baton.agents.secretary.inbox.read`. `python -m baton.secretary_read` is a compatibility module
wrapper with the same options; there is no `baton.secretary_read` executable. `python -m baton.agents.secretary.attention.decide`
is the canonical decision module; `python -m baton.secretary_decide` remains a compatibility module wrapper.

Secretary policy and installed-instance configuration code lives in `baton.config.secretary`. The Baton host
TOML format is unchanged. Secretary policy version 3 is required; policies v1 and v2 are unsupported. Private
state version 1 requires a new file; state version 2 is preserved. The installed integration JSON is a
separate version 1 worker configuration.

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
