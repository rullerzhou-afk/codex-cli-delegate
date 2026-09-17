# 安静等待与事件处理

派单前读取。本轮由 Python worker 观察执行方的结构化输出与 hooks，不调用模型；Codex 等待一次有意义的事件后再作判断。Claude 使用单轮 settings；Kimi 使用一次安装的 3 条委派 hooks，见 [Kimi 参考](kimi.md)。OpenCode DeepSeek 使用本轮插件通知及原生会话核验，见 [OpenCode 参考](opencode.md)。Pi 使用 JSON 事件与隔离的原生会话文件，不安装 hooks，见 [Pi 参考](pi.md)。没有桌面私有接口或定时模型轮询。

## 调用方式

`await-event` 返回 JSON 后退出。它可能持续数分钟，因此要把执行工具的短会话等待放在 **同一个 `functions.exec` 单元内部**，不能每次工具超时都回到模型分析。当前 Codex 桌面环境已实测：65 秒等待只返回一次；真实 Claude 的错误事件能在仍运行时返回，下一次等待返回最终结果。

先根据实际绝对路径、job ID 和 cursor 准备好正确引用的命令。首次使用 `--after=-1:0`，后续例如 `--after 0:1`。下面是 `functions.exec` 的调用示例；`command` 替换成已准备好的命令，不要把不可信任务文本拼接到 shell。

```javascript
// @exec: {"yield_time_ms": 1860000, "max_output_tokens": 3000}
const command = "python3 <absolute-skill-path>/scripts/delegate.py await-event JOB_ID --after=CURSOR";
let r = await tools.exec_command({cmd: command, yield_time_ms: 1000, max_output_tokens: 3000});
let output = r.output;
while (r.session_id) {
  r = await tools.write_stdin({
    session_id: r.session_id, chars: "", yield_time_ms: 55000, max_output_tokens: 3000
  });
  output += r.output;
}
text({exit_code: r.exit_code, output});
```

根据任务实际权限给执行调用使用正常的提权参数；不要因为等待阶段受限就把一个仍运行的 Claude 重新启动。`write_stdin` 的循环只在本地工具单元内执行，无须模型逐次参与。

示例外层等待约 31 分钟，只是调用工具的等待设置，不是任务运行上限。任务默认不限时；等待提前返回时续接原等待，不停止模型任务。若工具仍返回 `Script running with cell ID ...`，只用 `functions.wait` 继续同一 cell，并沿用足够长的外层等待时间，不另起 waiter。用户插话或取消等待时保留 job/cursor，先处理其指示。

如果当前宿主没有这组长等待工具，或持续强制几十秒就唤回模型，说明这条省额度路径未成立；保留任务并报告这个具体限制，不声称已实现后台自动唤醒，也不悄悄退回频繁轮询。

## 收到事件后的动作

- **`error`**：一次读取返回的 `event.error`、`progress`、进程状态。`last_progress_at` 晚于错误、工具已继续完成等表示后续活动；继续安静等待。如果需解释具体失败，只读相关短日志片段。相同错误在同一轮不重复通知，连续失败合并成一件事；API 终止错误和最终失败可以升级通知。错误本身不授权中断或重跑。
- **10 分钟 `checkpoint`**：记录本轮进展快照，不因仍在运行而返工。计时从每轮派发开始。
- **15 分钟 `checkpoint`**：读取与 10 分钟快照的比较。有增长就继续等待；没有增长则检查一次当前工具、输出和明确的错误/重试。`waiting_for_tool` 表示可能在等测试、构建等；`no_observed_progress` 表示没有观察到推进，都不是“已确定卡死”。缺少 token 数据、恢复后观察间隔不足时为未知。不自动杀进程、切换模型或重复派单。
- **`settled`**：跳过排队中的旧错误/检查点。仅 `phase=awaiting_review` 且 `verified.ok=true` 进入 Codex 独立审查；其他状态按恢复流程处理。`event=null` 表示该最终事件已经被提供的 cursor 覆盖，不重复 review。

每次事件处理后保留返回的 `cursor`。通常无须再运行一遍 `status`；返回结果已包含最新快照。对用户报告有意义的进展；健康等待期间不为发送“还没结束”而唤醒模型。

10、15 分钟各触发一次，完成即取消。15 分钟以后，继续等错误或完成，默认没有整轮硬超时；只有用户明确指定的 `--timeout` 才到点停止。不会无限每五分钟唤回一次。

## 信号和证据边界

Claude 本轮注册 `Stop`、`StopFailure`、`PostToolUseFailure`、`Notification(idle_prompt)`；Kimi 接收前三种事件。OpenCode 使用本轮插件；Pi 不依赖 hooks，直接读取 JSON 事件和原生 session JSONL。接收端只收匹配 job、轮次 token、session 和工作目录的主会话事件；忽略子代理与过期回调。Stop 可能来自暂停或其他 hook 的续跑，idle 提醒也不保证在 `-p` 下出现，因此它们只作记录，最终退出和结构化成功证据才决定可审查状态。没有 hook 时，进程退出与结构化工具失败仍有兜底。

`PostToolUseFailure` 是工具执行失败，不覆盖所有权限拒绝或参数校验错误；`StopFailure` 是 API 错误结束一轮。通知不是让 Claude 续跑的指令，脚本不会输出阻塞/催促内容。

计数只使用可核实的流式 usage，按 message ID 去重；普通 assistant 消息的 output_tokens 临时值不累计。实时计数缺失时保留未知，同时记录有无新内容、工具执行与返回。工具仅报告耗时的心跳不当作有效进展。最终输出 token 使用 result usage；这些观察值不用于估算账单或保证平台额度节省比例。

本机已真实验证 Stop、PostToolUseFailure、流式进展和同会话返工；StopFailure/idle 的去重与路由使用模拟 payload 验证，没有人为破坏账号或网络制造 API 错误。10/15 分钟逻辑用可控时间验证边界，不把模拟当作真实 15 分钟运行证据。

版本行为变化时参考：[Claude hooks](https://code.claude.com/docs/en/hooks)、[CLI 参数](https://code.claude.com/docs/en/cli-reference)、[usage 计数](https://code.claude.com/docs/en/agent-sdk/cost-tracking)。
