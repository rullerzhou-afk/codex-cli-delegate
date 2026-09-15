# Frozen black-box contract suite

`test_blackbox_contract.py` is the frozen Phase 0 black-box contract. It drives
only the public entry points:

- `skill/scripts/delegate.py` as a subprocess;
- `skill/scripts/delegate_mcp.py` over a real MCP stdio connection.

It asserts public JSON and on-disk state, never `claude_task` internals. The
older `test_contract.py` also exercises the lifecycle but reaches into saved
state; it is independent contract coverage, not the frozen black-box suite.

## Freeze rule (Phase 2)

During the Phase 2 runtime separation the frozen set is
`test_blackbox_contract.py`, its `fake_claude.py` and
`skill/tests/fake_sdk_cli.py` process fixtures, and the persisted contract files
under `fixtures/` that it reads. White-box tests may move with the
implementation, but if a refactor appears to require a change in this set, stop
and ship the public contract change as its own versioned change (with a schema
migration and regenerated fixtures) before continuing.

Adding new black-box coverage is allowed; weakening or deleting an assertion to
make a refactor pass is not.

## Process-identity marker

Modules that launch detached workers or observe local process identity declare
`PROCESS_IDENTITY = True`. CI excludes them from the portable fixture job and
runs them in the stable `macos-process-identity` job. That job is the intended
required check, but a workflow cannot enforce it: selecting it under branch
protection or a ruleset is a maintainer action and is not configured or tested
by this repo-only change. The repository test command reports which modules
were excluded rather than silently reporting green.
