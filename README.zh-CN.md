# Codex CLI Delegate

[English](README.md)

由 Codex 规划任务、委派 Claude Code／Kimi Code／OpenCode 执行，并独立验收结果的社区 skill。包含同会话返工、hooks 通知、完成证据核验、额度预警与中断恢复，也支持观察 Windows 上准确的一轮远程 Codex CLI 工作。

## 安装和使用

从本仓库根目录，按 [英文 README 的安装步骤](README.md#install)，将完整 `skill/` 文件夹安装为 `${CODEX_HOME:-~/.codex}/skills/codex-cli-delegate`。已有版本先备份并检查差异，不直接覆盖。

需要已有 Python 3、所选 CLI 及其登录；远程观察需要现有 SSH、Windows PowerShell 和 Node.js。本项目不安装模型客户端、不复制凭据、不配置代理。Kimi 的三条托管 hooks 需按专用参考从最终安装路径安装；Claude 和 OpenCode 使用本轮配置。

在 Codex 中可说：

> 使用 $codex-cli-delegate，让 OpenCode 只读审查这次修改；等待完成后，逐项核验它的结论。

公开入口为 `scripts/delegate.py`。内部模块 `claude_task.py`、状态目录 `~/.codex/claude-delegate` 暂保留原名，用于保持任务锁、旧任务和 hook 兼容。日常已有任务不会自动迁移；不要同时启用两份重复 skill，也不要搬走运行中的状态目录。

改名或搬迁已有安装时，Kimi hooks 的绝对路径需要迁移：等当前 Kimi 轮次结束，备份配置，保留旧目录；先用旧目录的 `kimi_hooks.py remove` 移除，再用新目录的 `install` 安装、`check` 核验。三步使用同一 Kimi 配置目录。详见 [迁移步骤](skill/references/kimi.md#改名或更换安装路径)。不要直接删掉被用户修改过的托管块；无需搬迁共享任务状态。Kimi hooks 还要求 `/usr/bin/python3 --version` 可正常执行。

## 当前边界

- 本机委派以 macOS 为已验证平台。将文件复制到 Windows 不等于 Windows 本机可运行；Mac 观察远程 Windows Codex 是另一项能力。
- 默认配置：Claude `claude-opus-5/max`，Kimi `kimi-code/k3-256k/max`，OpenCode `deepseek/deepseek-flash/high`（V4.1 Flash 正式调用名）。模型与校验逻辑一起固定，当前没有任意模型选择功能。
- OpenCode 当前验证 CLI 1.18.30；其他版本会被拒绝，需要先核对协议与 hooks。Kimi 要求已有思考配置；具体见 [后端参考](skill/references/kimi.md)。
- 默认只读的后端只有明确授权后才加入编辑或 Bash。工具选择不是 OS 沙箱；Kimi/OpenCode 的 Bash 允许整个工具，不能冒充 Claude 的命令级规则。
- idle 或完成 hook 不代表通过验收。进程退出后仍检查会话、本轮、模型和正式输出，再由 Codex 检查实际文件与测试。
- 默认不限返工次数，保留超时和异常检查。Claude 任一可靠额度窗口达到 90% 后暂停后续派单，当前轮继续完成；数据未知时不显示为零，也不保证账户永不越线。DeepSeek/Kimi 额度尚未接入。
- 不承诺唤醒已经结束的 Codex 任务。任务原文、思考、账号配置、运行日志和私人测试记录不随源码发布。

初始公开版通过 131 项 Python 测试和 11 项 JavaScript 测试。进程身份测试需要读取系统进程信息，受限环境中的拒绝不能当成功；本次在允许读取后复验通过。

本地测试方法见 [英文 README](README.md#tests)，不调用付费模型。真实 API 故障、其他 CLI 版本和 Windows 本机委派不能用本地自动化测试结果代替。

## 来源与许可证

作者 Ruller_Lulu。部分事件处理和 hook 共存设计与 [clawd-on-desk](https://github.com/rullerzhou-afk/clawd-on-desk) 一同演进；使用本 skill 不依赖桌宠应用。

采用 [MIT 许可证](LICENSE)。这是独立社区项目，与所支持的模型及 CLI 厂商没有官方从属关系。
