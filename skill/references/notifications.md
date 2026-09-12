# Background notifications

When the user chooses background execution, call `delegate_notify(owner, job_id, expected_round)` after each successful dispatch or revision. Confirm notification state `watching` and process `alive` before ending the Codex turn. If it already says `finished`, inspect the terminal delivery receipt. `failed`, `uncertain`, or `submitting` is not confirmation of reliable delivery.

The model worker and notification watcher run independently of the caller. Ending the conversation or disconnecting MCP does not cancel them. Keep the computer awake; model execution still needs network access. The notification program makes no model calls and does not wake Codex, revise, accept work, or release the checkout lock. Return to the original Codex task and job for independent review.

## Scope and fallback

Subscriptions cover one exact round. Repeated calls reuse the watcher and delivery receipts; arm again after each revision. `expected_round` rejects stale requests. The owner must be the actual calling task, never another task's ID.

An already loaded MCP may still expose seven tools. Use the equivalent script at the actual installed path; do not redispatch or stop Claude merely to load a new tool. The eight-tool MCP entry becomes available after the next connection reload.

```sh
python3 /absolute/skill/scripts/delegate_notify.py --owner <actual-task-id> arm <job-id> --expected-round 0
python3 /absolute/skill/scripts/delegate_notify.py --owner <actual-task-id> status <job-id>
python3 /absolute/skill/scripts/delegate_notify.py --owner <actual-task-id> disable <job-id> --expected-round 0
```

The script needs only Python 3.9+ standard libraries. It uses the shared default job state; reserve `--state-dir` for isolated tests. Disabling notifications leaves the model job running.

## Notification triggers

- Verified completion: ready for independent review, not already accepted.
- Failed, interrupted, or unverified completion: inspect the original evidence.
- An existing monitor quota-pause event, or a 15-minute checkpoint reporting no observed progress: notify once per category while work continues. No observed progress does not prove failure.
- For an explicitly limited round, the watcher reaching its timeout plus 120 seconds without a terminal state triggers a monitoring-ended reminder. With the default unlimited runtime it continues until a terminal state, disable, or a superseding round.

Normal progress, recoverable tool errors, and Stop/idle hooks alone do not announce completion. Accepted or explicitly stopped work stays quiet. Backend-specific reminders depend on events already supported by that backend's monitor. Immediate notification of every possible permission wait is not guaranteed.

## macOS visibility and receipts

Uses `/usr/bin/osascript`; it does not install a persistent system service, call a model, or modify global notification settings. Current notification text is Chinese and includes only fixed explanations, backend name, a short job ID, and round number. Prompts, reports, paths, and error payloads stay off the notification screen.

Send a real test notification with:

```sh
python3 /absolute/skill/scripts/delegate_notify.py probe
```

Check the banner or Notification Center. Notification permissions, Focus mode, and OS settings can suppress visibility. `submitted` means the OS command returned successfully, not that the user saw a banner. The program does not change those settings automatically.

Each round's `notification.json` records watcher identity and submission outcomes; `notification.log` holds diagnostics. Delivery is claimed before submission so a disconnect or crash does not cause automatic duplicate banners. A crash in that interval can leave `submitting`, meaning the outcome is uncertain; it is not automatically resent. Inspect failed or uncertain delivery and test the channel with `probe`; work and evidence remain saved.

The watcher does not automatically restart after being killed, sleep, or reboot. Inspect the original job and re-arm a still-unnotified round if needed. Local macOS notifications are implemented; remote Windows Codex observation remains a separate capability.
