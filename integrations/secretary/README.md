# Baton secretary integrations

These project skills explain how a local secretary instance can deliver decision briefs into Codex or Claude Code. Choose one product skill and install that folder into the project using the path specified in its `SKILL.md`.

- [Codex skill](codex/SKILL.md)
- [Claude Code skill](claude-code/SKILL.md)

## Requirements and limits

- Install and run on the same host and as the same user as the local Baton data and model authentication. Remote mode is unsupported.
- The runner uses POSIX file locks (Linux/macOS). The supplied systemd unit is for Linux; use another supervisor on macOS.
- Use an existing secretary v2 policy and Baton configuration. The current default policy uses OpenAI `gpt-6-luna` and `OPENAI_API_KEY`; an existing external v2 policy may use its configured provider adapter. This integration does not provide a CLI model adapter.
- Set `summary_bytes` explicitly to 4000 (maximum 6000) for hook output limits.
- The worker polls the read-only visible-event feed every two seconds, performs inference at startup or when visible events arrive, and does not infer on empty timer polls.
- Run only one worker per instance. The generated user unit uses `Restart=on-failure`; standalone `run` may be managed by another supervisor.
- systemd user-service enablement alone does not promise operation after logout or reboot. That depends on user-manager linger and host configuration.

The worker waits with asyncio between local event checks. A new visible event is its alert: it asks the
secretary model to choose wake, hold, or keep across the pending window. A wake stores a summary pointer.
Codex receives a queued request to retrieve that summary; Claude Code watches the pointer file and its
`asyncRewake` hook presents the summary. These are notifications to the principal, not another secretary model
loop. Hold/keep leave the underlying messages pending without waking the principal. Model errors back off
from five seconds to five minutes; no deterministic decision replaces a failed inference.

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

Supply model credentials to the service separately: a user service does not inherit your interactive shell's
environment. The generated unit optionally reads `environment` in the private instance directory. Create that
file with mode 0600 using your usual secret provisioning method (for direct OpenAI, it contains
`OPENAI_API_KEY=...`); never commit it. A command model also needs its executable and authentication available
in the service environment. The installer copies no credentials. Restart the service after changing its policy.

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

SessionStart binds the first matching project session. Other sessions are refused unless explicitly rebound. A hook or `receive` rechecks source visibility before showing a brief. Pending briefs survive worker restarts, and `receive` recovers a brief if the hook was missed. A principal acknowledges only after seeing the brief and its exact decision ID. Queueing or hook completion never counts as acknowledgement.

If several sessions share the project, use `bind SESSION_ID` to choose the principal explicitly before
starting the worker. A stopped or closed principal cannot receive a hook: Codex's resume is best-effort, and
Claude's file watcher exists only while its session is running. Pending summaries remain available for the
next session resume or an explicit `receive`. Hooks offer each decision once per session; missing an offer
does not acknowledge it. A resumed/compacted session receives its pending summary again.

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
This runner is started separately; it is not enabled with a `[integrations.secretary]` Baton server table.
