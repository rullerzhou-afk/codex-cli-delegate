# Changelog

Historical per-feature test counts live here so the README can report one
current, unambiguous repository total. For the current total and the single
command that produces it, see [Tests](README.md#tests).

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
- Current aggregate: 234 Python + 11 JavaScript tests pass locally with
  fixtures (no paid model call).

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
