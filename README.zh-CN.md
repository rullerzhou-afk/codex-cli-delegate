# Codex CLI Delegate

[English](README.md)

由 Codex 规划任务、通过本机 MCP 委派 Claude Code／Kimi Code／OpenCode 执行，并独立验收结果的社区 skill。Claude 使用 Agent SDK 保持连接，返工复用同一进程，停止后仍可恢复原会话；Kimi/OpenCode 保留原生 CLI 后端。包含同会话返工、hooks 通知、完成证据核验、额度预警与中断恢复，也支持观察 Windows 上准确的一轮远程 Codex CLI 工作。

## 安装和使用

从本仓库根目录，按 [英文 README 的安装步骤](README.md#install)，将完整 `skill/` 文件夹安装为 `${CODEX_HOME:-~/.codex}/skills/codex-cli-delegate`。已有版本先备份并检查差异，不直接覆盖。

MCP/SDK 需要 Python 3.12+ 和 `skill/requirements.txt` 中的固定依赖；旧 CLI 路径仍支持 Python 3.9+，不需要 SDK 依赖。需要已有所选 CLI 及其登录；远程观察需要现有 SSH、Windows PowerShell 和 Node.js。本项目不安装模型客户端、不复制凭据、不配置代理。Kimi 的三条托管 hooks 需按专用参考从最终安装路径安装；Claude 和 OpenCode 使用任务配置。Claude SDK 接入任务级 hooks，不自动继承全局或项目自定义 hooks。

完整复制 Skill 后，在最终安装位置创建虚拟环境并安装依赖，再按 [MCP 连接说明](skill/references/mcp.md#install-and-connect) 配置服务器及现有 Claude 可执行文件。刷新连接后，在实际 Codex 任务里调用 `delegate_list` 确认可用。配置存在不等于工具已经加载。

在 Codex 中可说：

> 使用 $codex-cli-delegate，让 OpenCode 只读审查这次修改；等待完成后，逐项核验它的结论。

MCP 入口为 `scripts/delegate_mcp.py`，旧命令入口 `scripts/delegate.py` 继续保留。Skill 主文负责委派与验收规则，原命令流程移到 [CLI 参考](skill/references/cli-workflow.md)。内部模块 `claude_task.py`、状态目录 `~/.codex/claude-delegate` 暂保留原名，用于保持任务锁、旧任务和 hook 兼容。日常已有任务不会自动迁移；不要同时启用两份重复 skill，也不要搬走运行中的状态目录。

原地升级前先备份 Skill 和配置，结束或停止运行中及空闲待续接的 SDK job；虚拟环境应在最终目录创建，不搬动已有环境。

改名或搬迁已有安装时，Kimi hooks 的绝对路径需要迁移：等当前 Kimi 轮次结束，备份配置，保留旧目录；先用旧目录的 `kimi_hooks.py remove` 移除，再用新目录的 `install` 安装、`check` 核验。三步使用同一 Kimi 配置目录。详见 [迁移步骤](skill/references/kimi.md#改名或更换安装路径)。不要直接删掉被用户修改过的托管块；无需搬迁共享任务状态。Kimi hooks 还要求 `/usr/bin/python3 --version` 可正常执行。

## 后台执行，结束后通知

每次派单或返工后，用 `delegate_notify` 为准确的 job 和 round 挂接通知。确认独立观察程序启动后，Codex 可以结束本轮；普通程序在完成、失败或需要检查时发 macOS 通知，不调用模型、不自动唤醒 Codex。你回来后再在原任务中独立验收。

系统弹窗已在 macOS 上实际确认可见；另用 SDK 协议模拟任务验证了调用方退出后仍完成并提交通知。普通进度和暂时的工具错误不刷屏，每轮重新挂接。旧 MCP 尚未加载第八个工具时，可使用等价脚本，不需重派任务。详见 [后台通知说明](skill/references/notifications.md)。

## 当前边界

- 本机委派以 macOS 为已验证平台。将文件复制到 Windows 不等于 Windows 本机可运行；Mac 观察远程 Windows Codex 是另一项能力。
- 默认配置：Claude `claude-opus-5/max`，Kimi `kimi-code/k3-256k/max`，OpenCode `deepseek/deepseek-flash/high`（V4.1 Flash 正式调用名）。模型与校验逻辑一起固定，当前没有任意模型选择功能。
- OpenCode 当前验证 CLI 1.18.30；其他版本会被拒绝，需要先核对协议与 hooks。Kimi 要求已有思考配置；具体见 [后端参考](skill/references/kimi.md)。
- 默认只读的后端只有明确授权后才加入编辑或 Bash。工具选择不是 OS 沙箱；Kimi/OpenCode 的 Bash 允许整个工具，不能冒充 Claude 的命令级规则。
- idle 或完成 hook 不代表通过验收。SDK 在本轮原生 result 到达后检查会话、本轮、模型和正式输出，旧 CLI 则在进程退出后核验，再由 Codex 检查实际文件与测试。
- 默认不限返工次数，保留超时和异常检查。Claude 任一可靠额度窗口达到 90% 后暂停后续派单，当前轮继续完成；数据未知时不显示为零，也不保证账户永不越线。DeepSeek/Kimi 额度尚未接入。
- 派单和返工带稳定请求 ID，断线后原参数重试不会重复派单；等待在程序内完成，不通过模型反复查状态。验收后关闭空闲 SDK 连接并释放目录锁。
- Pi、ACP 和 OpenCode Server 尚未实现。不承诺唤醒已经结束的 Codex 任务。任务原文、思考、账号配置、运行日志和私人测试记录不随源码发布。

本次 MCP/SDK 更新通过 158 项 Python 测试和 11 项 JavaScript 测试，包含真实 MCP 协议连接及由模拟 CLI 驱动的固定版本 SDK。进程身份测试需要读取系统进程信息，受限环境中的拒绝不能当成功；本次在允许读取后复验通过。

本地测试方法见 [英文 README](README.md#tests)，不调用付费模型。另有 macOS 真实三轮 SDK 验证：同进程返工、重启后恢复原会话、上下文保留、文件输出、Stop hooks 和缓存读取均有实际记录；不由此推算订阅额度节省比例。真实 API 故障、其他 CLI 版本和 Linux/Windows 本机 MCP/SDK 执行不能用本地自动化测试结果代替。详细边界见 [MCP 验证说明](skill/references/mcp.md#validation-and-maintenance)。

## 来源与许可证

作者 Ruller_Lulu。部分事件处理和 hook 共存设计与 [clawd-on-desk](https://github.com/rullerzhou-afk/clawd-on-desk) 一同演进；使用本 skill 不依赖桌宠应用。

采用 [MIT 许可证](LICENSE)。这是独立社区项目，与所支持的模型及 CLI 厂商没有官方从属关系。
