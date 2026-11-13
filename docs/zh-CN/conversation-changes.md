# 通过流式批次持久化 ToolLoop 会话

**0.11.0：批次契约 V2。** 在 `Runner.stream` 或 `resume_stream` 中传入
`StreamPersistenceOptions` 启用。旧 `on_conversation_change` 回调及其专属异常已经移除，
参见 [V1 到 V2 迁移](migration.md#0110)。不传配置时，保持原完整结果流及 trace 行为。

本模式支持 `ToolAgent`、已注册 tool agent 委派和 `AutoAgent` 的 tool 分支。
明确的 DAG 目标会被拒绝；自动路由选择 DAG 时也在进入 DAG 执行前明确失败。
DAG 原执行、事件和恢复保持不变，仅删除其旧回调专属钩子。

持久化按每次调用显式开启。工具内部如果自行调用 `run`、`stream`、`resume`、
`resume_stream` 或 `test_capability` 且未传 `persistence`，会启动一个普通的独立
run，拥有自己的结果和 trace，不会写入外层 run 的批次。需要持久化时，给内层
`stream` 传入 `persistence`，它会使用自己的交付。已注册的 `agent.*` 委派仍属于外层 run。
`design_dag()` 的规划调用也隔离外层持久化作用域。

宿主已提交的批次日志是长期权威来源。`ConversationState` 是有效模型上下文的投影，
审核 `RunCheckpoint` 是可移植恢复记录。SDK 不管理数据库、宿主权限、事务或文件存储。

## 读取、保存和确认

```python
import dagent

options = dagent.StreamPersistenceOptions(
    ack_timeout_seconds=None,
)

async for event in runner.stream(
    agent, input="继续调查", conversation=previous_state, persistence=options,
):
    if event.type == "conversation.changed":
        batch = event.data
        await host.commit_batch(batch)  # 应用事务和必需文件均已保存
        await runner.acknowledge_changes(batch.run_id, batch.batch_id)
    elif event.type == "response.content.delta":
        display(event.data.delta)
    elif event.type == "run.failed":
        handle_delivery_failure(event.data)
```

上例为便于阅读，串行处理读写。需要保存期间继续读取展示内容时，使用持续读取任务和一个
有界写入任务。完整离线 [示例](../../examples/conversation_changes.py) 展示该方式、
JSONL 重建、跨 Runner 审核恢复、文件保存和工作区迁移：

```bash
uv run python -m examples.conversation_changes
```

每个 delivery 同时最多一个待确认批次。输入变化和响应开始在模型派发前确认；接受的回复
在工具派发前确认；工具开始在执行前确认；结果在后续派发前确认。token 不逐个确认，最终
批次也需要 ACK。

保存失败时调用 `reject_changes(run_id, batch_id, message=...)`。拒绝和超时会停止后续
派发，不自动重放工具。活跃 delivery 内重复确认最近已确认批次是安全的；不匹配、过期或
已关闭交付返回 `False`。提交可能成功而 ACK 丢失，宿主应根据批次身份去重。
确认等待已超时后到达的 ACK 也返回 False，不会把终态标为已确认。

确认时限从消费者收到批次开始计算，`None` 不设持久化超时，批次排队等待读取的时间不计入。

展示事件和事实批次共用一条无界有序队列。入队不等待读取方，不因容量合并或丢弃增量。
只有事实批次发布后等待宿主 ACK，前序事实确认后才能派发下一步操作。压缩请求前不再
额外等待事件队列排空；资源批次保存仍等待 ACK，这段等待不计入压缩或模型时限。

宿主应持续消费 SDK 流，再独立向前端订阅者分发已保存事件。读取暂停时，未消费事件会
积压在内存中；队列不限制字节数或单帧大小。关闭流或 Runner 会停止交付，释放排队事件
和 ACK 等待。

## 公开批次契约

`RunStreamEvent.type == "conversation.changed"` 的载荷直接是
`ConversationChangeBatch`。新运行只发布 V2，已有 V1 日志继续由同一公开模型读取。

| 字段 | 含义 |
| --- | --- |
| `delivery_id / sequence / batch_id` | 每次 run/resume 一个交付，序号从 1 开始；身份用于宿主幂等 |
| `run_id / conversation_id` | 执行和公开会话身份 |
| `base_revision / revision` | 批次前后上下文版本，可跳跃或相等 |
| `conversation_schema_version` | 公开会话格式，当前为 4 |
| `messages` | 完整类型消息、来源、响应关联和 partial 标记 |
| `context` | 有序成员、新增、更新、停用、摘要和压缩状态变化 |
| `observations` | 响应和工具执行事实、错误和未知结果 |
| `resources` | 公开文件引用及定位输入 |
| `status` | 普通批次省略；终态为 completed、awaiting_review、failed 或 cancelled |
| `usage / context_usage` | 当前 run 的累计操作用量及上下文使用记录 |
| `error / workspace_path` | 公开 RunError 和已知工作区 |
| `output_item_id / output_text` | 互斥的输出消息引用或兜底文本 |
| `checkpoint` | 仅 awaiting_review 携带现有 V9 checkpoint，不含 trace |

审核恢复保留 run ID，产生新 delivery。事件序号和批次序号不同。只有观察的批次可以不改变
会话 revision。恢复后的累计用量应替换旧值，不能再次相加。

工具观察新增 `assistant_message_id`，使用“assistant 消息＋call ID”关联，避免 provider
跨轮复用 call ID 时串组。工具已经返回、但输出保存失败时，已知执行仍记录为完成；retention
说明数据不可用，运行终态报告存储错误。取消中的工具可记录 outcome_unknown，不伪造工具结果。

## 自行组装下一轮上下文

批次事实只追加一次。最新消息索引是投影，不能替代日志和消息版本。每批原子应用：

1. 校验顺序、身份和 base_revision，保存消息及必需资源。
2. 保存观察，应用成员变化、更新、摘要和压缩状态。
3. 保存运行终态及 checkpoint，提交后 ACK。

消息键为 `(origin.id, item.id)`。公开成员引用 `origin.id == conversation_id` 的消息。
validator、compactor、router、subagent 属于审计来源；存在消息或 partial=True 都不代表
它在根模型上下文中。摘要替换应移除 replaced_item_ids；替换值 None 表示清空摘要或压缩状态。
保持保留条目顺序，按顺序插入新增位置。更新和重新激活可能需要宿主消息索引。

宿主自行创建现有输入：

```python
state = dagent.ConversationState(
    id=conversation_id, revision=revision, items=tuple(active_messages),
    summary=summary, compaction_state=compaction_state,
)
```

原样保存 reasoning、refusal、usage、模型元数据、工具 ID 和参数、冻结展示、retention、
引用和压缩状态。模型请求仍由 SDK 组装；成功轮次保持原有提示词、工具、推理回放、压缩策略、
结果外置阈值和 provider 参数优先级。回归测试对比重建前后的实际 Chat Completions 和
Responses 请求内容。

运行最终失败时，原始响应没有工具调用且有正文的 assistant 消息保留在有效上下文中，
保留推理、usage 和模型元数据。观察记录及终态仍报告失败；partial=True 表示内容不完整，
是否进入上下文以 context 成员变更为准。这支持截断后的续写，也保留子 agent 返回给父模型的
部分回答。自动重试丢弃的回复、空回复、仅推理的失败及原始响应带调用的失败仍只入审计，
失败调用不执行、不激活。

普通最终输出解析 output_item_id，读取 `item.content.strip() or item.refusal.strip()`；
仅有 output_text 时使用它。展示增量是临时内容，被重试丢弃的响应不是正式回复。

## 中断和审核恢复

普通下一轮或重试使用新 run ID 和重建会话。`input=None` 沿传入上下文继续而不重复追加
用户请求，不会暗中移除已保留的失败文本。需要原样重试时，宿主从日志重建该回复之前的上下文；
续写部分回答可传“继续”等新 input。`input=None` 复用已保存附件，不接受新的
`input_uploads`。AutoAgent 重试继续 ToolLoop，不再次路由。持久化 ToolLoop 在模型派发前将尾部未闭合 assistant
工具组及关联结果移出有效上下文，并发布停用变化等待确认。更早闭合轮次和用户消息保留，
原始消息与已知、未知执行事实保留在日志中。不撤销副作用，不自动重放工具；孤立结果等格式
错误明确拒绝，不猜测修复。

审核继续原 run，需要现有公开 checkpoint：

```python
checkpoint = dagent.RunCheckpoint.model_validate_json(saved_checkpoint_json)
runner.validate_checkpoint(checkpoint)  # 只读，不消费审核
calls = checkpoint.pending_tool_invocations
stream = runner.resume_stream(
    checkpoint.review.approve(), checkpoint=checkpoint, persistence=options,
)
```

审核最终批次确认后即可恢复，无需等上一条流结束，此时只剩 SDK 的同步清理。仍在运行的
run（包括最终批次尚未确认的）会在消费审核决定前被拒绝。
投递归属跟随生产任务，旧读取迭代器不会影响新续审的取消或准入判断。
整体保存 checkpoint，不组装私有 pending batch。pending_tool_invocations 返回隔离的、
仍可能执行的剩余调用。SDK 检查结构、plan 指纹和已注册能力及 skill；企业策略仍由宿主检查。
审核最终批次 ACK 后立即释放本地重状态和 checkpoint，不受普通无限保留设置影响。
可在新 Runner 注册兼容能力后恢复。SDK 保留当前进程防重放 ID；跨 worker 审核去重由宿主事务负责。

## 体积、内存与失败

持久化模式不构建 ToolLoop trace，包括子 ToolLoop。停止发送 trace 快照、重复工具正文、
重复审核事件和完整 run.finished。普通终态不包含 conversation、model_thread、历史集合或
checkpoint，也没有新增 RunSummary。展示增量与最终权威消息仍各传一次；真实消息更新可以
提交新版本。审核 checkpoint 保留 V9 已有的完整状态和重复字段。

宿主持有历史时配置 `Runner(max_retained_runs=0)`。普通终态缓存继续服从原设置，默认 None
仍无限保留；普通持久化终态不缓存 checkpoint。有效上下文、当前响应、一个待确认批次及未消费
事件仍占用内存，事件队列用量取决于宿主读取速度。已使用的 run/review ID 持续增长，
不承诺进程总内存绝对恒定。

执行失败通过需要确认的 failed 终态批次报告。交付失败通过 run.failed 报告错误类型、消息、
delivery、失败批次及最后确认序号/版本，不附完整结果、不重发失败批次。SDK 不能声称未确认
终态已经保存。

持久化 cancel() 接受停止请求后不等待宿主保存。普通批次正在等待 ACK 时，确认先推进
会话基线，再基于该 revision 提交 cancelled 终态，以终态 ACK 判断取消完成。任何终态
开始交付后，cancel() 返回 False：执行已经结束，已有终态仍需 ACK、拒绝或超时。
关闭流会解除等待并中止工作，包括终态交付。审核终态确认或交付放弃时释放本地审核状态；
断连后根据已提交日志判断中断状态。
关闭 Runner 会让持久化读取正常结束，不转发生产任务的关闭取消异常；主动取消读取任务仍
传播 CancelledError。关闭操作不会声称未经确认的终态已经保存。

资源只包含引用。宿主通过 Runner.resolve_runtime_path 读取并在 ACK 前保存必需字节，
迁移工作区时自行恢复逻辑路径。SDK 不扫描或复制存储。可变附件与不可变结果、压缩归档继续
保持不同语义。每次交付用小容量缓存抑制已 ACK、路径与内容摘要相同的引用重复通知；
摘要变化、无摘要引用和可变附件仍通知。缓存淘汰及新交付可能再次通知，宿主仍需幂等。
上传工具位于公开 dagent.artifacts，无需导入运行时私有模块。
