# Conversations and notifications

A conversation is a titled thread shared by two or more roles. It is useful when several agents—or an
agent and a person—need to follow the same discussion. Baton stores a copy of each message for every
other participant, so they can read and reply at different times.

Conversations are optional. Direct messages and jobs work without them.

## Start a conversation

Enable conversations and give at least one role permission to create them. These are example entries;
add the permissions to your existing role tables rather than defining the same table twice.

```toml
[conversations]
enabled = true
max_participants = 8               # default
max_messages_per_hour = 30          # per conversation; default
escalation_role = "planner"        # optional

[roles.planner]
can_create_conversations = true
can_manage_participants = true

[roles.worker]

[roles.reviewer]

[roles.editor]
```

Create a thread with the CLI. The creating role must be one of the participants.

```sh
baton conversation new --as planner --title "Release plan" --with planner,worker,reviewer
```

Use the returned `conversation_id` in later commands:

```sh
CONVERSATION_ID="replace-with-the-returned-id"
baton message --as planner --conversation "$CONVERSATION_ID" --text "The draft is ready for review."
baton conversation history --as reviewer "$CONVERSATION_ID"
baton conversation add --as planner "$CONVERSATION_ID" editor
```

Adding or removing a participant requires `can_manage_participants`. A role with this permission can
manage participants even in a conversation it has not joined, so grant it deliberately. A conversation
must keep at least two participants. Baton records a host notice when membership changes.

MCP clients use `create_conversation`, `send_message`, `read_history`, `add_participant`, and
`remove_participant`. See the [tool reference](reference.md#mcp-tools) for their arguments.

## Read and reply

A conversation message reaches every other participant. It does not send a copy to the sender. If any
recipient's mailbox is full, Baton stores none of the copies and returns `conversation_full`; retry after
that participant has read its messages.

`read_messages` (or `baton read`) delivers a role's unread mailbox messages and advances its delivery
position. `read_history` (or `baton conversation history`) shows the retained thread without advancing that
position. So reading a thread in history does not clear its unread messages.

A participant can read retained messages from its periods of membership, including messages it sent.
A role with `can_view_all_conversations = true` can read retained conversation history even without joining,
including from before it joined. That permission does not reveal direct messages. History reports retained
sequence gaps within the reader's accessible range as `retention_gaps`; a page limit or history from before
a normal participant joined is not reported as lost history.

To reply to a particular message, pass its `message_id` as `reply_to` on `send_message`, or use
`--reply-to` with the CLI. The parent must be a retained message in the same conversation that the sender
can see. A reader who cannot see the parent receives a reference with a `null` sequence number, without
the parent's author or text. A reply otherwise behaves like any other conversation message.

## Ask for attention

A conversation message can name current participants in `attention`, or set `needs_decision` to signal
that the conversation needs a decision:

```sh
baton message --as planner --conversation "$CONVERSATION_ID" \
  --text "Please review the draft." --attention reviewer --needs-decision
```

These fields draw attention; they do not add participants, impersonate anyone, approve a decision, or move
a job. Baton uses the structured `attention` list, not names typed into the message text. Direct messages
cannot use these conversation-only fields.

`from_role` identifies the role that sent a message. A host notice has no sender role. A sender can include
someone else's words in `relayed`, separate from its own text; those words are the sender's account, not
proof of the named person's authorship. A human role's messages are marked separately, as explained in
[People and the browser](people.md#human-roles).

## Organise conversations with topics

On the current main branch, a topic is an optional label on a conversation. Use one or two lowercase
segments, such as `release` or `release/notes`. Each segment can contain 1–40 lowercase letters, digits,
and internal hyphens.

```sh
baton conversation new --as planner --title "Release plan" --with planner,worker \
  --topic release/notes
baton conversation list --as worker --topic release
baton conversation topic --as planner "$CONVERSATION_ID" --set release/plan
baton conversation topic --as planner "$CONVERSATION_ID" --clear
```

The `release` filter includes `release/notes`, but not `releases`. Filtering only narrows the conversations
a role can already read. Changing or clearing a label requires both membership and
`can_manage_participants`; it does not change the conversation's ID or participants.

Topic containers and guest-created threads are proposed in [PR #35](https://github.com/cesaregarza/baton/pull/35).
They are not part of the main-branch behavior described here.

## Notifications

A recipient can call `read_messages` or wait for work itself. Alternatively, configure a **wake** to tell
it that something is waiting. A wake carries a reason and identifiers, not message text or a job body;
the recipient reads the actual content with its own access.

Choose one wake destination per role: an operator-configured webhook, a local command, or an enabled
integration. For example:

```toml
[roles.worker.wake]
command = ["/usr/local/bin/notify-worker"]
```

The command runs without a shell and receives wake JSON on standard input. A webhook receives a POST;
`secret_env` can name an environment variable containing a Standard Webhooks signing secret. See the
[example config](../baton.example.toml) and [design notes](design.md#notifications-and-waits) for details.
The optional [MCP Events integration](integrations/openai-mcp-events.md) supports signed events for
subscribing clients. Wakes have been exercised end to end with a real client; test your own configuration.

A successful wake means that a webhook accepted it or a command exited successfully. It does not show
that an agent received, read, or acted on the message. When a wake fails, Baton retries with backoff;
`baton wakes` shows recent outcomes. A local agent already blocked in `baton wait` can simply receive its
work without a separate wake destination.

### Reduce interruptions in a busy conversation

Set `only_when_addressed = true` in a role's wake table to wake it for a conversation message only when
`attention` names it or `needs_decision` is set. The role still receives every message. Direct messages,
job changes, and a conversation rate-limit escalation still wake it.

```toml
[roles.worker.wake]
command = ["/usr/local/bin/notify-worker"]
only_when_addressed = true
```

Arrange for a quiet role to read on its own schedule. Otherwise unread messages can accumulate until its
mailbox is full and later conversation sends are refused.

### Reminders and delivery

Baton groups message wakes by the first unread message. A later message that addresses a quiet role still
wakes it for the waiting batch. After a successful message wake, Baton schedules reminders at five and
twenty minutes if that batch remains unread. A read retires unsent reminders. There are at most two
reminders per batch and six per role in a rolling hour; capped reminders wait for the window to move. If
the host was down past both due times, it sends only the second reminder. `baton wakes` identifies them
with `:r1` and `:r2` on the batch key.

A mailbox read advances the position before the response reaches its caller. A lost response, or leaving a
batch partly paged, can therefore cancel reminders without proving that the recipient processed every
message. A caller can recover retained messages by reading from a known sequence number. History reads do
not move the mailbox position or cancel reminders.

## Limits

By default a conversation accepts 30 participant messages per hour and has at most eight participants.
Configure `max_messages_per_hour` and `max_participants` in `[conversations]`. When a conversation exceeds
its message limit, a configured `escalation_role` receives a content-free event at most once per hour for
that conversation. Host notices do not count toward the message limit.

A mailbox can hold 500 unread messages. Baton refuses further sends to a full mailbox instead of silently
dropping them. Read messages are retained for 30 days after they were sent; unread messages are kept until
read.
