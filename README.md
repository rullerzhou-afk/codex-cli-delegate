# Codex CLI Delegate

[中文说明](README.zh-CN.md)

A community Codex skill for delegating coding and review work to **Claude Code, Kimi Code, and OpenCode**, with same-session revisions and independent acceptance by Codex.

It also observes the completion of an exact **remote Windows Codex CLI turn** over SSH. Remote observation and local delegation are separate capabilities.

## What it does

- Owns jobs by Codex task, locks checkouts, and resumes the saved backend session for revisions.
- Observes hooks and structured progress without repeated model calls to check status.
- Verifies native session, model, effort, completion, and formal output before handing work back for review.
- Retains local evidence and supports recovery and conservative process stopping.
- Pauses future Claude calls at observed 90% account quota while allowing an active round to finish.

`awaiting_review` means the execution evidence passed checks. Codex still needs to inspect the actual work before `accept`.

## Supported scope

| Capability | Current scope |
| --- | --- |
| Local Claude / Kimi / OpenCode delegation | Validated on macOS; not a supported Windows local runner |
| Remote Codex observation | macOS observer → existing Windows SSH host with Node.js and PowerShell |
| OpenCode profile | CLI 1.18.30; `deepseek/deepseek-flash`, variant `high` |
| Claude profile | `claude-opus-5`, effort `max`; restricted settings require CLI 2.1.248+ |
| Kimi profile | `kimi-code/k3-256k`, effort `max`; existing thinking settings and managed hooks required |

Profiles are fixed and verified by the adapters. This release does not expose arbitrary model selection. New CLI versions may need adapter changes; OpenCode explicitly rejects versions other than the verified one.

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

The **whole `skill/` folder** is required. Python 3 and the selected CLI must already be installed and authenticated. Node.js is needed for remote observation and the JavaScript tests. This repository does not install model clients, copy login credentials, or configure providers.

For Kimi, read [the setup reference](skill/references/kimi.md) before installing its three managed hooks from the final installed path. OpenCode uses per-process plugin configuration and preserves existing user plugins; Claude uses per-round settings.

Invoke `$codex-cli-delegate` in Codex, for example:

> Use $codex-cli-delegate to ask OpenCode to review this patch. Keep it read-only, wait for the result, and independently check each finding.

The public entry point is `scripts/delegate.py`. The internal `claude_task.py` module and `~/.codex/claude-delegate` state directory retain their legacy names for lock, hook, and recovery compatibility. Existing private installations are not automatically migrated. Do not enable two overlapping skill copies unintentionally or move a running job's state directory.

### Moving or renaming an existing installation

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

Completion hooks and idle events are notifications, not proof of success. The worker checks native records after process exit. Current-task waiting does not promise to wake an ended Codex task or operate while the desktop application is closed.

Quota monitoring uses Claude's native account windows. Missing or stale data remains unknown; this is not a guaranteed hard spending cap. Kimi and DeepSeek account quotas are not monitored.

Task prompts, CLI streams, credentials, configuration, and task evidence are **not part of this repository**. Exported evidence contains visible model responses and provenance, not private reasoning; review it before sharing. Local routing and file hashes are not a defense against a malicious process running as the same user.

## Tests

These checks use local fixtures and fake CLIs, without calling a paid model. `DELEGATE_SCRIPT` points to the implementation module because some tests inspect its internals; user commands use `delegate.py`:

```sh
export DELEGATE_SCRIPT="$PWD/skill/scripts/claude_task.py"
export PYTHONDONTWRITEBYTECODE=1
python3 -m unittest discover -s work/skill-verification -p 'test_*.py'
python3 -m unittest discover -s work/skill-v2 -p 'test_*.py'
python3 -m unittest discover -s work/skill-kimi -p 'test_*.py'
python3 -m unittest discover -s work/skill-kimi-hooks -p 'test_*.py'
python3 -m unittest discover -s work/skill-evidence -p 'test_*.py'
python3 -m unittest discover -s skill/tests -p 'test_*.py'
node --test skill/tests/test_remote_codex.cjs skill/tests/test_opencode_hook.mjs
```

The initial public version passed 131 Python tests and 11 JavaScript tests. Process-identity tests require permission to inspect local processes; restricted environments can produce false failures.

This project is derived from an implementation exercised with real macOS read/write, same-session revision, and hook tests. Those private runs are not portable proof of other machines or later CLI versions. Windows local execution and deliberately induced provider outages have not been validated. Detailed backend references currently include Chinese operating notes.

## Origins and license

Created and maintained by Ruller_Lulu. Event mapping, plugin coexistence, and exact-turn observation patterns were developed alongside [clawd-on-desk](https://github.com/rullerzhou-afk/clawd-on-desk); that desktop application is not required.

MIT licensed. See [LICENSE](LICENSE). This is an independent community project, not an official product or endorsement from OpenAI, Anthropic, Moonshot AI, DeepSeek, or OpenCode.
