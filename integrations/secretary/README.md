# Baton secretary integrations

These project skills explain how a local secretary instance can deliver decision briefs into Codex or Claude Code. Choose one product skill and install that folder into the project using the path specified in its `SKILL.md`.

- [Codex skill](codex/SKILL.md)
- [Claude Code skill](claude-code/SKILL.md)

## Requirements and limits

- Install and run the worker on the same host and as the same user as the local Baton data and model authentication. Remote delivery is unsupported; remote authenticated reads of archived summaries are supported.
- The runner uses POSIX file locks (Linux/macOS). The supplied systemd unit is for Linux; use another supervisor on macOS.
- Set integration `poll_seconds` and `budget_seconds` so `2 * budget_seconds + poll_seconds <= 60`; the examples use a 2-second poll and a 28-second budget. Set required `delivery_timeout_seconds` independently of the engine timeout. Delivery runs in one supervised background task while assessment continues; the status heartbeat retains a conservative allowance for its timeout. The engine timeout must be shorter than the assessment budget; install and startup validate this, as does standalone `decide` before work.
- Use a complete secretary v3 policy. Model ID, provider URL, API-key variable, timeout, reasoning effort, output limit, and cache mode are explicit policy values; there is no forced model ID. The independent decision engine is also required and can use JEV or a command adapter.
- Set `summary_bytes` explicitly to 4000 (maximum 6000) for hook output limits.
- Installation binds the activation floor to the current database head. The worker scans bounded, complete pages of visible events, including visible reply parents, with output space reserved for citations. It never truncates a page. Provider outages retry in their original mode. Invalid model content/output and `InboxTooLarge` count toward `max_attempts`; timeouts, rejected requests, and failed commands also count when another page has succeeded under the same policy since that page last failed. Saved results are recovered before invoking either provider again.
- Each poll reserves one full configured budget for a fresh alert and one for scheduled work. It runs one eligible scheduled lane per poll, alternating retry and digest priority, so retries and digests never receive leftover slivers. Held contiguous pages are combined into one bounded digest at `digest_seconds` intervals. Digest failures remain digests and eventually produce a keep notice, never an alert or wake.
- Run only one worker per instance. The generated user unit uses `Restart=on-failure`; standalone `run` may be managed by another supervisor.
- systemd user-service enablement alone does not promise operation after logout or reboot. That depends on user-manager linger and host configuration.

The decision engine evaluates every alert page independently. A `wake` verdict with `wake_probability` at or above
`wake_threshold` forces a wake even if model triage holds, keeps, or fails. JEV thresholds use `probabilities["wake"]`;
the separate wire `confidence` is validated but not used as the threshold. The command-engine protocol is version 3
and requires exactly `{"decision":"wake","wake_probability":0.91}`. The model may call a tool to retrieve the
cached decision and wake probability; the engine cannot explain its choice. If model output is unavailable for a
forced wake, the host receives a forced-wake notice. A model outage without a forced wake remains an outage, not
an alert. Sanitized engine, model, and delivery failures are logged and retained separately in health/status.
The worker checks missing model credentials at startup and distinguishes authentication, request rejection,
timeout, and provider unavailability. A component recovers after a successful attempt, retaining failure history.
If a missing model credential is supplied before restart, that current failure clears to `not_used` without
claiming the provider has accepted the key. Connection/pool timeouts remain outages; read/write timeouts and
HTTP 408/504 responses can count toward splitting only after another page has answered successfully.

Every summary, including a digest, accounts for and cites all events in its page; visible reply parents are
optional citations. Validation checks source references, not the truth of summary claims. The prompt forbids
invented assignments or approvals. Pending receipts retain every decision ID, surface wakes first, and announce
new IDs even if older ones remain unacknowledged. Later holds never clear pending wakes. Acknowledgement is
idempotent by summary ID, and delivered ranges merge only when contiguous.

Pending wake IDs remain queued across restarts and later holds never clear them. Codex receives a request to
retrieve summaries; Claude Code's hook presents new IDs to the bound session. Queueing or hook completion does
not acknowledge a summary. Read it and acknowledge its exact decision ID. Acknowledgement is idempotent, and
delivery ranges merge only when contiguous, so acknowledging a later wake cannot skip an earlier held gap.

## Create an instance

From the repository environment where `baton` is importable, use absolute paths:

