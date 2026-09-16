# Public contracts

This document records the observable contracts that were previously implicit:
job phases and their reservation/recovery semantics, the `CliError` code
vocabulary, persisted job/round state, and the evidence-manifest contract. It
deliberately does **not** repeat the MCP tool list or the timeout/wait tables;
those live in [MCP and persistent Claude sessions](../skill/references/mcp.md).

Schema namespaces are separate and independently versioned:

| Record | Namespace field | Namespace | Current version | Reader |
| --- | --- | --- | --- | --- |
| Job state (`job.json`) | `schema_namespace` | `codex-cli-delegate/job-state` | 1 | `claude_task.Context.load` |
| Evidence manifest (`provenance.json`) | `schema_namespace` | `codex-cli-delegate/evidence-manifest` | 1 | `review_evidence.verify` |

A change to one namespace never implies a change to the other.

## CLI route identity fields

Successful CLI `start` results and every entry in `list.jobs` include the
`backend` copied from persisted job state. `status` also includes that field.
Callers compare this saved value with the resolved route before reporting an
external identity; task labels and requested settings are not identity proof.
These additive response fields do not change either persisted schema.

New and current job-state writes persist `schema_namespace` alongside `schema`.
On read, a document that declares a different namespace fails closed with
`unsupported_schema`. The only tolerated absence is `schema_namespace` on the
legacy pre-namespace v1 shape, which still has `schema: 1`; it reads unchanged
and gains the namespace only if a later `save()` writes it. A missing `schema`,
or a supported version after v1 without a namespace, is refused.

## Job phases

| Phase | Reserves checkout | Busy | Recovery gate | Meaning |
| --- | --- | --- | --- | --- |
| `starting` | yes | yes | — | Job committed; launch transaction in progress |
| `running` | yes | yes | — | A worker owns the current round |
| `awaiting_review` | yes | no | none (plain `revise`) | Execution evidence passed; Codex must review independently |
| `failed` | yes | no | `--recover` | Evidence was finalized but verification demonstrably failed |
| `interrupted` | yes | no | `--recover` | No live process and no finalized result; evidence retained |
| `needs_attention` | yes | no | `--recover` | Evidence is ambiguous or cleanup/identity is uncertain |
| `stopped` | **no** | no | `--recover` | An explicit stop released the reservation; state and session retained |
| `accepted` | **no** | no | plain `revise` | Irreversible Codex decision; reservation released |

Reservation truth is `phase in RESERVING_PHASES`. The checkout is released only
by an explicit `accept` or an explicit, fully verified `stop`. `process_state`
(`starting`, `supervised`, `unverified`, `orphaned`, `unregistered`, `dead`,
`exited`, `stopped`, `stop_incomplete`, `accepted`) is diagnostic and never
grants or releases a reservation by itself.

### Recovery rules

- `reconcile` never relabels work done because a controller vanished, and never
  replays the original task. A worker that is gone without a finalized result
  becomes `interrupted`; an occupied-but-unverifiable process becomes
  `needs_attention` while the reservation is kept.
- `revise` on `failed`/`interrupted`/`needs_attention`/`stopped` requires
  `recover=true` (CLI `--recover`) and a prompt written after inspecting the
  evidence. It refuses while any recorded process is not provably gone
  (`live_process`).
- `stop` signals only a PID whose recorded start time and run token still
  re-verify. If any process cannot be confirmed gone, the phase stays/becomes
  `needs_attention`, `process_state=stop_incomplete`, and the reservation is
  retained.

### Accepted is terminal and reservation-free

`accepted` is irreversible: no worker, stop, or reconciliation path may
downgrade it, and it does not reserve the checkout. An accepted job may still
carry a cleanup `attention` block (for example `stop_incomplete` when a process
could not be confirmed stopped after acceptance); that attention is advisory and
does not change the phase. The MCP compact view reports this state as
`phase=accepted` with `next_action="done"`. Later cleanup uncertainty is
recorded in job/round state without changing `accepted`.

Same-session continuation after acceptance is allowed. It moves the accepted
record into `acceptance_history`, clears it from the active `accepted` field,
starts a new round, and again reserves the checkout for the new round.

## `CliError` code vocabulary

`CliError(code, message, **extra)` is the operator-visible failure type. The
public CLI prints `{"ok": false, "error": code, "message": message, ...extra}`
and exits non-zero; MCP returns the same object as a tool error. Machine callers
should switch on `error`, not on `message`.

### Input and addressing

