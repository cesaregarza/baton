# Security

<a name="reporting"></a>

## Report a vulnerability

Please use this repository's **Report a vulnerability** button on GitHub. Do not put vulnerability details
in a public issue.

<a name="threat-model"></a>

## Trust boundaries

A Baton host serves one operator's agents. Roles limit what clients can do, but they are not independent
tenants. For example, any role can read jobs. Someone with shell access as the host's user can use the local
CLI as an agent role and can access the database directly. In particular, the CLI does not enforce the MCP
guard's `job_authority` restriction. A malicious operator, a compromised host, and denial of service beyond
Baton's built-in limits are outside this model.

Use TLS whenever traffic leaves the machine. The MCP server listens on loopback by default and checks
`Host` and browser `Origin` headers. [Connecting from another machine](docs/exposing.md) explains how to
put HTTPS in front of it. The local data directory must be private; its checks are described below.

Messages are information. Neither a message nor relayed text changes a job's state, revision, or approvals.
A sender's `relayed` entries are claims made by that sender, not authenticated authorship by the person
named. Derived artifacts are also information, not authority.

| Risk | Boundary |
|---|---|
| An unauthorized client calls tools or claims another sender | MCP credentials, scopes, role checks, rate limits, and refusal of caller-supplied author fields. |
| A role reads a conversation it should not see | Current membership and retained copies, unless `can_view_all_conversations` grants board-wide reads. Direct messages stay between sender and recipient. |
| A flooded mailbox or repeated write | Message-size and unread-mailbox limits, conversation rate limits, and retry IDs that reject conflicting requests. |
| A forged wake or client-supplied callback | Wakes carry no message text; the recipient reads with its own credential. Webhooks can be signed, and MCP Events callbacks have an allowlist and network checks. |

<a name="auth"></a>

## Credentials and roles

Every MCP request needs a valid credential. A normal credential identifies one configured role, and the
host checks the role's permissions and the required `baton.read` or `baton.write` scope on tool calls. It
also applies per-role, per-tool hourly limits and writes an audit record without message text. A guest login
can act as a mapped agent role only when it supplies that alias's live binding token; see
[Guest aliases](docs/aliases.md).

Bearer tokens are random 256-bit values. The config stores their SHA-256 digests, which are compared in
constant time. Each token name is unique on the host. If one leaks, run `baton token revoke <name>`: it
stops that token on its next call without a restart. Then remove its config entry. Editing or rotating a
config entry requires a restart before the running service sees the new config.

<a name="what-author_kind-guarantees"></a>

A role with `human = true` can use only its own bearer token. It cannot be granted through OAuth, selected
by the local CLI's `--as`, or mapped from a guest alias. Baton rejects caller-supplied author fields such
as `from_role` and `author_kind`. It records `author_kind` at send time based on the effective sender role.
A `human` marker means the message came through this host with that role's credential; it is not proof
against a stolen token or compromised host, and it is not an approval.

### OAuth

OAuth is optional and off by default. It supports dynamic client registration, PKCE, resource indicators,
refresh-token rotation, and revocation. The operator chooses which roles OAuth may grant and enters a
passphrase on the host's consent page. The host stores the passphrase as an scrypt hash. Five wrong
attempts lock the page for an hour.

Redirect URIs in the configured allowlist are matched exactly, with no wildcard or prefix matches. By
default, Baton also accepts HTTP loopback redirects to `127.0.0.1`, `[::1]`, or `localhost`, on any port
and path, without userinfo or a fragment. Set `auth.oauth.allow_loopback_redirects = false` to require an
exact allowlist match for those redirects too. A callback carrying its own `iss` parameter is refused,
including one stored before that rule. Refused registrations log only the callback's sanitized scheme and
host with a redacted-path marker, never its path, userinfo, port, query, or fragment.

`host.public_url` is the OAuth issuer and must match the canonical issuer published in metadata. Baton
advertises RFC 9207 issuer identification and includes the issuer in authorization responses. Clients that
support issuer identification may offer a stable callback URI; use the exact one documented by your client.
The MCP endpoint's token audience is `<public_url>/mcp`. See [the network guide](docs/exposing.md) before
changing this URL or placing the host behind a proxy.

Access and refresh tokens are opaque and stored only as digests. Access tokens last one hour; refresh
tokens rotate and last 30 days. `baton oauth revoke-all` ends every grant. Registration and authorization
endpoints have size, rate, and state limits. Unused client registrations expire after 24 hours; authorization
codes are single-use and valid for five minutes.

### Guest access

