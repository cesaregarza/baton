---
name: baton-secretary-claude-code
description: Install and operate the local Baton secretary integration for Claude Code projects, including FileChanged wakeups and decision acknowledgements.
---

# Baton secretary for Claude Code

Use this skill when setting up or operating a Baton secretary instance that presents decision briefs in a Claude Code project. The integration is host-local: the worker and Claude Code must run as the same user and be able to access the same Baton SQLite database and model authentication. Remote delivery is unsupported.

## Install

1. Check the installed Claude Code version and official hook docs for `SessionStart`, `Stop`, background hooks, and `FileChanged`. Confirm the generated `FileChanged` hook's `asyncRewake` setting is supported by the installed version before applying hooks. If not, stop and report the version limitation.
2. Copy this skill folder into the project at `.claude/skills/baton-secretary-claude-code/` so it is available to that project's Claude Code sessions.
3. Choose an absolute, private, empty destination directory for the instance. Use a complete v3 secretary policy, Baton config, and project paths. Start from the [policy example](../../../baton/agents/secretary/README.md#policy); do not omit explicit model or decision-engine settings.
4. Generate the integration artifacts with the repository's current CLI:

   ```sh
   python -m baton.integrations.secretary install \
     --product claude-code \
     --destination /absolute/private/secretary-instance \
     --project /absolute/path/to/project \
     --config /absolute/path/to/baton.toml \
     --policy /absolute/path/to/secretary-policy-v3.json \
     --poll-seconds 2 \
     --budget-seconds 28 \
     --apply-hooks
   ```

   `python` is the interpreter running the module. `--apply-hooks` merges the generated integration hooks into the project's `.claude/settings.local.json`, preserving unrelated settings and making repeated application idempotent. It creates a backup before changing that project file. Do not edit global Claude settings for this integration.
5. Inspect the generated private `integration.json`, `hooks.json`, and user unit. Review the changed project settings and backup before enabling anything. The installer only writes local artifacts; it does not start the service.
6. For systemd user services, link the generated unit and start it:

   ```sh
   systemctl --user link /absolute/private/secretary-instance/secretary-XXXXXXXX.service
   systemctl --user daemon-reload
   systemctl --user enable --now secretary-XXXXXXXX.service
   systemctl --user status secretary-XXXXXXXX.service
   journalctl --user -u secretary-XXXXXXXX.service -f
   ```

   Use the exact generated unit name. `enable` configures the user manager; survival across logout or reboot depends on the host's user-service/linger setup. Alternatively, run the generated `python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json run` under a suitable supervisor. The singleton lock prevents two workers for one instance.

## Delivery and acknowledgement

Supply credentials through the service environment or the optional mode-0600 `environment` file in the
instance directory. A user service does not inherit your shell environment. Set variables named by model and
JEV `api_key_env` values. For command providers, set credentials there and list their names in the provider's
required `env` allowlist. Commands run as the Baton host user; environment filtering is not a filesystem sandbox.
Restart the worker after changing policy.

- Installation binds the activation floor to the current database head. The worker scans complete bounded pages and tracks durable scan progress separately from delivered coverage. Page output reserves space for required source citations; pages are split after repeated failures, never silently truncated.
- The independent engine evaluates each alert page. A `wake` with confidence at or above `wake_threshold` forces a wake even when model triage holds, keeps, or fails. The model may call a tool to read the cached decision and confidence; the engine cannot explain its choice. If the model cannot summarize a forced wake, the notice contains sources only.
- Transient failures retry in the same alert or digest mode, separately from worker health. After `max_attempts`, pages are split; an unsummarizable event eventually gets a source-only notice. No semantic fallback invents a decision.
- SessionStart binds the first matching project session. The instance refuses delivery to a different session. Use the explicit `bind SESSION` command only when intentionally reassigning the instance.
- SessionStart includes pending state; compaction/resume reinjects pending briefs. While the session is alive, the background `FileChanged` hook watches `pending.json` and asynchronously wakes Claude Code with the brief on stderr. Stop catches races when the file wake was missed. Do not create synthetic keep-alive model turns.
- Hooks and explicit `receive` re-check current source visibility before showing a brief. A brief remains pending across restarts and can be recovered with `receive` if a hook was missed.
- Pending receipts retain all decision IDs, surface wakes first, and announce new IDs even when older summaries remain unacknowledged. JSON `receive` returns every currently authorized pending summary with a trust label and acknowledgment command. `receive --text` prints complete JSON-framed summaries and acknowledgment commands when hook output is too large. Read a summary before acknowledging its ID; hook exit or successful wake is not acknowledgment. Later holds do not clear pending wakes; acknowledgments merge delivered ranges only when contiguous. Do not acknowledge on the principal's behalf.
- Set `summary_bytes` explicitly to 4000 (maximum 6000).

## Operate and remove

Use the generated instance path for runtime commands:

```sh
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json status
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json receive
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json acknowledge DECISION_ID
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json bind SESSION_ID
```

Stop with `systemctl --user disable --now secretary-XXXXXXXX.service`, then verify its generated unit symlinks were removed and run `systemctl --user daemon-reload`. Remove only this integration's hook entries from the project settings, retaining other hooks and settings; keep or securely remove the private instance state according to the owner's retention choice. Never clear pending decisions as an implicit part of uninstalling.

Verify with a synthetic config and inspect `status`, hook visibility, and delivery behavior before enabling real alerts. A final live model/session check requires the owner’s authorization for the inference and notifications.

## References

- [Claude Code hooks](https://code.claude.com/docs/en/hooks#filechanged)
- [Background hooks](https://code.claude.com/docs/en/hooks#run-hooks-in-the-background)
- [Secretary harness and policy](../../../baton/agents/secretary/README.md)