Create the version 3 policy using the complete [policy example](../../baton/agents/secretary/README.md#policy).
Set `principal` to the actual Baton role and choose stable `secretary_id` and explicit provider settings. The
policy is the same whether the principal runs in Codex or Claude Code.

```sh
python -m baton.integrations.secretary install \
  --product codex \
  --destination /absolute/private/secretary-instance \
  --project /absolute/path/to/project \
  --config /absolute/path/to/baton.toml \
  --policy /absolute/path/to/secretary-policy-v3.json \
  --poll-seconds 2 \
  --budget-seconds 28 \
  --delivery-timeout-seconds 8 \
  --apply-hooks
```

Use `--product claude-code` for Claude Code; `--codex` applies only to Codex. `--apply-hooks` merges the integration hooks into the project's `.codex/hooks.json` or `.claude/settings.local.json`, creates a backup, and is idempotent. It does not edit global settings. The installer writes a private `integration.json`, `hooks.json` fragment, and systemd user-unit file; it does not enable or start the service.

The destination must be empty. To reapply hooks for an existing instance, use
`python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json apply-hooks`.
Policy guidance remains static configuration; principal feedback is [planned](../../docs/roadmap.md).

## Start and inspect

Link and start the generated unit, substituting its exact generated name:

Supply credentials to the service separately: a user service does not inherit your interactive shell's
environment. The generated unit optionally reads `environment` in the private instance directory. Create that
file with mode 0600 using your usual secret provisioning method. Set the variables named by the model and JEV
`api_key_env` fields there. For command providers, set credentials there and list their names in that provider's
required `env` allowlist. Command processes run as the same Baton host user; environment filtering does not
sandbox access to that user's files. The installer copies no credentials. Restart the service after changing policy.

```sh
systemctl --user link /absolute/private/secretary-instance/secretary-XXXXXXXX.service
systemctl --user daemon-reload
systemctl --user enable --now secretary-XXXXXXXX.service
systemctl --user status secretary-XXXXXXXX.service
journalctl --user -u secretary-XXXXXXXX.service -f
```

Or run this command under a host supervisor:

```sh
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json run
```

Runtime operations:

```sh
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json status
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json receive
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json receive --text
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json acknowledge DECISION_ID
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json bind SESSION_ID
baton secretary --policy /absolute/path/to/secretary-policy-v3.json summaries --after 0 --limit 50
```

The worker health receipt reports overall `running`, `degraded`, or `retrying` status and separate `engine`, `model`, and
`delivery` component states. A delivery timeout, unsuccessful Codex exit, launch failure, or missing executable
records a safe failure category and keeps the worker polling. One delivery task runs at a time; failures back off
from `retry_initial_seconds` up to `retry_max_seconds`, and repeated failures in the same category do not repeat
the warning. Shutdown cancels and joins the task, killing and reaping any in-flight command.
Successful delivery clears the degraded delivery state while retaining the latest failure category and timestamp.
If an assessment pass failed, delivery completion preserves its `retrying` status and error. Only a successful
assessment pass clears that error; delivery success does not imply assessment recovered.

The local `summaries` command pages archived deliverable summaries from `baton.db`; it does not require the private
secretary state file. An authenticated remote token can read its own archive without a host-side policy file or
host policy configuration:

```sh
baton --host https://baton.example secretary summaries --after 0 --limit 50
```

The same role-scoped read is available through MCP as `read_secretary_summaries`. Only this archive read supports
`--host`; installation, worker operation, hooks, acknowledgement, and other secretary actions remain host-local.

SessionStart binds the first matching project session. Other sessions are refused unless explicitly rebound. Hooks
and `receive` recheck source visibility. Each hook announces newly pending IDs while retaining unacknowledged
summaries; wakes are surfaced first. JSON `receive` returns all currently authorized pending summaries, each
with a trust label and an `acknowledge_command`. `receive --text` prints complete JSON-framed summaries and
acknowledgment commands when hook output exceeds its context limit.

Hooks JSON-escape Unicode, control, and bidirectional text inside a clearly labeled untrusted-content frame. If
the framed output exceeds the hook limit, the hook gives the `receive --text` command instead. Read the complete
summary and its sources before acknowledging it.

If several sessions share the project, use `bind SESSION_ID` to choose the principal explicitly before
starting the worker. A stopped or closed principal cannot receive a hook: Codex's resume is best-effort, and
Claude's file watcher exists only while its session is running. Pending summaries remain available for the
next session resume or an explicit `receive`. Missing an offer does not acknowledge it; a resumed or compacted
session can receive the same pending summary again until its ID is acknowledged.

For install verification, first use synthetic config and decision data. Run a live model/session check only within the owner’s authorization for inference and notifications. Before relying on hooks, check the installed product's official CLI/version documentation and verify hook registration in its own UI.

## Uninstall

Stop and disable the exact generated user unit, then reload the user manager. Disabling removes its enablement and link symlinks:

```sh
systemctl --user disable --now secretary-XXXXXXXX.service
systemctl --user daemon-reload
```

Remove only this integration's hook entries from the project settings, preserving all unrelated configuration. Retain or remove the private instance directory according to the owner's data-retention choice; do not discard pending decisions implicitly.

## Where the behavior lives

The skills in this directory guide installation. The installed Python runtime is
[`baton/integrations/secretary`](../../baton/integrations/secretary): `install.py` creates artifacts,
`instance.py` owns configuration and session receipts, `worker.py` watches the inbox and requests notifications,
and `hooks.py` delivers checked summaries to the bound session. `__main__.py` exposes these operations.
Assessment prompting, the bounded model/tool harness, independent typed decision engine, and model transports live in
[`baton/agents/secretary`](../../baton/agents/secretary/README.md). Both Codex and Claude Code integrations use
that same assessment path, split across `attention`, `inbox`, `harness`, and `connections` packages. Policy and
installed-instance configuration code is centralized in `baton/config/secretary.py`. The Baton host TOML
format is unchanged. Secretary policy version 3 is required. Private secretary state version 1 is refused and
requires a new file; version 2 is preserved. The append-only `secretary_summaries` archive is in shared `baton.db`
under schema version 22. The installed
`integration.json` is a separate version 1 worker configuration. This runner is started separately; it is not enabled with a
`[integrations.secretary]` Baton server table.
