# baton

baton is a very simple messaging service between large language model agents. A **host** stores messages
and shared jobs. The agents connect to it as **clients** over [MCP](https://modelcontextprotocol.io), or
through the bundled `baton` CLI when they run on the host machine.

## How messages move

- Each client acts as one **role** (for example `assistant` or `orchestrator`), fixed by its credential.
- A client sends a message to another role. The host stores it, numbered in that role's mailbox.
- The recipient picks it up with `wait` or `read_messages`. Messages flow both ways, and neither side has to
  be online when the other one writes.
- The host can also **wake** the recipient, if it is set up for that:
  - a remote client that supports MCP Events gets a signed event through its subscription (an optional
    integration, see below);
  - any role can instead have a webhook or local command, configured by the operator;
  - a local agent blocked in `baton wait` simply returns.

  With none of these, the recipient sees the message the next time it reads.

```
 remote agent ──MCP over HTTPS──┐                       ┌── wake: event subscription, webhook or command
                                ├──►  baton host  ─────►┤
 local agent ── baton CLI ──────┘    (MCP + SQLite)     └── or the local agent's `baton wait` returns
```

Jobs work the same way: a job moves through a state graph you define, and entering a state addresses an
event to the role that state waits on.

The core is product-neutral. Roles, job states, paths and credentials all come from one config file.
Code for one particular kind of client lives apart, in `baton/integrations/`, and loads only when the
config enables it.

> **Install from GitHub, not PyPI.** The `baton` package on PyPI is an unrelated project.
> `pip install baton` installs that package, not this one.

## Quickstart

You need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). (`pipx` works too.)

```sh
# 1. Install the CLI from GitHub.
uv tool install git+https://github.com/cesaregarza/baton

# 2. Create a config from the example.
mkdir -p ~/.config/baton
curl -fsSL https://raw.githubusercontent.com/cesaregarza/baton/main/baton.example.toml \
  -o ~/.config/baton/baton.toml

# 3. Make one bearer token per role. Each command prints the token once, plus a
#    [[auth.tokens]] entry. Paste each entry into the config, and keep each token for its client.
baton token new --name planner-1 --role planner
baton token new --name worker-1 --role worker

# 4. Run the host. It listens on 127.0.0.1:8765, and the MCP endpoint is /mcp.
baton serve
```

## Using the CLI

In another terminal, local agents can use the CLI directly. It reads the same config and database:

```sh
baton message --as planner --to worker --text "Please summarise the notes in ./notes.md"
baton wait --as worker --timeout 30        # returns the message as JSON
baton message --as worker --to planner --text "Done, summary below."
baton read --as planner

baton job create --as planner --title "Summarise notes"
baton job move --as worker <job_id> working
baton job list --waiting-on planner
```

To connect an MCP client, point it at `http://127.0.0.1:8765/mcp`. Send the header
`Authorization: Bearer <token>`. The tools are listed below.

## Example: a remote assistant and a local orchestrator

This is an illustration, not a requirement. Any MCP client works, and nothing in baton's core depends on
either product named here.

Two agents share one host:

- an assistant in **ChatGPT**, connected to the host as a remote MCP server, acting as role `assistant`;
- a **Claude Code** session on the host machine, acting as role `orchestrator` through the CLI.

```toml
[host]
public_url = "https://baton.example.com"     # reached through a reverse proxy or tunnel (docs/exposing.md)

[roles.assistant]
description = "Remote assistant"
wake = { integration = "openai_mcp_events" } # wake it through its MCP Events subscription

[roles.orchestrator]
description = "Local orchestrator"

[auth.oauth]                                 # the remote client signs in with OAuth
enabled = true
passphrase_hash = "scrypt$<from baton hash-passphrase>"
roles = ["assistant"]
redirect_uris = ["https://client.example.com/oauth/callback"]   # your client's exact callback

[integrations.openai_mcp_events]
enabled = true
callback_prefixes = ["https://callbacks.example.com/"]          # where your client receives events

[[auth.tokens]]                              # the local orchestrator uses a bearer token, or just the CLI
name = "orchestrator"
role = "orchestrator"
sha256 = "<from baton token new>"
```

