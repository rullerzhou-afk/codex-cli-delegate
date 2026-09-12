# MCP and persistent Claude sessions

## Install and connect

Install the complete skill at its final location first. The MCP/SDK path requires Python 3.12+ and the pinned packages below; the retained CLI path supports Python 3.9+ without these packages.

```sh
skill_target="${CODEX_HOME:-$HOME/.codex}/skills/codex-cli-delegate"
python3.12 -m venv "$skill_target/.venv"
"$skill_target/.venv/bin/python" -m pip install -r "$skill_target/requirements.txt"
```

Before updating an existing installation, back up the skill and configuration and finish or stop its running/idle SDK jobs. Do not move a created virtual environment or the real job state. If the installation path changes, migrate Kimi hooks using the repository README; an in-place source update does not automatically require reinstalling hooks.

Add a stdio MCP entry to the Codex configuration, replacing every example path with the actual absolute path. Point `--claude-bin` to the existing authenticated Claude Code executable:

```toml
[mcp_servers.codex-cli-delegate]
command = "/absolute/skill/.venv/bin/python"
args = ["/absolute/skill/scripts/delegate_mcp.py", "--claude-bin", "/absolute/bin/claude"]
startup_timeout_sec = 20
tool_timeout_sec = 1900
```

`delegate_wait` defaults to 600 seconds and allows up to 1800 seconds, so the client timeout must be longer. See the [official Codex MCP configuration documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli). Reload the MCP connection using the controls available in your client, then actually call `delegate_list` from the intended Codex task. A configuration entry alone does not prove that task has loaded the tools. On the tested desktop version, turning the MCP entry off and on loaded the tools in the existing task.

The MCP display/configuration name is independent of the legacy internal server and state names. Do not run duplicate installations against the same jobs unintentionally.

Reuse the existing CLI login; the adapter explicitly selects that executable instead of substituting the SDK's bundled CLI. Preserve the original `CLAUDE_CONFIG_DIR` environment: on the tested macOS CLI, unset and explicitly set to the default path can select different authentication storage. Each job retains the raw environment value separately from its normalized transcript path.

## Tools and recovery

| Tool | Main inputs | Behavior |
| --- | --- | --- |
| `delegate_start` | owner, request_id, cwd, task, backend, permissions, inputs | Save a job and launch; Claude uses SDK, Kimi/OpenCode use CLI |
| `delegate_revise` | owner, request_id, job_id, expected_round, task, recover | Reuse an idle SDK connection or resume the saved native session |
| `delegate_status` | owner, job_id, details | Compact status; optional diagnostic details |
| `delegate_list` | owner | Find the caller's jobs; returns a jobs list |
| `delegate_wait` | owner, job_id, cursor, timeout | Programmatic event/round waiting without model calls |
| `delegate_stop` | owner, job_id | Stop verified owned processes and retain work and evidence |
| `delegate_accept` | owner, job_id, expected_round, notes, evidence_dir | Record independent review, close the SDK connection, release the checkout |

Use the caller's actual task ID as `owner` (for example its `CODEX_THREAD_ID`), not a value fixed when the shared MCP server starts. Ownership is a local coordination rule, not a security boundary against another process running as the same OS user.

Create a new `request_id` for each new start/revise. If the response is lost, retry the same ID and identical parameters. Receipts and the saved round both retain the request digest; reconnecting does not create another model call. Reusing an ID with changed content returns `request_conflict`. If the job was saved but launch failed, inspect it and explicitly recover rather than resubmitting new work.

`expected_round` rejects stale revisions and acceptance. Start wait with cursor `-1:0`, then reuse the returned cursor. Disconnecting or cancelling a wait does not stop the detached job. Recover with list/status before dispatching again. This does not guarantee waking an ended Codex task.

## Completion, hooks, and permissions

Claude's worker holds one `ClaudeSDKClient` connection between rounds. A native `result`, matching assistant model, session ID, and this round's native `effort=max` evidence establish the round boundary. The process can remain idle; no successful exit code is invented. CLI rounds still require process completion. `awaiting_review` is the handoff to independent review, not acceptance of the actual work.

Raw streams, hook events, errors, and native transcript indices stay in the shared default `${CODEX_HOME:-~/.codex}/claude-delegate` state root. Compact status exposes summaries and native token usage. Same-process continuation and restarted session recovery may both read cached input. Observed `cache_read_input_tokens` does not establish a subscription-quota savings percentage or guarantee future hits.

Task-scoped Stop, StopFailure, PostToolUseFailure, and idle Notification events are bound to the exact round through SDK callbacks. PreToolUse rejects Bash `run_in_background`. Global/project custom hooks are not automatically enabled: like the restricted CLI path, this adapter isolates task settings. Kimi retains its existing managed hook implementation.

Claude uses Read/Write/Edit/Bash/Glob/Grep with writes approved in the intended working directory and extra directories for reference reads. Bash needs narrowly authorized rules; these are not OS isolation and must not allow backgrounding or escaping the write scope. To change extra read directories, stop the existing job and use the retained CLI `revise --recover --read-dir` to rebuild its SDK connection.

At observed 90% in any applicable native Claude quota window, the active round finishes and subsequent calls pause. Missing, stale, or not-yet-refreshed quota remains unknown. Do not change state roots to bypass this guard.

## Validation and maintenance

The offline test suite uses the actual pinned SDK with a protocol fake CLI, plus a real MCP client/server connection. It covers same-process rounds, stop/resume, reconnect and request deduplication, owner and round checks, quota pausing, hooks, launch failure, and timeout. Process identity tests need permission to inspect local processes. See the repository README for all test commands.

A separate macOS run on 2026-09-12 used Claude Code 2.1.261 and `claude-opus-5/max`: two rounds shared a process; a third resumed the same session after stopping and starting a new process. Conversation context, file output, native verification, Stop events, and cache reads were observed. This was an implementation smoke test, not a public fixture or proof of another installation. Linux/Windows local MCP/SDK execution and deliberately induced provider outages are not validated. Other SDK hooks and all permission-denial cases were not separately exercised against the real provider in that run.

Dependencies are pinned to `claude-agent-sdk==0.2.152` and `mcp==2.2.0`. The adapter subclasses one internal SDK subprocess transport to retain raw events and actual process identity. Upgrading requires rechecking protocol behavior, hooks, evidence correspondence, and process cleanup. Kimi/OpenCode retain their CLI adapters; Pi, ACP, and OpenCode Server are not implemented.
