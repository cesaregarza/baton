# OpenAI MCP Events integration

This optional integration can notify an MCP client when a message or job change is waiting for its Baton
role. The client subscribes through MCP; Baton then sends signed, content-free events to the client's HTTPS
callback. The client still needs to read the message or job with its own credential.

The integration implements the server side of OpenAI's
[MCP Events contract](https://developers.openai.com/plugins/build/mcp-events). It has delivered wakes to a
real client end to end, including through an enrolled alias on a shared guest login, and is also covered by
offline tests. A callback accepting an event does not by itself show that the client read or acted on the
message. Test your own client and configuration: behavior can differ between deployments.

It is included in `baton/integrations/openai_mcp_events.py` and loaded only when enabled in the config.

<a name="setup"></a>

## Set it up

The following values are examples. Use the actual callback prefix and exact OAuth redirect URI documented
for your client, and expose the host over HTTPS as described in
[Connecting from another machine](../exposing.md).

```toml
[host]
public_url = "https://baton.example.com"

[roles.assistant]
wake = { integration = "openai_mcp_events" }

[auth.oauth]
enabled = true
passphrase_hash = "scrypt$<from baton hash-passphrase>"
roles = ["assistant"]
redirect_uris = ["https://client.example.com/oauth/callback"]

[integrations.openai_mcp_events]
enabled = true
callback_prefixes = ["https://callbacks.example.com/"]
# default_ttl_seconds = 604800      # 7 days
# min_ttl_seconds = 300             # 5 minutes
# max_ttl_seconds = 2592000         # 30 days
# max_per_role = 5
# max_total = 20
# timeout_seconds = 10
# rotation_overlap_seconds = 3600   # 60–86400; also the minimum time between rotations
```

The callback prefix is an allowlist: a callback must use HTTPS and the same host and port, and begin with
the prefix's path segments. There are no wildcards. It is separate from the OAuth redirect URI, which must
match its allowlist exactly. Restart `baton serve` after changing the config. Unknown settings, wrong types,
and values outside the allowed ranges are refused.

A callback must already be canonical: a lowercase ASCII host, no port for 443, and no empty, `.` or `..`
path segments. Userinfo, query strings, fragments, whitespace, backslashes, and encoded dot, slash,
backslash, or percent characters (`%2e`, `%2f`, `%5c`, `%25`, in any case) are refused. Baton stores and
sends the same URL it validated. A `not_allowed` refusal logs the callback's scheme and host, but not its
path. Add a prefix only if you trust the host.

Two constructor settings used by offline tests, `allow_private_callbacks` and `ca_file`, cannot be set in
the config parser.

<a name="what-it-does"></a>

## How a notification arrives

1. An authenticated client calls `events/list` and finds `baton.attention`.
2. The client calls `events/subscribe` with an HTTPS callback and a `whsec_` signing secret. Baton checks
   the callback, sends a signed challenge, and stores the subscription only after a successful response.
   The subscription is tied to the subscribing credential and role.
3. When a wake is due for that role, Baton POSTs an event to each live subscription:

   ```json
   {"eventId":"wake_…","name":"baton.attention","timestamp":"2026-01-01T00:00:00+00:00",
    "data":{"role":"assistant","reason":"message","first_unread_seq":4},"cursor":null}
   ```

4. The client checks the signature and reads the waiting work. For a job change, `reason` is `job.created`
   or `job.moved` and the data includes `job_id` and `event_seq` instead. No message text or job body is
   included.

The event's `timestamp` is when Baton first handled the wake and stays stable across retries. The
`webhook-timestamp` header is the send time of each attempt.

### Delivery outcomes

Baton keeps an outcome for each wake and subscription. A `2xx` response is accepted; `410` marks the
subscription inactive; and `413` is a final too-large outcome. Those destinations are not sent that wake
again. The core considers the wake delivered when every live destination has a final outcome and at least
one accepted it. Otherwise, the core retries only destinations without a final outcome and eventually
marks the wake failed if none accepts it.

Per-destination outcomes and the original occurrence time remain until the core wake is delivered, failed,
or superseded. This preserves retry behavior across a restart. Before each attempt, Baton checks that the
subscription is still active and unexpired and that its credential still holds the role. The core also
handles wake coalescing, leases, backoff, and supersession by a read. `baton wakes` shows wake outcomes.

`baton events status` shows subscription IDs, owners, expiry and state, plus recent delivery receipts
(three days, at most 500). Receipts outlive a wake's per-destination outcomes and can help match a
server-side acceptance with an event the client actually received. The command does not print callback
URLs, headers, tokens, or secrets.

## Shared guest logins

An alias enrolled as an agent role lets a shared `guest` login act as that role; see
[Guest aliases](../aliases.md#enroll-an-alias-as-a-role). Event subscriptions on a shared login need a
separate binding:

1. The guest subscribes over its OAuth grant with no arguments. The subscription belongs to that grant.
   A static guest bearer token has no grant and cannot subscribe.
2. Call `bind_events(token=<binding_token>)` with an alias that is currently mapped to a role. This sets
   the grant's one event target. Subscribing and binding can happen in either order.
3. A wake for that role goes to its own subscriptions and to live guest-grant subscriptions bound to the
   alias currently mapped to it. Baton rechecks the mapping and grant on every delivery.
4. To change targets, first call `unbind_events` with the bound alias's token. Binding the same alias twice
   has no effect; binding a different one while a target exists is refused.

`baton alias map` and `unmap` drop affected event targets; a rekey keeps them. The owner can use
`baton events unbind <grant>` to recover when an alias token is lost. Expired grant targets are swept as
cleanup. Each grant has at most one event target, and its event payload is unchanged.

<a name="security-model"></a>

## Security

The callback allowlist limits which hosts may receive events, but an allowed host can receive the
content-free event for its role. A signed challenge proves control of a callback, not who operates it.
Keep the allowlist narrow.

Baton applies these checks on both challenge and delivery:

- It resolves the callback host once per request and requires every result to be globally routable. It
  connects only to a validated address, while retaining the host name for TLS and the `Host` header.
- It follows no redirects and does not use proxy settings for these outbound requests.
- It uses one absolute deadline for DNS, connect, TLS, writes, and reads. DNS uses a fixed pool of four
  workers and a queue of four; a timed-out lookup keeps its slot until it returns.
- It requests identity encoding and limits response reads, including headers, to 64 KiB. Challenge responses
  must be unencoded; for event delivery, Baton uses the response status and does not read or decode the
  response body. Event bodies are limited to 256 KiB.
- It signs events using Standard Webhooks over the exact transmitted bytes. During secret rotation, the
  old and new secrets both sign for the configured overlap. A subscription keeps at most one previous
  secret and cannot rotate again during that overlap.

Subscriptions are bounded per role (per grant for guests) and overall, have bounded lifetimes, and stop
when their credential no longer has access. Replacing a bearer token, revoking OAuth grants, removing a
role from `auth.oauth.roles`, expiry, `events/unsubscribe`, and a `410` all end delivery as applicable.
Signing secrets are stored only in the integration's `0600` data-directory state file; they are not logged,
audited, returned, or placed in wakes.

<a name="the-contract-what-was-verified-and-what-is-pending"></a>

## Contract and verification

The offline tests cover the implementation against OpenAI's published
[MCP Events documentation](https://developers.openai.com/plugins/build/mcp-events), as read on 2026-10-27,
including:

- Discovery with `events: {}` and `events/list`, `events/subscribe`, and `events/unsubscribe` on the
  authenticated endpoint. Event definitions provide `name`, `description`, `delivery`, `inputSchema`, and
  `payloadSchema`.
- Subscribe parameters `name`, `arguments`, `delivery {mode, url, secret}`, `cursor`, and `ttlMs`; a result
  with `id`, `refreshBefore`, `cursor: null`, and `truncated: false`; deterministic subscription IDs; and
  repeat subscribe as refresh. Secrets use `whsec_` and decode to 24–64 bytes.
- A signed verification request with `X-MCP-Subscription-Id` and a body containing
  `type: "verification"` and a fresh challenge. A `2xx` response must echo the challenge, which Baton
  compares in constant time; failures use JSON-RPC `-32015` and a `data.reason`.
- Delivery fields `eventId`, `name`, `timestamp`, `data`, and `cursor`, without a top-level `type`;
  headers `webhook-id`, `webhook-timestamp`, `webhook-signature`, and `X-MCP-Subscription-Id`; and
  Standard Webhooks signatures. `2xx` acknowledges; `410` and `413` are final for that destination.
- Callback validation, global-address checks, no redirects, revocation checks, and state across restarts.

Baton's implementation choices include:

- A default subscription lifetime of seven days, bounded to five minutes through 30 days. A request for
  no expiration receives the maximum lifetime, so `refreshBefore` is not null.
- A fresh challenge on every subscribe, including refresh; successful challenges are not cached.
- Core wake retries from one minute up to one hour, with eight attempts. The event ID remains stable and
  each attempt gets a fresh webhook timestamp and signature.
- A one-hour default secret-rotation overlap, configurable from 60 seconds to one day. A challenge is
  signed only with the new secret.
- `-32015` reasons `invalid_url`, `not_allowed`, `private_address`, `unreachable`, `timeout`, and
  `challenge_failed`. The occurrence time is when the host first handles the wake.

Real-client wake delivery has been exercised end to end, including a shared guest login bound to an enrolled
alias. Check the callback and OAuth settings, subscription refresh, and delivery with your own client;
those details can vary by deployment. Baton accepts and ignores a supplied cursor.
