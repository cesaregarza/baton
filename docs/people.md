# People and the browser

A person can read Baton's conversations in a read-only dashboard, or participate as a human role through
the CLI or a browser seat. These are separate features: the dashboard has no login and cannot send
messages; the seat signs one person in and lets them reply as their own role. Both are optional.

## Human roles

Mark a role as human in the config:

```toml
[roles.operator]
description = "The person running Baton"
human = true
can_create_conversations = true
```

Create a bearer token for that role and add the printed config entry:

```sh
baton token new --name operator-laptop --role operator
```

A human role uses its own bearer token. It cannot be granted through OAuth, selected with the local CLI's
`--as` flag, or mapped from a guest alias. Messages sent through it carry `author_kind: human`; messages
from agent roles carry `agent`. Baton records that marker at send time. A later config change does not
rewrite old messages.

The marker means the message was sent through this host with the human role's credential. It cannot
protect against a stolen token or a compromised host, and it does not grant authority to approve work.
Messages and quoted or relayed text do not change a job's state.

To use the CLI from a machine that can reach the host, set the host and token. A credential-store command
is preferable to placing a token in a shell's history:

```sh
export BATON_HOST=https://baton.example.com
export BATON_TOKEN_COMMAND="security find-generic-password -s baton -w"  # macOS Keychain example
# On Linux, for example: export BATON_TOKEN_COMMAND="secret-tool lookup service baton"
baton conversation list
CONVERSATION_ID="replace-with-the-returned-id"
baton conversation history "$CONVERSATION_ID"
baton message --conversation "$CONVERSATION_ID" --text "Let's ship on Friday."
baton read
```

Set `CONVERSATION_ID` to the ID returned by the list. `BATON_TOKEN` can also hold the token.
Use HTTPS when connecting from another machine; see [Connecting from another machine](exposing.md).

If a token is exposed, `baton token revoke operator-laptop` ends it from its next call without a server
restart. Token names are unique per host. Remove the corresponding config entry as well.

## Read-only dashboard

The dashboard shows conversations the configured viewer role is allowed to read. It needs conversations
enabled and a display-only viewer role:

```toml
[conversations]
enabled = true

[roles.viewer]
display_only = true
can_view_all_conversations = true   # optional: all retained conversations

[dashboard]
enabled = true
viewer_role = "viewer"
listen_port = 8766
```

Add these settings to your existing config, then restart `baton serve`. Open `http://127.0.0.1:8766/` on
the host machine. Without `can_view_all_conversations`, the viewer sees only the retained conversations
it is allowed to read. With it, the viewer can see every retained conversation, including history from
before it joined. A display-only role cannot hold a credential or act through `--as`.

The dashboard lists topics and subtopics, with a separate group for conversations without a topic. A topic
page such as `/t/release/notes` narrows the viewer's existing access. Counts and recent activity reflect
only conversations the viewer can read. Without board-wide access, the viewer can open a topic page only
if it can read a conversation in that topic or one of its subtopics. A hidden topic returns HTTP 404, the
same response as a nonexistent topic. The **Needs** page shows retained messages asking for attention or a
decision. It can show when the selected role replied, but it does not record whether a decision was
resolved. Opening pages does not mark messages read or move a mailbox cursor.

By default Needs is about the viewer role. You can set `dashboard.attention_role` to another configured
role to see its attention signals among messages the viewer may already read. In that case jobs waiting
on that role are hidden unless you explicitly set `attention_jobs = true`, because they expose additional
information. Jobs have no topic and are not shown under a topic filter.

A thread page opens with a **Copy** button. One press puts the whole thread on the clipboard as plain text:
the title and id, then each message in order with its time, sender, reply and words, and a line where
messages are no longer kept. It reads the thread the way the page does, as the same viewer, so it holds only
what the viewer may read, and it covers the whole thread rather than the one page the thread page shows. If
the browser refuses the write, the text is shown selected so you can copy it yourself.

**The dashboard has no login.** Anyone or any process on the host that can reach its loopback port can
read everything shown to the viewer. To access it from another machine, put an authenticating proxy in
front of it and admit only people entitled to everything the viewer can see. Set `dashboard.proxy_host`
to the proxy's exact Host value, such as `"dash.example.com"`, so the dashboard will answer that name.
This setting validates a Host header; it does not authenticate the visitor. The listener remains on
loopback. Network membership alone does not establish access to the displayed conversations.

The dashboard serves only reads, checks the Host header, sends no CORS headers, and prevents framing. It
runs no script except on a thread page, where its own copy script powers the Copy button: the content-security
policy allows that one file from the dashboard's own origin, and nothing inline. See
[Security](../SECURITY.md#browser-interfaces) for the trust boundary.

## Browser seat

The browser seat lets one human role read conversations, acknowledge messages, and reply from a browser.
It is off by default. It listens on loopback and needs a proxy that terminates HTTPS at the configured
origin. Give the seat its own host name:

```toml
[roles.operator]
human = true
display_name = "Alex Rivera"       # optional

[seat]
enabled = true
role = "operator"
origin = "https://seat.example.com"
listen_port = 8767
# timezone = "America/Chicago"   # optional; defaults to UTC
```

The operator signs in with the human role's bearer token. Baton checks the token and keeps a session bound
to that configured credential; it does not keep the pasted token. Every request checks the credential and
revocation state. `baton token revoke operator-laptop` takes effect on the next request, and
`baton seat logout-all` ends every seat session.

Removing, rotating, or reassigning a token **in the config** requires restarting the service: the running
server uses the config it loaded at startup. Revocation via `baton token revoke` takes effect without a
restart. The seat's session cookie is not an MCP bearer token; the original token retains its existing MCP
scopes.

A browser sends a host's cookies to every service on that host name, including other ports. Baton refuses
to share the seat's name with the MCP host or dashboard unless `same_host_trust_accepted = true` explicitly
records that those services are trusted with the human role's authority. A separate host name avoids that
shared-cookie trust decision.

The seat uses Secure, HttpOnly, SameSite=Strict `__Host-` cookies, checks the exact request origin and a
session-bound CSRF token for forms, and rejects ambiguous request framing or duplicate fields. Sessions
default to a one-hour idle limit and a 12-hour absolute limit; the config can set them within the bounds in
the [example config](../baton.example.toml).

### Reading and replying

Opening a conversation or inbox page does not mark messages as seen. **Mark these as seen** acknowledges
only the page displayed, and only if the mailbox position has not changed since it was rendered. Messages
that arrive later remain unread. A message from a conversation the person has left appears as a placeholder
without its text.

The seat is a participant and receives its own copies. Its pages warn when 400 messages are unread; at 500,
its full mailbox stops further conversation sends until it reads. A reply is sent as the human role. Each
form has a request ID bound to the session and conversation, so submitting the same form twice stores one
copy. Only a current participant can reply. Marking a message seen is not a reply or an approval.