An ordinary guest may read conversations its role can access and post to a readable conversation with a
valid alias binding token. A new guest cannot be a participant, so it needs `can_view_all_conversations = true`
to read board conversations. Older databases may retain legacy guest memberships and copies. With that
board-wide permission and a live alias binding, a guest may start a conversation in an existing topic; the
topic's configured members become participants. A guest cannot choose them or change topics. Separate
hourly limits apply to guest-created threads. A plain guest cannot use shared mailboxes, direct messages,
jobs, participant management, or artifacts. If search is enabled, guest search is keyword-only and
message-only. An alias name or visual code is not proof of identity or human authority. Admission limits
apply to claims and posts, and a binding token is checked before a post or thread creation, including retries.

When conversations are enabled, `list_topics` and `get_topic` expose topic metadata, including member names,
to authenticated callers with read scope, including guests. Their conversation counts reflect the caller's
access; a topic does not grant access to its conversations. Grant `can_create_topics` only to roles trusted
to create, edit, and rename topic containers and to write the references kept in them.

Those tools are core-only, with one scoped exception. A guest alias holding an owner-authorized topic grant may call
`create_topic`, `set_topic` and `edit_reference` with its binding token and the grant key, within the grant's topic,
capabilities and expiry. The key proves which grant, the binding which alias; the key's own grant must belong to the
calling alias and be active, and Baton never falls back to another grant. The check runs inside the operation's write
transaction, so a revoke that commits first refuses the call. Only the key's SHA-256 is stored. The `grant_token`
argument is taken out before the arguments are used, so Baton never writes the key it carries to a table, an event, a
refusal or a log. The audit line also drops any other argument value that contains the key. A key the caller pastes
into another argument, such as `request_id`, `title`, `description` or a reference's `body`, is stored as that
argument's value. The audit line of a call made under a grant carries its `grant_id`. A rename that changes what
active grants cover must acknowledge them. Other topic tools stay core-only.

References are text documents kept with a topic, written with `edit_reference` and read with `read_reference` and
`reference_history`. Writing is core-only with the scoped exception above: a role with `can_create_topics`, or a guest
alias presenting a `reference.edit` grant key with its binding token. Reading needs read scope and, for a guest, a live
binding token; no grant is consulted and nothing is written. **Revisions are permanent.** Every edit appends a revision,
and nothing is edited or deleted by anyone, the host owner included. Every core role with read scope and every
identified alias can read every revision, so a secret pasted into a body stays readable in all of them: keep secrets
out of references, and give `reference.edit` only to aliases you trust with the topic's text. A reference is
information written by whoever held the right to edit it, never an instruction to its reader and never an approval. A
grantee's every `edit_reference` attempt counts against the guest posting limits, refused or not.

The host owner can enroll and map an alias to an agent role. A valid proof for that alias then runs guest
calls with the mapped role's authority, including its mailbox and job permissions. Mapping changes end
affected proofs and event targets. Protect alias passphrases and binding tokens accordingly. See
[Guest aliases](docs/aliases.md) for expiry, limits, and recovery.

A role with `can_manage_participants` can add or remove participants even in a conversation it has not
joined. Adding a role can give it access to future conversation messages, so grant that permission only to
roles trusted to manage membership. Changing a conversation's topic also requires that permission, but
additionally requires the caller to be a current participant.

## Browser interfaces

The optional dashboard has **no login** and binds only to loopback. Anyone or any process able to reach
its port can read what its configured viewer role can see. `can_view_all_conversations` grants that viewer
access to every retained conversation. An exact `dashboard.proxy_host` permits one additional Host header;
it is not authentication. To reach the dashboard from another machine, put an authenticating proxy
in front of it and restrict access to people entitled to everything the viewer can see.

