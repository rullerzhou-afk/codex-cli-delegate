# Pi and OpenRouter Union Alpha

The `pi` backend uses the installed Pi coding-agent CLI in non-interactive JSON
mode. It is a native CLI adapter, like Kimi/OpenCode; it does not use the Claude
Agent SDK and installs no hooks.

## Fixed route and prerequisites

- Provider: `openrouter`
- Model: `stealth/union-alpha`
- Saved profile identity: `openrouter/stealth/union-alpha`
- Thinking level: `off` (the model is published without reasoning support)
- Reference CLI: `@earendil-works/pi-coding-agent` 0.85.1. Other versions are
  capability-checked instead of rejected only for a different version string.

Install and authenticate Pi before delegation. The adapter checks
`pi auth check --provider openrouter --json --no-refresh` and then requires an
exact row for `openrouter stealth/union-alpha` from Pi's offline model list. It
does not edit `~/.pi/agent/settings.json`, `models.json`, credentials, or proxy
configuration. If the installed catalog does not contain Union Alpha, update
Pi's model catalog or add the exact model through Pi's documented custom-model
configuration, then confirm:

```json
{
  "providers": {
    "openrouter": {
      "models": [
        {
          "id": "stealth/union-alpha",
          "name": "Union Alpha",
          "api": "openai-completions",
          "reasoning": false,
          "input": ["text", "image"],
          "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
          "contextWindow": 262144,
          "maxTokens": 131072,
          "compat": {
            "supportsDeveloperRole": false,
            "thinkingFormat": "openrouter",
            "sendSessionAffinityHeaders": true
          }
        }
      ]
    }
  }
}
```

The zero-cost fields reflect the model metadata used for the 2026-09-17 smoke;
verify current OpenRouter pricing before adding them because the adapter checks
route identity, not billing. Merge this model entry into the existing
`~/.pi/agent/models.json`; do not replace unrelated providers or models. Then
confirm:

```sh
pi --offline --list-models stealth/union-alpha
```

The row must name both `openrouter` and `stealth/union-alpha`. A missing model,
missing authentication, or incompatible CLI fails before launch. The adapter
always supplies explicit provider/model flags and verifies the assistant's
actual native provider/model after exit, so Pi's configured default cannot
silently replace Union Alpha.

## Session and completion evidence

Each job receives a UUID before its first round and an isolated session
directory under the shared delegate state root. Round zero uses
`--session-id`; revisions use `--session` with the same UUID. The task is read
from the saved prompt on stdin rather than placed in the process command line.
A per-round session name and prompt marker bind process identity, JSON output,
and native session entries to the saved round.

The verifier requires all of the following after process exit:

- one matching JSON session header and native session header/cwd;
- an unchanged native-session prefix before every revision;
- exactly one current user turn carrying the round marker;
- assistant messages from `openrouter` / `stealth/union-alpha`;
- effective native `thinkingLevel=off` and no thinking content;
- a stopped final assistant message, `agent_end` without retry, and
  `agent_settled`;
- the same visible final text in Pi's JSON stream and native session.

Failure leaves the round unaccepted for inspection/recovery. `awaiting_review`
still requires Codex to inspect the actual files and tests independently.

## Tools and isolation boundary

The default selection is `read,grep,find,ls`. Add only the built-in tools the
task needs with repeated `--pi-tool` values or MCP `pi_tools`. `edit` and
`write` permit file changes; `bash` and `powershell` grant a whole shell, not a
command-pattern allowlist.

Delegated Pi processes pass `--no-extensions`, `--no-skills`,
`--no-prompt-templates`, `--no-themes`, `--no-context-files`, and
`--no-approve`. This narrows the loaded Pi surface and avoids project-local
customization; it is not an operating-system sandbox. The working checkout,
credentials, and the user's Pi configuration remain available to the process
under the OS user's permissions.

Pi/OpenRouter quota is not part of the Claude 90% quota gate. The advertised
availability and price of an OpenRouter stealth model may change; every new
start and revision repeats exact local auth/model preflight and each completed
round verifies its recorded native identity.
