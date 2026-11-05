# Baton secretary integrations

These project skills explain how a local secretary instance can deliver decision briefs into Codex or Claude Code. Choose one product skill and install that folder into the project using the path specified in its `SKILL.md`.

- [Codex skill](codex/SKILL.md)
- [Claude Code skill](claude-code/SKILL.md)

## Requirements and limits

- Install and run on the same host and as the same user as the local Baton data and model authentication. Remote mode is unsupported.
- The runner uses POSIX file locks (Linux/macOS). The supplied systemd unit is for Linux; use another supervisor on macOS.
- Use a secretary v2 policy and Baton configuration. The default model uses OpenAI `gpt-6-luna` and `OPENAI_API_KEY`; policy can select another model ID or the host-command transport. An optional decision engine is separately configured as JEV or a command. This integration does not provide a CLI model adapter.
- Set `summary_bytes` explicitly to 4000 (maximum 6000) for hook output limits.
- Installation binds the activation floor to the current database head. The worker scans bounded, complete pages of visible events, including visible reply parents, and never truncates a page. An oversized first event is retained as a failed page while scanning continues past it.
- The worker keeps its durable scan position separate from the contiguous delivered watermark. Hold, keep, and failed pages remain eligible for hourly digest retries while later pages continue to be scanned.
- Run only one worker per instance. The generated user unit uses `Restart=on-failure`; standalone `run` may be managed by another supervisor.
- systemd user-service enablement alone does not promise operation after logout or reboot. That depends on user-manager linger and host configuration.

The worker waits with asyncio between local event checks and asks the model to classify each page. Every summary,
including a digest, must account for and cite every event in its page; it may cite visible parents as well. Source validation checks references,
not the truth of summary claims, and the prompt forbids invented assignments or approvals. Hold, keep, and model
error pages stay available for hourly digest retries while the worker continues to later pages. Model errors back
off from five seconds to five minutes; no deterministic decision replaces failed inference. An operational alert
for a prolonged model outage remains an open product question; inspect worker status and logs for health.

Pending wake IDs remain queued until individually acknowledged. Codex receives a queued request to retrieve a
summary; Claude Code's hook presents the summary to the bound session. Queueing or hook completion does not
acknowledge it. A principal acknowledges only after seeing the summary and its exact decision ID. Pending wakes
survive worker restarts, and later hold/keep results never clear them. Acknowledgement is idempotent by summary ID,
and ranges merge only when contiguous, so acknowledging a later wake cannot skip an earlier held gap.

## Create an instance

From the repository environment where `baton` is importable, use absolute paths:

For example, a policy for an existing `planner` role using the default lightweight OpenAI model is:

```json
{"version": 2, "principal": "planner", "summary_bytes": 4000}
```

Set the principal to the actual Baton role. This model choice is independent of whether the principal runs
in Codex or Claude Code; an existing policy with a command model works in either integration as well.

```sh
python -m baton.integrations.secretary install \
  --product codex \
  --destination /absolute/private/secretary-instance \
  --project /absolute/path/to/project \
  --config /absolute/path/to/baton.toml \
  --policy /absolute/path/to/secretary-policy-v2.json \
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
file with mode 0600 using your usual secret provisioning method. For the default OpenAI model use
`OPENAI_API_KEY=...`; for a JEV decision engine use `TYPESAFE_API_KEY=...` (or the configured `api_key_env`).
Command adapters need their executable and any authentication available in the service environment. The installer
copies no credentials. Restart the service after changing its policy.

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
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json acknowledge DECISION_ID
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json bind SESSION_ID
```

SessionStart binds the first matching project session. Other sessions are refused unless explicitly rebound. A hook or `receive` rechecks source visibility before showing a brief. `receive` can recover pending briefs if a hook was missed. The same unacknowledged summary may be offered again after a session resumes or compacts; offers are not limited to once per session.

Hooks escape the model's summary so its text cannot close the reported-content delimiter. If escaping makes the
output too large, the hook gives a `receive` command instead. Read that complete summary before acknowledging it.

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
Assessment prompting, the bounded model/tool harness, optional typed decision engine, and model transports live in
[`baton/agents/secretary`](../../baton/agents/secretary/README.md). Both Codex and Claude Code integrations use
that same assessment path, split across `attention`, `inbox`, `harness`, and `connections` packages. Policy and
installed-instance configuration code is centralized in `baton/config/secretary.py`. The Baton host TOML
format is unchanged. Secretary policy version 2 is required; deterministic version 1 policies are unsupported,
and the retired state format is incompatible, so use a version 2 policy and a new state file. The installed
`integration.json` is a separate version 1 worker configuration. This runner is started separately; it is not enabled with a
`[integrations.secretary]` Baton server table.
