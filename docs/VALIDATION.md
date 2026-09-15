# Validation boundaries

This document separates what is actually evidenced from what is assumed. Keep
the categories distinct: passing one does not imply any other.

## 1. Fixture evidence

- **What it proves:** the public JSON and on-disk contract behaves as specified
  with fake CLIs, the real pinned `claude-agent-sdk` driven by a protocol-fake
  CLI, and an in-process MCP client connected to a real stdio subprocess MCP
  server. It covers job lifecycle, ownership, round guards, checkout
  reservation, request deduplication, quota pausing, hooks, launch failure,
  timeout, schema compatibility/rejection, and evidence sealing.
- **Command:** `python run_tests.py` (see [CONTRACTS.md](CONTRACTS.md#tests)).
- **Current result:** 234 Python and 11 JavaScript tests pass locally against a
  fixture-only environment. No paid model is called.
- **Does not prove:** real provider behavior, model identity/effort enforcement
  against a live provider, OS sandboxing, GUI visibility, Linux/Windows
  execution, or quota accounting.

## 2. Real-provider evidence

These are separate, self-reported macOS runs, not a single matrix. Attribute
each claim to its backend; a passing run for one provider does not transfer to
another.

- **Claude Code:** one three-round macOS SDK smoke verified same-process
  continuation, same-session recovery after restart, context retention, Stop
  events, and cache reads; fixed `claude-opus-5`/`max` verification was met.
  Recorded in
  [MCP validation](../skill/references/mcp.md#validation-and-maintenance).
- **Kimi:** one real Kimi 0.42.0 task used `ReadMediaFile`, returned image
  content, and identified a synthetic image's colors and shapes without Bash.
  Managed hooks are part of the supported setup. Recorded in
  [tools](../skill/references/tools.md).
- **OpenCode:** the installed 1.18.30 executable passed the real option probe
  for `deepseek/deepseek-flash` variant `high`. Changed version strings and
  record structures were exercised with fixtures, not a real upgraded provider.
- **Known limits carried forward from those runs:** the Sept 13 task-
  continuation smoke passed the first real Claude round, then the provider
  rejected the second message (`reasoning_extraction`) before executing the
  newly authorized command. Real execution of the new rule and post-accept
  continuation remain **NOT TESTED**; the refusal was retained and the job
  stopped without switching model/account/session. Deliberately induced
  provider outages have not been validated.
- **Does not prove:** that any backend validated a capability listed only for a
  different backend; portability to other machines or later provider CLI
  versions; every permission-denial case; or future quota behavior.

## 3. Operating-system and process-identity evidence

- **What it proves:** on macOS, recorded process start time plus a unique run
  token are re-verified before any signal; `ps` failure is never read as "gone";
  identity mismatch/unverifiable refuses to signal and keeps the reservation.
- **CI:** the `python-3.12-fixtures` job runs portable fixture modules and the
  portable JavaScript tests, and explicitly records the excluded
  process-identity modules and that JavaScript was included. The stable
  `macos-process-identity` job runs the process-identity modules and skips the
  portable JavaScript (already run in the fixtures job). A process-identity
  module failure is a red job, never a silently skipped green claim.
- **Enforcement is NOT TESTED:** a workflow file cannot make a check required.
  The `macos-process-identity` job is the intended label; a maintainer must
  select it as a required status check in GitHub branch protection or a ruleset.
  This repo-only change does not configure that enforcement.
- **Does not prove:** Linux or Windows local process identity, cancellation,
  locking, or recovery. The current runtime remains macOS-first and refuses
  unverified signals everywhere.

## 4. Notification evidence

- **What it proves:** the notification channel was visibly checked on macOS,
  separately from an SDK protocol-fake run that completed after its controller
  exited. One actual isolated terminal event was received in its original active
  Codex task with a matching queue receipt. Notification fixtures submit to
  macOS; an OS submission receipt alone does not prove a visible banner.
- **Opt-in only:** notifications are bound to one round, disabled by default,
  and arming on non-macOS fails with `notification_unsupported`.
- **NOT TESTED:** idle-task delivery, closed-app operation, reboot recovery, and
  automatic continuation for every provider/route.

## 5. Unsupported claims

The following are **not** claimed by this repository:

- Operating-system sandboxing or isolation. Worktrees, tool allowlists, and
  `--restricted` are specific controls, not an OS boundary.
- A guaranteed hard spending cap. Quota monitoring is best-effort on Claude's
  native windows; missing/stale data is unknown, and Kimi/DeepSeek account
  quotas are not monitored.
- Linux or Windows local delegation, MCP/SDK execution, GUI behavior, or
  notification delivery.
- Stable model routing, trigger decisions, or quota savings measured from
  behavior scenarios.
- Any defense against a malicious process running as the same OS user; local
  routing and file hashes are not that.
- ACP, OpenCode Server, or Pi support. ACP is an evaluated future transport, not
  an implemented one.

## Aggregate report

`python run_tests.py --json-out aggregate.json` writes a machine-readable result
containing per-suite counts, the JavaScript result, and the exact modules
included/excluded by the process-identity mode. Portable JavaScript runs in the
default and `--exclude-process-identity` modes and is reported as
`excluded: true` (with its filenames) in `--only-process-identity`, which is the
process-identity Python-only job.

A suite subprocess's exit status is part of the verdict: a nonzero exit fails
the suite and the aggregate even if the captured output looks like `OK` or a
passing TAP summary. The `--only-process-identity` mode additionally fails with
`process_identity_tests_skipped` if any selected identity test skips at runtime,
so an unavailable platform or capability cannot become a silent green. Do not
report a passing `--exclude-process-identity` run as if the process-identity
modules also passed, or a passing `--only-process-identity` run as if the
JavaScript tests also passed.
