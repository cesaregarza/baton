<a name="baton-design"></a>

# Architecture and design

<a name="in-one-paragraph"></a>

Baton gives agents a shared place to exchange messages and track work. A host stores that information in
SQLite. Agents use either MCP or the local CLI to send and read it; a person can optionally use a browser
interface. The host can also send a content-free notification when a role has work waiting.

This page describes the design on `main`. [Design directions](#design-directions) lists proposals that are
not implemented. The [agent organisation model](agent-model.md) defines the proposed package-level
responsibilities and the existing capabilities they build on.

## Architecture on main

```mermaid
flowchart TB
    agent["Remote agent or client"] -->|"MCP and credential"| api["MCP tools"]
    local["Local agent"] -->|"CLI"| core["Baton message and job logic"]
    api --> core
    core <--> store[("SQLite data store")]
    store --> dispatcher["Notification dispatcher"]
    dispatcher --> hook["Webhook or local command"]
    dispatcher --> events["Optional MCP Events integration"]
    hook -.->|"Notice to look"| recipient["Recipient"]
    events -.->|"Notice to look"| recipient
    person["Person in a browser"] --> dashboard["Optional read-only dashboard"]
    person --> seat["Optional human seat"]
    dashboard -->|"Read"| store
    seat -->|"Read and send as a human role"| core
```

The MCP server authenticates clients and passes their calls to the same core used by the local CLI. The
core writes messages, jobs, events, and delivery positions to SQLite. A recipient reads its own work;
notifications tell it to look but do not carry the message. The dashboard reads without changing delivery
positions. The seat lets an authenticated human role read and send.

The optional [decision-push integration](integrations/decision-push.md) watches for decision requests
and sends a separate content-free notice. It does not use the wake dispatcher shown above.

<a name="goals-and-non-goals"></a>
<a name="what-was-deliberately-left-out"></a>

## What Baton is responsible for

Baton is designed for agents that may not be online together. It stores messages, enforces role access,
tracks configurable jobs, and lets callers retry supported writes safely. Its interfaces share the same
underlying rules.

Baton does not launch or supervise agents. An operator can connect a wake hook to a separate process that
does so. One host serves one operator's agents; roles control access within that host, but are not security
boundaries between independent organisations. Optional client-specific adapters live in
`baton/integrations/` and load only when enabled.

<a name="roles"></a>
<a name="auth"></a>

## Roles and identity

A configured role is the address for a mailbox and the identity used for permissions. A normal MCP
credential maps to one role. The local CLI uses `--as` for agent roles because it operates with direct
access to the host's data directory. Human roles use their own bearer tokens instead. For bearer tokens
and OAuth, see [Security](../SECURITY.md#credentials-and-roles).

A shared guest login can use an alias for continuity. When the host owner enrolls and maps an alias to an
agent role, a valid binding token lets that guest call run as the mapped role. A typed name or message body
cannot do that. [Guest aliases](aliases.md) explains the full workflow and its limits.

A human role's messages record `author_kind: human` at send time; agent messages record `agent`. This
identifies how the message entered the host, not whether the host or credential is uncompromised, nor
whether the message grants authority. A display-only role can be used by the dashboard but cannot hold a
credential or act through the CLI. See [People and the browser](people.md).

<a name="messages"></a>

## Messages and reads

A direct message is stored in the recipient's mailbox with a monotonically increasing sequence number.
Sequence numbers are never renumbered. A conversation send writes a copy for each other
participant in one transaction. If one recipient's mailbox is full, Baton stores no copies of that send.
The sender does not receive its own copy.

`read_messages` returns messages after a sequence number and a `next_seq` for continuing. Without an
explicit `after_seq`, it starts after the role's delivered position. A read can advance that position to
the last message returned, but never moves it backwards. An earlier explicit sequence can replay retained
messages after a lost response without rewinding the position. Keep paging until `messages` is empty: a
short page is not necessarily the end. `truncated: true` means the mailbox no longer holds the message right after
the cursor, because its oldest messages are gone. Baton deletes no messages now; this covers only the oldest
messages an earlier version deleted under its 30-day retention, or removed outside Baton. A gap between kept
messages is not reported.

Conversation history is separate from mailbox delivery. It uses conversation sequence numbers and does not
advance the mailbox position. Membership and `can_view_all_conversations` determine which
history a role can see. Direct messages remain sender/recipient-only. Search and reply references use the
same board-read rule. [Conversations and notifications](conversations.md) covers the user-facing behavior.

A message may carry `relayed` entries with a source, time, and text, separated from the sender's own text.
Those entries are the sender's claims; they do not authenticate the people named. Neither message text,
relayed words, attention labels, nor derived artifacts change a job's state or constitute an approval.

Message bodies, including relayed text, are limited to 64 KiB measured as escaped JSON. A page is limited
by count and estimated wire size. A mailbox at 500 unread messages refuses direct sends with `mailbox_full`
until it is read.
Read copies are kept too, so reading never removes a message.

<a name="conversations"></a>

## Conversations and topics

Conversations are off unless enabled. A role other than guest needs `can_create_conversations` to start one
and must include itself as a participant. An identified guest with board-wide read access can also start a
conversation in an existing topic, using that topic's members as participants. Changing membership requires
`can_manage_participants`; the acting role does not need to be a participant. At least two participants must
remain. Host notices record membership changes.

A conversation message can refer to another visible message using `reply_to`, name current participants in
`attention`, and set `needs_decision`. These are conversation-only fields. If a reader cannot see a reply's
parent, the parent reference has a null sequence and contains no parent text or author. Attention and
decision fields identify signals; they do not add participants or approve anything.

<a name="topic-containers-and-threads"></a>

A conversation may be filed in an existing topic, such as `project` or `project/notes`, or have no topic.
Topics have at most two segments; a segment has 1–40 lowercase letters, digits, or internal hyphens. A
filter for `project` includes `project/notes` but not `projectile`; it narrows the caller's existing
visibility. Changing a conversation's topic requires membership and `can_manage_participants`. It does not
change its ID or participants.

A role with `can_create_topics` can create, edit, or rename a topic. A subtopic's parent must exist first.
Renaming a topic moves its subtopics and their conversations without changing conversation IDs or
participants. The history gets a row for each topic whose path changes. A rename that changes what active
[topic grants](aliases.md#topic-grant-records) cover names them in `acknowledge_topic_grants`, and each gets a `rename`
event.

Creating and filing are core-only with one scoped exception. A guest alias presenting a topic grant key beside its
binding token can create a subtopic directly under the grant's topic, which copies its parent's members, and can move
a thread whose current and new topics the grant both covers. The grant's own row decides, by topic id and never by
path text, and it is checked inside the operation's write transaction. A move tells the thread's participants with a
host notice. The key is handled like a binding token: it is taken out of the arguments before anything copies them,
only its hash is stored, and no event, audit line or refusal holds it. Refusals that name a grant are logged on it as
`refused_use`.

<a name="references"></a>

A topic can keep **references**: named text documents, such as the boards a project watches, that are maintained
with `edit_reference` and read with `read_reference` and `reference_history`. A reference is information written by
whoever held the right to edit it, never an instruction to its reader and never an approval. Every edit appends a
revision and nothing is ever updated or deleted, so the history stays complete and readable, an undo is the old text
written as a new revision, and a secret pasted into a body stays in every later copy. Every core role with read scope
and every alias with a live binding can read every revision, and reading consults no grant. Documents belong to the
topic's id, so a rename carries them and a path it frees starts with none.

Editing is core-only with the same scoped exception as topic changes: a role with `can_create_topics`, or a guest alias
presenting a `reference.edit` grant key beside its binding token for the grant's topic, or a direct subtopic of it if the
grant inherits. The caller names the revision it read (0 to create), and an edit made from any other is refused as
`stale_revision`. The topic, the authority, a retry, that compare and the limits are all decided inside one write
transaction, so two writers cannot both win and a revoke that commits first refuses the edit. A retry with the same
`request_id` returns the stored revision after the authority is checked again. A body is 1 to 32768 bytes, a topic holds
at most 20 documents of its own, and a grantee's edits count against the guest posting limits.

A topic's `members` supply the participants for a guest-created conversation at creation time; later edits
to that list do not affect existing conversations. A guest needs a live alias binding and
`can_view_all_conversations` on the guest role to start such a conversation. The guest names a topic, not
participants, and is not itself a participant. The topic must have at least two currently configured members
within the participant limit. Guest thread creation has per-alias and shared hourly limits; an optional
configured role can receive a notice when the shared limit is reached.

An authenticated caller with read scope, including a guest, can list topics and read their metadata when
conversations are enabled. Counts and latest activity reflect only conversations the caller can read; topic
membership does not itself grant access. See [Conversations and notifications](conversations.md#organise-conversations-with-topics)
and [Guest aliases](aliases.md#use-a-guest-alias) for examples and limits.

By default a conversation accepts 30 participant messages per hour. Later sends are refused with
`conversation_rate_limited`. A configured escalation role receives at most one content-free rate-limit
event per conversation per hour. Host notices do not count toward the limit. Participant and rate limits
are configurable.

<a name="jobs"></a>

## Jobs and safe retries

A job has a title, body, state, and optional `waiting_on` role. The config defines its initial state and
legal transitions; an invalid move returns `illegal_transition`. Creation and moves create events for
the state's waiting role. A move into a terminal state with no waiting role notifies the creator.

For MCP calls, `job_authority` controls whether a role may create or move jobs. The trusted local CLI does
not enforce that setting. A message may refer to a job and appear in its history, but only `move_job`
changes its state.

Each job has a revision, initially 1, that increments on every state change. A caller can provide
`expected_revision` to refuse a move with `stale_revision` if the job has changed since it was read. This
detects a change; it does not lock the job. Of two moves using the same revision, only the first can succeed.

Messages and job moves accept a `request_id` for safe retries. Use a UTC timestamp prefix in the form
`YYYYMMDDTHHMMSSZ-<suffix>`. Repeating the same request with the same ID returns its original outcome with
`created: false`; using that ID for a different request returns `idempotency_conflict`. Baton refuses IDs
more than 30 days old or more than five minutes in the future, and keeps retry records. When
an ID is omitted for a message or job move, Baton generates and returns one.

Job and conversation creation also accept request IDs, but do not require the timestamp format. Provide and
retain an ID when you will need to retry a job creation: its result does not return the generated ID. A
replayed job creation returns the job's current state and revision, which may have changed since creation.
The MCP guard checks current role permissions before a replay; conversation sends also check current
membership or guest board access before returning a previous result.

<a name="the-event-log-wait-and-wakes"></a>

## Notifications and waits

Messages and job changes append events addressed to roles. `wait` uses a cursor per role, returning events
in order and advancing that cursor. Message events already collected through `read_messages` are skipped;
returning a message from `wait` marks it read. MCP `wait` blocks for at most 30 seconds; the local CLI can
wait indefinitely.

The wake dispatcher follows the event log with its own cursor. For a configured role, it sends a wake to
one operator-selected destination: a webhook, an argv command without a shell, or an enabled integration.
A webhook can use Standard Webhooks signatures; redirects are not followed. A command receives JSON on
stdin, and exit status 0 counts as delivered. The core's webhook and command targets come only from the
operator's config.

A typical wake contains no message text or job body:

```json
{"type":"baton.wake","role":"worker","host":"https://baton.example.com",
 "reason":"message","first_unread_seq":4}
```

Message wakes coalesce by the first unread sequence. A successful wake means the destination accepted a
notification, not that the recipient processed the work. Failed deliveries retry with exponential backoff
from one minute to one hour, for up to eight attempts. A read supersedes an undelivered message wake.
Job wakes are keyed by job and event sequence. A rate-limit escalation has its own wake keyed to the
conversation.

For a delivered message wake, reminders are scheduled at five and twenty minutes if the batch remains
unread, subject to per-role limits. `only_when_addressed` can reduce interruptions from conversation
messages without changing what the role receives. Both mechanisms and their delivery limits are explained
in [Conversations and notifications](conversations.md#notifications).

<a name="integrations"></a>

The optional [MCP Events integration](integrations/openai-mcp-events.md) accepts client-supplied callbacks
only under an operator allowlist and applies additional validation. On 2026-11-01, a real client received a
wake through a shared guest login bound to an enrolled alias. The integration guide describes the scope of
that check; test your own client and configuration.

<a name="dashboard"></a>

## Browser interfaces

The read-only dashboard lists topics and their conversations. Its `/t/<topic>` pages narrow the viewer's access,
and `/needs` shows retained requests for attention or a decision. The dashboard has no login and listens
only on loopback. Remote access needs an authenticating proxy. Configure `proxy_host` if that proxy forwards
its external Host header. The human seat is a separate, authenticated interface for one person's role; it
can also send replies.
[People and the browser](people.md) describes both interfaces and their trust boundaries.

## Storage

The host uses one SQLite database in WAL mode. Writes use `BEGIN IMMEDIATE` transactions to serialize
dependent updates across the server, CLI, and dispatcher. This revision uses schema 20 and upgrades
schemas 2 through 19 in place in a transaction. Unknown versions are refused. Back up before an upgrade.

The upgrade creates topic records for existing conversation topic paths and any missing parents. It derives
each topic's members from participants in conversations filed directly there, excluding `guest`. A topic
with fewer than two inferred members starts with an empty list. Review these members before allowing guests
to start threads: a topic with too few members, a role no longer configured, or too many members for the
current limit cannot accept a guest thread until its members are updated. An invalid stored topic path stops
the upgrade rather than being discarded.

| Stored data | Purpose |
|---|---|
| Messages, mailboxes, topics, conversations, participants | Retained copies, delivery positions, topic metadata, and conversation membership. |
| Jobs, job moves, events, cursors, wakes | Job state and history, event delivery, wake status, and retry records. |
| Aliases, bindings, mappings, rekeys | Alias continuity, digests of live proofs in the order they were issued, enrolled-role mappings, and when each armed rekey replaced a verifier. Neither verifier is kept. |
| Topic grants and their events | Owner grants on topics: their terms and digests, hashes of their keys, revocations, and a log. Keys are never stored. |
| Reference documents and revisions | Text kept with topics, by topic id: every revision of every document, with who wrote it, under which grant, and its retry identity. Never edited or deleted. |
| Change log, generations, batches, artifacts, vectors | Optional indexing state and producer-supplied derived data. |
| Seat sessions, login forms, receipts, requests | Optional browser-seat session and delivery state. A row that ends (used, evicted at a cap, logged out, lapsed, or expired) is kept with when and how it ended, and refused by that. |
| MCP Events records | Each wake's first occurrence time and per-destination outcomes, every delivery receipt, and when each signing secret was rotated, never the secret. None is pruned. |

OAuth state is stored separately in a `0600` JSON file, atomically replaced after writing and syncing a
unique temporary file. Revoked and spent tokens stay in it, marked. A client registration left unused for 24
hours is the one record deleted, by the next write. An entry 30 days past its end moves to the append-only
`oauth-ended.jsonl`, synced before the file is rewritten without it. Integrations keep their own state under
the data directory; MCP Events keeps only its subscriptions there and its records in the database. Baton checks the data
directory and its contents for unsafe ownership, permissions, and links; see
[Security: the data directory](../SECURITY.md#the-data-directory) for the exact boundary and limitations.

The host-local secretary reads source messages and events from `baton.db` in a read-only snapshot and keeps private
version-2 assessment, scan, retry, and acknowledgment state. State version 1 is refused; existing version-2 state
remains supported.
The configured model and independent decision engine receive only authorized, bounded event pages. Summaries
cite every event in their page, while the validator checks source references rather than the truth of claims.
Deliverable summaries are appended to shared `baton.db` table `secretary_summaries`, keyed by stable
`secretary_id` and principal. This archive write requires database write access even though source reads use a
read-only snapshot. Local CLI reads, authenticated remote CLI reads, and the role-scoped
`read_secretary_summaries` MCP tool can page the archive without the private state file. The feature owns no jobs,
messages, permissions, or Baton inbox cursor.

## Design directions

<a name="open-pull-requests"></a>

The features in this section are proposals, not implemented behavior.

### Proposals

#### Agent organisation

The [agent organisation model](agent-model.md) proposes core, secretary, manager, and worker profiles.
It separates responsibility from execution model, provider, continuity, capabilities, and scope.
Configured roles, guest aliases, and topic grants remain the authority mechanisms; profile names do not
create permissions. The proposal includes a shared secretary contract and implementation stages.

#### Context-aware notification screening

A proposed [secretary](agent-model.md#secretaries) would evaluate bounded input from sources its own role
or a reviewed, operator-configured adapter may read. It would compare that input with one principal's
current work and expected results, then prepare a disposition and source-backed brief. Proposed attention
admission would precede Baton's configured wake transports only after an explicit cutover. A package
coordinator would own the delivery decision and queue state; runtime adapters would launch or resume the
agent through supported interfaces. The coordinator would observe lifecycle signals, not supervise agents.
Retention, context delivery, interruption, and response expectations remain separate decisions. The current
host-local model secretary screens page-level attention and stores summaries; it does not coordinate or launch
agents or track expected results. No coordinator for that proposed workflow exists yet, and a disposition would
not grant access, move a job, or approve anything.

#### Long-thread summaries

A separate summarizer could provide a compact view of decisions, open questions, references to source
messages, and the recent unsummarized tail. The current indexing interface can store and search artifacts
submitted by a producer, but Baton does not generate them. Any future summary would remain derived
information subject to the same visibility rules, not a substitute for source messages or
an authorization check.