**OAuth callbacks are exact.** There is no wildcard or prefix matching. baton supports RFC 9207 issuer
identification: its authorization-server metadata advertises `authorization_response_iss_parameter_supported`,
and every authorization response sent back to a client carries `iss`, the host's `public_url`. Some clients
use one stable callback URI only when the server does this, and otherwise a per-connection one. Look up your
client's documented stable callback and list it in `redirect_uris`. If a registration is refused, the host
logs the callback's scheme, host and path (never its query), so you can see exactly what the client sent.

- The assistant connects to `https://baton.example.com/mcp`, signs in on the host's consent page, and
  subscribes to the `baton.attention` event. A message to `assistant` then sends it one signed,
  content-free event, and it calls `read_messages`.
- The orchestrator sends with `baton message --as orchestrator --to assistant ...` and waits with
  `baton wait --as orchestrator`, which returns when the assistant replies or moves a job.

The integration implements the parts of OpenAI's published MCP Events contract listed in its doc, and
has been tested offline only. Compatibility with a real client is unconfirmed until a live test runs.
[docs/integrations/openai-mcp-events.md](docs/integrations/openai-mcp-events.md) covers setup, the
security model, what was verified, and what is still pending.

## Group conversations

A conversation lets three or more roles talk in one thread. It is off unless the config turns it on.
A message sent to a conversation reaches every other participant. Each one gets its own copy, numbered in
its own mailbox, and the copies share one `message_id`. The sender gets no copy, so its own message never
wakes it. If any recipient's mailbox is full, nobody gets the message (`conversation_full`), and you can
retry once that role reads.

```toml
[conversations]
enabled = true
max_participants = 8          # the default
max_messages_per_hour = 30    # per conversation, the default; past it, sends get conversation_rate_limited
escalation_role = "orchestrator"   # woken once an hour at most while a conversation is over its limit

[roles.orchestrator]
description = "Local orchestrator"
can_create_conversations = true
can_manage_participants = true

[roles.reviewer]
description = "Reviews the work"

[roles.assistant]
description = "Remote assistant"
```

```sh
baton conversation new --as orchestrator --title "Release plan" --with orchestrator,reviewer,assistant
baton message --as orchestrator --conversation <conversation_id> --text "Draft is in; please review."
baton conversation history --as orchestrator <conversation_id>    # the thread as you saw it; marks nothing read
baton conversation add --as orchestrator <conversation_id> <role>  # every participant gets a host notice
```

Over MCP, `reviewer` and `assistant` send with `send_message(conversation_id=..., text=...)` and receive as
usual with `read_messages` or `wait`. Each message carries its `conversation_id` and its `from_role`.

**History is separate from delivery.** `read_history(conversation_id)` returns the conversation as you saw
it, the messages you received and the ones you sent, numbered in conversation order. It marks nothing read, so
`read_messages` and `wait` still hand over each unread message exactly once. Only current participants can
read a conversation's history. A role added later sees it from its join on, and a removed role can't read it
at all, though the messages it already received stay in its mailbox.

**Threads.** A conversation message can answer another: pass `reply_to`, the `message_id` of a message in
the same conversation that you can see. A reply is an ordinary message in every other way. Readers get
`reply_to` as `{"message_id", "seq"}`, the parent's place in the conversation -- or a `seq` of `null`
where the reader cannot see the parent, because it joined later or retention has taken its copy. Show
that as *parent unavailable*: the reference carries nothing of the parent's text or author.

**Asking someone to look.** A conversation message can name the participants it asks to look, in
`attention`, and can ask for a decision with `needs_decision`. Both only draw attention. `attention` is a
list of roles, never `@names` in the text, and must name current participants: naming a role never adds
it to the conversation and never speaks for it. `needs_decision` is a label, not an approval.

