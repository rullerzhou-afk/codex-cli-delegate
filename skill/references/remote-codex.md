# Windows Codex CLI 的远程完成通知

这是独立观察入口，不改变 Claude/Kimi 后端，不启动、恢复或终止远端 Codex。适用于 Mac 经现有 SSH 控制 Windows 上的 Codex CLI，包括 Windows 已登录桌面 session 1 中的 CLI。观察器在 SSH 会话中读日志，不代表测试工作也在桌面会话执行；GUI 证据须单独验证。

## 派单后立即接入

先记录原派单 Codex 的真实 owner、远端 host、准确 session、turn、工作目录和该 session 的原生日志绝对路径。不要用“最新会话”代替已确认身份。CLI `exec --json` 的 `thread.started` 提供 session；原生日志的 `task_started.turn_id` 提供 turn。交互 CLI 也可以从已绑定的原生日志读取身份。无法取得身份时报告缺口，不随便另起工作。

以下 `<remote-script>` 是本 skill 目录内 `scripts/remote_codex.py` 的绝对路径。默认状态根是 `${CODEX_HOME:-~/.codex}/claude-delegate/remote`；`--owner` 默认读取 `CODEX_THREAD_ID`。所有参数属于当前任务；不能借用其他 owner 操作其 job。

```text
python3 <remote-script> track --host <ssh-host> --session <准确session> --turn <准确turn> --cwd <Windows绝对工作目录> --log <Windows原生日志绝对路径>
python3 <remote-script> await-event <job> --after <上次cursor>
python3 <remote-script> status <job>
```

`track` 首次通过 PowerShell 向远端 `~/.codex/remote-delegate/runtime/<sha256>.cjs` 安装内容校验过的只读观察器，再启动本地独立接收进程。需要远端现有 Node、PowerShell 和已能无交互连接的 SSH 别名；不会安装软件、修改 SSH shell、代理、Codex 配置或 hooks 信任记录。首次未知 SSH 主机密钥或登录受阻应按现有连接方式处理，不能关闭主机密钥校验。

`track` 只确认跟踪已建立。**不要在这里结束当前 Codex 轮次或改用每 20 分钟模型回查。** 用返回的 cursor 进入 `await-event`，收到 `running` 就保存 cursor 继续等待；等待本身不调用远端模型。收到完成后立刻核对产物并继续独立验收。遵守当前工具的最长单次等待限制，不反复读取全文日志。默认单次本地等待 900 秒；超时返回当前状态和同一 cursor，不能称为完成。

## 状态与处理

| 状态 | 含义与下一步 |
| --- | --- |
| connecting / unknown | 连接中或准确轮次尚未出现；保留身份，等待或检查启动回执 |
| running | 准确轮次有原生记录；保存 cursor，继续等待 |
| stalled | SSH 仍连通，但原生记录超过 15 分钟未更新；只表示缺少进展证据，不能断言等待审批、卡死或完成 |
| disconnected | 接收中断；观察器自动重连，使用上次日志前缀摘要核对并补读；不重启模型 |
| observer_stopped | 本机接收器已停止；调用 `reconnect` 恢复观察，不重新派单 |
| awaiting_review | 准确轮次有 `task_complete`、实际 model/effort 和完整正式回复；需要 Codex 核对修改、产物、模型要求与测试 |
| interrupted / failed / superseded | 原轮次中断、失败或尚未完成就进入其他轮次；不触发成功验收 |
| incomplete_evidence / observer_error | 完成证据缺失、路由/摘要不符、日志被截断或格式不兼容；检查明确错误，不降低核验门槛 |
| accepted | 本机 owner 已写入独立验收说明，正式回复摘要再次核对通过 |

Stop hook、工具完成、CLI 退出、心跳和进程存活都不是成功证据。当前接入使用原生日志事件，**不保证识别未写入日志的权限/用户输入等待**；不能声称已经验证审批即时提醒。若用户报告权限等待，需要对准确桌面任务另做只读诊断。

正式回复保存为该 job 的 `final.md`，状态保存其 SHA-256、完整已读日志前缀 SHA-256、准确源路径、版本和实际模型/深度。不会导出思考、工具参数、命令输出或完整原始日志。摘要用于发现后续改动，不是远端来源不可伪造的签名。模型与深度沿用远端用户配置；与派单要求不符时不得接受。

```text
python3 <remote-script> accept <job> --notes-file <Codex独立验收记录>
python3 <remote-script> reconnect <job>
python3 <remote-script> detach <job>
```

`accept` 只接受 `awaiting_review` 且本地正式回复未改变的结果；重要争议按 skill 的原文留存规则另行归档。`reconnect` 只重启缺失的本地接收器，活跃接收器由锁保护；不能把终态改回运行，也不续接模型。`detach` 仅停止本 job 的观察连接，保留远端工作。Mac/应用重启后，重新打开原任务，用 `status` 查看并在非终态时 `reconnect`。本机接收器可在当前轮结束后保留回执，但**不会自动唤醒已结束的 Codex 任务**。

日志上限 256 MiB、单记录上限 4 MiB；未写完的末行暂不解析。超过上限或日志发生轮换时明确报告观察错误。首次连接及重连按有界块重放准确文件；平时仅读取新增部分。旧轮次完成、重复完成和其他会话事件不能结束本轮。Windows shell 适配独立于母仓库 POSIX 部署器，复用了 Clawd 的轮次隔离、有界读取和完成信号须复核的原则。
