# baton design

## In one paragraph

A baton **host** is one process plus one SQLite file. It runs an MCP server, and every agent that wants to
talk to another agent connects to it as a **client**. Each client acts as exactly one **role**, such as
`planner` or `worker`. Its credential fixes the role. Clients send each other messages and move shared
**jobs** through a state graph. The host stores everything, so a sender never needs the recipient to be
online. A recipient finds out about new work by calling `wait`, by reading, or through an optional **wake**:
a webhook or local command that the host fires when something is addressed to the role.

```
 agent A (remote) ──MCP/HTTPS──┐                         ┌── webhook / command (wake)
                               ├──►  baton host  ──────► ┤
 agent B (local CLI) ──SQLite──┘    (MCP + SQLite)       └── agent wakes, then reads
```

## Goals and non-goals

Goals:

- Messages flow both ways between agents that are not online at the same time.
- Every interface follows one model: the MCP tools, the CLI and the wake hook.
- Every write (`send_message`, `create_job`, `move_job`) is safe to retry over an unreliable network.
- The host is small enough to read in an afternoon, and depends only on the MCP SDK, uvicorn and httpx.

Non-goals:

- **Running agents.** baton doesn't launch, sandbox or supervise anything. A wake hook can start an agent,
  but that is the operator's script.
- **Product integrations.** There's nothing for any particular model vendor, issue tracker or chat app.
- **Multi-tenant hosting.** One host serves one operator's agents. Roles separate the agents' views of
  the host. They are not separate security domains for different organisations.

## Roles

