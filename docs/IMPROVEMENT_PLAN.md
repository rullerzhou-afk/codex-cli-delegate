# Improvement Plan: External Delegation Policy, Runtime Boundaries, and ACP Evaluation

Status: revised proposal after adversarial review

Date: 2026-09-15

## Objective

Make `codex-cli-delegate` easier to understand, test, and extend while preserving the behavior that distinguishes it from a generic agent launcher:

- exact job ownership, request deduplication, and round checks;
- same-session revision with no fixed revision limit;
- checkout reservations, explicit cleanup evidence, and refusal to signal a process whose identity cannot be re-verified;
- fixed provider profiles with native completion evidence;
- Claude quota pausing at 90%, after the active round finishes;
- background notification, optional Codex queue return, and independent Codex acceptance.

The plan borrows external-delegation principles from Codex Worker Routing and evaluates `acpx` as a transport dependency. Native Codex worker routing remains a host concern. This repository will not proxy native workers, store their state, or become a general-purpose multi-agent platform.

## Decisions to preserve

1. The coordinating Codex owns scope, integration, acceptance, and delivery.
2. An external delegate receives one coherent responsibility. The coordinator does not duplicate the same investigation or implementation while that delegate is active.
3. Corrections continue the same job and native session when recovery evidence permits it.
4. One writer owns an overlapping checkout at a time. Ports, databases, services, and output directories remain task-level coordination concerns unless a later proposal adds an explicit reservation contract.
5. User authorization, data-transfer boundaries, and provider cost remain explicit. There is no silent route or model fallback.
6. Native output, model identity, session identity, round identity, and completion state are evidence inputs. A worker's final message is not acceptance.
7. Waiting and notifications do not make model calls. Retry after an uncertain request reuses the same request ID and parameters.

## Target boundaries

The system should have four visible layers:

1. **External-delegation policy** — the Skill describes when and how to hand a coherent responsibility to Claude, Kimi, or OpenCode. Direct work and host-provided native worker selection remain outside this repository. Explicit user instructions select the route. Without an explicit external-route instruction, this Skill does not claim routing precedence; it applies only after its tools start an external job. Any installed host routing skill continues to own native-worker decisions.
2. **Orchestration** — provider-independent job ownership, rounds, request deduplication, checkout reservations, quota gates, recovery, notification subscriptions, and acceptance records.
3. **Transport adapters** — Claude Agent SDK, current Kimi/OpenCode CLI adapters, and an optional experimental ACP adapter. A transport reports events; it does not decide acceptance or bypass orchestration policy.
4. **Evidence normalization** — provider-native records are preserved, then summarized into one versioned completion record without erasing provenance.

The CLI and MCP entry points remain thin interfaces over the same orchestration core.

## Phase 0: Freeze the observable contract

Before restructuring code:

- document only the currently missing public contracts: job phases and their reservation/recovery semantics, the `CliError` code vocabulary, persisted job/round fields, and the evidence-manifest contract; link to the existing MCP reference instead of duplicating its tool and timeout tables. State explicitly that an accepted job remains terminal and reservation-free while still being allowed to carry cleanup attention details with `next_action=done`;
- give job state and evidence manifests separate schema namespaces. Make the job-state `schema` effective on read, define its supported range and migration registry, and retain the manifest's existing fail-closed read check with an explicit supported-version list. Accepted evidence manifests remain immutable and are never silently rewritten;
- add fixtures for existing and accepted jobs at job schema N and for evidence manifest schema N, including the current public shapes, and prove that they load without rewriting;
- require an N-to-N+1 migration entry and regenerated post-migration fixture before accepting a job-state contract change. An evidence-manifest change requires a new manifest schema version plus fixtures proving that supported older manifests remain readable or fail with the documented compatibility result;
- expand the existing subprocess CLI and real stdio MCP tests into a black-box contract suite that drives the public `scripts/delegate.py` and `scripts/delegate_mcp.py` entry points and asserts only public JSON and on-disk state;
- freeze that black-box suite during Phase 2; white-box tests may move with implementation, while a requested public contract change must stop the refactor and proceed as a separate versioned change;
- add one repository command that runs every Python and JavaScript check in the repository and emits one machine-readable total;
- keep historical per-feature test counts in the changelog rather than presenting them as the current total;
- add portable Python 3.12 fixture CI and a separate required macOS process-identity job. Sandboxed identity failures must be labelled and must not be silently skipped into a green identity claim;
- publish a validation document that separates fixture, real-provider, operating-system, notification, and unsupported evidence.

