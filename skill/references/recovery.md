# 恢复与限制

仅在恢复任务、解释阻塞或排查会话验证失败时读取。

以下进程与恢复规则适用于两个后端。Kimi 的会话发现与前缀校验另见 [Kimi 调用与证据边界](kimi.md)。首次调用未留下可识别的会话时，`revise` 拒绝续接；先核实旧进程与已有产物，再决定后续工作。不要手填 session ID 或修改状态绕过校验。

## 恢复时先确认事实

保留同一 owner、job ID、工作目录和 Claude session ID。调用 `list`、`status` 读取保存的状态；这两项不发送模型请求。CLI 的会话恢复依赖本地记录，不能用 `--continue` 选择“最近的会话”代替精确 ID。

- **运行中**：worker 身份仍可核实，沿用已返回的 cursor 继续 `await-event`。不要同时手工打开相同 Claude 会话写入消息。
- **等待 review**：先审查已有结果，不重复原任务。
- **孤立进程 / orphaned**：管理该轮调用的 worker 已消失，但 Claude 进程仍在。不要另起一份相同工作。按当前任务需要等待，或用 `stop` 请求停止经过身份校验的进程，再检查已产生的改动。
- **interrupted / failed**：确认没有旧进程继续写文件，检查任务目录、最终事件、错误及 Claude 会话记录。只有在有明确恢复任务且授权仍适用时，使用 `revise <job_id> --recover --prompt-file <恢复指令>`；恢复指令必须说明当前已完成内容及剩余目标。它计入返工次数。
- **进程身份、会话或记录无法确认**：报告需要处理的对象与原因。不要仅凭 PID 杀进程，不删除记录来解锁，不用新会话假装原会话恢复。

`stop` 停止工作但保留文件。停止不等于回滚，不自动删除 worktree、修改或日志。恢复不能凭 Claude 的文字回复断言成功：须有当前调用的真实完成状态和新的模型/深度证据。

事件 cursor 丢失时，可从 `--after=-1:0` 读取现状；已结束状态优先于旧错误和过期检查点。重放的事件不授权重做任务或重复 review。旧版本任务没有 monitor 文件时，仍可读取最终状态和既有证据；不要为补 hooks 重启一个还在运行的 Claude。

## 运行边界

每轮 worker 按需创建，有时间上限，结束后退出；没有全局常驻服务。启动调用器的进程退出后，worker 可以继续本轮并留下结果，使重新打开的控制任务有记录可查。这不等于对所有 Codex 应用崩溃、系统重启、休眠或断网场景作出保证。

同一默认状态根目录下，脚本阻止未结束任务占用同一 Git checkout 或重叠的工作目录。不同 owner 是防误路由的标识，不是不同用户之间的安全边界；该脚本也不能阻止用户从其他终端手动修改工作目录。

状态目录及 Claude 原生记录都是本地文件，不同步到远端。状态目录不是业务仓库的一部分。真实日志可能包含任务内容和代码，按本机用户文件保管，不公开上传。

## 模型与深度核实

`claude-opus-5` 是模型，`max` 是思考深度，Max 是订阅名称。三者分别确认。每次调用带明确参数，实际模型从响应核对；实际深度从本次新增的 assistant 元数据核对，不查看或转述原始思考内容。

本机 Claude Code 2.1.261 的记录结构已实测，但它可能随客户端更新变化。记录缺失、字段变化或组织策略导致降档时，应报告无法确认，而不是猜测成功。不要在 skill 执行时自行升级 CLI、修改永久设置或变更代理。

官方参考，仅在版本行为变化时按需查阅：

- [Claude 程序化调用](https://code.claude.com/docs/en/headless)
- [Claude 会话恢复](https://code.claude.com/docs/en/sessions)
- [Claude 模型与思考深度](https://code.claude.com/docs/en/model-config#adjust-effort-level)

## CLI 占位记录导致的校验失败

Claude SDK 续接偶尔会在会话记录中加入 `No response requested.`。调用器只识别严格匹配的 `<synthetic>`、零 token、明确非 API error 的 SDK 占位形态；它必须不出现在当前 stdout，并且当前真实回复与新增会话记录逐条匹配正文摘要、消息 ID、session、模型和 max 深度。其他合成输出、错误消息和缺证据仍然失败。排除项只保存 UUID、行号、摘要与识别规则；不删除原记录。

已完成且仅因上述记录核验失败的 Claude 任务，在修复调用器并获得原任务范围内的恢复授权后，可对原始证据重新核验：

```text
python3 <script> revalidate <job_id> --expected-round <当前轮次> --expected-stream-sha256 <原轮次保存的stdout摘要>
```

此操作不调用模型、不增加返工次数、不改基线或原始日志，也不自动验收。它要求原模型/session 与退出证据已通过、所有记录进程确认退出、原结束时 stdout 封条及任务书完整；只在占位识别和全部校验通过后转为 `awaiting_review`。旧 verification、阶段、状态和新核验结果保留在 `revalidations`。新增 transcript 摘要明确标为 `revalidation_time_only`，不补称结束时已封存。失败仍保留失败状态。

成功后导出到新的 evidence 目录，保留旧失败导出和待验收包；重新完成独立 review，再用新目录调用 `accept`。验收还会检查原流、任务书和重核验时的 transcript 摘要是否保持一致。此路径只处理已定位的 CLI 占位误判，不恢复超时、失败结果、未知进程、降档或身份缺失。
