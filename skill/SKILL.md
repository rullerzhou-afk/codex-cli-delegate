---
name: codex-cli-delegate
description: Delegate authorized coding and review tasks from Codex to Claude Code, Kimi Code, or OpenCode; track exact remote Windows Codex turns. Use for delegation, same-session revisions, completion monitoring, evidence export, and recovery. Codex plans and independently accepts results.
---

# Codex CLI Delegate

Use `scripts/delegate.py` from this skill's actual installed path. Default backend: Claude. Select Kimi with `start --backend kimi` and OpenCode with `start --backend opencode`. The backend stays fixed within a job; revisions resume its saved session. Reuse existing CLI installations and logins.

This is a community skill, not an official integration from any CLI or model provider. The local runner is validated on macOS. Copying the skill to Windows does not make it a Windows local runner.

## Select the route

- Claude: read [quota and required inputs](references/quota-and-inputs.md). Uses per-round restricted settings and hooks.
- Kimi: read [Kimi setup and native evidence](references/kimi.md). Requires existing thinking configuration and three explicitly installed managed hooks. Normal dispatch checks them without rewriting user configuration.
- OpenCode: read [OpenCode tools and hooks](references/opencode.md). Adds a per-process plugin while preserving existing plugins. Default profile is read-only.
- Remote Windows Codex: read [remote observation](references/remote-codex.md) and use `scripts/remote_codex.py`. This observes an already identified remote turn; it does not dispatch a new Codex turn.

The supporting references currently contain detailed Chinese operating notes; public setup is documented in the repository's English README and Chinese companion.

## Authorization and task preparation

1. Establish the task, acceptance criteria, authorized directories and tools, and applicable project instructions. Save a task file with enough context to execute the work. Do not send credentials, unrelated conversations, or unnecessary files.
2. Use the calling task's real `CODEX_THREAD_ID` as owner. If unavailable, pass its actual identifier with `--owner`; never borrow another task's owner.
3. Use the intended existing checkout or an authorized isolated worktree, preserving any prior edits. Record the baseline for independent review.
4. Keep real jobs on the shared default state directory `${CODEX_HOME:-~/.codex}/claude-delegate`. Its legacy name preserves checkout locks and recovery compatibility. A different state root bypasses cross-job conflict detection; reserve `--state-dir` for isolated tests.
5. Give only the tools needed for the authorized task. Claude command rules use `--allow-tool`; Kimi and OpenCode tool selections are separate and not interchangeable. Bash permission in those backends grants the whole shell tool, not a command allowlist. Do not bypass permissions.
6. For Claude, pass required files with `--require-file` and explicitly authorized outside reference directories with `--read-dir`. Read large files in chunks; truncated output is not the full file. OpenCode currently does not support those flags.

Restricted settings do not create an OS sandbox or a worktree. Check the actual modified paths and artifacts. Delegation does not confer permission to publish, push, merge, deploy, send external messages, or alter unrelated user configuration.

## Verified profiles

| Backend | Fixed model | Effort / variant |
| --- | --- | --- |
| Claude Code | `claude-opus-5` | `max` |
| Kimi Code | `kimi-code/k3-256k` | `max` |
| OpenCode | `deepseek/deepseek-flash` | `high` |

These are the current adapter profiles, not universal recommendations or arbitrary-model support. Do not substitute models in task text. A requested model change requires adapting the invocation and native verification together and validating the new profile. Report mismatched settings, rejected API requests, and missing evidence; never silently downgrade.

## Dispatch, wait, review

Before waiting, read [events and quiet waiting](references/events.md). Keep the calling Codex task active until completion, independent review, user cancellation, or an actionable blocker. A launched worker does not wake a task that has already ended.

```text
python3 <absolute-skill-path>/scripts/delegate.py start --cwd <directory> --prompt-file <task-file>
python3 <absolute-skill-path>/scripts/delegate.py await-event <job-id> --after=-1:0
python3 <absolute-skill-path>/scripts/delegate.py status <job-id>
```

Save the job ID, backend, session, cwd, and cursor. Continue with the cursor returned by `await-event`; do not poll full transcripts or create a new job because a tool wait yielded. Observe the current environment's wait limits. Error notifications and 10/15-minute checkpoints indicate what to inspect, not automatic permission to stop or restart a model.

Completion hooks and idle events are hints. Only `awaiting_review` with verified native completion is ready for review; it is not acceptance. Independently inspect diffs, rejected tools, test results, and requested artifacts. Backend final text alone cannot prove the task succeeded.

```text
python3 <absolute-skill-path>/scripts/delegate.py revise <job-id> --prompt-file <revision-file>
python3 <absolute-skill-path>/scripts/delegate.py await-event <job-id> --after <saved-cursor>
python3 <absolute-skill-path>/scripts/delegate.py accept <job-id> --notes-file <review-notes>
```

Revisions are unlimited by default, with per-round timeout and error/progress monitoring. Repeated identical failures without progress require diagnosis, not mechanical retries. Explicit finite limits remain supported; `0` disables revisions. For an old capped job, `revise --max-revisions unlimited` removes that cap without changing sessions when authorized by the user.

Claude quota at or above 90% in any applicable native window pauses new dispatches and revisions while letting the current round finish. Report the affected window and continue its review. Unknown/stale quota is not zero; absent reliable usage may allow a dispatch, so this is not a guaranteed hard spending cap. Do not bypass a pause using another state directory, threshold, account, or model. Kimi and DeepSeek account quota are not monitored by this feature.

## Evidence and recovery

For material findings, blockers, architectural decisions, or disagreements, read [original evidence and adjudication](references/review-evidence.md). Export the current round's visible model text and source metadata; preserve both accepted and rejected findings with the independent decision and its basis. Do not publish private reasoning. Use `accept --evidence-dir` when material review evidence is required. State who did and did not review the work.

```text
python3 <absolute-skill-path>/scripts/delegate.py list
python3 <absolute-skill-path>/scripts/delegate.py stop <job-id>
```

After interruption, inspect the original job before dispatching again. If it is running, resume waiting; if handed off, continue review. For orphaned processes, uncertain identity, missing records, or eligible evidence-only revalidation, read [recovery](references/recovery.md). Stop only verified owned processes; preserve evidence and work files. Do not present unsaved sessions as resumable.

Report the result, actual verified profile, independent checks, remaining limitations, and job/evidence locations. Runtime evidence and credentials stay local; publishing this tool does not authorize uploading task records.