Exit gate: a clean checkout reproduces one documented fixture total; both schema N fixture families load unchanged; unknown schemas fail closed; job-state field changes require an explicit migration and fixtures; and evidence-manifest changes preserve immutable old evidence under a separately versioned reader contract.

## Phase 1: Tighten external-delegation policy

Update the Skill frontmatter, main instructions, and references together:

- state positive triggers for explicit Claude/Kimi/OpenCode delegation, continuation of an external job already started by this Skill, and recovery of that job after interruption or context loss;
- state negative triggers for native-worker-only requests, explicit solo work, casual explanations, tiny work, and work that is already nearly complete;
- delegate only when a whole responsibility can be transferred with acceptable coordination cost;
- use one external worker for investigation, implementation, focused verification, and necessary documentation when those parts are tightly coupled;
- forward new constraints to an active worker promptly;
- continue the same worker for rework instead of creating phase-named workers;
- avoid adding a second external reviewer by default; independent Codex acceptance remains mandatory, while adversarial review is selected when risk or the user requires it;
- treat worktrees and tool allowlists as specific controls, never as proof of an operating-system sandbox;
- document co-installation precedence: without an explicit external route this Skill does not claim routing precedence or start a new external job; it must still recover and resolve jobs its tools previously started. A host routing skill may select a native worker, but this repository neither observes nor controls that worker. Once this Skill starts an external job, its job, round, recovery, and acceptance rules apply through release of its reservation.

Behavior scenarios are human-observation aids, not proof of stable model routing or quota savings. Automated checks may verify that the trigger and precedence text remains present; they cannot prove a model will always make the desired choice.

Exit gate: documentation and checked trigger text cover explicit external delegation, same-worker continuation, post-interruption recovery and reservation release, solo and small-work exclusions, unavailable external routes, and coexistence with a native-worker routing skill without claiming unmeasured savings.

## Phase 2: Separate the current runtime by responsibility

Refactor incrementally behind the existing CLI, MCP schema, state directory, and command behavior:

- extract persisted job and round access from process control;
- isolate ownership, checkout reservation, and request-deduplication transactions;
- isolate process identity, stop, timeout, and recovery logic;
- isolate provider-neutral completion and acceptance records;
- keep provider parsing and invocation inside transport adapters;
- keep quota observation as an orchestration gate with Claude-specific evidence input;
- keep notification and Codex queue delivery outside model execution.

The Phase 0 black-box contract suite remains unchanged throughout this refactor. White-box tests may be relocated or rewritten as their implementation seams move. Each extraction must pass the frozen black-box suite, the relevant white-box tests, and old-state fixtures before the next extraction. Avoid a flag-day rewrite and avoid changing file formats merely to match a new module layout.

Exit gate: all existing public behavior is preserved, old jobs remain readable, and adding a fake transport no longer requires editing provider-independent lifecycle code.

## Phase 3: Run a bounded `acpx` transport evaluation

Treat `acpx` as an optional, exact-version dependency, not vendored source and not an immediate replacement. The pilot uses a project-local npm installation with a lockfile and integrity data. It never invokes `@latest`, never requires a global install, and leaves the current CLI adapter available when the dependency is absent.

The pilot is limited to Kimi and OpenCode. No Claude round may launch with the pilot state root, so the pilot cannot bypass Claude quota state. Pilot records and native evidence are preserved for review and removed only through a separate, explicit cleanup decision.

