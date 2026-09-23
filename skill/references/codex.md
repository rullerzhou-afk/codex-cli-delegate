# Codex (separate `codex exec` session)

Read this only when using the `codex` backend or diagnosing its evidence. The
backend hands work to a separate local `codex exec` session. Like Kimi, OpenCode
and Pi it is a native CLI adapter and installs no hooks. It is separate from
[remote Windows Codex](remote-codex.md): that path is a bounded single-turn
carrier, while this backend is local and supports same-thread revisions.

## Fixed route and prerequisites

- Backend `codex`, fixed to model `gpt-6-sol` with reasoning effort `xhigh`.
  There is no downgrade and no model fallback.
- The default executable is the runtime bundled with the ChatGPT desktop app,
  `/Applications/ChatGPT.app/Contents/Resources/codex`, which is the same
  runtime the app's own threads use. Without it, the adapter uses `codex` on
  `PATH`. `--codex-bin` overrides both. On the 2026-09-23 validation machine,
  standalone CLI 0.154.0 had no `gpt-6-sol` in its catalog and the service
  rejected the request. Desktop runtime 0.155.0-alpha.9.2 and standalone CLI
  0.156.0 both offered the model.
- Before every round the adapter checks `--version` and the options that
  `exec` and `exec resume` need. It also requires `codex debug models` to list
  `gpt-6-sol` with `xhigh`. Otherwise the round fails with
  `codex_incompatible` or `codex_model_missing`.
- The existing Codex login under `CODEX_HOME` (default `~/.codex`) is reused;
  `config.toml` is never edited. The job records `CODEX_HOME`, and a later
  change makes revisions fail with `codex_home_changed`.

## Invocation

First round:

```text
codex exec --json --ignore-user-config --ignore-rules --skip-git-repo-check \
  -m gpt-6-sol -c model_reasoning_effort="xhigh" -c approval_policy="never" \
  -c sandbox_mode="<read-only|workspace-write>" -o <round-dir>/codex-last-message.txt -C <cwd> -
```

The prompt arrives on stdin behind a per-round marker,
`[codex-delegate:<run_token>]`. Revisions run `codex exec resume <same options>
<thread_id> -`, which continues the same thread and native rollout. `resume`
has no `-C` or `--sandbox` option, so the process runs in the job cwd and the
sandbox is selected with `-c sandbox_mode`.

- The user's `config.toml` and execpolicy rules are not loaded. The delegated
  Codex therefore gets none of the user's MCP servers (including this delegate,
  which prevents recursive dispatch), notify programs, hook trust state, or
  day-to-day approvals. Codex auth and AGENTS.md instructions still apply.
- The only mirrored user setting is `[features] respect_system_proxy = true`.
  When it is set, the run adds `--enable respect_system_proxy` and suppresses
  the under-development feature warning, so network routing matches the
  desktop app.
- `CODEX_THREAD_ID` and `CODEX_SQLITE_HOME` are removed from the child
  environment, so a coordinating Codex task's identity and state database do
  not leak into the worker.

## Sandbox selection (`codex_tools`)

Codex always has a sandboxed shell, and commands cannot be restricted one by
one. Choose Claude when machine-enforced command rules are required.
`codex_tools` selects the sandbox:

| Value | Effect |
| --- | --- |
| `read` (default) | Read-only sandbox |
| `write` | workspace-write: writes only to the cwd and temporary directories |
| `network` | Outbound network access; requires `write` |

Approval is fixed at `never`: an operation outside the sandbox fails back to
the model instead of waiting for approval. `read_dirs` and `required_files`
apply only to Claude. The read-only sandbox can already read the disk, so name
reference paths in the task text.

## Completion and verification

A round reaches `awaiting_review` only when all of the following hold:

- the process exited with code 0;
- the JSON stream contains exactly one `thread.started` and one `turn.started`
  and ends with `turn.completed`, with no `turn.failed` or `error`; on a
  revision the thread must equal the saved thread;
- the native rollout `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*-<thread>.jsonl`
  is found by thread id, because a resumed thread keeps writing in its original
  day folder. Its first `session_meta` record matches the thread id and cwd and
  has originator `codex_exec`;
- the user message carrying this round's marker appears exactly once in the
  whole file, after the revision baseline, and its turn is the last turn in the
  file;
- that turn's `turn_context` matches the requested model, effort, cwd,
  `sandbox_policy` (including `network_access`), and `approval_policy`;
- the turn has `task_complete`, and its `last_agent_message` equals both the
  last streamed `agent_message` and the `-o` file;
- a read-only round has no completed `file_change` item;
- on a revision, the rollout content before the baseline (path, byte size,
  SHA-256) is unchanged.

After process exit the adapter waits up to 5 seconds for the rollout to be
flushed. A missing or mismatched item is never treated as success. These checks
prove execution and identity only. The coordinator still reviews the work
independently. The round's verification summary is written to
`codex-native.json` in the round directory.

## Rate limits

Each round reads `rate_limits` from the marked turn's native `token_count`
record: `used_percent`, `window_minutes` and `resets_at` for the primary and
secondary windows. The snapshot is stored in the verification result and
returned as `quota` in MCP receipts, where `at_or_above_90` flags usage of 90%
or more. Dispatch is not paused, unlike the Claude 90% pause. Delegated runs
draw on the same Codex account usage as the user's other Codex work. If the
limit is exhausted, the request fails and the round is reported as failed; the
adapter never switches model or account.

## Coordinators

- **Claude Code.** Register the MCP server at user scope through a small
  launcher. The launcher removes Claude Code session variables and passes a
  nonexistent `--claude-bin` path, so the Claude backend fails with `no_claude`
  instead of delegating to Claude Code itself:

  ```sh
  #!/bin/sh
  for name in $(env | sed -n -E 's/^(CLAUDECODE|CLAUDE_CODE_[A-Za-z0-9_]*)=.*/\1/p'); do
    unset "$name"
  done
  exec /path/to/skill/.venv/bin/python /path/to/skill/scripts/delegate_mcp.py \
    --claude-bin /nonexistent/claude-backend-disabled "$@"
  ```

  Register it with `claude mcp add --scope user codex-cli-delegate --
  /path/to/launcher.sh`. Pass the Claude Code session id as `owner` and always
  use `wake_codex=false`, because `codex queue` only reaches Codex tasks. To be
  woken when a round ends, run `scripts/delegate.py --owner <id> await-event
  <job> --after <cursor>` as a background shell command. It returns on an
  error, a 10- or 15-minute checkpoint, or completion.
- **Codex.** `wake_codex=true` can return the result to the original task.
  Open this backend only when the user explicitly asks for a separate Codex
  session, never for plain solo work.

## Known limits

- macOS only. Process identity uses the executable path and the kernel start
  time. A desktop-app update can replace the binary at the same path; the next
  round re-probes it and records the new version.
- A thread archived to `archived_sessions` has no rollout under `sessions`, so
  revisions fail with `codex_session_missing`.
- Web search, MCP tools, and image input (`-i`) are not wired in.
