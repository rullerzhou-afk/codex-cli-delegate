# Codex CLI Delegate

[中文说明](README.zh-CN.md)

A community Codex skill for delegating coding and review work to **Claude Code, Kimi Code, and OpenCode**, through a local **MCP server**, with same-session revisions and independent acceptance by Codex. The skill provides operating rules; MCP provides eight delegation tools. The original CLI entry point remains available.

It also observes the completion of an exact **remote Windows Codex CLI turn** over SSH. Remote observation and local delegation are separate capabilities.

## What it does

- Keeps Claude Agent SDK sessions alive between revision rounds; resumes the saved native session after a worker restart.
- Adds authorized command rules and reference inputs to the same Claude job, refreshing an idle connection automatically. Accepted jobs can be continued with their prior acceptance preserved. Long paths and commas in command rules are supported.
- Defaults to no wall-clock termination; explicit runtime limits remain available, and old jobs can remove their limit during same-session recovery.
- Owns jobs by Codex task, locks checkouts, and deduplicates retried MCP requests. Kimi/OpenCode retain their native CLI adapters.
- Optionally sends macOS reminders and returns terminal results to the original Codex task for independent review, without model polling while waiting.
- Observes hooks and structured progress without repeated model calls to check status.
- Verifies native session, model, effort, completion, and formal output before handing work back for review.
- Retains local evidence and supports recovery and conservative process stopping.
- Pauses future Claude calls at observed 90% account quota while allowing an active round to finish.

`awaiting_review` means the execution evidence passed checks. Codex still needs to inspect the actual work before `accept`.

## When to delegate

This skill acts only on an explicit external route; a host routing skill still owns native-worker decisions. A new external job requires both an explicit request for Claude, Kimi, or OpenCode and one whole coherent responsibility — investigation, implementation, focused verification, and only the documentation that is tightly coupled — that can be transferred at acceptable coordination cost. Neither condition alone is enough. Continuing an external job this skill started, or recovering it after interruption, are allowed resolution paths that do not need a new explicit request.

An established user alias for a backend counts as naming that route. A named external route is fulfilled only by a job on the matching external backend; a native Codex worker cannot satisfy it, be reported as that backend, or receive a name, alias, translation, or variant implying that identity. If you explicitly name multiple external routes, each named route is dispatched with one coherent responsibility. Several read-only reviewers may inspect the same subject, but multiple workers must not write the same checkout. Those workers are requested participants, not extra reviewers added by default.

Do not start a new external job for native-worker-only requests, explicit solo work, casual explanations, tiny work, or work that is already nearly complete. Use one worker, forward new constraints promptly, and continue the same worker and job for rework instead of creating phase-named jobs. Independent Codex acceptance is mandatory; do not add a second external reviewer by default, and choose adversarial review only when you request it or the risk requires it. Worktrees and tool allowlists are specific controls, not an operating-system sandbox.

Without an explicit external route this skill does not claim routing precedence and does not start a new external job, but it still recovers and resolves jobs its tools previously started. In that no-route case, host routing may select a native worker. Once this skill starts an external job, its job, round, recovery, and acceptance rules apply through release of its reservation. If an explicitly requested external route is unavailable, report it and never silently substitute a route, model, or account, including by using a native worker. See [the delegation policy](skill/references/delegation-policy.md).

## Supported scope

| Capability | Current scope |
| --- | --- |
| Local Claude SDK / Kimi CLI / OpenCode CLI delegation | Validated on macOS; not a supported Windows local runner |
| Remote Codex observation | macOS observer → existing Windows SSH host with Node.js and PowerShell |
| OpenCode profile | Reference CLI 1.18.30; capability-checked versions; `deepseek/deepseek-flash`, variant `high` |
| Claude profile | `claude-opus-5`, effort `max`; restricted settings require CLI 2.1.248+ |
| Kimi profile | `kimi-code/k3-256k`, effort `max`; existing thinking settings and managed hooks required |

Profiles are fixed and verified by the adapters. This release does not expose arbitrary model selection. OpenCode records the actual version and checks required CLI options before starting; a different version alone does not block execution. Native session, model, effort, and completion verification remain mandatory after each round. Passing the option check is not proof that every behavior of a new release is compatible.