### Preconditions before a provider call

- record the exact `acpx` and adapter versions. Use acpx's mock-agent/conformance path first to retest issue #535 (a tool refusal can cancel the whole Codex turn) and issue #499 (ACP bridge, model, or MCP processes can accumulate across sequential use) without a paid model call;
- write a per-provider mapping table: current delegate control, ACP mechanism, lost semantics, native evidence source, and acpx exit-code mapping (`0/1/2/3/4/5/130`);
- reject any mapping that silently widens a permission or treats an ACP client-side cwd boundary as an operating-system sandbox;
- require the adapter to advertise the exact fixed model, expose a control that produces the required effort/variant in the native record, and expose the provider `agentSessionId` needed for native evidence lookup. A missing precondition records a negative result and stops that provider pilot before a paid model call;
- define process ownership before launch: this repository sends no direct operating-system signal unless it independently re-verifies the target identity at signal time. ACP cancellation may be requested only through the explicit session bound to the expected job and round; adapter-owned signalling remains unverified cleanup until process or lease evidence confirms the resulting state;
- use an explicit ACP session name derived from the delegate job ID for every command. Bare prompt auto-resume and cwd-walk session selection are forbidden;
- define lifecycle expectations and their cleanup timeouts in the adapter contract. An intentionally reusable queue owner may remain supervised while a job awaits review or revision. Stop and pre-terminal cleanup failures may move the job to `needs_attention`. Acceptance remains an irreversible terminal decision that releases the checkout: later cleanup uncertainty is recorded in job/round `process_state` and attention details without changing `accepted`. A verified gone state includes a recorded process that exited and a process that was never recorded; any other identity state remains unresolved.

### Pilot topology

Begin functional checks in one isolated pilot state root. All pilot jobs and both candidate transports share that root; do not create a separate state root per job. Then run an explicit coexistence scenario in a temporary Git repository:

- one current CLI job and one ACP job use distinct owners but the same pilot state root;
- overlapping-checkout reservation rejects the second writer;
- retrying the same request ID within one owner does not create a second provider turn, while distinct owners may correctly reuse the same request ID for independent jobs;
- each orchestration job maps to exactly one explicit ACP session and one provider-native session;
- cancellation and acceptance affect only the expected job and round;
- using the acpx mock agent, an out-of-band `--no-wait` prompt against a delegate-owned session does not advance the orchestration `current_round` or phase and is detected as unaccounted native activity during verification. Real provider pilots never submit an unaccounted prompt;
- the ACP internal queue never becomes an independent source of orchestration truth.

This exercises shared ownership without placing experimental records in the production state root.

### Kimi pilot

Use the existing authenticated `kimi acp` command. Verify:

- explicit session creation and same-session revision;
- working-directory boundaries and required input visibility;
- the written Kimi permission mapping, including partial denial;
- cancellation, timeout, disconnect, and recovery;
- structured output correspondence with Kimi's native session record;
- exact model and thinking-profile evidence;
- supervised idle state and verified cleanup after sequential, accepted, stopped, and interrupted jobs;
- coexistence with the existing managed Kimi hooks.

### OpenCode pilot

Run only after the Kimi pilot passes. Launch the already installed, capability-checked OpenCode executable. Do not use the acpx default `npx -y opencode-ai acp` command. Before the model call, require advertised support for `deepseek/deepseek-flash`, a setting that produces native variant `high`, and a provider `agentSessionId` that resolves to the corresponding OpenCode record. Verify the same lifecycle and permission cases as Kimi.

### Adoption gate

Before the pilot, inventory the Kimi/OpenCode code that owns process launch, session continuation, cancellation, and provider-version workarounds. Define a repeatable counting unit for provider-specific lifecycle branches and include dependency pinning, acpx upgrade checks, and the compatibility suite on the cost side. Adopt an ACP adapter only if the final production integration:

