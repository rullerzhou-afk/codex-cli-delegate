# Kimi 调用与证据边界

仅在选用 Kimi 后端或排查 Kimi 证据时读取。目前 Kimi 适配只启用 macOS，本机验证 Kimi Code 0.39.0；使用现有登录与原生会话目录。通知使用一次性安装的 3 条委派 hooks；调用任务时不改配置。

## 调用与范围

Kimi 同样用 `-p` 非交互调用，配合 `--output-format stream-json`；该模式自带自动执行权限，不要额外加 `--auto` 或 `--yolo`。它没有 Claude 的窄 Bash 命令规则，禁止把 `--allow-tool` 当作生效的 Kimi 权限；调用器会拒绝这类组合。

```text
python3 <script> start --backend kimi --cwd <工作目录> --prompt-file <任务文件>
```

默认包含 Read / ReadMediaFile / Glob / Grep，支持文字与媒体审查。要实现代码，逐项列出需要的工具；一旦指定，列表替代默认值：

```text
python3 <script> start --backend kimi --cwd <隔离工作目录> --prompt-file <任务文件> --kimi-tool Read --kimi-tool ReadMediaFile --kimi-tool Glob --kimi-tool Grep --kimi-tool Write --kimi-tool Edit --kimi-tool Bash
```

`Bash` 表示整个 shell 工具可用，不能保证只执行某个测试命令。只在任务已授权执行命令、目录和改动基线明确时启用；若任务需要机器强制的窄命令规则，选支持该规则的 Claude 后端。工具列表不是操作系统沙箱，Read 等也不承诺强制限制文件路径。任务书要明确文件范围，并由 Codex 检查实际修改。不要因 Kimi 选择工具失败自动扩大工具列表。

每个 job 使用单独 agent profile，排除 MCP、子代理、cron 和技能工具；使用空技能目录。初次创建绑定 profile，续接读取 Kimi 保存的同一 profile。Kimi 仍加载自己的用户配置、hooks 和适用的项目指导。默认状态目录与 Claude 共用，避免两个后端同时占用同一 checkout。

`--kimi-bin` 可指定可执行文件，默认查找 `kimi`。任务文字作为独立 argv 参数传给 CLI，没有 shell 拼接；因此任务文件限制 64 KiB，并且本机同一用户查看进程参数可能看见任务文字。不要在任务书放密钥。

## 模型、完成与续接

固定请求 `kimi-code/k3-256k`。Kimi 当前没有单次调用的 effort 参数；调用器要求已有 `[thinking]` 明确 `enabled=true`、`effort="max"` 才启动，并逐轮从原生 `llm.request` 核对实际 `model=k3-256k`、`modelAlias=kimi-code/k3-256k`、`thinkingEffort=max`。未满足时报告原因，不擅自修改用户设置，不静默降档。

Kimi 自行生成 `session_<UUID>`。调用器用原生索引中相同 cwd 和本轮唯一 prompt 标记发现会话，不能只按“最新会话”选择。返工使用 `--session <已保存ID>`，同时核验前一轮 wire 文件前缀长度与摘要，避免把旧模型证据算成本轮成功。

Kimi 没有 Claude 的 result 消息。进入 `awaiting_review` 要同时满足：进程退出 0；stream 的唯一 resume hint 指向同一会话；原生 cwd / 会话一致；本轮有模型与深度证据；原生 turn completed 和最终 step end_turn；实际工具列表匹配；最终文本与原生最终步骤文本一致。任何字段缺失或变更都不能猜成功。这里只证明 CLI 执行及身份，业务结果仍由 Codex 独立验收。

## 等待与错误

沿用 [安静等待](events.md) 的 `await-event` 和 cursor 用法。接收 `PostToolUseFailure`、`StopFailure`、`Stop`。前两者通知工具失败或整轮失败；Stop 只记录提示，不改变完成门槛。接收端核对 owner、job、当前轮次 token、cwd 和带本轮唯一标记的原生 session；忽略其他会话、子代理、过期或已停止/结束轮次。hook 只保存事件和工具标识，不保存错误正文、工具输入或思考内容；对 Kimi 始终静默返回，不阻塞或催促续跑。

原生 wire 的结构化错误提供兜底，先给异步 hook 一秒接收时间；迟到或重复错误按相同事件摘要去重。stream 的 provider retry 也会触发事件。不搜索普通输出中的 “error”。`progress.hooks_seen` 记录实际收到的 hooks；`notification_mode` 区分 hooks 加原生兜底与旧任务的原生模式。