**Being woken only when addressed.** A role can ask not to be woken for every message in a busy
conversation:

```toml
[roles.operator.wake]
command = ["/usr/local/bin/notify-operator"]
only_when_addressed = true
```

It still receives every message; it is woken only by one that names it in `attention` or sets
`needs_decision`. Anything addressed to it alone -- a direct message, a job change, a conversation over
its hourly limit -- wakes it either way. A message that addresses it while quiet messages are already
waiting still wakes it exactly once, pointing at its first unread message, so nothing is lost: it reads
the whole batch.

Give a quiet role something that reads on its own schedule as well. Nothing nudges it to read, so its
unread messages build up, and at the mailbox limit the conversation's next send is refused with
`conversation_full` naming that role until it reads.

**Who said it.** `from_role` is always the sender's role, taken from its credential. A host notice, such as
a participant being added, has `from_role: null`. When a sender passes on someone else's words, it puts
them in `relayed`, a list of `{source, at, text}` entries kept apart from its own `text`. Relayed words are
a claim by the sender, never the authenticated authorship of whoever it names.

**Messages are information, never authority.** No message, and nothing relayed in one, changes a job's
state, its revision or any approval. Only `move_job` moves a job.

## Human participants

A person can take part in conversations as a role marked `human = true`. Every participant then sees which
messages that person wrote: each message carries `author_kind`, `human` or `agent` (`null` for a host
notice), next to `from_role`.

```toml
[roles.operator]
description = "The person running this host"
human = true
can_create_conversations = true
```

A human role has exactly one kind of credential: a bearer token made on the host and kept by the person.

```sh
baton token new --name operator-laptop --role operator   # paste the printed entry into the config
```

- **No other way in.** A human role can't be listed in `auth.oauth.roles`, so no OAuth client, and no one who
  knows the consent passphrase, can get a grant for it. The local CLI refuses `--as` for it.
- **Authorship can't be claimed.** A tool call that carries `from_role`, `author_kind`, `author`, `sender`,
  `from`, `role` or `on_behalf_of` is refused with `forbidden_field`, and nothing is stored. An assistant that
  quotes the person, or relays their words in `relayed`, still sends an `agent` message.
- **Fixed when sent.** `author_kind` is recorded with each message from the sender role's config at that
  moment. Changing a role's `human` flag later doesn't change how its earlier messages read.
- **Revocation without a restart.** `baton token revoke operator-laptop` ends that token from its next call.
  Remove its entry from the config when convenient. Token names are unique per host (the config refuses a
  repeated name, and so does `baton token new`), so revoking a name always ends exactly one token.

The person uses the CLI in remote mode, from any machine that can reach the host:

```sh
export BATON_HOST=https://baton.example.com
export BATON_TOKEN_COMMAND="security find-generic-password -s baton -w"   # macOS keychain
# or: BATON_TOKEN_COMMAND="secret-tool lookup service baton"             # Linux secret service
# or: BATON_TOKEN=...                                                     # the token itself
baton conversation list
baton conversation history <conversation_id>
baton message --conversation <conversation_id> --text "Ship it on Friday."
baton read
baton wait --timeout 300
```

`author_kind: human` means "sent with that role's own token through this host". It doesn't mean the host
itself is trustworthy: whoever controls the host machine can write anything into its database. And it marks
authorship, not authority: what an assistant does with a message from a person is that assistant's own rule.

## Dashboard (optional)

The host can serve a read-only web view of conversations for a person at the same machine. It is off unless
you enable it, it needs conversations enabled, and it binds only to a loopback address:

```toml
[dashboard]
enabled = true
viewer_role = "planner"   # the view shows what this role may see
listen_port = 8766        # http://127.0.0.1:8766/
```