- preserves the written permission mapping and native evidence guarantees;
- keeps this repository as the sole owner of job, round, retry, acceptance, and cancellation decisions;
- maintains a one-to-one mapping among orchestration job, explicit ACP session, and native provider session;
- preserves the refuse-on-doubt rule for direct process signalling, binds protocol cancellation to the expected session and round, and passes sequential, denial, cancellation, acceptance, and abrupt-owner cleanup checks;
- produces a strict net reduction in owned provider-specific lifecycle branches after counting the ACP integration and its ongoing compatibility obligations, excluding tests and documentation, while native evidence readers may remain;
- pins an exact dependency and passes the compatibility suite for every proposed acpx upgrade.

The known second scheduler, permission-granularity mismatch, and idle queue-owner lifecycle are gate inputs, not later monitoring concerns. Keep the current CLI adapter as the supported path until all gates pass. Keep Claude on the current Agent SDK path. A failed evaluation is a valid result.

## Phase 4: Improve public operation and maintenance

- add `CHANGELOG.md`, release tags, and a provider compatibility table tied to observed evidence;
- assemble a read-only diagnostic command from existing checks: pinned SDK version, OpenCode executable/flag probe, Kimi managed-hook check, tool catalog, loaded MCP tools, quota freshness, and stale process identity;
- document threat boundaries for same-user processes, shell-capable tools, state files, hooks, ACP adapters, and exported evidence;
- make platform labels explicit: fixture-tested, real-provider-tested, notification-tested, and unsupported;
- if ACP is adopted, update every statement that currently says ACP is unimplemented or Kimi/OpenCode use only native CLI adapters;
- keep English canonical documentation and provide a concise Chinese operator guide for installation, recovery, and validation boundaries.

Exit gate: a new user can install, diagnose, run the repository fixture suite, understand which runtime claims are current, and remove the integration without reading implementation files.

## Phase 5: Port, then validate additional platforms

After the internal boundaries and CI are stable:

1. implement platform-dispatched process identity, locking, signalling, and executable discovery while preserving the refuse-on-doubt contract, then replace the current unconditional non-macOS refusal guards only for backends whose platform implementation exists;
2. add platform-specific fixture tests without changing support claims;
3. validate local Linux MCP and provider execution on a real Linux host;
4. validate local Windows process identity, cancellation, recovery, state locking, and provider execution on a real Windows host;
5. make desktop notifications optional and platform-specific while keeping job state and acceptance platform-neutral;
6. retain remote Windows Codex observation as a separate capability from local Windows delegation.

No platform moves from unsupported to supported based only on copied files or fixture tests.

## Remaining risks

- **Split session identity:** orchestration, ACP, and provider-native session IDs can refer to different conversations despite intended one-to-one mapping.
- **Evidence dilution:** normalized events can hide missing or contradictory native records.
- **Compatibility loss:** module cleanup can strand existing jobs, hooks, or state directories despite fixtures.
- **Routing overengineering:** a scoring or planner subsystem could cost more than the simple external-delegation decision it replaces.
- **Dependency churn:** a pre-1.0 transport can move maintenance work rather than remove it.
- **False portability:** green fixture tests do not establish real provider, GUI, notification, or operating-system behavior.

## Recommended delivery order

1. Schema read-check, migration policy, missing contract documentation, compatibility fixtures, frozen black-box tests, one-command checks, and CI.
2. External-delegation trigger and precedence update with bounded behavior scenarios.
3. Incremental runtime separation behind unchanged interfaces.
4. ACP version pin, provider permission/profile mapping, identity design, and no-call preflight.
5. Isolated Kimi ACP pilot plus shared-root coexistence scenarios inside the pilot environment.
6. Isolated OpenCode ACP pilot only if its exact profile and native session preconditions pass.
7. Opt-in ACP adapter only after the adoption gate passes.
8. Public maintenance improvements.
9. Platform implementation followed by real-host validation.

Every step leaves the existing supported path usable. If an ACP evaluation fails, retain the current adapter, preserve the evidence, and avoid adding a permanent abstraction with no demonstrated benefit.
