# Windows Codex CLI 的远程派单与完成通知

这是独立能力，不属于 Claude/Kimi/OpenCode/Pi 外部后端，也不能满足用户对这些路线的点名要求。它只处理 Windows 上的 Codex CLI：可以从 Mac 发起一个有界 `codex exec` 任务，或只观察一个已经存在的准确轮次。第一阶段不支持远端 Claude/Kimi/OpenCode/Pi、桌面 `queue`、多 agent、远程 stop、断线续跑或通用 shell。

## 发起一个 Windows Codex 任务

以下 `<task-script>` 是本 skill 目录内 `scripts/remote_codex_task.py` 的绝对路径。默认状态根是 `${CODEX_HOME:-~/.codex}/claude-delegate/remote/tasks`；`--owner` 默认读取 `CODEX_THREAD_ID`。所有命令必须使用当前任务的真实 owner。

远端必须已经具备：可无交互登录的 SSH 别名、已知且匹配的主机密钥、PowerShell、Node、Codex CLI、有效登录，以及已经存在的工作目录。派单器不会安装软件、修改 SSH/代理、修改 Codex 持久配置或建立远端服务。

策略文件必须是仅当前用户可读写的 JSON（Unix 模式不得开放 group/world 权限）。示例中的主机、目录、模型和超时都应按机器实际情况收紧：

```json
{
  "version": 1,
  "sites": {
    "windows-dev": {
      "host": "existing-ssh-alias",
      "cwd_roots": ["D:\\work"],
      "codex_home": "C:\\Users\\USER\\.codex",
      "models": ["gpt-6-astra"],
      "efforts": ["xhigh"],
      "sandboxes": ["read-only", "workspace-write"],
      "windows_sandbox": "elevated",
      "max_timeout_seconds": 900,
      "allow_non_git": false
    }
  }
}
```

`windows_sandbox` 必须明确写成 `elevated` 或 `unelevated`。优先用 `elevated`；若 `codex doctor --json` 明确显示 elevated 初始化失败，可以在确认机器适合官方较弱后备后，把该站点固定为 `unelevated`。派单器不会自动降级，也不会改写远端 `config.toml`。

任务正文从文件读取，经标准输入发送；正文、目录和其他变量不进入 PowerShell 命令字符串。每个新请求使用新的 UUID；相同 request ID 与相同请求会去重，内容或策略控制变化会报冲突。

```text
python3 <task-script> --policy <0600-policy.json> dispatch \
  --site windows-dev --agent codex --cwd 'D:\work\repo' \
  --request-id <新UUID> --prompt-file <task.md> \
  --model gpt-6-astra --effort xhigh --sandbox workspace-write --timeout 900

python3 <task-script> await-event <job> --after <上次cursor>
python3 <task-script> status <job>
python3 <task-script> receipt <job>
python3 <task-script> reclaim <job> --notes-file <远端工作区人工检查记录>
python3 <task-script> accept <job> --notes-file <Codex独立验收记录>
```

派单首先向远端 `~/.codex/remote-delegate/runtime/<sha256>.cjs` 写入并校验内容寻址的 runner，然后本机 worker 保持一条 SSH **载荷连接**，直到远端任务结束。30 秒 keepalive、连续 10 次无响应才把连接视为丢失；远端 runner 自己执行有限超时。Mac 端不能因为模型暂时没有输出就杀载荷连接。

这不是断线后继续运行的队列：Mac 睡眠、网络中断、应用/本机 worker 被终止，都可能让 Windows Codex 一同退出并留下部分修改。连接丢失后至少等待 keepalive 窗口，再用 `receipt` 核对远端 PID 与创建时间；进程探测失败与“进程明确不存在”是不同结果，缺少肯定证据时保持 `unknown`。目录锁不会因“看起来过期”自动删除。只有回执已是 `killed_by_carrier_loss`、人工检查了远端工作区并写下非空记录、锁的 request/cwd 匹配、心跳确实过窗且准确 PID/创建时间已明确不存在时，才可显式调用 `reclaim`；记录摘要会写入远端回执。

