# Decision push

An optional integration that tells one role's channel, such as a topic on a push-notification service, that
a decision is waiting for it. It never says what the decision is. It is off unless the config enables it.

```toml
[integrations.decision_push]
enabled = true
role = "operator"                           # whose decisions are pushed
url = "https://push.example.com/decisions"  # where the notice is POSTed
secret_env = "BATON_PUSH_TOKEN"             # optional: the environment variable holding a bearer token
link_base = "https://dash.example.com"      # optional: the dashboard's address, for a link to the item
max_per_hour = 10                           # 1 to 1000
timeout_seconds = 10
```

## What triggers a push

A conversation message the configured role received that carries `needs_decision`. Nothing else pushes:
not attention, not a direct message, not a job change, and not a role's own question. Each message pushes
at most once. A retried send stores nothing new, and a restarted host keeps its place. Turning the
integration on sends nothing for decisions already waiting: `baton serve` takes its starting point, the end
of the role's mailbox, before the server accepts any send, so a decision sent just after startup is still
pushed.

## What a push carries

A plain POST:

```
POST <url>
Title: A decision is waiting in Baton
Click: <link_base>/c/<conversation_id>?after_seq=<seq - 1>#m-<seq>
Authorization: Bearer <token>                 (only with secret_env)
Content-Type: text/plain; charset=utf-8

Waiting for your decision. Open it: <link_base>/c/<conversation_id>?after_seq=<seq - 1>#m-<seq>
```

`Title` and `Click` are headers some push services read as the notification's title and tap target; another
webhook can read the body and ignore them. Without
`link_base` there is no `Click` and no link, and the body reads "Waiting for your decision. Open Baton to
see it." A push never carries the message's words, relayed words, the
conversation's title or who sent it, because a push passes through someone else's servers. The link
names only the conversation's id and the message's number. It opens the dashboard page that starts at
that message, the same link the dashboard's own Needs you page uses, so it works however deep the
conversation is. The dashboard shows it only to someone who can reach it.

## Failures and limits

- **A send never waits on a push.** Pushes run on their own thread beside the server, so a slow or failing
  channel can't delay or fail a send.
- **A failed push is not retried.** A timeout, an error status or a refused connection is logged as
  `decision push failed` or `decision push refused: HTTP <status>`, without the URL, the token or any
  content, and counted. The decision still shows on the dashboard's Needs you page.
- **At most `max_per_hour` pushes in any hour.** What the cap holds back is counted, not sent later.
- The URL and the token come only from the config and the environment. Neither is ever logged. The
  config refuses, when it loads, a `url` or `link_base` that names no host, has a port that isn't 1 to
  65535, has a malformed IPv6 address or carries credentials, and a token written into the config itself.
- A push lost to a crash or shutdown between claiming a decision and sending it is not sent later: pushes
  are at most once.
- Its state, `decision_push.json` in the data directory, holds only a position in the mailbox, the
  times of recent pushes and three counts.
