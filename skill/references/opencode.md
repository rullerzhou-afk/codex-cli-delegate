# DeepSeek：通过 OpenCode CLI 委派 DeepSeek

使用 `scripts/delegate.py start --backend opencode --cwd <实际目录> --prompt-file <任务书>`。当前已验证 OpenCode **1.18.30**、`deepseek/deepseek-flash`、variant `high`，这是本次验证的固定配置。复用本机登录，不能自动切回默认模型或其他供应商。1.18.30 是历史实测参考版本，不是版本白名单。启动前检查实际版本和 `run --help` 的所需参数；版本号变化本身不阻止派单。运行后继续核对 hooks、原生数据库和模型等证据。参数预检通过不代表所有行为已经验证。每轮记录启动前观察到的实际版本。

## 工具与权限

默认仅 `read/glob/grep`。编程任务按已授权范围显式重复传 `--opencode-tool`，例如三个读取工具再加 `--opencode-tool edit`。edit 涵盖 OpenCode 的 write/edit/apply_patch；`--opencode-tool bash` 是整个 Bash 工具权限，不等价于 Claude 的命令级允许规则，只在任务确实需要并已授权时给出。不能把 `--allow-tool` 传给 OpenCode，也不提供 `--auto` 或跳过权限参数。

每轮通过 `OPENCODE_CONFIG_CONTENT` 叠加本轮插件和专用 agent，默认拒绝未选工具、子代理、网络与外部目录；插件再次核对本轮工具选择。未知请求不自动批准。OpenCode 非交互 run 会拒绝待询问的权限请求，不能把拒绝误报为人工批准或自动重试。工具失败需检查原始记录和产物；最终文本并不证明工具全部成功。

任务在实际 `--cwd` 中运行，不创建 OS 沙箱。调用器同时设置 OpenCode `--dir` 与子进程 `PWD`，否则该版本会误用上层启动目录。文件准备由 Codex先检查，`--read-dir/--require-file` 暂只用于 Claude；OpenCode需要额外目录访问时先调整受控实现并验证，不能复制零散文件假装拥有完整项目。工具权限不是操作系统隔离；显式允许 Bash 后仍须核查改动范围。

## hooks 与完成判定

先检查过 Clawd 的 `hooks/opencode-plugin/index.mjs`、`hooks/opencode-family-plugin/core.mjs` 及当前 OpenCode 源码：沿用单导出插件、精确 session ID、主子会话隔离和权限事件只观察的原则。Clawd 的权限桥负责桌宠交互；此处不复制其桥接服务，也不向其发送权限回复。

`opencode_hook.mjs` 只通过本轮配置加入，用户的桌宠插件和永久配置保持原样。本轮环境带任务 token；`chat.message` 匹配派单标记后绑定 session，事件缺少 ID 或来自其他/子会话时忽略。记录 `session.idle/session.status`、`session.error`、`permission.asked/replied`、工具错误的有限元数据，不向状态写入思考或完整工具输出。

`idle/Stop` 仅是提示。进程退出后，对照本机 SQLite 只读会话记录确认实际工作目录、根会话、准确本轮 prompt、assistant parentID、模型、variant、完成状态以及正式文本与本轮 JSON 输出一致。返工使用保存的 `--session`，并排除返工前的消息；另一个用户消息混入时拒绝验收。会话缺失或证据冲突时保留 needs_attention，不另起任务冒充续接。

仍使用公共任务锁、owner、无限返工、超时、中断恢复、`await-event` 与 Codex独立验收。正式回复可通过现有 `evidence` 命令导出；原始思考不作为正式证据导出。DeepSeek 账户额度没有接入可靠来源，额度显示 unknown/null，不套用 Claude 的 90% 额度规则或把 token 数当额度。

官方参考：[插件事件](https://opencode.ai/docs/plugins/)、[CLI](https://opencode.ai/docs/cli/)、[本次核对的 run 实现](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/opencode/src/cli/cmd/run.ts)。

## V4.1 Flash model name

The official API name is `deepseek-flash`. The older `deepseek-v4-flash` alias is temporarily routed to V4.1 Flash; new jobs use the official alias. Existing jobs keep their recorded model. Source: https://deepseek.com/news/deepseek-v4-1-flash/

旧 MCP 连接若仍返回 `opencode_version` 的精确版本错误，检查原 job 后使用已更新 CLI 的同等入口；不能要求用户降级或重复派发已有工作。刷新连接后加载能力检查。

Additional optional tools: webfetch, websearch, todowrite, and lsp. Inputs write/apply_patch/multiedit normalize to edit permission, which permits all these file modifications. Existing provider/language-server requirements still apply; see [capabilities](tools.md).
