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

## Messages

`send_message(to, text, job_id?, request_id?)` stores the text as the recipient's next sequence number
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

**Job wakes** are keyed by the job and its event seq.

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
  - Redirect URIs must exactly match `auth.oauth.redirect_uris`, or be loopback (RFC 8252 §7.3) when
    `allow_loopback_redirects` is on.
  - Tokens are opaque and random, and stored only as SHA-256 digests. Access tokens last 1 hour, and
    refresh tokens 30 days, rotating on every use.
  - `baton oauth revoke-all` ends every grant at once.

Each tool call needs `baton.read` or `baton.write`, counts against an hourly limit per role and tool, and
appends one line to `audit.jsonl`. The line records the time, role, tool, the ids involved (`to`, `job_id`,
`request_id`, `to_state`) and the outcome, never any text.

## Storage

One SQLite database (`<data_dir>/baton.db`) in WAL mode, at schema version 2. A database from another
version is refused, and baton doesn't migrate. Every operation is one `BEGIN IMMEDIATE`
transaction, so the MCP server, the CLI and the dispatcher can all write safely. Its tables:

| Table | Holds |
|---|---|
| `messages` | message text, sender, recipient, seq, optional job |
| `mailboxes` | each recipient's last and read seq |
| `message_requests` | retry identity of each send (digest only), for 31 days |
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
- **A web dashboard.** `baton job list`, `baton wakes` and the database itself are the views.