| 状态 | 含义与下一步 |
| --- | --- |
| preparing / connecting / running | 本机正在准备或载荷连接仍活跃；继续等待，不把心跳当完成 |
| completed_claimed | runner 找到准确 session/turn/原生日志并看到终态；随后还要通过独立观察器 |
| awaiting_review | 原生日志再次确认 cwd、model、effort、sandbox、approval、正式回复和来源摘要；Codex 再核对实际产物 |
| cwd_busy | 该远端目录已有任务锁；不得自动清锁或换状态根绕过 |
| timed_out / failed / incomplete_evidence / observer_error | 超时、运行失败或证据不完整；不得接受 |
| carrier_lost_pending_recheck / carrier_aborted_by_local_error / killed_by_carrier_loss / unknown | 连接丢失或本机解析失败后的保守状态；可能有部分写入，先等待窗口并检查远端工作区 |
| accepted | 本机 owner 已核对正式回复、回执和实际产物，并保存验收说明 |

`awaiting_review` 不是业务验收。必须核对 `final.md`、远端实际改动/产物、准确 session/turn/log、CLI 路径和版本、实际 model/effort/sandbox/approval，再调用 `accept`。本机 `stream.ndjson` 是私有的完整 `codex exec --json` 事件流，可能含推理摘要、工具参数和工具输出；不要把它当普通结果发布或提交。对外状态和 `final.md` 只保留规范化证据与正式回复。

原生日志能独立证明 Codex 轮次报告的 `sandbox=workspace-write` 和 `approval=never`，但当前日志没有独立字段证明底层 Windows 实现究竟是 `elevated` 还是 `unelevated`。`windows_sandbox` 因此是策略固定并传给单次调用的“请求值”，不是从会话日志反证出的“实际实现值”。官方将 `unelevated` 作为 elevated 初始化失败时的后备；它隔离更弱，可能依赖用户 ACL，并且同用户进程边界更弱。使用前应把这个差异当成明确风险，而不是把 `workspace-write` 等同于更强的 elevated 隔离。

## 只观察已经存在的 Windows Codex 轮次

以下 `<observer-script>` 是本 skill 目录内 `scripts/remote_codex.py` 的绝对路径。观察器不启动、恢复或终止远端 Codex，适用于已从其他入口取得准确身份的任务。

先记录真实 owner、远端 host、准确 session、turn、工作目录和该 session 的原生日志绝对路径。不要用“最新会话”代替已确认身份。CLI `exec --json` 的 `thread.started` 提供 session；原生日志的 `task_started.turn_id` 提供 turn。无法取得身份时报告缺口，不随便另起工作。

```text
python3 <observer-script> track --host existing-ssh-alias \
  --session <准确session> --turn <准确turn> \
  --cwd <Windows绝对工作目录> --log <Windows原生日志绝对路径>
python3 <observer-script> await-event <job> --after <上次cursor>
python3 <observer-script> status <job>
```

`track` 只确认跟踪已建立。保存 cursor 并继续 `await-event`；等待本身不调用模型。默认单次等待 900 秒，超时只返回当前状态。`task_complete`、实际 model/effort/sandbox/approval、完整正式回复和来源摘要同时具备后才进入 `awaiting_review`。默认观察器可在多轮 session 中定位指定 turn；派单器自己的二次证明会额外启用单轮约束。

观察器支持 `reconnect` 和 `detach`：前者恢复本机日志接收，不恢复模型；后者只停止本 job 的观察连接。日志上限 256 MiB、单记录上限 4 MiB；重连按有界块补读并校验已读前缀摘要。Stop hook、CLI 退出、进程存活和心跳都不是成功证据。GUI、权限弹窗和未写入原生日志的用户输入等待仍需单独验证。Windows 远端文件依赖 `%USERPROFILE%\.codex` 的 ACL；代码中的 POSIX mode 参数不构成 Windows 权限保证。