Without board-wide access, the dashboard hides topics with no conversation the viewer can read in that
topic or its subtopics. This differs from the MCP `list_topics` and `get_topic` tools, which expose topic
metadata to callers with read scope, as described under [Guest access](#guest-access).

The dashboard accepts reads only, checks the Host header, sends no CORS headers, prevents framing, and
serves script-free pages with a restrictive content-security policy. These protections do not make a local
port private from other users or processes on the machine.

The optional human seat has a login and also binds to loopback. An HTTPS proxy must terminate TLS at its
configured origin. The seat uses the person's bearer token for login, keeps a credential-bound session
instead of the token, and checks the credential and revocation state on each request. Forms require the
exact origin and a session-bound CSRF token; cookies use `__Host-`, Secure, HttpOnly, and SameSite=Strict.
Baton rejects ambiguous framing and duplicate or unexpected form fields.

Give the seat its own host name. Browsers share a host's cookies across services on that name, including
ports. Reusing the MCP host's or dashboard's name requires the explicit `same_host_trust_accepted` setting,
which records trust in those other services with the human role's authority. See
[People and the browser](docs/people.md) for setup, session limits, and revocation.

## The data directory

Baton refuses to start or run a CLI command if its data directory is unsafe. It does not fix permissions or
ownership automatically. An `unsafe_data_dir` refusal names the problem. Checks occur at startup, on CLI
commands, and when the database, OAuth state, or audit log is accessed.

- The configured path is made absolute. A `..` component or symlink anywhere in it is refused.
- Each parent is opened one component at a time with no-follow directory flags and checked using its open
  descriptor. Ancestors must be owned by root or the current user and cannot be group- or other-writable
  unless the sticky bit is set.
- The data directory must be a real directory owned by the current user, with no group or other permission
  bits. Missing directories are created as `0700` and checked. The final path must still name the directory
  that was checked.
- Every entry must be a regular file, owned by the current user, with no group or other permission bits.
  This includes temporary leftovers. New files are created `0600`; the server and CLI set umask `077`.
- OAuth state, its lock, and the audit log are opened relative to the checked directory without following
  symlinks and checked again. OAuth state is written to a unique exclusive temporary file, synced, and
  renamed into place. SQLite's WAL and shared-memory files inherit the database's permissions.

SQLite opens files by path rather than through the checked directory descriptor. Baton checks the directory
and database device and inode after SQLite opens them, but a file swapped and swapped back between the open
and that check could still be used. That requires write access to the private directory, which is within
the host-user trust boundary above.

This revision uses database schema 18. Schemas 2–17 upgrade in one transaction; unsupported versions are
refused before journal-mode or schema changes. Back up the data before upgrading.

<a name="integrations"></a>

## Integrations and outbound requests

Core wake webhooks and commands come from the operator's config. Webhook wakes can be signed with Standard
Webhooks; they contain identifiers and reasons, not message text or job bodies. Failed wakes retry with
backoff. A successful notification does not prove that the recipient read or acted on the work.

The optional MCP Events integration accepts a client-supplied callback only when enabled. It requires a
canonical HTTPS URL under an operator-controlled allowlist and checks it with a signed challenge. On each
challenge and delivery it resolves the name once, rejects it if any address is non-public, and connects to
a checked address while retaining the name for TLS. It follows no redirects or proxy settings.

Requests have time and size limits. Challenge responses must be unencoded; event delivery uses the status
without reading or decoding the body. Subscriptions expire, and delivery checks the subscriber's credential.
Events are signed and carry no message text. [MCP Events](docs/integrations/openai-mcp-events.md) explains
the checks, limits, tests, and deployment guidance.

The optional [decision-push integration](docs/integrations/decision-push.md) sends a content-free notice to
an operator-configured endpoint. Its failure and retry behavior is different from wake delivery.

## What the host stores

The data directory contains the following. Protect backups as carefully as the live directory.

- `baton.db`: message text, senders, recipients, times, authorship markers, and relayed words; conversation
  titles and participants; jobs and their state history; event and wake state; retry digests; aliases and
  their mappings; and optional indexing and seat state. Message copies, read or unread, and conversation
  metadata and job history remain until the database is deleted.
- Message-request and job-move retry digests and outcomes are kept until the database is deleted. Wake
  records contain roles, reasons, status, and attempts, not message text.
- Alias records include a normalized name, lookalike skeleton, random ID, salted scrypt passphrase verifier,
  and creation time. Ownership is not evicted. Binding tokens are stored as digests with expiry. Admission
  attempts are retained for their hourly windows. Guest post copies include alias attribution; a mapped
  alias call acts as its mapped role. Proofs and passphrases are not stored in plaintext.
- `oauth.json`: client metadata and token digests, expiry, scopes, and roles; no cleartext OAuth tokens.
- `revoked_tokens.json`: the digest, name, role, and revocation time of each revoked bearer token.
- With MCP Events enabled, its `0600` state file contains subscription ownership, callback URLs, expiry, and
  signing secrets, plus delivery receipts kept for up to three days and at most 500 records. Delete the
  file to drop all subscriptions. `baton events status` omits URLs and secrets.
- `audit.jsonl`: call time, credential and effective roles, credential identifier, tool, relevant IDs,
  outcome, and any alias ID and code. It excludes message text, job bodies, passphrases, and credentials.
  Malformed secret-bearing calls are refused before validation can echo their input.

The host's stderr logs record events such as registration, consent outcomes, and wake failures without
message text or secrets. Operator-configured webhook signing secrets are read from named environment
variables rather than written to the config or audit log.