DeepSeek V4.1 Flash uses the official API name `deepseek-flash`. The provider currently routes its older V4 Flash alias to V4.1 temporarily. [Official announcement](https://deepseek.com/news/deepseek-v4-1-flash/).

## Install

Clone or download this repository, then run from its root:

```sh
skill_root="${CODEX_HOME:-$HOME/.codex}/skills"
skill_target="$skill_root/codex-cli-delegate"
if [ -e "$skill_target" ]; then
  echo "Existing installation found. Back it up and review changes before replacing it."
else
  mkdir -p "$skill_root"
  cp -R skill "$skill_target"
fi
```

The **whole `skill/` folder** is required. MCP/SDK needs **Python 3.12+** and the pinned dependencies; the original CLI path supports Python 3.9+ without them. The selected CLI must already be installed and authenticated. Node.js is needed for remote observation and the JavaScript tests. This repository does not install model clients, copy login credentials, or configure providers.

For Kimi, read [the setup reference](skill/references/kimi.md) before installing its three managed hooks from the final installed path. OpenCode uses per-process plugin configuration and preserves existing user plugins; Claude uses isolated task settings and task-scoped SDK hooks. Global/project custom hooks are not automatically inherited.

For MCP, create the environment at the final installed path and install the pinned packages:

```sh
skill_target="${CODEX_HOME:-$HOME/.codex}/skills/codex-cli-delegate"
python3.12 -m venv "$skill_target/.venv"
"$skill_target/.venv/bin/python" -m pip install -r "$skill_target/requirements.txt"
```

Then follow [MCP configuration and connection verification](skill/references/mcp.md#install-and-connect). The configuration must point to this environment and your existing Claude executable. Reload the connection and call `delegate_list` in the intended Codex task to verify availability.

Invoke `$codex-cli-delegate` in Codex, for example:

> Use $codex-cli-delegate to ask OpenCode to review this patch. Keep it read-only, wait for the result, and independently check each finding.

The MCP entry point is `scripts/delegate_mcp.py`; the retained public CLI entry point is `scripts/delegate.py`. The skill prefers MCP and routes CLI diagnostics to [the original workflow](skill/references/cli-workflow.md). The internal `claude_task.py` module and `~/.codex/claude-delegate` state directory retain their legacy names for lock, hook, and recovery compatibility. Existing private installations are not automatically migrated. Do not enable two overlapping skill copies unintentionally or move a running job's state directory.

### Run in the background and notify me

After dispatch or revision, call `delegate_notify` for the returned job and round. Once the detached watcher confirms readiness, Codex can end its turn; a local program watches completion and submits a macOS notification. For automatic continuation, pass `wake_codex=true`: one terminal message enters the original task through the official `codex queue` command. Codex checks current evidence, reviews independently, and re-arms after any same-session revision. `false` selects desktop-only reminders; omission preserves the round's setting, initially false. Waiting makes no model calls; resumed review consumes normal Codex usage.

The notification channel was visibly checked on macOS, separately from an SDK protocol-fake run that completed after its controller exited. An OS submission receipt alone does not prove a visible banner. Normal progress stays quiet; accepted or manually stopped work stays quiet. Notifications are opt-in and bound to one round. See [setup, script fallback, and delivery limits](skill/references/notifications.md).

The return path passed 31 notification checks and 15 MCP/SDK checks with local fixtures. One actual isolated terminal event was received in its original active Codex task, with a matching queue receipt. Idle-task delivery, closed-app operation, and reboot recovery were not exercised by that smoke check. This feature requires local Codex CLI support for `queue --thread --message`.

### Moving or renaming an existing installation

Before an in-place update, back up the skill/configuration. Existing workers and MCP servers may retain loaded code; for these additive revision changes, use the updated CLI fallback without interrupting unrelated jobs. Dependency or incompatible runtime updates require a planned drain of affected jobs. Create virtual environments at their final location; do not move an existing environment. Keep the shared job state and native sessions in place.

Kimi's managed hooks contain the absolute installation path. Wait for active Kimi rounds to finish and back up your Kimi configuration locally. Keep the old skill directory until migration is complete, then run:

```sh
python3 /absolute/old-skill/scripts/kimi_hooks.py remove
python3 /absolute/new-skill/scripts/kimi_hooks.py install
python3 /absolute/new-skill/scripts/kimi_hooks.py check
```

Use the same Kimi configuration directory for every command (`KIMI_CODE_HOME`, or append the same `--kimi-home /absolute/kimi-home`). Only retire the old copy after the new check passes. If the old path is gone, restore the matching old version there first. If a managed block was edited, inspect the differences against your backup; do not remove it solely because it has managed markers. Keep configuration backups private. This migration does not require moving job state.

The Kimi hook receiver requires a working `/usr/bin/python3`; verify it with `/usr/bin/python3 --version`. A Python executable elsewhere on PATH is insufficient. An existing Kimi configuration is required.

## Permissions and evidence

Default OpenCode/Kimi profiles are read-only. Opting into their Bash tool grants shell capability, not Claude-style command-pattern filtering. See the backend references before adding tools. This tool does not create an operating-system sandbox.

Completion hooks and idle events are notifications, not proof of success. The worker checks native records at the SDK result boundary or, for CLI rounds, after process exit. SDK acceptance closes the idle connection; waiting alone makes no new model calls. Automatic task continuation is a separate opt-in notification subscription, not a consequence of waiting or receiving a hook. Closed-app and reboot recovery are not guaranteed.

Quota monitoring uses Claude's native account windows. Missing or stale data remains unknown; this is not a guaranteed hard spending cap. Kimi and DeepSeek account quotas are not monitored.

Task prompts, CLI streams, credentials, configuration, and task evidence are **not part of this repository**. Exported evidence contains visible model responses and provenance, not private reasoning; review it before sharing. Local routing and file hashes are not a defense against a malicious process running as the same user.

## Tests

All checks use local fixtures and fake CLIs, without calling a paid model. Run the complete repository suite with one command and one machine-readable aggregate:

```sh
export CLAUDE_DELEGATE_PYTHON=/path/to/python3.12   # interpreter with the pinned dependencies
"$CLAUDE_DELEGATE_PYTHON" run_tests.py
```

`run_tests.py` runs every Python and JavaScript test and prints one JSON result (per-suite counts, JavaScript counts, and the exact process-identity modules included or excluded). Add `--json-out aggregate.json` to save it.

Current local aggregate: **243 Python and 11 JavaScript tests pass** with fixtures. This is the only current total; historical per-feature counts are in the [changelog](CHANGELOG.md).

The suite drives the public `scripts/delegate.py` and a real stdio `scripts/delegate_mcp.py`. The frozen black-box contract is described in [public contracts](docs/CONTRACTS.md) and the [freeze marker](work/skill-verification/BLACKBOX_FROZEN.md). Process-identity tests launch real detached workers and need permission to inspect local processes; CI runs portable Python/JavaScript fixtures on Linux and a separately labelled `macos-process-identity` job. That macOS job is the intended required check, but a workflow cannot enforce it: selecting it under branch protection or a ruleset is a maintainer action and is not configured or tested by this repository. Some older white-box tests still inspect `claude_task` internals and are expected to move with the remaining runtime refactor.

Module responsibilities, the provider-transport seam, and the phase roadmap (Phase 0–2 implemented; Phase 3–5 not started) are in [architecture](docs/ARCHITECTURE.md).

Evidence categories and unsupported claims are separated in [validation boundaries](docs/VALIDATION.md). Real macOS provider runs, notification checks, and known limits are recorded there; they are not portable proof of other machines or later CLI versions.

## Origins and license

Created and maintained by Ruller_Lulu. Event mapping, plugin coexistence, and exact-turn observation patterns were developed alongside [clawd-on-desk](https://github.com/rullerzhou-afk/clawd-on-desk); that desktop application is not required.

MIT licensed. See [LICENSE](LICENSE). This is an independent community project, not an official product or endorsement from OpenAI, Anthropic, Moonshot AI, DeepSeek, or OpenCode.

Historical per-feature results and the task-continuation `reasoning_extraction`
limit are recorded in the [changelog](CHANGELOG.md) and
[validation boundaries](docs/VALIDATION.md).

### Tool capability update (2026-09-13)

Kimi's default read-only selection includes ReadMediaFile for image/video input; WebSearch, FetchURL and TodoList are selectable. Claude supports NotebookEdit and explicitly authorized WebFetch/WebSearch. OpenCode adds webfetch/websearch/todowrite/lsp, with write/apply_patch aliases mapped to edit. CLI and MCP share one catalog; `scripts/delegate.py capabilities` lists it offline. See [capabilities and existing-session limits](skill/references/tools.md).

One real Kimi 0.42.0 task used ReadMediaFile, returned image content, and correctly identified a synthetic image's colors and shapes without Bash. New web/notebook/LSP tools have not been exercised against their real providers. Existing Kimi sessions retain their saved tool profile; this update does not change it.
