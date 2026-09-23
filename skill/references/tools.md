# Tool capabilities

Run `scripts/delegate.py capabilities` to list supported names without a model call. CLI and MCP share one catalog; unknown selections fail before launching a model. Kimi/OpenCode/Pi dispatch/status `tools` describes the configured selection; native records establish actual availability and use.

| Task | Claude | Kimi | OpenCode | Pi | Codex |
| --- | --- | --- | --- | --- | --- |
| Text | Read / Glob / Grep | Read / Glob / Grep | read / glob / grep | read / grep / find / ls | sandboxed shell; default `read` (read-only sandbox) |
| Images/media | Read for images/PDF; extract video frames separately | ReadMediaFile for images/video; included in the default read-only set | read depends on model media support; the DeepSeek profile is not validated for vision | Union Alpha accepts images, but the delegated built-in read tool's media path is not yet validated | not validated for delegated runs |
| File changes | Write / Edit, scoped by checkout permissions | Explicit Write / Edit | edit covers write/apply_patch/multiedit | explicit write / edit | codex_tools: write (workspace-write; cwd and temporary directories only) |
| Notebooks | NotebookEdit uses Edit path permissions | Authorized scripts | Authorized scripts | authorized shell scripts | authorized shell scripts |
| Web | allow_tools: WebSearch, WebFetch(domain:example.com) | kimi_tools: WebSearch / FetchURL | opencode_tools: webfetch / websearch | no built-in web tool; authorize shell only when the task permits it | web search not wired in; codex_tools: network (requires write) opens sandbox network access |
| Task lists | Tracked by the caller | TodoList | todowrite | tracked by the caller | Codex's own plan tool |
| Language services | Plugin LSP not integrated | No mapped tool | lsp, with an existing language server | extensions disabled for delegated runs | not integrated |

Explicit Kimi/OpenCode/Pi lists replace defaults; selection never implicitly adds shell, write, or network authority. Codex always has a sandboxed shell, so `codex_tools` selects the sandbox rather than individual tools; approval is fixed at `never`. See [Codex](codex.md). For Kimi image-processing work, retain ReadMediaFile alongside Read/Glob/Grep and authorized Write/Edit/Bash. Pure visual inspection needs no Bash. Pi enables only selected built-in tools and disables extension discovery; adding Bash or PowerShell grants an unrestricted shell tool.

Kimi media depends on model vision and web search on a host provider. OpenCode websearch and lsp require the existing provider/configuration or language server. Merely accepting a tool name does not establish that dependency. Claude WebFetch uses an auxiliary model to extract page content; it is not a raw document fetch or evidence that all processing used the fixed primary model.

## Existing sessions

Claude can add web permissions through revise; its idle SDK connection refreshes while retaining the session. OpenCode and Pi rounds keep their saved selection. Kimi 0.42.0 cannot combine --agent-file with --session; saved sessions retain their bound tool profile. Installing this update does not add tools to old sessions. Do not edit private session storage or claim a profile changed when it did not.

For old jobs lacking vision, Codex can perform the visual acceptance directly. If further delegated execution is necessary, preserve the old job and explicitly identify supplemental work; do not redo the whole task or call quantitative checks visual review. Normal corrections continue the original session.

MCP, child agents, background tasks, interactive plan approvals and scheduling need their own routing and lifecycle integration. They are not implicitly enabled by this catalog.

Sources: [Kimi tools](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/tools.html), [Claude tools](https://code.claude.com/docs/en/tools-reference), [OpenCode tools](https://opencode.ai/docs/tools/), [Pi repository](https://github.com/earendil-works/pi). Reviewed against local Kimi 0.42.0 on 2026-09-13 and Pi 0.85.1 on 2026-09-17.
