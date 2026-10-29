# OpenAI MCP Events integration

An optional integration that wakes a remote MCP client, such as a ChatGPT connector, when something on
the host is addressed to its role. It implements the server side of OpenAI's
[MCP Events](https://developers.openai.com/plugins/build/mcp-events) contract: the client subscribes to an
event over its authenticated MCP session, and the host POSTs a signed webhook to the client's callback
each time it should look.

The code is `baton/integrations/openai_mcp_events.py`, and it ships with baton. The core never imports it
unless the config enables it.

## What it does

1. The client calls `events/list` and sees one event, `baton.attention`.
2. It calls `events/subscribe` with an HTTPS callback URL and a `whsec_` signing secret. The host requires
   the URL to be canonical, checks it against your allowlist, verifies it with a signed challenge, and then
   stores the subscription. The subscription belongs to the role of the credential that made it. The host
   also records that credential's identity, so it can check later that the same credential still holds
   that role.
3. When a message or job change is addressed to that role, the core's wake dispatcher hands the wake to
   the integration. The integration POSTs one event to each live subscription of the role:

   ```json
   {"eventId": "wake_…", "name": "baton.attention", "timestamp": "2026-01-01T00:00:00+00:00",
    "data": {"role": "assistant", "reason": "message", "first_unread_seq": 4}, "cursor": null}
   ```

   The event names what to look at and nothing else. The client then calls `read_messages` or `wait`
   with its own credential. For a job change, `reason` is the event kind (`job.created`, `job.moved`),
   and `data` carries `job_id` and `event_seq`. `timestamp` is when the host first handled the wake, and
   it stays the same on every retry. `webhook-timestamp` is the send time of each attempt.
4. **Outcomes, per destination.** The integration keeps an outcome for each (wake, subscription) pair:
   - **accepted:** the destination answered 2xx;
   - **gone:** it answered `410`, and the subscription becomes inactive;
   - **too large:** it answered `413`.

   Each of these is final for that destination, which is never sent that wake again. A wake counts as
   delivered when every live subscription of the role has a final outcome and at least one accepted it.
   Until then the core's backoff retries it, sending only to the destinations without a final outcome.
   Outcomes and the occurrence time are kept exactly as long as the core wake: they are dropped once the
   core reports the wake delivered, failed or superseded, and never by age, so a restart days later still
   resends only to unfinished destinations, with the original `timestamp`. Before each attempt, the
   subscription is checked again: it must still exist, be active and unexpired, and its credential must
   still hold its role.
5. The core still owns everything else about the wake: one wake per unread batch, supersession by a read,
   leases, backoff, attempts, and `baton wakes`. If no destination ever accepts a wake, the core marks it
   `failed` after its attempts. Nothing is sent anywhere else.

## Setup

Synthetic values throughout. Use your client's real callback location.

```toml
[host]
public_url = "https://baton.example.com"      # must be reachable by the client over HTTPS

[roles.assistant]
wake = { integration = "openai_mcp_events" }

[auth.oauth]
enabled = true
passphrase_hash = "scrypt$<from baton hash-passphrase>"
roles = ["assistant"]
redirect_uris = ["https://client.example.com/oauth/callback"]   # the client's stable callback, exactly

[integrations.openai_mcp_events]
enabled = true
# Callbacks must fall under one of these: same scheme (https), exact host, same port, and the
# prefix's path segments first, compared segment by segment. No wildcards.
callback_prefixes = ["https://callbacks.example.com/"]
# Optional, shown with their defaults:
# default_ttl_seconds = 604800   # 7 days, when the client doesn't ask
# min_ttl_seconds = 300
# max_ttl_seconds = 2592000      # 30 days; a request for "no expiry" gets this
# max_per_role = 5
# max_total = 20
# timeout_seconds = 10           # one absolute deadline for each request: DNS, connect, TLS, every write and read
# rotation_overlap_seconds = 3600  # 60 to 86400: how long a replaced secret still signs, and the least time
#                                  # between two rotations of one subscription
```

Every setting is checked: booleans must be TOML booleans (`true`, not `"true"`), and every number has a
type and a range. Unknown settings are refused.

**Canonical callbacks.** A callback URL is refused unless it is already in canonical form:
- `https`, a lowercase ASCII host, and a port only if it isn't 443;
- a path of non-empty segments, none of them `.` or `..`;
- no `%2e`, `%2f`, `%5c` or `%25` in any case, no backslash, no whitespace;
- no userinfo, query or fragment.

Rebuilding the URL from its parts must give back exactly the input. The string that is checked is the
string that is stored, and it is sent unchanged.

If a subscribe is refused as `not_allowed`, the host log names the callback's scheme and host, and nothing
else. Add a matching prefix only if you trust that host. Restart `baton serve` after changing the config.

The offline tests also use two test-only settings, `allow_private_callbacks` and `ca_file`, for a
loopback receiver with its own certificate. The config parser refuses both, so they can only be set by
test code, through the constructor.

## Security model

| Concern | Defence |
|---|---|
| A client subscribing, unsubscribing or receiving for another role | The role comes only from the credential. Subscription ids are derived from the role, and delivery filters by role. Arguments are refused. |
| The host made to call arbitrary URLs (SSRF) | HTTPS only, canonical URLs only (no `.` or `..` segments, or encoded separators that a server might read differently), under an operator allowlist compared segment by segment. The allowlist is rechecked on every request, and the checked string is the one sent. |
| DNS rebinding to internal addresses | On every request (challenge and delivery), the name is resolved once, every address must be global, and the connection goes only to those addresses. TLS SNI and `Host` keep the name. A name re-pointed at a private, loopback, link-local or metadata address is refused at the next request. |
| Redirects and proxies | Requests go through httpcore, which follows no redirects and reads no proxy settings. A redirect fails the challenge or the delivery. |
| Callback takeover | A signed, single-use challenge must be echoed by a 2xx response (compared in constant time) before anything is stored. A refused subscribe keeps no state. |
| Forged or replayed events at the client | Standard Webhooks signatures over `id.timestamp.body` with the subscription's secret. Each attempt gets a fresh `webhook-timestamp`. When a refresh brings a new secret, events are signed with both secrets for the rotation overlap, then with the new one only. |
| Unbounded key rotation | A subscription keeps at most one previous secret, never a duplicate, so an event carries at most 2 signatures. A second rotation inside one overlap window is refused, and the overlap is at least 60 seconds. |
| Leaking content through a wake | Events carry only `role`, `reason`, and a sequence number or job key. Never message text or job bodies. |
| Unbounded state from public calls | Caps per role and overall, TTL bounds, expired subscriptions dropped, outcomes dropped with their finished core wake, and a 256 KiB cap on event bodies. |
| A slow or hostile callback | One absolute deadline per request. Every blocking step, the connect, the TLS handshake, each write and each read (headers included), gets only what is left of it, so a trickled or stalled response can't stretch it. DNS runs on a fixed pool of 4 resolver threads with a queue of 4: a lookup that times out keeps its slot until it returns, no thread is ever added, and a full pool fails at once as unreachable. `Accept-Encoding: identity` is sent, and any other `Content-Encoding` fails without decoding. Each read asks the socket for no more than what is left of a 64 KiB cap on the whole response, headers included, so nothing past it is read. |
| Revoked access | Each delivery attempt needs the subscribing credential to still exist with the same role: the same bearer token (name, digest, role), or the same OAuth grant, still unexpired, for the same resource, and for a role OAuth may still grant. A normal refresh keeps the grant. Replacing the token, `baton oauth revoke-all`, or removing the role from `auth.oauth.roles` ends delivery, as do expiry, `events/unsubscribe` and a `410`. |
| Secret exposure | Secrets live only in `<data_dir>/openai-mcp-events.json` (`0600`, checked like every file in the data directory). They are never logged, never written to the audit log or a wake, and never returned. |

Remaining risk:

- An allowlisted host that you trust can still receive the content-free events for its role.
- The challenge proves control of the callback, not who operates it. Keep the allowlist to hosts you
  intend.

## The contract: what was verified, and what is pending

**Source:** OpenAI, "MCP Events", `https://developers.openai.com/plugins/build/mcp-events`, read on
2026-10-27. The page carries no version or date other than the MCP protocol version `2026-07-28`.

Verified against that page, and covered by the offline tests:

- `events: {}` in `server/discover` capabilities; `events/list`, `events/subscribe` and `events/unsubscribe`
  on the same authenticated endpoint.
- The event-definition fields: `name`, `description`, `delivery: ["webhook"]`, `inputSchema` and
  `payloadSchema`.
- Subscribe:
  - the params are `name`, `arguments`, `delivery {mode, url, secret}`, `cursor` and `ttlMs`;
  - the result is `id`, `refreshBefore`, `cursor: null` and `truncated: false`;
  - the id is deterministic, from principal, URL, name and canonical arguments;
  - a repeat subscribe refreshes.
- The secret format: `whsec_` and 24–64 decoded bytes. HTTPS callbacks.
- Verification:
  - the body is `type: "verification"` plus a fresh challenge;
  - it is signed, with `X-MCP-Subscription-Id`;
  - a 2xx response must echo the challenge, compared in constant time;
  - failure is JSON-RPC `-32015` with `data.reason`.
- Delivery:
  - the body fields are `eventId`, `name`, `timestamp`, `data` and `cursor`, with no top-level `type`;
  - the headers are `webhook-id` (= `eventId`), `webhook-timestamp`, `webhook-signature` and
    `X-MCP-Subscription-Id`;
  - Standard Webhooks signing over the exact bytes sent;
  - bodies are at most 256 KiB;
  - 2xx acknowledges, and `410` and `413` are not retried;
  - `timestamp` is the occurrence time, stable across retries;
  - during a secret rotation, both secrets sign, as space-separated signatures;
  - a 10-second timeout.
- The security rules: resolve and validate at connection time, connect to the validated address while
  keeping the name for TLS, block non-public addresses, follow no redirects, apply all of this to
  verification and delivery alike, and stop delivery on expiry or revoked access.
- State is kept across restarts.

Choices the page leaves to the server, made here:

- `ttlMs: null` ("no expiration") is granted the maximum TTL, so `refreshBefore` is never null.
- The page gives no default TTL. Here it is 7 days, bounded to 5 minutes – 30 days.
- **A deliberate deviation from the contract:** every subscribe is verified with a fresh challenge. The page
  says a successful verification may be cached for a bounded period, by principal and callback URL. This
  integration never caches one, so a refresh costs one more signed request to the callback.
- Retries use the core's backoff (1 minute doubling to 1 hour, 8 attempts), and go only to destinations
  without a final outcome. Each attempt keeps `eventId` and gets a fresh `webhook-timestamp` and
  signature.
- The page asks for "a short rotation window" without a length. Here it is one hour
  (`rotation_overlap_seconds`, from 60 seconds to a day), and a subscription may rotate at most once per
  window. A challenge is signed with the new secret only.
- The occurrence time is when the host first handled the wake, usually within a second of the message or
  job change.
- The `-32015` reasons used are `invalid_url`, `not_allowed`, `private_address`, `unreachable`,
  `timeout` and `challenge_failed`. The page names `challenge_failed` and `timeout` as examples.

**Pending: not verified. It needs a live acceptance test with a real client, which hasn't been run.**

- That a real ChatGPT connector discovers, subscribes, passes the challenge, accepts the signed events and
  refreshes before `refreshBefore`, end to end against this host.
- The callback host and path a real client uses, so the right `callback_prefixes` can be set.
- Whether the client sends `cursor` values, or rotates secrets on refresh, in practice. This integration
  accepts and ignores `cursor`, and handles a rotation as described above.

Until then, treat compatibility as unconfirmed. Only the offline tests have exercised this code.
