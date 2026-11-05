---
name: baton-secretary-codex
description: Install and operate the local Baton secretary integration for Codex projects, including hook delivery and decision acknowledgements.
---

# Baton secretary for Codex

Use this skill when setting up or operating a Baton secretary instance that presents decision briefs in a Codex project. The integration is host-local: the worker and Codex must run as the same user and be able to access the same Baton SQLite database and model authentication. Remote delivery is unsupported.

## Install

1. Check the current Codex hook support using the installed `codex --help` and the in-product `/hooks` command. If the installed version does not support the required hook events, stop and report that; do not guess a compatible configuration.
2. Copy this skill folder into the project at `.agents/skills/baton-secretary-codex/` so it is available to that project's Codex sessions.
3. Choose an absolute, private, empty destination directory for the instance. Use the existing v2 secretary policy, Baton config, and project paths. Do not create user-specific policy, role, or path assumptions.
4. Generate the integration artifacts with the repository's current CLI:

   ```sh
   python -m baton.integrations.secretary install \
     --product codex \
     --destination /absolute/private/secretary-instance \
     --project /absolute/path/to/project \
     --config /absolute/path/to/baton.toml \
     --policy /absolute/path/to/secretary-policy-v2.json \
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

Supply model credentials through the service environment or the optional mode-0600 `environment` file in the
instance directory. A user service does not inherit your shell's `OPENAI_API_KEY`. The v2 policy can use the
direct OpenAI transport or an existing host-command adapter. Restart the worker after changing its policy.

- The worker polls the read-only visible-event feed every two seconds. It performs inference at startup and when new visible events arrive; an empty poll does not trigger an inference.
- A Codex assessment sends one `codex queue --thread <bound-id> --message ... --resume` request. A successful command means the message was queued, not that a model read or acted on it. Resuming an idle thread can warn or fail; check the target session.
- SessionStart binds the first matching project session. The instance refuses delivery to a different session. Use the explicit `bind SESSION` command only when intentionally reassigning the instance.
- Hooks and explicit `receive` re-check current source visibility before showing a brief. Stop offers a fresh summary only and respects the stop-hook recursion guard. Do not create synthetic keep-alive turns.
- A brief remains pending across restarts and can be recovered with `receive` if a hook was missed. A principal acknowledges only after reading the brief and its exact decision ID, using `acknowledge ID`. Hook exit or successful queueing is never an acknowledgement.
- Set `summary_bytes` explicitly to 4000 (maximum 6000). Do not acknowledge on the principal's behalf.

## Operate and remove

Use the generated instance path for runtime commands:

```sh
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json status
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json receive
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json acknowledge DECISION_ID
python -m baton.integrations.secretary --instance /absolute/private/secretary-instance/integration.json bind SESSION_ID
```

Stop with `systemctl --user disable --now secretary-XXXXXXXX.service`, then verify its generated unit symlinks were removed and run `systemctl --user daemon-reload`. Remove only this integration's hook entries from the project file, retaining other hooks and settings; keep or securely remove the private instance state according to the owner's retention choice. Never clear pending decisions as an implicit part of uninstalling.

Verify with a synthetic config and inspect `status`, hook visibility, and delivery behavior before enabling real alerts. A final live model/session check requires the owner’s authorization for the inference and notifications.

## References

- [Codex hooks](https://learn.chatgpt.com/docs/hooks)
- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
