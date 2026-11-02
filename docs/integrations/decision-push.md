# Decision push

Decision push is an optional way to tell someone that a conversation needs their decision. Baton sends a
short notice to an endpoint you configure, such as a push-notification service. The notice does not include
the conversation text. The integration is off until you enable it.

```toml
[integrations.decision_push]
enabled = true
role = "operator"                           # the role whose decision requests should trigger a notice
url = "https://push.example.com/decisions"  # the notification endpoint
secret_env = "BATON_PUSH_TOKEN"             # optional environment variable containing a bearer token
link_base = "https://dash.example.com"      # optional dashboard address for a link to the message
max_per_hour = 10                           # 1–1000
timeout_seconds = 10
```

Use your own endpoint and, if you provide `link_base`, a dashboard address that the recipient can access.
The [dashboard](../people.md#read-only-dashboard) needs its own authentication when accessed remotely.

<a name="what-triggers-a-push"></a>

## When it sends

A new conversation message triggers a push when the configured role receives it and the message has
`needs_decision` set. Attention alone, direct messages, job changes, and the role's own question do not
trigger a push.

Each message is considered at most once. Retrying the same send does not create a new message. The
integration keeps its place across restarts. When first enabled, it starts at the end of the role's mailbox,
so it does not send notices for older decisions already waiting. The starting position is established
before the server accepts new sends.

<a name="what-a-push-carries"></a>

## What the notice contains

The integration sends a plain-text POST. With `link_base` configured, the request has this shape:

```text
POST <url>
Title: A decision is waiting in Baton
Click: <link_base>/c/<conversation_id>?after_seq=<seq - 1>#m-<seq>
Authorization: Bearer <token>                 (only when secret_env is set)
Content-Type: text/plain; charset=utf-8

Waiting for your decision. Open it: <link_base>/c/<conversation_id>?after_seq=<seq - 1>#m-<seq>
```

Some push services use the `Title` and `Click` headers for the notification and tap target. Another
endpoint can use the body and ignore those headers. Without `link_base`, Baton omits `Click` and asks the
recipient to open Baton to see the decision.

The link contains a conversation ID and message sequence and opens the dashboard at that message. The
push does not contain the sender, conversation title, message text, or relayed words. Accessing the linked
page is still subject to the dashboard's access controls.

## Failures and limits

Sending a conversation message does not wait for a push. The integration runs separately, and a failing
notification channel does not fail the original message.

A failed push is **not retried**. A timeout, refused connection, or error response is logged and counted
without the URL, token, or message content. The hourly cap also drops excess pushes rather than delaying
them. A crash or shutdown after a decision is claimed but before it is sent can lose that push. The
original conversation message remains in Baton and may be visible on the dashboard's Needs page.

The URL and optional token come only from config and the named environment variable. Baton refuses a URL
or `link_base` with no host, an invalid port or IPv6 address, or embedded credentials; it also refuses a
token written directly into the config. Neither URL nor token is logged.

The integration stores its mailbox position, recent push times, and three counts in `decision_push.json`
in the data directory. If that file is unreadable or has an unexpected shape, the host continues serving
with decision push disabled for that run. Baton logs the file name, leaves the file untouched, and does not
reset its position. An operator can move it aside and restart to start afresh from the end of the mailbox;
that may leave older decisions without a push.
