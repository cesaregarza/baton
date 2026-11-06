---
name: baton-secretary-codex
description: Install and operate the local Baton secretary integration for Codex projects, including hook delivery and decision acknowledgements.
---

# Baton secretary for Codex

Use this skill when setting up or operating a Baton secretary instance that presents decision briefs in a Codex project. The integration is host-local: the worker and Codex must run as the same user and be able to access the same Baton SQLite database and model authentication. Remote delivery is unsupported; authenticated remote reads of archived summaries are supported.

## Install

1. Check the current Codex hook support using the installed `codex --help` and the in-product `/hooks` command. If the installed version does not support the required hook events, stop and report that; do not guess a compatible configuration.
2. Copy this skill folder into the project at `.agents/skills/baton-secretary-codex/` so it is available to that project's Codex sessions.
3. Choose an absolute, private, empty destination directory for the instance. Use a complete v3 secretary policy, Baton config, and project paths. Start from the [policy example](../../../baton/agents/secretary/README.md#policy); do not omit explicit model or decision-engine settings.
4. Generate the integration artifacts with the repository's current CLI:

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

   `python` is the interpreter running the module. `--apply-hooks` merges the generated integration hooks into the project's `.codex/hooks.json`, preserving unrelated settings and making repeated application idempotent. It creates a backup before changing that project file. Do not edit global Codex settings for this integration.
5. Inspect the generated private `integration.json`, `hooks.json`, and user unit. Review and trust the exact hook definitions in Codex `/hooks`; the project configuration must also be trusted. Changed definitions require trust again. The installer only writes local artifacts; it does not start the service.
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
instance directory. A user service does not inherit your shell environment. Set the variables named by model
and JEV `api_key_env` values. For command providers, set the credential in that environment and list its name
in the provider's required `env` allowlist. Commands run as the Baton host user; environment filtering is not a
filesystem sandbox. Restart the worker after changing policy.

- Installation binds the activation floor to the current database head. The worker scans complete bounded pages and tracks durable scan progress separately from delivered coverage. Page output reserves space for required source citations; pages are split after repeated failures, never silently truncated.
- The required `delivery_timeout_seconds` is independent of the engine timeout. The engine timeout must be shorter than the assessment budget; install and worker startup validate this. A standalone `decide` call checks due retries before scanning new work, and a trigger matching a failed source page retries that page.
- The independent engine evaluates each alert page. A `wake` with `wake_probability` at or above `wake_threshold` forces a wake even when model triage holds, keeps, or fails. JEV thresholds use `probabilities["wake"]`, not its separate wire `confidence`; command engines use protocol 3 and return exactly `decision` plus `wake_probability`. The model may call a tool to read the cached decision and wake probability; the engine cannot explain its choice. If the model cannot summarize a forced wake, the worker emits a forced-wake notice. A model outage without a forced wake remains an outage, not an alert.
- Provider outages back off in the same mode. Invalid model content/output and `InboxTooLarge` count toward `max_attempts`; timeouts, rejected requests, and failed commands also count when another page has succeeded under the same policy since that page last failed. Each poll reserves one full budget for a fresh alert and one for scheduled work; one scheduled lane gets the whole reserved budget per poll, with retry/digest priority alternating. Digest failures retain digest mode and eventually produce a keep notice, never an alert or wake. Engine, model, and delivery failures have separate sanitized health/status categories and warnings. Codex delivery runs in one supervised background task while assessment continues; retries double to the policy cap and warnings occur when the failure state changes. Saved results resume after a crash without invoking either provider again; acknowledged results are not delivered again. Hourly calls wait only on digest retries. Connection/pool timeouts remain outages; HTTP 408/504 and read/write timeouts follow the page-specific retry rule.
- A Codex assessment sends one `codex queue --thread <bound-id> --message ... --resume` request. A successful command means the message was queued, not that a model read or acted on it. Resuming an idle thread can warn or fail; check the target session.
- SessionStart binds the first matching project session. The instance refuses delivery to a different session. Use the explicit `bind SESSION` command only when intentionally reassigning the instance.
- Hooks and explicit `receive` re-check current source visibility before showing a brief. Stop offers a fresh summary only and respects the stop-hook recursion guard. Do not create synthetic keep-alive turns.
- Pending receipts retain all decision IDs, surface wakes first, and announce new IDs even when older summaries remain unacknowledged. JSON `receive` returns every currently authorized pending summary with a trust label and acknowledgment command. `receive --text` prints complete JSON-framed summaries and acknowledgment commands when hook output is too large. Read a summary before acknowledging its ID; hook exit or successful queueing is not acknowledgment. Later holds do not clear pending wakes; acknowledgments merge delivered ranges only when contiguous.
- Set `summary_bytes` explicitly to 4000 (maximum 6000). Do not acknowledge on the principal's behalf.

## Operate and remove

Use the generated instance path for runtime commands:

```sh
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json status
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json receive
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json acknowledge DECISION_ID
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json bind SESSION_ID
baton --host https://baton.example secretary summaries --after 0 --limit 50
```

Remote archive reads use the authenticated token's role and need no host-side policy file. The matching MCP tool is
`read_secretary_summaries`; other secretary commands remain host-local.

Stop with `systemctl --user disable --now secretary-XXXXXXXX.service`, then verify its generated unit symlinks were removed and run `systemctl --user daemon-reload`. Remove only this integration's hook entries from the project file, retaining other hooks and settings; keep or securely remove the private instance state according to the owner's retention choice. Never clear pending decisions as an implicit part of uninstalling.

Verify with a synthetic config and inspect `status`, hook visibility, and delivery behavior before enabling real alerts. A final live model/session check requires the owner’s authorization for the inference and notifications.

## References

- [Codex hooks](https://learn.chatgpt.com/docs/hooks)
- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Secretary harness and policy](../../../baton/agents/secretary/README.md)