| Code | Meaning |
| --- | --- |
| `no_owner`, `bad_owner` | Missing or malformed owner |
| `bad_id`, `bad_round`, `bad_task`, `bad_notes`, `bad_request_id` | Malformed identifier, round, task, notes, or request id |
| `bad_cwd`, `bad_file`, `bad_allow_tool`, `bad_timeout`, `bad_revisions`, `bad_wait`, `bad_cursor` | Malformed option or input |
| `bad_tools`, `unsupported_tools` | Unsupported tool selection |
| `unsupported_backend`, `wrong_backend` | Backend mismatch, including options for the wrong backend |
| `no_claude` | Claude executable not found or not executable |
| `required_file_unreadable`, `required_file_outside_scope` | Declared input cannot be read or is outside authorized scope |
| `kimi_prompt_size` | Kimi prompt exceeds the CLI argument limit |
| `bad_state`, `unsupported_schema` | Unreadable or unsupported persisted job state (fails closed) |

### Job, round, and concurrency

| Code | Meaning |
| --- | --- |
| `not_found`, `forbidden` | Unknown job, or job not owned by the caller |
| `busy` | Job still running; wait or stop first |
| `stale_round` | Expected round differs from the job's current round |
| `checkout_conflict`, `cwd_busy` | Another unfinished job holds the checkout |
| `needs_recover`, `bad_phase` | Phase transition is not allowed |
| `revision_limit` | No correction calls remain |
| `already_accepted` | Job is already accepted |
| `unverified` | Accept refused because model/effort verification did not pass |
| `live_process`, `processes_not_gone` | An earlier process is not provably gone |
| `lock_timeout` | State lock was busy past the timeout |
| `unsafe_path` | Symlinked or escaping job path |
| `worker_spawn_failed`, `worker_exception` | Worker could not start or crashed defensively |

### Evidence, quota, and SDK

| Code | Meaning |
| --- | --- |
| `evidence_missing`, `evidence_changed`, `evidence_arguments` | Missing, mutated, or misused evidence |
| `invalid_evidence`, `invalid_review_evidence`, `not_revalidatable` | Evidence package or revalidation refused |
| `ambiguous_transcript` | Transcript lookup could not establish a unique ground truth |
| `quota_paused` | A Claude quota window is at/over 90%; the active round may finish |
| `account_context_changed` | Legacy job's Claude config environment changed |
| `sdk_missing`, `sdk_unavailable`, `sdk_version`, `sdk_identity`, `sdk_missing_result`, `sdk_close_incomplete`, `sdk_scope_changed` | SDK transport preflight, identity, result, or scope problems |
| `kimi_platform`, `kimi_permissions`, `kimi_missing`, `kimi_effort_config`, `kimi_hooks_missing`, `kimi_session_missing`, `kimi_session_ambiguous` | Kimi CLI adapter preflight, permissions, hooks, and resume failures |
| `opencode_platform`, `opencode_permissions`, `opencode_missing`, `opencode_probe`, `opencode_incompatible`, `opencode_session_missing` | OpenCode CLI adapter preflight, probe, compatibility, and resume failures |
| `claim_consumed`, `stale_worker`, `stopped_before_launch` | Private worker claim/round guarding |
| `request_conflict` | A `request_id` was reused with different inputs |
| `bad_notification`, `notification_closed`, `notification_identity`, `notification_spawn_failed`, `notification_start_timeout`, `notification_unsupported` | Notification validation, watcher lifecycle, and platform support |
| `wake_target`, `wake_unavailable` | Codex queue return target/probe |

Process-level failures that are not `CliError`: `os_error` (unexpected OSError),
`interrupted` (Ctrl-C, exit 130), and worker `worker_exception` records.

## Persisted job state

`job.json` is written atomically with mode `0600`. Current top-level fields
(`schema=1`, `schema_namespace=codex-cli-delegate/job-state`):

`schema`, `schema_namespace`, `job_id`, `owner`, `cwd`, `reservation_key`, `session_id`, `phase`,
`created_at`, `updated_at`, `timeout`, `max_revisions`, `revisions_used`,
`current_round`, `backend`, `transport`, `model`, `effort`, `claude_bin`,
`allow_tools`, `read_dirs`, `required_files`, `claude_config_dir`,
`claude_config_env`, `stop_requested`, `process_state`, `cli_version`,
`final_report`, `rounds`, plus optional `attention`, `accepted`,
`acceptance_history`, `stopped`, backend tool fields, SDK worker fields, and
`opencode_version`/`opencode_compatibility`.

Each entry in `rounds` begins as `claude_task.new_round(...)` and gains
lifecycle fields as it runs. Current round fields include: `round`, `kind`,
`run_token`, `launch_claim`, `claim_consumed`, `prompt`, `prompt_sha256`,
`status`, `started_at`, `started_epoch`, `finished_at`, `exit_code`,
`finalized`, `evidence`, `prior_assistant_uuids`, `baseline_status`, `worker`,
`claude`, `claude_launch_pending`, `verification`, `timeout`, `request`, and,
once finished, `duration_s`, `timed_out`, `evidence_sha256`, `access`,
`continued_from`/`recovered_from`, optional `revalidations`, and native-session
verification fields for CLI backends.

