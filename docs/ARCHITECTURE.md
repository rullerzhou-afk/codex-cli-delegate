# Architecture

The runtime keeps one public surface (CLI `scripts/delegate.py`, MCP
`scripts/delegate_mcp.py`, state directory, job/round shapes) while separating
responsibilities behind small modules. `claude_task.py` remains the composition
root. Two distinct boundaries apply:

- **Read/import aliases remain** through `claude_task` for the extracted names.
  Composition-root callers still use its aliases for `identity_state`,
  `terminate_recorded`, `reconcile`, `launch_transaction`, `worker_run`,
  `finalize_worker_failure`, `build_claude_argv`, `verify_round`, and
  `subprocess`. Extracted module internals resolve their own names locally; for
  example, tests of `delegate_process.wait_with_timeout` patch
  `delegate_process.terminate_recorded`. `delegate_recovery` deliberately calls
  back through `claude_task.identity_state` for its retained orchestration patch
  point.
- **Schema policy is owned and mutated in `delegate_job_store`.** Its namespace/
  version constants and migration registry are defined there, so assigning
  `claude_task.JOB_STATE_MIGRATIONS`/`JOB_STATE_SUPPORTED_VERSIONS` is no longer
  an effective patch point; tests that exercise migrations mutate
  `delegate_job_store` directly. No proxy/metaprogramming is added to preserve
  that internal test mutation.

## Modules

| Module | Responsibility |
| --- | --- |
| `delegate_core.py` | Shared `CliError` type (one class across the `claude_task`/`__main__` instances) |
| `delegate_job_store.py` | Persisted job/round access, path layout, job-state schema + migration |
| `delegate_ownership.py` | Owner validation, checkout reservation/conflict, request-dedupe transaction |
| `delegate_process.py` | Process identity/liveness and conservative (Darwin refuse-on-doubt) termination |
| `delegate_recovery.py` | Live-process blockers, conservative reconciliation, process view |
| `delegate_completion.py` | Provider-neutral completion and acceptance record transitions |
| `delegate_transport.py` | Transport adapter registry plus Claude/Kimi/OpenCode adapters |
| `claude_task.py` | Composition root: CLI parser, public commands, worker lifecycle, Claude parsing/verification |
| `claude_sdk_backend.py` | Claude Agent SDK transport (its own persistent worker) |
| `kimi_backend.py`, `opencode_backend.py` | Native CLI provider adapters |
| `claude_events.py`, `claude_quota.py`, `delegate_notify.py` | Hooks, quota observation, notifications/queue return |
| `delegate_service.py`, `delegate_mcp.py` | MCP-facing application API and stdio entry point |
| `review_evidence.py` | Independent evidence-manifest contract |
| `remote_codex.py`, `remote_codex_agent.cjs` | Remote Windows Codex turn observation (separate capability) |

## Transport seam

`delegate_transport.Transport` is the seam between orchestration and a provider.
`cmd_start` and `cmd_revise` select provider start/revision preparation through
the adapter (`transport_preflight`, `start_config`, `revision_additions`,
`revision_config`, `revision_prompt_check`), and `claude_task.worker_run`
resolves the adapter for `prepare`, `launch_checks`, `launch_stamp`,
`capture_child`, and `verify`. Adding an adapter means subclassing `Transport`
and calling `register(...)`.

Scope of that claim: provider **start/revision preparation** and **CLI worker
dispatch** are adapter-driven. The Claude **Agent SDK** path keeps one
disclosed compatibility branch that is not yet routed through the adapter:
`revise_transaction` raises `sdk_scope_changed` for an idle SDK connection whose
authorized scope changed, and `cmd_revise`/`cmd_accept` call
`claude_sdk_backend.close_idle`. Moving that through the adapter is a remaining
refactor, not something this change claims.

`work/skill-verification/test_transport_seam.py` proves this: a registered fake
enters through the real `cmd_start`, its detached worker resolves the fake, and
Codex acceptance completes the job — without editing lifecycle dispatch. The
public CLI/MCP backend allowlist is separate and still permits only the three
supported providers.

Claude quota observation stays an adapter `launch_checks`/`revision_config`
gate, and notification/queue delivery stays outside model execution.

### Honest limitation: callback through `claude_task`

Adapters call back into `claude_task` and the provider modules through module
attributes (for example `verify_round`, `build_claude_argv`, `identity_state`)
so the existing white-box patch points keep working. That is a deliberate
compatibility seam, not a dependency inversion: the extracted lifecycle modules
still resolve `claude_task` by its canonical module name, so a worker running
`claude_task.py` as `__main__` shares the `delegate_core.CliError` class but is a
distinct module instance from the one the adapters import. Behavior is
identical; a future refactor that removes this callback would need to move the
affected functions and their patch points together.

The test-only fake registers in the worker subprocess through a test-side
`sitecustomize` on `PYTHONPATH`; no product dynamic-loading or plugin mechanism
exists.

## Phase roadmap status

| Phase | Status |
| --- | --- |
| Phase 0 — freeze the observable contract | Implemented and committed |
| Phase 1 — tighten external-delegation policy | Implemented and committed |
| Phase 2 — separate the current runtime by responsibility | Implemented (behavior-preserving; frozen black-box suite unchanged) |
| Phase 3 — bounded `acpx` transport evaluation | Planned / not started |
| Phase 4 — public operation and maintenance | Planned / not started |
| Phase 5 — port, then validate additional platforms | Planned / not started |

Phases 3–5 have not run: there has been no ACP pilot, no OpenCode Server or Pi
work, and no Linux/Windows implementation. The local runner remains validated on
macOS only.

## Compatibility

Phase 2 changes no CLI/MCP JSON, tool schema, state directory, job/round
persisted shape, request receipt format, provider profile, permission, quota, or
exit/error code. The frozen black-box suite
(`work/skill-verification/test_blackbox_contract.py`) is unchanged. Old,
current, and accepted job fixtures load unchanged; no schema version was bumped.
