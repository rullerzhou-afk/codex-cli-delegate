---
name: codex-cli-delegate
description: Delegate explicitly requested coding and review work to Claude Code, Kimi Code, OpenCode, Pi, a separate Codex session, or an established user alias for one of them through MCP, with same-session revisions, quiet waiting, recovery, and independent Codex acceptance; continue or recover jobs this Skill started. Also dispatches and tracks bounded remote Windows Codex turns.
---

# Codex CLI Delegate

Codex prepares the task and independently reviews the result. Prefer the `codex-cli-delegate` MCP; Claude uses a persistent Agent SDK connection. For setup and protocol details, read [MCP and SDK](references/mcp.md). If MCP is unavailable or you are diagnosing an older job, use the retained [CLI workflow](references/cli-workflow.md) and `scripts/delegate.py`. Missing tools are not a reason to dispatch the same work again.

## When to delegate

This Skill acts only on an explicit external route. A new external job requires both an explicit request for Claude, Kimi, OpenCode, or Pi, by name or by invoking this Skill, and one whole coherent responsibility (investigation, implementation, focused verification, and necessary documentation only when those parts are tightly coupled) that can be transferred at acceptable coordination cost. Neither condition alone is enough. Continuing an external job this Skill already started, or recovering it after interruption or context loss, are allowed resolution paths that do not need a new explicit request.

Resolve every canonical name and established alias to its backend before dispatch. For one responsibility, merge repeated labels that resolve to the same backend and start at most one job per distinct canonical backend. A named route is satisfied only after starting or continuing a matching job and confirming its saved `backend` in the start record or `delegate_status`. Report model and effort only after native completion verification. The coordinating Codex must not label or report any worker as an external backend unless that saved backend matches; a native worker has no matching external job and cannot satisfy the route.

If the user explicitly names multiple distinct external routes, dispatch one matching job per backend. Give each job one self-contained responsibility; several read-only reviewers may inspect the same subject. Every job that has not released its reservation occupies its checkout regardless of tool profile, so concurrent jobs, including read-only reviews, require distinct non-overlapping worktrees or clones; the same checkout and its nested paths conflict. These are requested participants, not extra reviewers added by default.

Do not start a new external job for native-worker-only requests, explicit solo work, casual explanations, tiny work, or work that is already nearly complete. Use one worker for the coupled responsibility, forward new constraints promptly, and continue the same worker and job for rework instead of creating phase-named jobs. Independent Codex acceptance stays mandatory; do not add a second external reviewer by default, and choose adversarial review only when the user requests it or risk requires it.

The `codex` backend runs a separate local `codex exec` session with the fixed
`gpt-6-sol/xhigh` profile and resumes the same thread for revisions. It mainly
serves non-Codex coordinators such as Claude Code. A coordinating Codex opens it
only when the user explicitly asks for a separate Codex session, never for plain
solo work. See [Codex](references/codex.md).

When the user explicitly asks the Mac Codex agent to dispatch work to a Windows
Codex agent, use the separate [Remote Windows Codex](references/remote-codex.md)
entry point. It supports one bounded `codex exec` task only. It is not one of
the external backends above and does not reuse the `delegate_start` lifecycle.
The remote cwd, model, effort, sandbox, timeout, and native Windows sandbox
implementation must be fixed by a private policy allowlist.

Worktrees and tool allowlists are specific controls, not an operating-system sandbox. Without an explicit external route this Skill does not claim routing precedence or start a new external job, but it must still recover and resolve jobs its tools previously started; in that no-route case, a host routing skill may select a native worker, which this repository neither observes nor controls. Once this Skill starts an external job, its job, round, recovery, and acceptance rules apply through release of its reservation. If an explicitly requested external route is unavailable, report it and never silently substitute a route, model, or account, including by using a native worker. See [external delegation policy](references/delegation-policy.md).

## MCP workflow

- `delegate_start`: default `timeout=null` means no wall-clock kill. Only set seconds when the user explicitly requests a runtime limit; inspect the saved timeout rather than relying on the installed version. Provide task text, the intended checkout, authorized scope, acceptance criteria, and necessary inputs. Use the calling Codex task's actual ID as `owner`, not the MCP server's startup task ID. Before reporting that a named route was started or satisfied, confirm the saved `backend` in the returned start record or `delegate_status`; report model and effort as verified only after native completion verification.
- Generate one stable `request_id` per new dispatch or revision. Retry an uncertain call with the same ID and identical parameters; use a new ID for changed work. Save the returned job ID, round, session ID, and cursor.
- Background mode: after each successful start/revise, call `delegate_notify` with the actual owner, job ID, and `expected_round`. If the user wants Codex to continue automatically when finished, also pass `wake_codex=true`; `false` selects desktop-only reminders, and omission preserves this round's setting (new subscriptions default to false). Confirm `watching` with process `alive`, or inspect the completed delivery receipt; automatic return requires `wake_codex=true` and a live watcher or `wake.state=queued`. If the loaded MCP lacks the option, use the equivalent [notification script](references/notifications.md). Do not claim a reminder is arranged if arming failed.
- `delegate_wait`: wait inside the program, normally 600 seconds, without model polling. Start with cursor `-1:0`, then pass the returned cursor. A wait timeout does not mean the job failed; continue waiting without restarting it.
- `awaiting_review` means execution evidence passed verification. Inspect the actual diff, prior-edit baseline, artifacts, and relevant tests. For corrections, use `delegate_revise` with the current `expected_round` to continue the same backend session, including a previously accepted job. Claude revisions can add `allow_tools`, `read_dirs`, and `required_files`; the runner refreshes an idle connection when needed while preserving the session and prior acceptance records. Revisions are unlimited by default; diagnose repeated failures rather than retrying mechanically. Revision timeout defaults to inherit; use `timeout=null` to remove an old job's limit after inspection.
- `delegate_accept`: supply the current round and actual independent review notes. Acceptance closes the idle SDK connection and releases the checkout lock; it does not publish or merge changes.
- After interruption, recover the original work using `delegate_list` / `delegate_status`. Wait if running. Inspect failed or stopped rounds before revising with `recover=true`. `delegate_stop` stops only verified owned processes and preserves files and sessions.

