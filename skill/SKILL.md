---
name: codex-cli-delegate
description: Delegate authorized coding and review tasks to Claude Code, Kimi Code, or OpenCode through MCP, with same-session revisions, quiet waiting, recovery, and independent Codex acceptance. Also tracks exact remote Windows Codex turns.
---

# Codex CLI Delegate

Codex prepares the task and independently reviews the result. Prefer the `codex-cli-delegate` MCP; Claude uses a persistent Agent SDK connection. For setup and protocol details, read [MCP and SDK](references/mcp.md). If MCP is unavailable or you are diagnosing an older job, use the retained [CLI workflow](references/cli-workflow.md) and `scripts/delegate.py`. Missing tools are not a reason to dispatch the same work again.

## MCP workflow

- `delegate_start`: provide task text, the intended checkout, authorized scope, acceptance criteria, and necessary inputs. Use the calling Codex task's actual ID as `owner`, not the MCP server's startup task ID.
- Generate one stable `request_id` per new dispatch or revision. Retry an uncertain call with the same ID and identical parameters; use a new ID for changed work. Save the returned job ID, round, session ID, and cursor.
- `delegate_wait`: wait inside the program, normally 600 seconds, without model polling. Start with cursor `-1:0`, then pass the returned cursor. A wait timeout does not mean the job failed; continue waiting without restarting it.
- `awaiting_review` means execution evidence passed verification. Inspect the actual diff, prior-edit baseline, artifacts, and relevant tests. For corrections, use `delegate_revise` with the current `expected_round` to continue the same backend session. Revisions are unlimited by default; diagnose repeated failures rather than retrying mechanically.
- `delegate_accept`: supply the current round and actual independent review notes. Acceptance closes the idle SDK connection and releases the checkout lock; it does not publish or merge changes.
- After interruption, recover the original work using `delegate_list` / `delegate_status`. Wait if running. Inspect failed or stopped rounds before revising with `recover=true`. `delegate_stop` stops only verified owned processes and preserves files and sessions.

## Constraints

- Preserve the user's authorized scope and existing changes. Use isolated worktrees when concurrent edits require them. Keep real jobs in the shared default state root `${CODEX_HOME:-~/.codex}/claude-delegate`; custom state roots are for isolated tests.
- Fixed verified profiles: Claude `claude-opus-5/max`, Kimi `kimi-code/k3-256k/max`, OpenCode `deepseek/deepseek-flash/high`. Never silently substitute a model or effort. Changing a profile requires adapting invocation and verification together.
- Claude uses restricted task settings. Declare outside references with `read_dirs`, required inputs with `required_files`, and only narrow authorized Bash rules in `allow_tools`. SDK mode rejects Bash `run_in_background`; do not authorize commands that background themselves or evade the write scope. This is not an OS sandbox. To change read directories, stop and use CLI `revise --recover --read-dir` on the existing job.
- Any applicable Claude quota window at 90% pauses subsequent dispatches and revisions while allowing the active round to finish. Unknown or stale quota is not zero. Do not bypass a pause with a different account, model, threshold, or state root. Read [quota and inputs](references/quota-and-inputs.md) when needed.
- Preserve complete visible model output and exact provenance for material findings, blockers, architecture decisions, or disagreements. Follow [evidence and adjudication](references/review-evidence.md), independently decide each finding, and pass `evidence_dir` to accept. Status summaries are not original evidence; identify reviewers who did not participate.
- Keep the calling task active through waiting, review, and necessary revisions until acceptance, user cancellation, or an actionable blocker. An idle worker does not guarantee waking an ended Codex task.

## Backend references

- [Kimi](references/kimi.md) and [OpenCode](references/opencode.md): native CLI adapters, tool selection, configuration, and existing hooks. Their Bash permission grants the entire shell tool, not Claude command-pattern filtering. MCP reuses these adapters; ACP and OpenCode Server are not implemented. Pi is not implemented.
- [Remote Windows Codex](references/remote-codex.md): observe an exact existing remote session/turn, then verify its artifacts. This is separate from local delegation and uses `scripts/remote_codex.py`.
- [Recovery](references/recovery.md): uncertain process identity, missing native evidence, and eligible historical revalidation.

The local runner is validated on macOS. Runtime evidence and credentials remain local; publishing the tool does not authorize uploading task records.
