# Security

## Reporting

Please report vulnerabilities privately through GitHub's **Report a vulnerability** button on this
repository, not in a public issue.

## Auth

- **Every MCP request needs a credential.** Without a valid bearer token the response is 401. A
  credential maps to exactly one role, and tools act only as that role. No tool takes a role, a path or a
  command.
- **Bearer tokens** are 256-bit random values. The config holds only their SHA-256 digests, which are
  compared in constant time. Each token's name is unique per host. If a token leaks, `baton token revoke
  <name>` ends exactly that token from its next call, with no restart; then delete its entry.
- **OAuth** (off by default):
  - Clients register dynamically, but only with redirect URIs that exactly match the allowlist, or with
    loopback URIs if those are allowed. There is no prefix or wildcard matching, and a callback that
    carries its own `iss` parameter is refused. A client stored before that rule, whose callback carries
    `iss`, is ignored, and no redirect is ever sent to a callback that already has one. A refused
    registration is logged as the callback's scheme and host only, with a `[path redacted]` marker. No
    character of its path, nor its userinfo, port, query or fragment, is ever logged. Control characters are
    removed, and the whole value is capped at 200 characters.
  - Issuer identification (RFC 9207): `public_url` is the issuer, so it must be written exactly as the OAuth
    metadata publishes it, with a plain ASCII path. The config refuses any other form and names the one to
    write. The metadata advertises
    `authorization_response_iss_parameter_supported`,
    and every authorization response redirected to a client, success or error, carries `iss` equal to the
    metadata's `issuer`. A client that checks it can't be tricked into sending a code to the wrong server
    (a mix-up attack).
  - Authorization requires PKCE and the operator's passphrase on the host's consent page. The passphrase
    is stored as an scrypt hash. Five wrong attempts lock the page for an hour.
  - Access and refresh tokens are opaque, and stored only as SHA-256 digests. Refresh tokens rotate.
    `baton oauth revoke-all` ends every grant.
  - The public endpoints `/register` and `/authorize` are size-limited and rate-limited, both per source
    and overall. Registered-client and pending-login counts are capped.
- **Transport.** The MCP endpoint refuses a `Host` header other than its `public_url` name or loopback, and a
  browser `Origin` other than its own and those configured. It listens on loopback by default. Use TLS
  whenever traffic leaves the machine (see [docs/exposing.md](docs/exposing.md)).
- **Local CLI.** The CLI opens the database directly and trusts `--as`, except for a human role, which it
  refuses. Anyone who can run it as the host's user can act as any other role. The filesystem permissions on
  `data_dir` are the boundary (below).
- **Human roles** (`human = true`) hold only bearer tokens. They can't be offered through OAuth, the CLI
  refuses `--as` for them, and a request naming an author field is refused (`forbidden_field`).

### What `author_kind` guarantees

`author_kind: human` on a message means it was sent with a human role's own token, through this host, and
was recorded that way when sent. It doesn't guarantee:

- anything if the host machine is compromised: whoever controls it can write into the database directly;
- anything if the person's token leaks: revoke it with `baton token revoke`;
- authority: an authenticated message from a person is information, and whether an assistant acts on it is
  that assistant's own policy. baton has no approval message type.

## The data directory

baton refuses to start, and every command refuses to run, unless the data directory is private. It
**never changes** what it finds: no `chmod`, no `chown`. A refusal (`unsafe_data_dir`, exit status 2) names
the path, what is wrong, and the fix. These checks run at `serve` startup, at every CLI command, and on
every access to the database, the OAuth state and the audit log:

- **One path.** `data_dir` is turned into one absolute path, used to check, create and open it. A `..`
  component is refused, and so is a symlink anywhere in the path: write the real path.
- **Parents.** The path is opened one component at a time from `/`, with `O_DIRECTORY | O_NOFOLLOW`, and each
  directory is checked with `fstat` on the descriptor actually opened. Every ancestor must be owned by root
  or the current user, and must not be group- or other-writable unless it has the sticky bit (as `/tmp`
  does). Nothing is checked by path, so a directory swapped in after its check is never the one used.
- **The directory.** `data_dir` must be a real directory owned by the current user, with no group or other
  permission bits. A missing one is created `0700`, together with any missing parents, each inside its
  already-checked parent and then opened and checked itself. At the end, the path must still name the
  directory that was checked.
- **Its contents.** Every entry in it must be a regular file, not a symlink, owned by the current user,
  with no group or other bits. That includes leftovers such as an old temp file.
- **Opening files.** The directory is opened with `O_DIRECTORY | O_NOFOLLOW` and checked with `fstat` on
  that descriptor. The OAuth state, its lock and the audit log are opened relative to it with
  `O_NOFOLLOW`, then checked again with `fstat`. A link swapped in after the check is refused, not
  followed.
- **New files** are created `0600`. `serve` and the CLI also set the process umask to `077`. The database
  is created `0600` before SQLite opens it, and SQLite gives its `-wal` and `-shm` files the database's
  permissions.
- **The OAuth state** is written to a unique, exclusively created temp file in the directory, fsynced, and
  renamed into place. A fixed temp name is never used.

- **The database schema.** A database from another schema version is refused before anything changes it.
  baton doesn't migrate: move the old file aside.

What remains: SQLite opens its files by path, not through the checked descriptor. After it opens, baton
checks that the path still names the directory and database file it checked (device and inode), and
refuses otherwise. Someone who swapped the file and swapped it back between SQLite's open and that check
could still make SQLite use another file. That needs write access to the `0700` directory, which means
the same user or root, which is out of scope below.

## Integrations

Optional integrations (`baton/integrations/`) load only when the config enables them. The MCP Events
integration is the only one that accepts a URL from a client. When it is enabled:

- callbacks must be canonical HTTPS URLs (no `.` or `..` segments, encoded separators, userinfo, query or
  fragment) under an operator allowlist, compared segment by segment. The checked string is the one sent;
- each callback is verified with a signed challenge before anything is stored, and a refusal keeps no state;
- every request resolves the name once, requires every address to be global, and connects only to those
  addresses, keeping the name for TLS and `Host`. It follows no redirects and ignores proxy settings;
- subscriptions belong to the subscribing credential's role, are capped per role and overall, and expire.
  Every delivery needs that same credential (the same token, or the same OAuth grant) to still hold that
  role;
- every request has one absolute deadline that bounds each blocking step (connect, TLS, every write and read),
  resolves names on a fixed, bounded thread pool, refuses content-encoded responses, and reads at most
  64 KiB in total;
- a subscription keeps at most one previous signing secret and rotates at most once per overlap window;
- test-only settings can't be set from the config;
- events are signed (Standard Webhooks) and carry no message text;
- signing secrets are kept only in a `0600` state file in the data directory, and are never logged,
  audited or returned.

Its doc in `docs/integrations/` lists what is verified against the published contract, and what still
needs a live test.

## Threat model

| Threat | Mitigation |
|---|---|
| Someone on the network calls the MCP endpoint | A token or an OAuth grant is required; scopes and an hourly limit apply to every tool |
| One client impersonates another role | The role comes from the credential, never from arguments. A message's `from_role` is always its sender's credential role, and a request carrying an author field is refused (`forbidden_field`) |
| A guest role reads shared mailbox state, mutates jobs, or uses a supplied alias as authority | Guest dispatch is limited by the authenticated role to board reads and message-only keyword search; guest direct messages, membership, posting and event-subscription mutations are refused. Alias-shaped author fields are rejected before tool argument validation |
| A restricted fixed role creates or moves a job, including by replaying an old request | `roles.<name>.job_authority = false` refuses both job mutations before the tool body and idempotent replay; omitted settings preserve the existing default |
| An assistant posting as the person, or making relayed text read as theirs | A human role has only its own bearer token: never an OAuth grant, never `--as`. `author_kind` is recorded at send time from the sender's role; quoted or relayed words read as the sender's (`agent`) |
| A message, or words relayed in one, taken as an approval | Messages are information, never authority: no message changes a job's state, revision or approvals. Relayed words are typed apart from the sender's text and are never anyone's authenticated authorship |
| A role reading or posting in a conversation it isn't in | Only current participants can send or read (`not_a_participant`); creating conversations and managing participants each need a config permission |
| A stolen access token | Expires in 1 hour; revoke with `baton oauth revoke-all`, or `baton token revoke <name>` for a bearer token |
| Guessing the consent passphrase | scrypt hash, 5 failures per hour, then lockout |
| An open registration endpoint used to fill memory or disk | Size and rate limits, a cap on clients, and unused registrations expire after 24 h |
| Redirect-based code theft | Exact redirect allowlist, PKCE, single-use codes valid for 5 minutes |
| DNS rebinding against a local host | `Host` and `Origin` checks |
| Reaching the dashboard through a proxy (`dashboard.proxy_host`, off by default) | It accepts one exact extra Host value and never reads `X-Forwarded-*`. That is Host validation, not authentication: any local process can send that Host, so the proxy in front must authenticate, and admit only people entitled to everything the viewer role sees (every conversation, with `can_view_all_conversations`). Tailnet membership alone isn't that permission |
| The host made to fetch internal URLs (SSRF) | The core never fetches a client-given URL: its wake targets come only from the operator's config. The optional MCP Events integration accepts client callbacks only under an operator allowlist, with the defences below |
| A forged or replayed wake | A wake carries no content; the agent reads with its own credential. Webhooks can be signed (Standard Webhooks) |
| A message flood | Per-recipient unread cap (`mailbox_full`, or `conversation_full` with no copy stored), message size cap, per-role tool rate limits, and an hourly cap per conversation that wakes an escalation role |
| Retried writes duplicating messages or jobs | Idempotent `request_id`s; a conflicting retry is refused |

Out of scope:

- a malicious operator, or anyone with shell access as the host's user;
- denial of service beyond the limits above. Put a proxy in front for that;
- confidentiality between roles on the same host. Any role can read any job, and roles are not tenants.

## What the host stores

All of it is under `data_dir`:

- `baton.db` (SQLite):
  - **message text** with sender, recipient, time and `author_kind`, and any relayed words with their stated source and
    time. A conversation message is stored once per recipient. Read messages are deleted 30 days after
    they were sent; unread ones are kept until they are read;
  - **conversation titles and participants**. Kept until you delete the database;
  - **job titles, bodies and state history**, including the short notes given with `move_job`. Kept until
    you delete the database;
  - SHA-256 digests of message requests, and of job moves with each move's recorded outcome, for 31 days;
  - wake records: role, reason, status and attempts. Never message text.
- `oauth.json` (`0600`): registered clients' metadata (name, redirect URIs), and token **digests** with
  their expiry, scopes and role. No token is stored in the clear.
- With the MCP Events integration on, its state file holds each subscription's role, client id, callback
  URL, expiry and **signing secret** (`0600`). Delete it to drop every subscription.
- `revoked_tokens.json` (`0600`): the digest, name and role of each bearer token ended by `baton token revoke`,
  and when.
- `audit.jsonl`: one line per tool call with the time, role, tool, ids and outcome. **Never message text,
  job bodies or credentials.**

The host logs to stderr: registrations, consent outcomes, wake failures. Those logs contain no secrets,
tokens or message text. Webhook secrets are read from the environment variable named in the config, and
are never written anywhere.
