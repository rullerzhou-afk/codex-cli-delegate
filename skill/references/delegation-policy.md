# External delegation policy

This reference is the canonical policy for when and how this Skill hands work
to Claude, Kimi, or OpenCode, and how it coexists with host routing. The Skill
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
  identities. One cannot be reported as the other.

## Delegation gate

A new external job requires both of these together:

- An explicit request for Claude, Kimi, or OpenCode, by name or by invoking
  this Skill; and
- One whole coherent responsibility that can be transferred with acceptable
  coordination cost. That responsibility should include investigation,
  implementation, focused verification, and the necessary documentation when
  those parts are tightly coupled.

Neither condition alone opens the gate. Continuation and recovery of an
external job this Skill already started are allowed resolution paths that do
not need a new explicit request. The negative triggers below still stop a new
dispatch.

An established user alias for Claude, Kimi, or OpenCode is equivalent to
naming that canonical external route. Resolve the alias before dispatch. A
named external route is satisfied only by a job whose saved backend matches
that route; a native worker cannot satisfy it and must not be named or reported
as that external backend.

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
- One writer owns an overlapping checkout at a time.

## Independent acceptance, not a second reviewer

- Independent acceptance by Codex remains mandatory. A worker's final message
  is not acceptance.
- Do not add a second external reviewer by default.
- Select adversarial review only when the user requests it or the risk of the
  change requires it.
- If the user explicitly names multiple external routes, dispatch each named
  route. They are user-requested participants, not reviewers added by default.

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

## Behavior scenarios

These are human-observation aids, not automated proof and not a substitute for
the contract checks:

- A user asks to have OpenCode review a patch: delegate to OpenCode with a
  read-only profile and review independently.
- A user names an established alias for OpenCode: resolve the alias and start
  an OpenCode backend job. A native worker with a similar task name is not that
  job and must not be presented as the requested route.
- A user explicitly asks both Claude and OpenCode to review: start one matching
  external job for each named route, then independently assess both results.
- A user asks Codex to explain a function: answer directly; do not delegate.
- A user asks for native-worker routing: leave it to the host; do not start an
  external job.
- After an external job is interrupted and the context is lost: list and
  recover that job; do not start a parallel one.

The scenarios and the checked policy text are not proof of stable model routing
or quota savings. They only keep the stated triggers and precedence present.