Roles are names in the config (`[roles.<name>]`). A role is the only identity baton knows. Messages are
addressed to a role, job states name the role they wait on, and the audit log records the role. The MCP
tools never take a role argument. The role comes from the credential (see [Auth](#auth)), so a client
can't speak for another role. The local CLI takes `--as <role>`, because anyone who can run it can already
read the database.

A role marked `human = true` is a person. It holds only a bearer token, never an OAuth grant or `--as`, and
every message records an `author_kind` (`human` or `agent`) at send time, so participants can tell a
person's words from an assistant's. See the README's "Human participants".

## Messages

`send_message(to, text, job_id?, relayed?, request_id?)` stores the text as the recipient's next sequence number
(`seq`). The numbers start at 1 and have no gaps. They are allocated under SQLite's write lock, and the
last one is kept in a `mailboxes` row, so deleting old messages never renumbers.

Reading is cursor-based:

- `read_messages(after_seq)` returns the messages after a seq you have already seen, plus a `next_seq` to
  continue from. A short page is not the end of the mailbox: keep paging until `messages` comes back empty.
- With no `after_seq`, the host uses the recipient's own delivered position, `read_seq`, so you get only
  what you haven't been given yet. Every read moves `read_seq` forward.
- `truncated: true` means retention has already deleted messages after your cursor.

**Idempotency.** A `request_id` begins with its UTC creation time (`YYYYMMDDTHHMMSSZ-<suffix>`).
Re-sending with the same id and the same body returns the original result, with `created: false`.
Re-sending with the same id and a different body is refused as `idempotency_conflict`. Ids more than 30
days old, or dated in the future, are refused. That lets the host forget retry identities after 31 days
without ever accepting a stale retry twice. When no id is given, the host makes one and returns it.

**Bounds.**

- A message is at most 64 KiB, measured as escaped JSON.
- A page is limited by count (100) and by an estimated wire size.
- A recipient with 500 unread messages makes senders get `mailbox_full` until it reads. That is
  backpressure, not loss.
- Read messages are deleted 30 days after they were sent.

**Relayed words.** A message may carry words its sender is passing on from someone else, in `relayed`: a
list of `{source, at, text}` entries, kept apart from the sender's own `text`. A malformed entry is
refused, and the refusal names the field. Relayed words are the sender's claim about what someone else
said. They are stored and returned under the sender's `from_role`, and are never that person's
authenticated authorship. They count toward the 64 KiB message limit.

**Messages are information, never authority.** Nothing a message says, in its text or in what it relays,
changes a job's state, revision or approvals. Only `move_job` moves a job.

## Conversations

A conversation is a titled thread among two or more roles, up to `max_participants` (default 8). It is off
unless `[conversations] enabled = true`. A role may open one only with `can_create_conversations`, and
must list itself among the participants. Adding or removing participants needs `can_manage_participants`.

**Fan-out.** `send_message(conversation_id=...)` stores one copy for each other participant, in one
transaction. Each copy is the recipient's next seq in its own mailbox, and the copies share one
`message_id`. The result gives each recipient's seq in `seqs`. Every recipient gets its copy, or, if any
recipient's mailbox is full, none does (`conversation_full`, naming the role). The sender gets no copy.
From each recipient's side a conversation message is an ordinary message with a `conversation_id`, so
cursors, `wait`, retention and wakes work exactly as for direct messages. In particular, each recipient is
woken once per unread batch, and the sender is never woken by its own message.

**Who may read and send.** Only current participants may send to a conversation or read its history
(`not_a_participant`).

**History.** `read_history(conversation_id, after_seq?)` is kept apart from delivery. It returns the
conversation as the reader saw it, its own messages included, each with `from_role` and
`message_id`. Its `seq` is the conversation's own gap-free order (`conversation_seq`), the same for every
participant. It marks nothing read and moves no cursor, so `read_messages` and `wait` still hand over each
unread message exactly once. Pages have the same count and size limits as `read_messages`. A reader's
history is what it received and what it sent, which is exactly the messages sent while it was a
participant: a role added later sees the conversation from the notice of its join on, and a role added
back after a removal sees each stretch it was in. A message drops out of a reader's history once
retention deletes the reader's copy. A removed role can't read the history at all, but the messages it
already received stay in its mailbox.

**Threads.** A conversation message may carry `reply_to`, the `message_id` of a message it answers. A
reply is an ordinary message in every other way: it fans out, pages, retries and expires like any other,
and `reply_to` is part of the send's retry identity. `reply_to` must name a message in the same
conversation that the sender can see, or the send is refused as `invalid_request` and nothing is stored,
so a reply can never point at a message that was not the sender's to answer. A direct message has no
thread to reply into.

Readers see `reply_to` as `{"message_id", "seq"}`, where `seq` is the parent's place in the conversation.
Visibility is the reader's, not the sender's: where the reader cannot see the parent -- it joined after
the parent was sent, or retention has taken its copy -- `seq` is `null` and a reader shows the reply as
*parent unavailable*. The reference never carries the parent's text or its author, so a thread cannot
leak a message the reader was not party to. Nesting is left to the reader: the host stores one flat
conversation order and does not limit how deep replies go.

**Attention.** A conversation message may name the participants it asks to look, in `attention`, a list
of roles, and may ask the conversation for a decision with `needs_decision`. Both only draw attention.
None of the three is carried by a direct message, and supplying one on a `to=` send is refused as
`invalid_request` rather than ignored -- an empty `attention` or `reply_to` included, since a field that
was supplied at all was supplied on purpose. `attention` is structured, never parsed from `@name` text,
and is checked against the conversation's current participants: naming a role that is not in the conversation is refused (`not_a_participant` for
a configured role, `unknown_role` for one the config does not have), and naming a role never adds it or
speaks for it. `needs_decision` is a label and nothing more: like any message it leaves every job's
state, revision and approvals alone. baton still has no approval message type.

**Participants.** Adding a participant sends a host notice to every participant, the new one included.
Removing one sends a notice to those who remain, and the removed role receives nothing from then on. A
host notice has `from_role: null` and a `notice` object (`{"event", "participant", "by"}`). A conversation
keeps at least two participants.

**Topics.** A conversation may carry an optional `topic`: one or two lowercase slug segments, `project`
or `project/subtopic`. A segment is 1-40 characters of `a-z`, `0-9` and inner hyphens, which means a
stored topic can carry neither of SQL's `LIKE` metacharacters, so a prefix filter matches it literally
with nothing to escape. `null` is how no topic arrives and how one is cleared. A topic is validated whole
when it is written, and a filter is validated the same way before it is used.

It is a label for people, not an identifier: the `conversation_id` is unchanged by a rename, so nothing
that refers to the conversation moves. Setting, renaming or clearing one takes `can_manage_participants`
*and* membership of that conversation -- a label on a conversation is the conversation's own business, so
a role with the permission that is not in it is refused like any non-participant.

Filtering is whole-segment: a topic matches itself and anything one separator deeper, so `project` takes
`project/alpha` and never `projectile`. It narrows and never widens -- `list_conversations` still joins on
membership, so a filter cannot surface a conversation the caller could not already list. An unfiltered
list keeps the conversations with no topic, and the dashboard groups by topic with the untagged in a group
of their own, so a topic organises the view without becoming a requirement. Threads are unchanged: a
subtopic is not a thread, and replies are still `reply_to`.

**Limits.** A conversation accepts `max_messages_per_hour` messages from its participants (default 30;
host notices don't count). Past that, sends are refused as `conversation_rate_limited`, and the host
addresses one `conversation.rate_limited` event to `escalation_role`, if one is configured, at most once an
hour per conversation. The event, and its wake, carry only the conversation's id.

**Retries.** A conversation send is idempotent by `request_id`, exactly like a direct one. A retry
returns the original `message_id` and `seqs` with `created: false`, and stores nothing.
`create_conversation` takes a `request_id` too.

## Jobs

A job has a title, a body, a state and the role it is `waiting_on`. The state graph is in the config:

```toml
[jobs]
initial = "open"
[jobs.states.open]
waiting_on = "worker"
next = ["working", "cancelled"]
```

- `move_job` follows one edge of the graph, or the move is refused as `illegal_transition`.
- Each job has a `revision`, starting at 1, which goes up by one on every state change and only then: a
  message about the job leaves it alone. Every job read and write returns it.
- **Retrying a move.** `move_job` takes a `request_id` under the same rules as messages (see
  Idempotency above), and the host makes one if you don't. A retry with the same id and body returns the
  original outcome, with `created: false`, and changes nothing, even if the job has moved since. The same id
  with a different body is `idempotency_conflict`.
- **Guarding a move.** `move_job` also takes an optional `expected_revision`. If the job's state has changed
  at all since that revision, including a change that came back to the same state, the move is refused as
  `stale_revision` and nothing is recorded. That is the whole guarantee: it detects any state change since
  the revision you read. It is not a lock, and it does not order concurrent moves beyond that. Of two moves
  made against the same revision, the first wins and the second is refused.
- Entering a state appends an event addressed to that state's `waiting_on` role.
- A state with no `next` is terminal. Reaching one tells the job's creator.
- Any role may move a job along a legal edge. The graph, not the role, decides what may happen next.
- A message may name a job. It is then logged in that job's history too.

## The event log, `wait` and wakes

Every job change and every message appends a row to one `events` table. Each row has a global position
(`pos`), an `audience` role and a `kind`.

**`wait`** keeps one cursor per role. It returns the events addressed to the role after that cursor,
oldest first, and moves the cursor forward. Two waits for the same role never return the same event.
Message events carry the message itself, and returning one marks it read. Messages already fetched with
`read_messages` are skipped. Over MCP, `wait` blocks for up to 30 s. From the CLI it can block
indefinitely.

**Wakes** are for agents that aren't sitting in `wait`. A dispatcher thread follows the event log with its
own cursor. For each event addressed to a role with a `wake` hook, it records one wake and delivers it:

- **webhook:** a POST of the wake JSON. When `secret_env` is set, it is signed with
  [Standard Webhooks](https://www.standardwebhooks.com/): `webhook-id`, `webhook-timestamp` and
  `webhook-signature = v1,base64(HMAC-SHA256(id.timestamp.body))`. Redirects are not followed.
- **command:** an argv, run without a shell, with the wake JSON on stdin. Exit status 0 means delivered.

A wake carries no message text and no job body:

```json
{"type": "baton.wake", "role": "worker", "host": "https://baton.example.com",
 "reason": "message", "first_unread_seq": 4}
```

The woken agent reads with its own credential. A duplicated, late or forged wake therefore reveals nothing
and changes nothing.

**Message wakes coalesce.** The wake is keyed by the role's first unread seq, so a burst of messages wakes
the role once. The next wake comes only after the role has read. A read also supersedes a pending,
undelivered wake.

**A role can ask to be woken only when a message addresses it**, with `only_when_addressed` in its
`[roles.<name>.wake]` table. It still receives every message: the setting changes which ones interrupt
it, not which ones reach it, and a quiet message waits unread in its mailbox like any other. A
conversation message addresses the role when it names the role in `attention` or sets `needs_decision`;
one that does neither wakes nothing. Anything addressed to the role alone wakes it either way: a direct
message, a job change, a `conversation.rate_limited` escalation. A host notice singles nobody out, so it
does not wake a quiet role.

Quiet messages never swallow the next one that matters. Because the wake is keyed by the role's first
unread seq, a message that addresses a role with a batch of quiet messages already waiting still wakes
it exactly once, and that one wake points at the first unread seq, so the role reads the whole batch.
Coalescing and retries cannot turn that into two wakes: the key is the same either way.

**Backpressure still applies to a quiet role**, and being quiet makes it likelier to bite: nothing nudges
the role to read, so its unread messages accumulate until the mailbox limit refuses the conversation's
next send with `conversation_full`, naming that role. That is the existing backpressure rather than
anything this setting adds, and it is loss-free -- a read clears it. But an operator who makes a role
quiet should give it something that reads on its own schedule, not only on a wake.

**Job wakes** are keyed by the job and its event seq. A `conversation.rate_limited` event, the one event
outside any job, gets one wake of its own: `{"reason": "conversation.rate_limited", "conversation_id": ...}`.

**Retries.** A failed delivery is retried with exponential backoff, from 1 minute up to 1 hour, for up to
8 attempts, and is then marked `failed`. `baton wakes` shows recent outcomes. The webhook URL and the
command come only from the operator's config. Clients cannot register callbacks, so the host makes no
outbound request on a client's behalf.

## Auth

All credentials resolve to a role.

- **Bearer tokens.** `baton token new` prints a random token once, together with a config entry holding
  only its SHA-256 digest. The host compares digests in constant time. Tokens are the simple option for
  scripts, local agents and clients that let you set a header.
- **OAuth 2.1** (optional) is for MCP clients that expect the
  [MCP authorization](https://modelcontextprotocol.io/specification/latest/basic/authorization) flow.
  - The host serves protected-resource metadata (RFC 9728) and authorization-server metadata (RFC 8414).
  - It supports dynamic client registration (RFC 7591), PKCE, resource indicators (RFC 8707), refresh
    token rotation and revocation.
  - Consent is a page on the host. The operator enters a passphrase, stored only as an scrypt hash, and
    picks one of the roles listed in `auth.oauth.roles`.
  - Every authorization response redirected to a client carries `iss` (RFC 9207), and the metadata
    advertises it, so clients that require issuer identification can use one stable, exact callback.
  - Redirect URIs must exactly match `auth.oauth.redirect_uris`, or be loopback (RFC 8252 §7.3) when
    `allow_loopback_redirects` is on.
  - Tokens are opaque and random, and stored only as SHA-256 digests. Access tokens last 1 hour, and
    refresh tokens 30 days, rotating on every use.
  - `baton oauth revoke-all` ends every grant at once.

Each tool call needs `baton.read` or `baton.write`, counts against an hourly limit per role and tool, and
appends one line to `audit.jsonl`. The line records the time, role, tool, the ids involved (`to`,
`conversation_id`, `participant`, `job_id`, `request_id`, `to_state`) and the outcome, never any text.

## Storage

One SQLite database (`<data_dir>/baton.db`) in WAL mode, at schema version 5. Every operation is one
`BEGIN IMMEDIATE` transaction, so the MCP server, the CLI and the dispatcher can all write safely.

**Migration.** A version-2 database is upgraded in place the first time version 3 opens it, in one
transaction, so a crash leaves it at version 2. `messages` and `message_requests` are rebuilt, because a
conversation message's copies share one id and a conversation send records several seqs; every row is
copied, so no seq, cursor or retry identity changes. A version-3 database gains `messages.author_kind`.
Either way, every message already stored is marked `agent`, since no role could be human before. Any other
version is refused before anything is written. Its tables:

| Table | Holds |
|---|---|
| `messages` | one row per copy: text, sender, `author_kind`, recipient, seq, optional job, conversation, relayed words or host notice |
| `mailboxes` | each recipient's last and read seq |
| `message_requests` | retry identity of each send (digest only) and its seq or seqs, for 31 days |
| `conversations`, `participants` | each conversation's title, creator, last `conversation_seq` and current participants |
| `jobs` | title, body, state, waiting_on, revision, creator |
| `job_moves` | retry identity (digest) and recorded outcome of each move, for 31 days |
| `events` | the ordered log: kind, actor, audience, short summary, small JSON data (never message text) |
| `cursors` | each role's `wait` position, and the dispatcher's |
| `wakes` | each wake owed, its status and attempts |

OAuth state (registered clients and token digests) is a separate `0600` JSON file, `<data_dir>/oauth.json`.
It is replaced atomically: written to a unique, exclusively created temp file in the same directory,
fsynced, then renamed over the old one.

`data_dir` must be private, and baton checks it rather than trusting it. See
[SECURITY.md](../SECURITY.md#the-data-directory) for exactly what is checked.

**Reads and `wait`.** `read_messages` with an explicit `after_seq`, `get_job` and `read_job_events` are
plain reads, safe to repeat. `wait`, and `read_messages` without a cursor, move the role's delivered
position forward. If a response is lost in transit, re-read with `read_messages(after_seq=...)` from the
last seq you hold, and `read_job_events` for jobs.

## Integrations

An integration is code for one particular kind of client. It lives in `baton/integrations/`, and the core
imports it only when an `[integrations.<name>]` table enables it. A role uses it for wakes with
`wake = { integration = "<name>" }`. The integration may add MCP methods and take over the final step of
delivering a wake. Coalescing, leases, backoff, attempts and `baton wakes` stay in the core. Its state goes
through the same data-directory checks as everything else. The integrations available, and what each
one verifies, are documented in `docs/integrations/`.

## Dashboard

An optional module, `baton/dashboard.py`, off unless `[dashboard] enabled = true`. It serves a read-only view
of conversations from the host's own store, on a standard-library HTTP server beside the MCP server. It has
no dependencies, no scripts and no token: the host reads its own database.

**What it shows.** What one configured `viewer_role` may see: the conversations that role is in, each from
its join point, exactly as `read_history` returns them. A role sees every conversation only when its config
sets `can_view_all_conversations`, which defaults to false. Each message shows `from_role` and a badge;
relayed entries render in their own block, apart from the sender's text, and participant notices render as
notices.

**Read-only and bookmark-neutral.** It serves only GET and HEAD; anything else gets `405`. Viewing reads the
way `read_history` does, so it marks nothing read, moves no cursor and changes no unread count.

**Threat model: the machine it runs on.** The view has no login, so any person or process that can reach
the port can read what the viewer role sees; on a shared machine, choose a viewer role that sees little.
The config refuses any `listen_host` but `127.0.0.1` or `::1`. Against other sites open in a browser on the
same machine:

- It answers only when the `Host` header is its own loopback address and port, and refuses anything else
  with `421`, so a site that rebinds its own name to loopback reads nothing.
- It sends no CORS headers, so another origin can't read a response.
- Every response carries `Content-Security-Policy: default-src 'none'; style-src 'self'; base-uri 'none';
  form-action 'none'; frame-ancestors 'none'`, with `X-Frame-Options: DENY` and `X-Content-Type-Options:
  nosniff`. Pages are rendered on the server with every value escaped, and contain no scripts.
- It has no write endpoints, so there is nothing for a cross-site request to change.

Reaching the view from anywhere but the machine itself needs its own authentication in front of it, and
is the operator's separate decision.

## What was deliberately left out

- **Agent runners and approval gates.** Launching, sandboxing and approving an agent's actions belongs in
  the agent's own tooling. A wake hook is the integration point.
- **Client-registered callbacks in the core.** The core never fetches a URL a client gives it; its wake
  targets come only from the operator's config. Callbacks that clients register belong to the optional
  MCP Events integration (`baton/integrations/`, documented in `docs/integrations/`), loaded only when
  the config enables it. It accepts only HTTPS URLs under an operator allowlist, verifies each one with a
  signed challenge, resolves the name once per request and connects only to global addresses, follows no
  redirects, signs every event, and keeps role isolation.
- **Push to phones or chat.** Point a wake webhook at whatever notification service you use.
- **A web dashboard in the core.** `baton job list`, `baton wakes` and the database itself are the core's
  views. An optional, read-only dashboard module is allowed (see [Dashboard](#dashboard)): it is off unless
  the config enables it and changes nothing when it is on.