Compatibility fixtures for an existing `awaiting_review` job and for an
`accepted` job (with cleanup attention) are in
`work/skill-verification/fixtures/`. They load without rewrite, and unknown
versions fail closed.

### Migration policy

Phase 0's addition of the `schema_namespace` metadata field is the one
deliberately additive exception: it is tolerated as absent on the legacy v1
shape, needs no version bump, and is persisted by current writers. It is not a
precedent for changing other persisted fields.

1. Any change to a persisted job-state *field or meaning* beyond that additive
   metadata requires an `N -> N+1` entry in
   `delegate_job_store.JOB_STATE_MIGRATIONS` and bumped
   `delegate_job_store.JOB_STATE_SCHEMA_VERSION`/
   `delegate_job_store.JOB_STATE_SUPPORTED_VERSIONS` values.
2. A regenerated post-migration fixture must be committed alongside it; the
   migration itself must write the namespace so no supported version carries an
   absent namespace.
3. Migrations run on read, in memory; a read never rewrites the file. The next
   `save()` persists the migrated, current-shaped job.
4. A migration must return an object at exactly `N+1`; other output and migration
   exceptions fail as `bad_state`. A foreign namespace is rejected before a
   migration can run.
5. A missing or unsupported `schema` raises `unsupported_schema` (`namespace`,
   `supported` in `extra`). A foreign declared namespace raises it with
   `namespace`/`expected`; a supported version after legacy v1 that omits the
   namespace is also refused.

Direct reads and enumeration use the same gate. An unreadable, foreign, or
newer `job.json` stops reservation-sensitive listing and dispatch instead of
being ignored. Inspect it with a compatible version or restore it from a known
backup; do not delete state that may still represent an active reservation.
Quota history import is advisory: it skips unsupported job documents without
interpreting their fields.

## Evidence-manifest contract

`review_evidence.export` writes one directory containing `provenance.json`,
`prompt.md`, `INDEX.md`, and one `response-NNNN.md` per visible model response.
The manifest is created with `O_EXCL` at mode `0600`; an accepted export is
immutable and is never silently rewritten.

- `schema` (currently `1`) and `schema_namespace`
  (`codex-cli-delegate/evidence-manifest`).
- `sources[]` bind each exact visible response to its stream line, JSON pointer,
  byte offsets, and record digest.
- `files{}` seal every exported file with `sha256` and `bytes`.
- `source_stream` records the path, digest, byte count, and whether the seal is
  `round_finalization` or only `export_time_only`.
- `subject.origin` must be `declared_by_exporter`.

`review_evidence.verify` re-checks every seal and, when given the job, requires
the manifest metadata and responses to match the saved round and original
stream. A manifest with an unsupported/foreign schema version or namespace, a
missing field, a changed file, or a tampered index fails closed with
`EvidenceError` (`invalid_review_evidence` through the CLI). Legacy manifests
without `schema_namespace` are still readable; a different declared namespace is
refused.

An evidence-manifest change requires a new manifest schema version plus fixtures
proving supported older manifests remain readable or fail with the documented
compatibility result. Accepted manifests are never migrated in place.

## Frozen black-box suite

The public contract above is protected by
`work/skill-verification/test_blackbox_contract.py`, which drives
`skill/scripts/delegate.py` as a subprocess and `skill/scripts/delegate_mcp.py`
over a real MCP stdio connection and asserts only public JSON and on-disk state.
See [BLACKBOX_FROZEN.md](../work/skill-verification/BLACKBOX_FROZEN.md). It is
frozen during the Phase 2 runtime refactor.

## Tests

Run every Python and JavaScript test with one command:

```sh
python run_tests.py
```

`--exclude-process-identity` runs the portable fixture Python modules *and* the
portable JavaScript tests (the Linux fixtures job); `--only-process-identity`
runs only the process-identity Python modules and skips the portable JavaScript
(it already ran in the fixtures job), so the two CI jobs do not duplicate it.
Both emit one JSON aggregate and record exclusions explicitly. Missing test
runtimes are reported inside that aggregate rather than escaping as a traceback.

A suite subprocess's exit status participates in the verdict: a Python or
JavaScript suite that exits nonzero fails even if its captured text contains an
otherwise parseable `OK` or a passing TAP summary. In
`--only-process-identity`, any test that skips at runtime fails the mode with
`errors: ["process_identity_tests_skipped"]`; selected identity tests are never
silently skipped into a green claim. The `macos-process-identity` job is the
intended required macOS check, but a workflow cannot enforce that: selecting it
under branch protection or a ruleset is a maintainer action and is **NOT
TESTED** by this repo-only change.
