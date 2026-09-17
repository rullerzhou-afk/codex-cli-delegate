# External delegation policy

This reference is the canonical policy for when and how this Skill hands work
to Claude, Kimi, OpenCode, or Pi, and how it coexists with host routing. The Skill
summarizes it in `SKILL.md`; the README states the user-visible version.

It is deliberately policy text, not a routing engine, scoring system, or
scheduler. There is no silent route or model fallback.

## Boundary

- This Skill applies only after one of its tools starts an external job. A host
  routing skill continues to own native-worker decisions.
- This repository does not proxy native workers, store their state, or become a
  general-purpose multi-agent platform.
- The coordinating Codex owns scope, integration, acceptance, and delivery.
- A native Codex worker and an external backend job are different execution
  identities. The coordinating Codex reports an external job from its saved
  backend and must not label or report any worker as a different route. Host-side
  native-worker names remain outside this repository's observation and control.

## Delegation gate

A new external job requires both of these together:

- An explicit request for Claude, Kimi, OpenCode, or Pi, by name or by invoking
  this Skill; and
- One whole coherent responsibility that can be transferred with acceptable
  coordination cost. That responsibility should include investigation,
  implementation, focused verification, and the necessary documentation when
  those parts are tightly coupled.

Neither condition alone opens the gate. Continuation and recovery of an
external job this Skill already started are allowed resolution paths that do
not need a new explicit request. The negative triggers below still stop a new
dispatch.

An established user alias for Claude, Kimi, OpenCode, or Pi is equivalent to
naming that canonical external route. Resolve every name and alias to its
canonical backend before dispatch. For one responsibility, merge repeated
labels that resolve to the same backend and start at most one job per distinct
canonical backend. An unresolved alias does not open the delegation gate.

A named external route is satisfied only by a job whose saved backend matches
that route. Before reporting that the route was started or satisfied, read the
saved backend from the start record or `delegate_status` and compare it with the
resolved route. Report model and effort as verified only after native completion
verification. The coordinating Codex must not label or report any worker as an
external backend unless that saved backend matches; a native worker has no
matching external job and cannot satisfy the route.

## Negative triggers (do not delegate)

Do not start a new external job when any of these holds:

- The request is for a native worker only, or the user relies on host Codex
  worker routing.
- The user asked for solo work, or to handle it without an external worker.
- It is a casual explanation or question, not a delegated responsibility.
- The work is tiny.
- The work is already nearly complete.

Negative triggers stop a new dispatch. They do not stop recovery or resolution
of an external job this Skill already started.

## One responsibility, one worker

- Use one external worker for tightly coupled investigation, implementation,
  focused verification, and necessary documentation.
- Forward new constraints to an active worker promptly, instead of waiting for
  the next round.
- Continue the same worker and job for rework. Do not create phase-named
  workers or phase-named jobs.
- Every job that has not released its reservation occupies its checkout,
  including read-only jobs and jobs awaiting review or recovery. The same
  checkout and nested paths conflict. Concurrent jobs require distinct,
  non-overlapping worktrees or clones. Do not use a different state root to
  bypass this reservation: it does not remove the reservation requirement and
  it prevents conflict detection across state roots.

## Independent acceptance, not a second reviewer

- Independent acceptance by Codex remains mandatory. A worker's final message
  is not acceptance.
- Do not add a second external reviewer by default.
- Select adversarial review only when the user requests it or the risk of the
  change requires it.
- If the user explicitly names multiple distinct canonical external routes,
  dispatch one matching job per backend. They are user-requested participants,
  not reviewers added by default. Give each job one self-contained
  responsibility. Several read-only reviewers may inspect the same subject,
  but concurrent jobs still require distinct non-overlapping worktrees or
  clones because reservations apply regardless of tool profile.

## Controls, not a sandbox

Worktrees, restricted settings, and tool allowlists are specific controls.
They are not an operating-system sandbox and are not proof of one. Check the
actual modified paths and artifacts.

## Co-installation precedence

- Without an explicit external route, this Skill does not claim routing
  precedence and does not start a new external job.
- It must still recover and resolve external jobs its tools previously started.
- When no external route has been named, a host routing skill may select a
  native worker; this repository neither observes nor controls that worker.
- Once this Skill starts an external job, its job, round, recovery, and
  acceptance rules apply through release of its reservation.
- If an explicitly requested external route is unavailable, report it; do not
  silently substitute a route, model, or account, including by using a native
  worker.
- If one of several requested routes is unavailable, continue only the other
  requested routes whose jobs pass the gate, report the unavailable route, and
  do not replace it.

## Behavior scenarios

These are human-observation aids, not automated proof and not a substitute for
the contract checks:

- A user asks to have OpenCode review a patch: delegate to OpenCode with a
  read-only profile and review independently.
- A user asks Pi to inspect a repository: delegate to the Pi backend with its
  read-only profile, then verify the saved backend and exact OpenRouter model.
- A user names an established alias for OpenCode: resolve the alias and start
  an OpenCode backend job. A native worker with a similar task name is not that
  job and must not be presented as the requested route.
- A user names OpenCode and two aliases for it for one review: resolve and merge
  them, then start one OpenCode job rather than duplicate jobs.
- A user explicitly asks both Claude and OpenCode to review: start one matching
  external job for each named route in distinct non-overlapping checkouts, then
  confirm each saved backend and independently assess both results.
- A user asks Codex to explain a function: answer directly; do not delegate.
- A user asks for native-worker routing: leave it to the host; do not start an
  external job.
- After an external job is interrupted and the context is lost: list and
  recover that job; do not start a parallel one.

The scenarios and the checked policy text are not proof of stable model routing
or quota savings. They only keep the stated triggers and precedence present.
