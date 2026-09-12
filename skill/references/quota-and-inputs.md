# 额度预警和文件准备

## Claude 90% 规则

按用户的选择，达到阈值时**让已经开始的当前轮完成，并暂停之后的派单和返工**。默认阈值 90%，检查 `five_hour`、`seven_day`、`seven_day_opus` 中已有的适用数据；不监控 Kimi、Codex 的额度。当前轮本身仍可能继续消耗并越过 90%，所以这不是账户用量绝不超过 90% 的硬上限。

```text
python3 <script> quota
python3 <script> status <job>
python3 <script> await-event <job> --after <cursor>
```

数据来自已登录 Claude Code 的原生 `rate_limit_event`，包括本机 2.1.261 实测的 `rate_limit_info.unifiedWindows.*.utilization`，也兼容单窗口 `utilization`。比例以 0..1 输入，换成百分比后保留精度；89.6% 不会因显示四舍五入为 90 而提前暂停。母仓库 `hooks/claude-rate-limits.js` 提供的窗口/单位映射用于对照；它处理状态栏的 0..100 `used_percentage` 并取整，不能直接当作本阈值判断函数。

每个状态根和 Claude 配置目录分别缓存账户额度，跨本 skill 的 Codex owner 共享阈值，但不共享任务原文。新 worker 实时接收事件，触达阈值时保存 `quota_pause` 通知；主任务下次 await-event 会收到。启动/返工检查缓存并补读最近的已有原生事件，实际启动前再检查一次。没有复制凭据或额外调用额度 API，也不会为查额度启动模型。

状态解释：

- `available`：已取得尚未过期的实时观测，并且没有窗口触达阈值。
- `paused`：至少一个窗口达到 90% 或原生事件已明确拒绝用量；新调用返回 `quota_paused`，保留原 job/session，可继续审查和接受已完成的结果。
- `unknown`：数据缺失、超过 15 分钟，或只从没有事件时间戳的旧日志导入；保留“最近已知百分比”和来源，不能当作 0%。没有已知超限窗口时允许任务开始，并明确当前无法保证实时阈值保护；新一轮收到原生额度后更新。

达到阈值的旧观测不会因 15 分钟过去就消失；保留暂停直到已知重置时间到达，或有更新的可靠观测解除。重置只表示旧窗口过期，不意味着新窗口为 0%。从旧日志导入时，较晚的文件修改时间不能用同一窗口的更低用量清除已知高用量；工具输出可能在很晚才写入旧文件。

只覆盖本机此 Claude 配置目录下、本 skill 能读取到的原生事件。其他终端或设备的消耗，需等下一条包含新额度的事件才能得知；同时已经启动的各轮都允许完成。数据缺失、CLI 版本变化或当前调用中额度更新延迟时，应如实报告，不承诺秒级账户总量硬限制。

官方语义参考：[RateLimitEvent](https://code.claude.com/docs/en/agent-sdk/python#ratelimitevent)、[状态栏账户额度](https://code.claude.com/docs/en/statusline#rate-limit-usage)。账户额度与 `context_window.used_percentage` 是两件事。

## 避免首次读取失败

实际失败记录显示：交接文件在 `--cwd` 外，而启动器启用了 `--restricted`。这是 Claude 文件工具的访问范围限制，不是文件副本不完整，也不是自动创建了操作系统沙箱。

派单者应先确认实际工作目录、必需文件和已授权的参考目录。例如源码在 `review-source`，交接清单在其父目录时，应把父目录作为明确的读取范围传入：

```text
python3 <script> start --cwd /absolute/review-source --prompt-file /absolute/task.md --read-dir /absolute/references --require-file /absolute/references/HANDOFF.md --require-file /absolute/review-source/main.py
```

`--read-dir`、`--require-file` 可重复。目录和必需文件会保存到 job，返工自动沿用，也可以在 revise 时追加。预检确认真实路径、存在性、可读性、是否在声明范围内，保存字节数和 SHA-256；只传元数据，不把哈希当成 Claude 已读过文件。目录外的符号链接仍需声明其真实目标范围。没有声明的输入不能保证预检覆盖。

附加目录通过 Claude 的 `--add-dir` 传入，默认编辑授权仍只覆盖主工作目录。原有明确 `--allow-tool` 规则仍会生效，不应为读取文件随意增加宽泛的写入或 Bash 权限。`--restricted` 的配置隔离、已有 hooks 和独立会话继续保留；任务书要携带适用的项目指导。

普通读文件优先使用 Read/Glob/Grep。文件较大时明确 Read 的 offset/limit，必要时连续读取到所需范围；“工具返回部分内容”和“完整读过文件”必须区分。路径不存在、单次内容过大、权限范围不符是不同问题，应按实际错误处理，不一概称为沙箱问题。

官方说明：[restricted 与 add-dir](https://code.claude.com/docs/en/cli-usage)、[文件权限和绝对路径语法](https://code.claude.com/docs/en/permissions#read-and-edit)。文件编辑应使用 `Edit(path)` 规则，它同时约束 Write；Claude 不使用 `Write(path)` 作路径判断。
