# Changelog

Historical per-feature test counts live here so the README can report one
current, unambiguous repository total. For the current total and the single
command that produces it, see [Tests](README.md#tests).

## Unreleased — Phase 2: separate the runtime by responsibility

- Extracted persisted job/round access and schema/migration into
  `delegate_job_store.py`, ownership/reservation/request-dedupe into
  `delegate_ownership.py`, process identity/termination into
  `delegate_process.py`, live-blockers/reconciliation into
  `delegate_recovery.py`, and completion/acceptance transitions into
  `delegate_completion.py`. A shared `delegate_core.CliError` keeps one error
  class across the `claude_task`/`__main__` instances.
- Added `delegate_transport.py`, the provider adapter seam. `cmd_start` and
  `cmd_revise` select provider start/revision preparation through the adapter,
  and the CLI `worker_run` dispatch is provider-neutral; a new adapter (or test
  fake) is registered without editing that dispatch. The Claude Agent SDK
  idle-connection refresh (`sdk_scope_changed`/`close_idle`) remains a disclosed
  compatibility branch outside the adapter. The public CLI/MCP allowlist still
  permits only Claude/Kimi/OpenCode.
- Moved the reusable process wait/timeout action into `delegate_process.py`, and
  the provider-neutral stop and acceptance transitions into
  `delegate_recovery.stop_transition` and
  `delegate_completion.apply_acceptance`; `cmd_stop`/`cmd_accept` are thin
  orchestration adapters. Added focused regression tests (never signals an
  arbitrary PID).
- `work/skill-verification/test_transport_seam.py` registers a fake adapter and
  drives `cmd_start` -> detached worker completion -> `cmd_accept` through the
  real provider-independent commands (the worker finds the fake via a test-side
  `sitecustomize`; no product plugin mechanism).
- Behavior-preserving: no CLI/MCP JSON, tool schema, state directory, job/round
  shape, request receipt, profile, permission, quota, or error-code change. The
  frozen black-box suite and old/current/accepted fixtures are unchanged; no
  schema bump. See [architecture](docs/ARCHITECTURE.md).
- Current aggregate: 243 Python + 11 JavaScript tests pass locally with
  fixtures (no paid model call).

## Unreleased — Phase 1: tighten external-delegation policy

- Added the canonical [external delegation policy](skill/references/delegation-policy.md):
  positive and negative triggers, one-worker continuation, independent
  acceptance without a default second reviewer, controls-not-an-OS-sandbox, and
  co-installation precedence.
- Updated the Skill frontmatter and main instructions, the retained CLI workflow
  reference, README, and README.zh-CN together for user-visible policy.
- Added durable policy-text contract checks. They keep the trigger and
  precedence text present; they do not prove stable model routing or quota
  savings, and they cannot verify the unavailable-route behavior end to end.
- Historical aggregate for this phase: 240 Python + 11 JavaScript tests passed
  locally with fixtures (no paid model call).

## Unreleased — Phase 0: freeze the observable contract

- Separate job-state (`codex-cli-delegate/job-state`) and evidence-manifest
  (`codex-cli-delegate/evidence-manifest`) schema namespaces, enforced on read
  with fail-closed unknown-version handling.
- Added the `JOB_STATE_MIGRATIONS` mechanism, current-shape fixtures for
  existing/accepted jobs and manifests, and compatibility/rejection tests.
- Added the frozen public black-box suite over `delegate.py` and real stdio
  `delegate_mcp.py`.
- Added `run_tests.py`, a single machine-readable Python + JavaScript aggregate.
- Added portable Python 3.12 fixture CI and a stable, separately labelled
  `macos-process-identity` job. That job is the intended check; selecting it
  under branch protection or a ruleset is a maintainer action and is not
  configured or tested by this repo-only change. Added
  [validation boundaries](docs/VALIDATION.md) and
  [public contracts](docs/CONTRACTS.md).

## Tool capability update (2026-09-13)

Kimi's default read-only selection includes `ReadMediaFile`; `WebSearch`,
`FetchURL`, and `TodoList` are selectable. Claude supports `NotebookEdit` and
explicitly authorized `WebFetch`/`WebSearch`. OpenCode adds
`webfetch`/`websearch`/`todowrite`/`lsp`, with write/apply_patch aliases mapped
to edit. CLI and MCP share one catalog; `scripts/delegate.py capabilities` lists
it offline.

Historical result: **43 focused local checks passed.** One real Kimi 0.42.0
task used `ReadMediaFile`, returned image content, and correctly identified a
synthetic image's colors and shapes without Bash. New web/notebook/LSP tools
were not exercised against their real providers.

## Task-continuation update (2026-09-13)

Historical result: **173 Python and 11 JavaScript checks passed with local
fixtures**, including MCP parameter transport, configuration refresh, preserved
session/acceptance history, checkout conflicts, and long/comma command rules.
The real Claude smoke passed the first round, then the provider rejected the
second message (`reasoning_extraction`) before executing the newly authorized
command. Real execution of the new rule and post-accept continuation remain
**NOT TESTED**; the refusal was retained and the test job stopped without
switching model/account/session to retry it.

## Version compatibility update

Historical result: **five targeted tests** were added. Its **23 OpenCode
tests**, **15 CLI contract tests**, and **11 JavaScript checks** passed. The
installed 1.18.30 executable also passed the real option probe; other version
strings and changed record structures were tested with fixtures, not real
upgraded provider runs.

## Notification return path

Historical result: **31 notification checks** and **15 MCP/SDK checks** passed
with local fixtures. One actual isolated terminal event was received in its
original active Codex task, with a matching queue receipt. Idle-task delivery,
closed-app operation, and reboot recovery were not exercised by that smoke
check.

## Earlier complete suite

Historical result: the previous complete suite passed **164 Python tests and 11
JavaScript tests**, including real MCP protocol connections and the pinned SDK
driven by a fake CLI.
