---
name: codex-cli-delegate
description: Delegate authorized coding and review tasks to Claude Code, Kimi Code, or OpenCode through MCP, with same-session revisions, quiet waiting, recovery, and independent Codex acceptance. Also tracks exact remote Windows Codex turns.
---

# Codex CLI Delegate

Codex prepares the task and independently reviews the result. Prefer the `codex-cli-delegate` MCP; Claude uses a persistent Agent SDK connection. For setup and protocol details, read [MCP and SDK](references/mcp.md). If MCP is unavailable or you are diagnosing an older job, use the retained [CLI workflow](references/cli-workflow.md) and `scripts/delegate.py`. Missing tools are not a reason to dispatch the same work again.

## MCP workflow

- `delegate_start`: default `timeout=null` means no wall-clock kill. Only set seconds when the user explicitly requests a runtime limit; inspect the saved timeout rather than relying on the installed version. Provide task text, the intended checkout, authorized scope, acceptance criteria, and necessary inputs. Use the calling Codex task's actual ID as `owner`, not the MCP server's startup task ID.
- Generate one stable `request_id` per new dispatch or revision. Retry an uncertain call with the same ID and identical parameters; use a new ID for changed work. Save the returned job ID, round, session ID, and cursor.
- Background mode: after each successful start/revise, call `delegate_notify` with the actual owner, job ID, and `expected_round`. If the user wants Codex to continue automatically when finished, also pass `wake_codex=true`; `false` selects desktop-only reminders, and omission preserves this round's setting (new subscriptions default to false). Confirm `watching` with process `alive`, or inspect the completed delivery receipt; automatic return requires `wake_codex=true` and a live watcher or `wake.state=queued`. If the loaded MCP lacks the option, use the equivalent [notification script](references/notifications.md). Do not claim a reminder is arranged if arming failed.
- `delegate_wait`: wait inside the program, normally 600 seconds, without model polling. Start with cursor `-1:0`, then pass the returned cursor. A wait timeout does not mean the job failed; continue waiting without restarting it.
- `awaiting_review` means execution evidence passed verification. Inspect the actual diff, prior-edit baseline, artifacts, and relevant tests. For corrections, use `delegate_revise` with the current `expected_round` to continue the same backend session, including a previously accepted job. Claude revisions can add `allow_tools`, `read_dirs`, and `required_files`; the runner refreshes an idle connection when needed while preserving the session and prior acceptance records. Revisions are unlimited by default; diagnose repeated failures rather than retrying mechanically. Revision timeout defaults to inherit; use `timeout=null` to remove an old job's limit after inspection.
- `delegate_accept`: supply the current round and actual independent review notes. Acceptance closes the idle SDK connection and releases the checkout lock; it does not publish or merge changes.
- After interruption, recover the original work using `delegate_list` / `delegate_status`. Wait if running. Inspect failed or stopped rounds before revising with `recover=true`. `delegate_stop` stops only verified owned processes and preserves files and sessions.

## Constraints

- Before tasks that execute scripts, builds, or tests, check only the necessary commands, dependencies, and inputs. Reuse the existing environment; pure reviews do not need a full environment checklist. Resolve preparation within existing authorization.
- Preserve the user's authorized scope and existing changes. Use isolated worktrees when concurrent edits require them. Keep real jobs in the shared default state root `${CODEX_HOME:-~/.codex}/claude-delegate`; custom state roots are for isolated tests.
- Fixed verified profiles: Claude `claude-opus-5/max`, Kimi `kimi-code/k3-256k/max`, OpenCode `deepseek/deepseek-flash/high`. Never silently substitute a model or effort. Changing a profile requires adapting invocation and verification together.
- Claude uses restricted task settings. Declare outside references with `read_dirs`, required inputs with `required_files`, and only narrow authorized Bash rules in `allow_tools`. SDK mode rejects Bash `run_in_background`; do not authorize commands that background themselves or evade the write scope. This is not an OS sandbox. Add authorized commands or references directly with revise. Supply the actual permitted command forms so the agent does not have to guess path spellings; resolve equivalent forms within the existing authorization. User/project custom permissions and hooks are not automatically inherited. If the MCP schema is old, use the installed skill virtual environment to run `scripts/delegate.py revise` with `--expected-round`, `--allow-tool`, `--read-dir`, or `--require-file`; do not redispatch the job or ask the user to restart it.
- Any applicable Claude quota window at 90% pauses subsequent dispatches and revisions while allowing the active round to finish. Unknown or stale quota is not zero. Do not bypass a pause with a different account, model, threshold, or state root. Read [quota and inputs](references/quota-and-inputs.md) when needed.
- Preserve complete visible model output and exact provenance for material findings, blockers, architecture decisions, or disagreements. Follow [evidence and adjudication](references/review-evidence.md), independently decide each finding, and pass `evidence_dir` to accept. Status summaries are not original evidence; identify reviewers who did not participate.
- Follow the user's choice of background notifications or in-task waiting. Background mode may end the current Codex turn after the watcher confirms readiness. Desktop-only mode resumes when the user returns; `wake_codex=true` sends one terminal event to the original task through `codex queue`. On return, recheck the job, round, and notification flags; ignore superseded, disabled, accepted, or stopped work. Review independently and re-arm each revision. Queue delivery never accepts work or releases checkout locks. Waiting makes no model calls; resumed review consumes normal Codex usage. In-task waiting continues through review until acceptance, user cancellation, or an actionable blocker.

Read [background notifications](references/notifications.md) for arming, disabling, testing visibility, and delivery limits.

## Backend references

- [Kimi](references/kimi.md) and [OpenCode](references/opencode.md): native CLI adapters, tool selection, configuration, and existing hooks. Their Bash permission grants the entire shell tool, not Claude command-pattern filtering. MCP reuses these adapters; ACP and OpenCode Server are not implemented. Pi is not implemented.
- [Remote Windows Codex](references/remote-codex.md): observe an exact existing remote session/turn, then verify its artifacts. This is separate from local delegation and uses `scripts/remote_codex.py`.
- [Recovery](references/recovery.md): uncertain process identity, missing native evidence, and eligible historical revalidation.

The local runner is validated on macOS. Runtime evidence and credentials remain local; publishing the tool does not authorize uploading task records.