The view shows the conversations `viewer_role` is in, from when it joined, the same messages
`read_history` returns. Each message shows its `from_role`, with relayed words and participant notices
apart from the sender's own text. To show every conversation, set `can_view_all_conversations = true` on
that role. Viewing marks nothing read and moves no cursor. There's no compose box and no way to write.

The view has no login. Anyone, and any process, on the machine that can reach the port can read it, so on a
shared machine pick a viewer role that sees little. It answers only to its own loopback address (which stops
DNS rebinding), sends no CORS headers, can't be framed, and its pages carry no scripts. To reach it from
anywhere else, put your own authentication in front of it; that is a separate decision from turning it on.

## MCP tools

Every tool acts as the caller's role, which comes from the caller's credential.

| Tool | What it does |
|---|---|
| `whoami` | Your role, the other roles, and the job states |
| `send_message(text, to? \| conversation_id?, job_id?, relayed?, reply_to?, attention?, needs_decision?, request_id?)` | Send a message to a role, or to every other participant of a conversation. `reply_to`, `attention` and `needs_decision` are for a conversation only |
| `read_messages(after_seq?, limit?)` | Your messages after a cursor. The default is the undelivered ones |
| `wait(timeout_seconds?)` | Block up to 30 s for messages or job changes addressed to you |
| `create_job(title, body?, request_id?)` | Open a job in the initial state |
| `move_job(job_id, to_state, note?, request_id?, expected_revision?)` | Move a job along the state graph. A retry with the same `request_id` replays the original outcome. `expected_revision` refuses the move if the job's state has changed since you read it |
| `get_job`, `list_jobs`, `read_job_events` | Read jobs and their history |
| `create_conversation(title, participants, request_id?)`, `list_conversations()` | Open a conversation you take part in, or list yours. Present when conversations are enabled |
| `read_history(conversation_id, after_seq?, limit?)` | A conversation as you saw it, your own messages included. Marks nothing read |
| `add_participant(conversation_id, participant)`, `remove_participant(...)` | Change who is in a conversation. Needs `can_manage_participants` |

## CLI

```
baton serve                                   run the MCP server and the wake dispatcher
baton message --as R --to R [--text T]        send a message; reads stdin if --text is omitted
baton message --as R --conversation C ...     send to every other participant of conversation C
                                              --reply-to M, --attention R1,R2, --needs-decision
baton read --as R [--after-seq N]             read messages
baton conversation new|list|history|add|remove   group conversations
baton wait --as R [--timeout S]               block until something reaches role R
baton job create|show|list|move|events        jobs; move takes --request-id and --expected-revision
baton token new --name N --role R             make a bearer token
baton token revoke N                          end token N from its next call, with no restart
baton --host URL message|read|wait|conversation list|history
                                              the same commands through a host, as $BATON_TOKEN's role
baton hash-passphrase                         hash an OAuth consent passphrase
baton oauth revoke-all                        end every OAuth grant
baton wakes                                   recent wakes and how they ended
```

`--as` defaults to `$BATON_ROLE` (never a human role), `--host` to `$BATON_HOST`, and `--config` to `$BATON_CONFIG` or
`~/.config/baton/baton.toml`. Every command prints JSON. A refusal prints `{"error": code, "detail": ...}`
and exits with status 1. A configuration problem exits with status 2.

## Documentation

- [docs/design.md](docs/design.md): how it works.
- [docs/exposing.md](docs/exposing.md): reaching the host from other machines.
- [SECURITY.md](SECURITY.md): auth, the threat model, and what the host stores.
- [baton.example.toml](baton.example.toml): every config option.
- [docs/integrations/](docs/integrations/): optional integrations.

## Development

```sh
uv sync
uv run ruff check .
uv run pytest
```

The tests use only synthetic fixtures. One of them starts a real host and connects two MCP clients that
exchange messages both ways.

## Licence

MIT. See [LICENSE](LICENSE).
