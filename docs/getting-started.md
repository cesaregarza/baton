# Getting started

This guide starts with two roles on one machine. You'll send a message, open a conversation, and create a
job. Then you can connect an MCP client or put the host behind HTTPS for access from elsewhere.

## Install and configure Baton

You need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). Install from this repository; the
`baton` package on PyPI is unrelated. If you already have a config at the path below, save it before
running `curl`: the command replaces the file.

```sh
uv tool install git+https://github.com/cesaregarza/baton
mkdir -p ~/.config/baton
curl -fsSL https://raw.githubusercontent.com/cesaregarza/baton/main/baton.example.toml \
  -o ~/.config/baton/baton.toml
```

You can choose a different config file with `--config` or `BATON_CONFIG`.

The example config defines two roles, `planner` and `worker`, and a job workflow. A **role** is the name
Baton uses to address a participant and apply its permissions. Both roles can be agents. You can rename or
add roles in the configuration to suit your own work.

Baton creates its data directory when needed. By default, it is `~/.local/share/baton`. It must be private
to the user running Baton; the [security guide](../SECURITY.md#the-data-directory) explains the checks.

## Exchange a message

Run these commands on the machine with the config and data directory:

```sh
baton message --as planner --to worker --text "Please summarise the notes."
baton read --as worker
baton message --as worker --to planner --text "The summary is ready."
baton read --as planner
```

Each command prints JSON. The first read returns the planner's message; the second returns the worker's
reply. You can also wait for new messages or job events:

```sh
baton wait --as worker --timeout 30
```

The local CLI opens the data directory directly, so this example does not need a running server or a
token. The `--as` flag is trusted local access, not a way to authenticate over the network. A person who
can run the CLI as the host's user can act as a local agent role. Human roles use their own tokens instead;
see [People and the browser](people.md).

## Start a conversation

A conversation is a titled thread for two or more roles. To turn it on, edit your config: add
`can_create_conversations = true` to the existing `[roles.planner]` table, and add this new table:

```toml
[conversations]
enabled = true
```

Now create a conversation. The creator must be among its participants:

```sh
baton conversation new --as planner --title "Research notes" --with planner,worker
```

Copy the returned `conversation_id` into the following variable, then send a message and read the thread:

```sh
CONVERSATION_ID="replace-with-the-returned-id"
baton message --as planner --conversation "$CONVERSATION_ID" --text "What did you find?"
baton conversation history --as worker "$CONVERSATION_ID"
```

Reading history does not mark mailbox messages as delivered. `baton read --as worker` still collects the
worker's unread copy. For replies, topics, participant permissions, and notifications, see
[Conversations and notifications](conversations.md).

## Track a job

The example config also defines a job workflow. Create a job and copy its returned `job_id`:

```sh
baton job create --as planner --title "Summarise the notes"
JOB_ID="replace-with-the-returned-id"
baton job move --as worker "$JOB_ID" working
baton job list --waiting-on worker
```

A job moves only along the transitions in the config. In the example, `working` can move to `review` or
`cancelled`; the `review` state waits on `planner`. Messages can discuss a job, but sending a message does
not change its state.

## Connect an MCP client

An MCP client uses a credential to connect to the running host. For a client that can send a bearer token,
create one for its role:

```sh
baton token new --name worker-client --role worker
```

The command prints JSON with a `token` and a `config_entry`. Save the token securely for the client; it is
shown only once. The entry contains a digest of the token, not the token itself. In JSON, the entry's line
breaks appear as `\n`; add them as actual new lines in your config. The result looks like this, with the
digest from your output:

```toml
[[auth.tokens]]
name = "worker-client"
role = "worker"
sha256 = "replace-with-the-printed-digest"
```

Repeat for each client that needs its own credential. Start the server after editing the config:

```sh
baton serve
```

For a client on the same machine, use `http://127.0.0.1:8765/mcp` and configure the header
`Authorization: Bearer <token>`. The client calls tools such as `send_message`, `read_messages`, and
`wait`; see the [MCP reference](reference.md#mcp-tools). The role comes from the credential, so the client
doesn't choose a different sender with `--as`.

Some clients connect with OAuth instead of a manually supplied token. Baton supports optional OAuth; its
settings are in [the example config](../baton.example.toml) and the rules are in
[Security](../SECURITY.md#credentials-and-roles).

For a client on another machine, follow [Connecting from another machine](exposing.md) to set up HTTPS
and `host.public_url` before sharing the address or a credential. Keep tokens and OAuth passphrases out of
messages, logs, and repositories.

## Where to go next

- Add a webhook or command to notify a role when work arrives: [notifications](conversations.md#notifications).
- Let a person read or reply: [People and the browser](people.md).
- Learn about message delivery and retrying writes: [Architecture and design](design.md).
- Browse all supported settings: [baton.example.toml](../baton.example.toml).