输入/输出 token 来自每步落盘的 `usage.record`，不是逐 token 实时数据。未落盘时为 unknown，不能据此认定卡死；10/15 分钟比较保留这个边界。工具调用与完成记录用于补充判断进展。

## 通知 hooks 安装与移除

Kimi 0.39.0 的 hooks 来自用户配置，没有 Claude 式的单轮 settings 参数。辅助脚本只管理自己带标记的 3 条 `[[hooks]]`，原有 Clawd 和用户配置保留原字节；配置内的账号、模型、网络设置不复制到任务文件。

```text
python3 <skill目录>/scripts/kimi_hooks.py check
python3 <skill目录>/scripts/kimi_hooks.py install
python3 <skill目录>/scripts/kimi_hooks.py remove
```

首次安装按用户当前授权执行，后续 `start` / `revise` 只检查。缺失或被修改时明确报错，不自动恢复用户删除的 hooks。脚本接受 `--kimi-home`，默认读取 `KIMI_CODE_HOME` 或 `~/.kimi-code`。安装须从最终 skill 路径运行；搬迁时按下方步骤迁移旧 hooks。遇到重复标记、用户编辑过的托管块、inline hooks 数组或符号链接时保持配置并报告，不能覆盖猜测修复。

### 改名或更换安装路径

托管 hooks 写入的是安装脚本的绝对路径。新路径的脚本会拒绝安装或移除旧路径生成的块；这是保护配置的校验，不应通过强制删除绕过。

1. 等待所有 Kimi 委派轮次结束。保留旧 skill 目录，并在本机备份实际使用的 Kimi `config.toml`；备份含有账号配置，不要提交到仓库。
2. 用旧安装目录执行移除，再从最终的新目录安装并检查：

```text
python3 <旧skill目录>/scripts/kimi_hooks.py remove
python3 <新skill目录>/scripts/kimi_hooks.py install
python3 <新skill目录>/scripts/kimi_hooks.py check
```

三步必须使用相同的 Kimi 配置目录；如果设置了 `KIMI_CODE_HOME`，保持该变量一致，或在每条命令后传入同一个 `--kimi-home <配置目录>`。确认检查通过后，才停用或移走旧 skill 副本。无需移动共享任务状态目录。

若旧目录已经丢失，先恢复旧版本到原路径；若托管块被用户修改，保留备份并逐项检查差异，不按标记无条件删除。

hooks 接收端固定使用 `/usr/bin/python3`，仅在 PATH 中另装 Python 不足以满足此条件。安装前运行 `/usr/bin/python3 --version`，确认它确实可用；本轮未验证没有 Command Line Tools 的 macOS 环境。Kimi 必须已有正常配置，辅助脚本不会创建账号配置。

命令先检查专用的本轮环境变量；普通 Kimi 会话没有该变量，不启动 Python 接收端。环境变量随委派子进程传递，不改用户 shell。接收端是同一用户的本地路由隔离，不是对恶意同用户进程的安全边界。

为保持旧任务可恢复，共用状态中的 `claude` / `claude_launch_pending` 字段仍表示执行方进程；查看 `backend` 判断实际后端。不要据字段名猜调用了 Claude。

Kimi 会重写进程标题，不能继续用 argv 标记确认其身份。macOS 适配在直接启动子进程时核实父进程、进程组、用户及可执行路径，并记录内核微秒级创建时间；后续停止/恢复核对创建时间、用户、进程组和路径。元数据无法读取时保守拒绝发送信号，不靠 PID 或标题猜测。

官方参考（版本行为变化时按需查阅）：[Kimi hooks](https://www.kimi.com/code/docs/kimi-code-cli/customization/hooks.html)、[Kimi CLI 参数](https://www.kimi.com/code/docs/kimi-code-cli/reference/kimi-command.html)、[配置覆盖规则](https://www.kimi.com/code/docs/kimi-code-cli/configuration/overrides.html)。

Default read-only tools now include ReadMediaFile in addition to Read/Glob/Grep. Explicit tool lists replace defaults: retain ReadMediaFile for image/video tasks. Optional tools include WebSearch, FetchURL, and TodoList. See [capabilities](tools.md) for host dependencies and the saved-profile limitation on old sessions.