## Constraints

- Select task capabilities without asking the user for tool names. Kimi defaults include ReadMediaFile; explicit implementation tool lists must retain it for visual work. Select native web tools when research is needed. Claude supports NotebookEdit. Check returned `tools` and native evidence; selected tools are not proof of runtime availability. See [tool capabilities](references/tools.md).

- Before tasks that execute scripts, builds, or tests, check only the necessary commands, dependencies, and inputs. Reuse the existing environment; pure reviews do not need a full environment checklist. Resolve preparation within existing authorization.
- Preserve the user's authorized scope and existing changes. Every concurrent job on an overlapping checkout needs its own non-overlapping worktree or clone, even when read-only; do not bypass this reservation with another state root. A custom state root disables conflict detection across state roots; it does not release or supersede the reservation. Keep real jobs in the shared default state root `${CODEX_HOME:-~/.codex}/claude-delegate`; custom state roots are for isolated tests.
- Fixed verified profiles: Claude `claude-opus-5/max`, Kimi `kimi-code/k3-256k/max`, OpenCode `deepseek/deepseek-flash/high`, Pi `openrouter/stealth/union-alpha/off`, Codex `gpt-6-sol/xhigh`. Never silently substitute a model or effort. Changing a profile requires adapting invocation and verification together.
- Claude uses restricted task settings. Declare outside references with `read_dirs`, required inputs with `required_files`, and only narrow authorized Bash rules in `allow_tools`. SDK mode rejects Bash `run_in_background`; do not authorize commands that background themselves or evade the write scope. This is not an OS sandbox. Add authorized commands or references directly with revise. Supply the actual permitted command forms so the agent does not have to guess path spellings; resolve equivalent forms within the existing authorization. User/project custom permissions and hooks are not automatically inherited. If the MCP schema is old, use the installed skill virtual environment to run `scripts/delegate.py revise` with `--expected-round`, `--allow-tool`, `--read-dir`, or `--require-file`; do not redispatch the job or ask the user to restart it.
- Any applicable Claude quota window at 90% pauses subsequent dispatches and revisions while allowing the active round to finish. Unknown or stale quota is not zero. Do not bypass a pause with a different account, model, threshold, or state root. Read [quota and inputs](references/quota-and-inputs.md) when needed. The Codex backend only reports each round's native rate-limit snapshot as `quota` (`at_or_above_90` flags 90% or more) and does not pause dispatch.
- Preserve complete visible model output and exact provenance for material findings, blockers, architecture decisions, or disagreements. Follow [evidence and adjudication](references/review-evidence.md), independently decide each finding, and pass `evidence_dir` to accept. Status summaries are not original evidence; identify reviewers who did not participate.
- Follow the user's choice of background notifications or in-task waiting. Background mode may end the current Codex turn after the watcher confirms readiness. Desktop-only mode resumes when the user returns; `wake_codex=true` sends one terminal event to the original task through `codex queue`. On return, recheck the job, round, and notification flags; ignore superseded, disabled, accepted, or stopped work. Review independently and re-arm each revision. Queue delivery never accepts work or releases checkout locks. Waiting makes no model calls; resumed review consumes normal Codex usage. In-task waiting continues through review until acceptance, user cancellation, or an actionable blocker.

Read [background notifications](references/notifications.md) for arming, disabling, testing visibility, and delivery limits.

## Backend references

- [External delegation policy](references/delegation-policy.md): positive and negative triggers, one-worker continuation, independent acceptance, and co-installation precedence.
- [Kimi](references/kimi.md), [OpenCode](references/opencode.md), and [Pi](references/pi.md): native CLI adapters, tool selection, configuration, JSON events, and native evidence. Bash or PowerShell permission grants the entire shell tool, not Claude command-pattern filtering. MCP reuses these adapters; ACP and OpenCode Server are not implemented.
- [Codex](references/codex.md): a separate local `codex exec` session, sandbox selection with `codex_tools` (`read`, `write`, `network`), native rollout verification, same-thread revisions, and rate-limit reporting.
- [Remote Windows Codex](references/remote-codex.md): policy-bound SSH dispatch, exact session/turn observation, carrier-loss boundaries, and independent acceptance. This is separate from local delegation.
- [Recovery](references/recovery.md): uncertain process identity, missing native evidence, and eligible historical revalidation.

The local runner is validated on macOS. Runtime evidence and credentials remain local; publishing the tool does not authorize uploading task records.
