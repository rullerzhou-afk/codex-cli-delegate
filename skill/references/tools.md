# Tool capabilities

Run `scripts/delegate.py capabilities` to list supported names without a model call. CLI and MCP share one catalog; unknown selections fail before launching a model. Kimi/OpenCode dispatch/status `tools` describes the configured selection; native records establish actual availability and use.

| Task | Claude | Kimi | OpenCode |
| --- | --- | --- | --- |
| Text | Read / Glob / Grep | Read / Glob / Grep | read / glob / grep |
| Images/media | Read for images/PDF; extract video frames separately | ReadMediaFile for images/video; included in the default read-only set | read depends on model media support; the DeepSeek profile is not validated for vision |
| File changes | Write / Edit, scoped by checkout permissions | Explicit Write / Edit | edit covers write/apply_patch/multiedit |
| Notebooks | NotebookEdit uses Edit path permissions | Authorized scripts | Authorized scripts |
| Web | allow_tools: WebSearch, WebFetch(domain:example.com) | kimi_tools: WebSearch / FetchURL | opencode_tools: webfetch / websearch |
| Task lists | Tracked by the caller | TodoList | todowrite |
| Language services | Plugin LSP not integrated | No mapped tool | lsp, with an existing language server |

Explicit Kimi/OpenCode lists replace defaults; selection never implicitly adds shell, write, or network authority. For Kimi image-processing work, retain ReadMediaFile alongside Read/Glob/Grep and authorized Write/Edit/Bash. Pure visual inspection needs no Bash.

Kimi media depends on model vision and web search on a host provider. OpenCode websearch and lsp require the existing provider/configuration or language server. Merely accepting a tool name does not establish that dependency. Claude WebFetch uses an auxiliary model to extract page content; it is not a raw document fetch or evidence that all processing used the fixed primary model.

## Existing sessions

Claude can add web permissions through revise; its idle SDK connection refreshes while retaining the session. OpenCode rounds keep their saved selection. Kimi 0.42.0 cannot combine --agent-file with --session; saved sessions retain their bound tool profile. Installing this update does not add tools to old Kimi sessions. Do not edit private session storage or claim a profile changed when it did not.

For old jobs lacking vision, Codex can perform the visual acceptance directly. If further delegated execution is necessary, preserve the old job and explicitly identify supplemental work; do not redo the whole task or call quantitative checks visual review. Normal corrections continue the original session.

MCP, child agents, background tasks, interactive plan approvals and scheduling need their own routing and lifecycle integration. They are not implicitly enabled by this catalog.

Sources: [Kimi tools](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/tools.html), [Claude tools](https://code.claude.com/docs/en/tools-reference), [OpenCode tools](https://opencode.ai/docs/tools/). Reviewed against local Kimi 0.42.0 on 2026-09-13.
