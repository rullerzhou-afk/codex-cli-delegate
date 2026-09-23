# Changelog

Historical per-feature test counts live here so the README can report one
current, unambiguous repository total. For the current total and the single
command that produces it, see [Tests](README.md#tests).

## Unreleased — Windows Kimi dispatch

- The remote Windows dispatcher now runs Kimi Code as well as Codex
  (`--agent kimi`). A site opts in with a `kimi` block: the Kimi home, the
  fixed `kimi-code/k3-256k` / `max` profile, and the tools a task may choose.
  Each task picks tools with `--kimi-tool`; the default is read-only (Read,
  ReadMediaFile, Glob, Grep). Kimi has no sandbox on Windows: Write, Edit, or
  Bash act with the remote user's rights, so a failed or timed-out write task
  always carries `partial_write_risk`.
- The runner starts the npm entry script with `node.exe` directly, never
  through `cmd.exe`, which would cut the `-p` task at 8191 characters. It writes
  a per-job agent profile and an empty skills directory. It requires the
  existing `[thinking]` section to be `enabled = true` / `effort = "max"`
  without changing it, enforces the policy's git-only rule itself, and caps
  task text plus marker at 24,000 characters.
- Verification has two layers. The runner finds the session through Kimi's own
  index by exact id and cwd, then checks the marker, model, effort, bound tools,
  ending, and final text. The Mac fetches `state.json` and the main
  `wire.jsonl` in digest-checked chunks and repeats the checks in Python through
  the macOS adapter's `check_turn`. That function was extracted from
  `kimi_backend.verify` without changing its rules.
- `revise` continues a verified Kimi job's native session as a linked job
  (`parent_job`, `round`), with the verified record as its baseline. A session
  continued elsewhere since then is refused (`kimi_session_changed`). A parent
  still awaiting review becomes `superseded`.
- 11 Python and 10 JavaScript fixture tests were added. Current aggregate:
  300 Python and 30 JavaScript tests pass (portable 215 + 30, process identity
  85).
- Live runs on Windows 11 with Kimi Code 0.42.0 are recorded in
  [validation](docs/VALIDATION.md): a read-only task, a same-session revision,
  a write task, and a Codex regression run after the runner change.

## Unreleased — task wording for the Codex backend

- `skill/references/codex.md` adds a "Writing the task" section. Task text from
  a model coordinator such as Claude Code can read as adversarial or emphatic,
  and a provider-side pre-classifier may treat adversarial wording as a request
  for offensive security work. The section keeps every technical requirement,
  replaces adversarial vocabulary with engineering terms, gives an output
  sentence for review tasks, and asks the coordinator to rewrite and revise the
  same job after a refusal instead of switching model or route. `SKILL.md`
  points to it.
- The table sets default wording, not a ceiling: a confirmed security finding
  keeps accurate security terms. A new policy-text test pins that clause, the
  review output sentence, and the no-substitution rule.
- `docs/VALIDATION.md` now reports the current totals; it still showed the
  counts from before the Codex backend. Current aggregate: 289 Python and 20
  JavaScript tests pass (portable 204 + 20, process identity 85).

## Unreleased — Claude profile moves to Opus 5.5

- The fixed Claude profile is now `claude-opus-5-5` / `max` instead of
  `claude-opus-5` / `max`. Effort is still passed explicitly, because Opus 5.5
  defaults to `medium`. Model and per-entry effort verification are unchanged.
- A Claude revision records the current fixed profile on the job. When that
  profile differs from the job's saved one, the job also moves to the
  currently resolved Claude Code CLI, because each job pins a versioned CLI
  path that may predate the model. An idle SDK connection is then refreshed
  before the same session continues, just as it is for authorized additions.
  Same-profile revisions keep the job's pinned CLI.
- The fake Claude CLIs now report the model they were launched with instead of
  a hard-coded one. One new SDK lifecycle test moves an old-profile job to the
  current model and CLI, then checks that a same-profile revision keeps it.
- The executing Claude Code CLI must know `claude-opus-5-5`; 2.1.280 is the
  first version observed with it. The profile has fixture coverage only; no
  real run yet.

## Unreleased — local Codex backend

- Added a native `codex` backend: a separate local `codex exec` session with
  the fixed `gpt-6-sol` / `xhigh` profile. It mainly serves non-Codex
  coordinators such as Claude Code; a coordinating Codex opens it only on an
  explicit request for a separate session.
- Rounds run `codex exec --json`; revisions run `codex exec resume <thread>`,
  so corrections continue the same native thread. The prompt travels on stdin
  behind a per-round marker. Delegated runs pass `--ignore-user-config
  --ignore-rules`, so no MCP servers (including this delegate), notify
  programs, hook trust or day-to-day approvals carry over; only the user's
  `respect_system_proxy` feature is mirrored. `CODEX_THREAD_ID` and
  `CODEX_SQLITE_HOME` are removed from the worker environment.
- The default executable is the ChatGPT desktop app's bundled runtime; `PATH`
  and `--codex-bin` are fallbacks and overrides. Preflight checks the `exec` /
  `exec resume` options and requires the CLI's own `debug models` catalog to
  offer the exact model at `xhigh`; there is no fallback.
- `codex_tools` selects the sandbox: `read` (default, read-only), `write`
  (workspace-write) and `network` (requires write). Approval is fixed at
  `never`. The additive MCP input enters request digests only for Codex
  requests.
- Completion requires exit 0 and exactly one thread and one turn in the JSON
  stream, ending in `turn.completed`. The native rollout, found by thread id
  across day folders, must show the marked turn with the requested model,
  effort, cwd, sandbox, network access and approval.
  `task_complete.last_agent_message`, the last streamed `agent_message` and
  the `-o` file must agree. Revisions also check the pre-revision rollout
  prefix digest, and a later foreign turn fails closed.
- Each round reports the native Codex rate-limit snapshot as `quota`
  (`at_or_above_90`); dispatch is not paused.
- Evidence export understands Codex `agent_message` items, and notifications
  name the backend `Codex`.
- Tests: 17 new Codex backend tests plus catalog coverage. One real macOS
  two-round smoke, driven from Claude Code with desktop runtime
  0.155.0-alpha.9.2, verified the profile, sandbox, same-thread resume, and
  process identity.

## Unreleased — bounded Windows Codex SSH dispatch

- Added a separate, policy-bound `remote_codex_task.py` entry point and a
  content-addressed Windows Node runner. This is a single Codex `exec` carrier,
  not a fifth Claude/Kimi/OpenCode/Pi transport and not a generic remote shell.
- Pinned existing SSH host aliases, cwd roots, `CODEX_HOME`, model, effort,
  sandbox, approval policy, native Windows sandbox implementation, finite
  timeout, prompt hash, owner, and request ID. Prompt/task values travel over
  stdin rather than the PowerShell command string.
- Added request dedupe, one-task-per-cwd locks, private bounded streams,
  heartbeat/receipt state, PID plus process-start identity, conservative
  carrier-loss reconciliation, and exact native session/turn/log verification.
  The existing read-only observer remains the final completion authority.
- Kept the SSH carrier alive for the whole model run after a real Windows probe
  showed detached children die when the SSH session closes. Mac sleep/network
  loss can therefore terminate the task and leave partial writes; this phase
  intentionally has no detach/reconnect, queue, multi-agent, or remote stop.
- Live Windows 11 / Codex CLI 0.155.0 validation exposed a broken elevated
  sandbox that silently produced read-only turns. Completion now fails on
  actual model/effort/sandbox/approval mismatch. A policy-pinned, per-invocation
  `unelevated` fallback completed an exact file-write proof without changing
  persistent Windows or proxy configuration.
- Added a tri-state Windows process probe, partial-write recovery on every
  write-capable failure, and explicit stale-lock reclaim gated by human
  inspection plus exact request/cwd/PID/start-time proof. A controlled carrier
  termination exercised the full wait window and exact-lock reclaim.
- Made the independent native-log observer verify effective sandbox and
  approval policy, reject ambiguous turns and remote terminal-state injection,
  and validate the local exec stream identity before review.
- Current aggregate: 270 Python and 20 JavaScript tests pass across the
  portable and macOS process-identity groups.

## Unreleased — Pi / OpenRouter Union Alpha backend

- Added `pi` as a fourth external backend through the existing transport seam.
  It uses Pi's non-interactive JSON mode, one isolated native session per job,
  exact same-session revisions, and stdin task delivery.
- Fixed the route to OpenRouter `stealth/union-alpha` with thinking `off`.
  Every start/revision checks Pi capabilities, OpenRouter auth, and exact model
  resolution; every completed round verifies native session/cwd, unchanged
  revision prefix, actual provider/model, effective thinking level, completion,
  and visible final-text correspondence. No route/model fallback is accepted.
- Delegated Pi runs disable extensions, skills, prompt templates, themes, and
  project context files. The default tool selection is read/grep/find/ls;
  write/edit and whole-shell tools require explicit selection.
- Added Pi to CLI/MCP, status, notifications, evidence export, English/Chinese
  policy, capability docs, public contracts, architecture, and validation
  boundaries. Existing jobs and schema version remain compatible.
- Real macOS smoke: Pi 0.85.1 completed two rounds in the same isolated native
  session through OpenRouter Union Alpha, with model/effort/session evidence
  passing both times. Future stealth-model availability and pricing remain
  outside this repository's guarantees.
- Follow-up hardening records Pi's Node process through Darwin kernel identity
  after its argv title changes, normalizes malformed auth output, binds
  current-round effort/tool/completion evidence, requires a session header for
  evidence export, removes blocking duplicate revision probes, and documents
  the actual project-trust and offline flag semantics. A follow-up resolves the
  launcher interpreter from its shebang and tests identity through the Pi
  transport itself.
- Historical Pi aggregate at that update: 257 Python and 11 JavaScript tests
  passed across the portable and macOS process-identity groups.

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
  compatibility branch outside the adapter. At that phase boundary the public
  CLI/MCP allowlist permitted Claude/Kimi/OpenCode; the later Pi update extends
  it through the same seam.
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
- Phase 2 itself preserved CLI/MCP JSON, tool schema, state directory, job/round
  shape, request receipts, profiles, permissions, quota, and error codes. A
  later additive route-identity fix exposes the saved `backend` in retained CLI
  `start`/`list` replies. Persisted fixtures and schemas remain unchanged; no
  schema bump. See [architecture](docs/ARCHITECTURE.md).
- Historical Phase 2 aggregate: 243 Python + 11 JavaScript tests passed locally
  with fixtures (no paid model call).

## Unreleased — Phase 1: tighten external-delegation policy

- Added the canonical [external delegation policy](skill/references/delegation-policy.md):
  positive and negative triggers, one-worker continuation, independent
  acceptance without a default second reviewer, controls-not-an-OS-sandbox, and
  co-installation precedence.
- Bound established user aliases to their canonical external backends at the
  Skill entry point. A native Codex worker cannot satisfy or impersonate a
  named external route; multiple explicitly named routes are each dispatched,
  while an unavailable route fails closed instead of falling back.
- Canonicalize and deduplicate route names before dispatch, require saved-backend
  and native-completion evidence before identity claims, align the distributed
  default prompt with the two-condition gate, and document that every active
  reservation needs a non-overlapping checkout even for read-only jobs.
- Updated the Skill frontmatter and main instructions, the retained CLI workflow
  reference, README, and README.zh-CN together for user-visible policy.
- Added durable policy-text contract checks. They keep the trigger and
  precedence text present; they do not prove stable model routing or quota
  savings, and they cannot verify the unavailable-route behavior end to end.
- Exposed the saved backend in retained CLI `start` and `list` results, clarified
  that custom state roots disable conflict detection across state roots rather
  than removing reservations, and pinned native model/effort verification across
  the Skill and both public READMEs.
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
