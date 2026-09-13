# Background notifications

When the user chooses background execution, call `delegate_notify(owner, job_id, expected_round)` after each successful dispatch or revision. Confirm notification state `watching` and process `alive` before ending the Codex turn. If it already says `finished`, inspect the terminal delivery receipt. `failed`, `uncertain`, or `submitting` is not confirmation of reliable delivery.

The model worker and notification watcher run independently of the caller. Ending the conversation or disconnecting MCP does not cancel them. Keep the computer awake; model execution still needs network access. The notification program makes no model calls. Desktop-only mode requires returning to the original task for review. With `wake_codex=true`, one terminal event is queued to that task so Codex can independently review the result and continue within the original authorization. Sending the event does not accept work or release the checkout lock; resumed review uses normal Codex usage.

## Scope and fallback

Subscriptions cover one exact round. Repeated calls reuse the watcher and delivery receipts; arm again after each revision. `expected_round` rejects stale requests. The owner must be the actual calling task, never another task's ID.

An already loaded MCP may lack `delegate_notify` or its new `wake_codex` input. Use the equivalent script at the actual installed path; do not redispatch or stop Claude merely to load a new tool. Updated tool schemas become available after the next connection reload.

```sh
python3 /absolute/skill/scripts/delegate_notify.py --owner <actual-task-id> arm <job-id> --expected-round 0
python3 /absolute/skill/scripts/delegate_notify.py --owner <actual-task-id> status <job-id>
python3 /absolute/skill/scripts/delegate_notify.py --owner <actual-task-id> disable <job-id> --expected-round 0
```

For automatic continuation, append `--wake-codex` to `arm`; `--notify-only` disables continuation while retaining desktop reminders. The script needs only Python 3.9+ standard libraries. It uses the shared default job state; reserve `--state-dir` for isolated tests. Disabling notifications leaves the model job running.

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

## Return to the original Codex task

Automatic continuation is opt-in: `delegate_notify(owner, job_id, expected_round, wake_codex=true)`. The owner must be the actual calling task UUID. The local Codex CLI must expose `queue --thread --message`; this is probed before arming. A missing capability reports `wake_unavailable` and leaves desktop-only mode available. No Codex database edits or model-based polling are used.

Only a completed, failed, or interrupted round queues a fixed message with its job/round and local receipt location. It contains no agent output. Progress, recoverable errors, quota warnings, and monitoring expiry while the worker remains active do not queue a message. Codex must read current state and evidence before independent review, ignore disabled/superseded/accepted/stopped work, and re-arm after same-session revision. Existing authorization and quota/permission/model refusals still apply.

`wake.state=queued` plus the exact target UUID and `queued_message_id` confirms enqueueing, not receipt or acceptance. Submission is claimed before calling the CLI. A timeout, lost acknowledgment, or crash can leave `uncertain`/`submitting`; the program does not retry automatically and risk duplicate turns. Failed or uncertain queue delivery raises a desktop reminder. Existing desktop receipts do not prevent enabling the first return message for that round. Disabling cannot withdraw an already queued message, so the receiving task checks the current flag again.

A macOS smoke check delivered one isolated terminal event to the original active Codex task; the received marker matched the queue receipt. It did not call an external model. Idle-task delivery, closed-app behavior, and reboot recovery were not exercised in that check. The watcher is not a persistent service.
