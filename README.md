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

- The assistant connects to `https://baton.example.com/mcp`, signs in on the host's consent page, and
  subscribes to the `baton.attention` event. A message to `assistant` then sends it one signed,
  content-free event, and it calls `read_messages`.
- The orchestrator sends with `baton message --as orchestrator --to assistant ...` and waits with
  `baton wait --as orchestrator`, which returns when the assistant replies or moves a job.

The integration implements the parts of OpenAI's published MCP Events contract listed in its doc, and
has been tested offline only. Compatibility with a real client is unconfirmed until a live test runs.
[docs/integrations/openai-mcp-events.md](docs/integrations/openai-mcp-events.md) covers setup, the
security model, what was verified, and what is still pending.

## MCP tools

Every tool acts as the caller's role, which comes from the caller's credential.

| Tool | What it does |
|---|---|
| `whoami` | Your role, the other roles, and the job states |
| `send_message(to, text, job_id?, request_id?)` | Send a message to a role |
| `read_messages(after_seq?, limit?)` | Your messages after a cursor. The default is the undelivered ones |
| `wait(timeout_seconds?)` | Block up to 30 s for messages or job changes addressed to you |
| `create_job(title, body?, request_id?)` | Open a job in the initial state |
| `move_job(job_id, to_state, note?, request_id?, expected_revision?)` | Move a job along the state graph. A retry with the same `request_id` replays the original outcome. `expected_revision` refuses the move if the job's state has changed since you read it |
| `get_job`, `list_jobs`, `read_job_events` | Read jobs and their history |

## CLI

```
baton serve                                   run the MCP server and the wake dispatcher
baton message --as R --to R [--text T]        send a message; reads stdin if --text is omitted
baton read --as R [--after-seq N]             read messages
baton wait --as R [--timeout S]               block until something reaches role R
baton job create|show|list|move|events        jobs; move takes --request-id and --expected-revision
baton token new --name N --role R             make a bearer token
baton hash-passphrase                         hash an OAuth consent passphrase
baton oauth revoke-all                        end every OAuth grant
baton wakes                                   recent wakes and how they ended
```

`--as` defaults to `$BATON_ROLE`, and `--config` defaults to `$BATON_CONFIG` or
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
