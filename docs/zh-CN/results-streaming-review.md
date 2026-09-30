# 会话、结果、流式事件与审核

达智 0.8 将“跨 run 的多轮会话”和“同一 run 的审核续跑”明确分开。这是一次有意的
破坏性变更：

- `ConversationState` 用于在多个独立 run 之间延续会话；
- `RunCheckpoint` 用于恢复停在审核门上的同一个 run；
- 原始 OpenAI `messages` 和 `RunState` 不再是 continuation 输入。

## 延续多轮会话

每次调用只提交本轮用户输入，并传入上一轮返回的有界会话：

```python
first = await runner.run(agent, input="记住发布颜色是蓝色。")

second = await runner.run(
    agent,
    input="发布颜色是什么？",
    conversation=first.conversation,
)
print(second.output_text)
```

模型失败时，在展示 `output_text` 前检查 `result.status == "failed"` 和
`result.error.code`。仅有推理或空响应分别返回 `reasoning_only_response` 和
`empty_response`。最终文本保持为空；此前工具输出保留在 conversation 与 trace，
不会被拼成替代答案。最后一条 `AssistantMessage.model_call` 保存终止信息及重试次数。
详见[模型失败与 Token 计数](model-context-and-reasoning.md)。

`ConversationState` 与 provider 无关，包含有类型的用户消息、助手消息、工具结果、可选
摘要和 revision。它不包含 system prompt 或 provider 请求参数。

Runner 级 `extra_system_prompt` 属于 resolved run plan，而不是 conversation。
Review resume 会恢复 checkpoint 中冻结的值。

不要自行把 `result.new_items` 追加回会话。`new_items` 是当前 run 的审计增量；
`result.conversation` 已经是下一轮应传入的完整有界状态。

## 实际输入模型的内容

每次模型调用前，统一的上下文组装器会按以下顺序创建 OpenAI-compatible 请求：

1. 当前 system prompt；
2. 可选的早期会话摘要；
3. 最近的有类型会话或内部 model thread；
4. 工具 schema 或 planner response schema。

助手推理内容会保留在 `AssistantMessage.reasoning` 中供展示和审计。
`ContextPolicy.reasoning_replay` 控制请求投影：默认 `active_run` 只在当前 run 的
模型/工具步骤之间回放；`none` 从不回放；`all_runs` 还会回放更早的 conversation runs。

工具调用与对应工具结果始终保持结构完整。工具结果文本可以在模型上下文中做首尾截断，
但不会删除审计记录，也不会破坏 `tool_call_id` 配对。

## 上下文限制与压缩

私有 vLLM provider 在可用时通过 `/tokenize` 获取精确请求计数与 `max_model_len`。
默认自动使用探测到的 context value；显式值会覆盖它，但不能超过已探测的 server limit。
探测失败会 warning 并 fallback 到 128K（131,072 tokens）context window。
DeepSeek 官网模型使用官方公布的上限；context usage 中的 `model_context_window_tokens`
区分模型规则识别与服务端精确计数。输出长度默认不设置：

```python
provider = dagent.Provider(
    base_url="http://localhost:8000/v1",
    model="local-model",
    context_window_tokens=None,
    max_output_tokens=None,
)
```

使用 `ContextPolicy` 配置每个 agent 的上下文行为：

```python
agent = dagent.ToolAgent(
    profile="conversation",
    context=dagent.ContextPolicy(
        reasoning_replay="active_run",
        compaction_trigger_ratio=0.8,
        compaction_retain_ratio=0.16,
        summary_max_tokens=8192,
        compaction_reasoning_effort="low",
        max_tool_result_tokens=2048,
        max_total_tool_result_tokens=16384,
    ),
)
```

达到阈值后，达智会先总结完整旧 run；需要时从 active request 省略最旧的已回放
reasoning；然后总结过大 active run 中已完成的中间步骤。该过程完全由 token 驱动，
不存在最低保留轮数。当前用户输入、未闭合工具链和最新原子步骤会保留。正常压缩路径调用
当前模型并计入一次 model turn telemetry；摘要调用失败时使用确定性有界摘要并记录
fallback 原因。输入仍超过硬预算时，会进一步缩短工具结果展示，以软阈值为目标，
保留状态、恢复信息和短摘录。必须保留的输入仍放不下时，在 generation 前抛出
`ContextWindowExceeded`。详见[工具结果恢复](tool-result-recovery.md)。
compactor 请求本身有独立的输出限制和 reasoning effort。`ContextSummary` 会记录 source
是否被截断、provider usage、模型调用 metadata 和上下文估算。摘要 reasoning 会被丢弃；
后续投影摘要正文及独立保留的结果索引入口。

`result.context_usage` 会提供精确/估算 token 数、发现的 server limit、reasoning
回放/省略、保留/压缩 item 数、工具结果截断数以及压缩方法。
`context.compaction.finished` 要求实际发生摘要压缩；单纯结果存储或展示缩短不会发送该事件。

## 推理内容与 provider usage

OpenAI-compatible 的 `reasoning_content`/`reasoning` 字段和
`<think>...</think>` 内容会统一进入 `AssistantMessage.reasoning`，可见回答单独保存：

```python
for item in result.new_items:
    if isinstance(item, dagent.AssistantMessage):
        print(item.content)
        print(item.reasoning)
        print(item.usage)  # provider 提供时可用
```

类型化流事件中，推理增量使用 `response.reasoning.delta`，回答增量使用
`response.content.delta`。

## 大型工具与 MCP 结果

通常情况下，256 KiB 以内的工具/MCP 文本内联保存；展示预算截断也会触发原文保存。
更大的文本、二进制 value 和 MCP
二进制 payload 会原子写入 run workspace，并转换为带校验和的 `ContentReference`。
模型只看到有界预览和 workspace 相对引用。

```python
runner = dagent.Runner(
    workspace="agent-workspace",
    runtime_directory=".runtime",
    provider=provider,
    result_storage_policy=dagent.ResultStoragePolicy(
        max_inline_bytes=256 * 1024,
    ),
)
```

这个 runner 的结果目录是 `<run-workspace>/.runtime/results`。
`ResultStoragePolicy` 控制内联大小阈值和单次 shell 采集上限（默认 64 MiB），
存储位置由 runner 统一拥有。低于内联阈值但被模型展示预算截断的文本也会保存。
最低信息预算、读取权限、分页、压缩索引与故障语义见[工具结果恢复](tool-result-recovery.md)。

SDK 只负责 run workspace 内的标准化；长期上传、保留策略、访问控制和 URL 生成由 host
负责。

上传的工作文件可以修改或删除，包括等待审核期间；生成运行结果或恢复审核时不重新
校验上传时的附件元数据。这与上文受完整性校验保护的 `ContentReference` 结果数据
不同。工作区续聊与原始快照归属见[编辑上传文件](runner-and-configuration.md#编辑上传文件)。

静态 DAG trace 会保留外置 value 以及 `stdout`/`stderr`/error 字段的类型化引用。
Map node 的父级 value 保持有界；只有获准的下游 value expression 读取时，executor
才会解析对应的索引引用。这样既保证 checkpoint 可安全序列化为 JSON，也保留完整
dataflow 和审计恢复能力。

## 恢复审核

需要人工审核时请设置 `review="careful"`。默认 `fast` 对所有可审核操作（包括越界）自动批准，不暂停。

run 等待审核时应持久化完整 checkpoint：

```python
result = await runner.run(agent, input="写发布说明。", review="careful")

if result.requires_review:
    checkpoint_json = result.checkpoint.model_dump_json()
```

恢复后通过专用 API 续跑：

```python
checkpoint = dagent.RunCheckpoint.model_validate_json(checkpoint_json)
decision = result.review.approve(feedback="继续，保持简洁。")

resumed = await runner.resume(
    decision,
    checkpoint=checkpoint,
)
```

0.8 不再提供 `Runner.run(..., checkpoint=...)`、`run(..., state=...)` 或
`resume(..., state=...)`。checkpoint 会冻结 profile、capability/skill scope、
capability definition 指纹、策略、限制、planner 模式和已消耗预算，避免审核在不同
语义下恢复。其中包括 context window 和 output reserve；即使 provider 配置在恢复期间
发生变化，续跑再次进入审核门时，新 checkpoint 仍沿用原先冻结的限制。

同一 checkpoint 流程也适用于受支持的静态 DAG Agent 节点审核。checkpoint 会保存挂起的
节点 invocation 和内部 tool-agent state，并将已注册 Agent 的内部工具纳入已解析 capability
scope。直接 Agent 节点的执行配置会写入指纹，profile 或运行时设置变更时不会悄然改变续跑。
支持的拓扑和策略行为见[静态 DAG](static-dag.md#agent-节点工具审核)。

### 一次审核整轮工具调用

运行时先预检同一模型回复中的全部工具；只要有待审核项，整轮都等待，包括低风险工具。
`review.capability_calls` 按模型顺序列出待审项。只有一个待审项时，继续使用
`pending_review.capability_call` 和 `payload`；多个待审项使用
`pending_review.capability_calls`，单项字段为空。批量条目包含调用身份、参数、风险、
原因和边界详情。`queued_call_count` 表示这一轮其他仍在等待的调用数量。

```python
choices = [
    dagent.CapabilityReviewDecision(invocation_id=call.invocation_id, approved=index != 1)
    for index, call in enumerate(result.review.capability_calls)
]
resumed = await runner.resume(
    result.review.decide(choices, feedback="拒绝第二个待审调用。"),
    checkpoint=result.checkpoint,
)
```

每个待审调用必须恰有一个决定。遗漏、重复、未知 ID 在执行前报错，审核仍可重新提交。
`ReviewDecision.capability_decisions` 与整批 `approved` 互斥。
现有 `review.approve()` / `review.reject()` 和
`ReviewDecision(review_id=..., approved=...)` 继续有效；批量场景表示决定全部待审项。
旧 `review.capability_call` 仍供单项审核使用。

提交后按原始顺序处理调用：拒绝项返回 `denied`，普通工具错误返回 `failed`，其余继续。
不会因审核将剩余调用标为 `skipped`，也无需额外调用模型重新发起这些工具。
取消、steer 和必需结果存储失败仍按既有语义中止。执行时再次检查边界；出现新的授权
要求时暂停剩余队列，不重复执行已完成前缀。只有已批准的边界项才能增加当前 run 的路径授权。

Checkpoint 保存完整顺序、决定和执行位置，也适用于已支持的静态 DAG 直接 Agent 节点。
批次不跨模型回复，也不合并不同 DAG 节点；处理完当前工具轮后才再次调用模型。

本地 API 的现有审核恢复路由支持 `{"approved": true}`，或逐项决定：

```json
{"capability_decisions": [{"invocation_id": "call_1", "approved": true}, {"invocation_id": "call_2", "approved": false}]}
```

逐项请求不得同时传入 `approved`。旧请求未提供这两个字段时，保留默认整批批准行为。
WebUI 要求每项明确选择，支持全选批准/拒绝、查看完整参数以及一次提交。
审核认领和锁仍由宿主管理。确定性示例见
[batch_tool_review.py](../../examples/batch_tool_review.py)；升级待审运行前参阅
[迁移说明](migration.md#unreleased)。


## 调整正在运行的 ToolAgent

使用 `Runner.steer(...)` 可以在不取消当前模型调用或 capability 调用的前提下，为正在执行的
根 `ToolAgent` run 补充文本指令。为 run 指定明确 id，在 task 中启动它，并等应用确认 run
已经开始后再提交 steer：

```python
run_task = asyncio.create_task(
    runner.run(
        agent,
        input="起草发布说明。",
        run_id="release_note_run",
    )
)

# 稍后在 run 仍活跃时调用。
receipt = await runner.steer(
    "release_note_run",
    "重点说明破坏性 API 变化，不要写性能数据。",
)
assert receipt.status == "queued"

result = await run_task
```

`steer` 在消息成功排队后立即返回。Tool loop 会在下一个协作式安全点，将排队消息按 FIFO
顺序分别加入为 `UserMessage`：模型调用前、当前模型调用后，或当前 capability 调用后。
它不会中断已经开始的模型请求或 capability。如果一条 assistant 响应请求了多个
capability，当前调用会执行完毕，尚未开始的调用会被跳过，让模型根据最新指令重新判断。

邮箱最多容纳 32 条消息。拒绝会抛出明确异常：

- `RunNotActiveError`：run 尚未开始或已经结束；
- `RunNotSteerableError`：活跃 run 是 DAG、正在 validation，或正在等待 review；
- `SteerQueueFullError`：已经有 32 条消息排队。

只有根 tool-agent 执行支持 steer。`AutoAgent` 只有在 route 解析为 `tool` 后才支持；
`dynamic_dag` route 会被拒绝。静态和动态 DAG 始终不支持 steer。嵌套子 agent 不会消费
根邮箱；子 agent 返回后，由根循环应用这些指令。

`resume(...)` 或 `resume_stream(...)` 正在继续已批准的 tool-agent review 时同样可以
steer。已经停在 `awaiting_review` 的 run 会拒绝 steer；此时应把指导信息放在 review
decision 的 `feedback` 中。结果 validation 会收到初始请求和所有已应用 steer，
`RunState.user_request` 则始终保留初始请求。

Steer 不会增加 `ToolAgent.max_steps`。如果已没有下一次模型调用额度，排队指令会以
`step_limit_exhausted` 原因丢弃，run 状态为 failed。其他丢弃原因包括
`run_cancelled`、`run_failed` 和 `runner_closed`。可运行示例见
[`examples/steering.py`](../../examples/steering.py)。

## 流式调用

```python
async for event in runner.stream(agent, input="准备答案。"):
    if event.type == "response.reasoning.delta":
        show_reasoning(event.data.delta)
    elif event.type == "response.content.delta":
        show_content(event.data.delta)
    elif event.type == "context.compaction.finished":
        show_context_usage(event.data.usage)
    elif event.type == "steer.queued":
        show_queued_steer(event.data.steer_id, event.data.content)
    elif event.type == "steer.applied":
        show_applied_steer(event.data.steer_id)
    elif event.type == "steer.discarded":
        show_discarded_steer(event.data.steer_id, event.data.reason)
    elif event.type == "run.finished":
        result = event.data.result
```

审核续跑对应 `resume_stream(decision, checkpoint=checkpoint)`。
`run.finished` 中的 `RunResult` 与非流式调用一致。序列化 result 包含
`output_value`；静态 run 中它是 `DAGSpec.output` 的精确解析值，而 `output_text` 保持
兼容 rendering。`RunStreamEvent.model_validate(...)` 会恢复同样的 typed event payload，
并根据 envelope `type` 保留精确 data class，即使多个 event payload 的字段完全相同。

### 审计 Tool 执行失败

必需结果存储失败会立即停止 Tool 执行，包括审核恢复路径。
`Runner.run()` 和 `Runner.resume()` 抛出公共异常
`dagent.RunExecutionError`，其 `result` 是失败的 `RunResult`；
原始存储异常保留在 `__cause__` 中供诊断。

```python
try:
    result = await runner.run(agent, input="生成报告")
except dagent.RunExecutionError as exc:
    audit = exc.result.model_dump(mode="json")
```

`Runner.stream()` 和 `Runner.resume_stream()` 则发出唯一终态事件
`run.failed`。`event.data.result` 携带同样的失败快照；原有的
`message`、`error_type` 保留，`error_type="ResultStorageError"`。
无法形成快照的其他错误仍使用 `result=None`。

通过失败调用的 `trace` 条目读取调用参数和 `CapabilityResult`。
执行状态与存储失败独立：工具已完成时，
`capability_execution.result.status="completed"`，但调用 trace 和运行状态为失败。
`retention.storage_warnings` 标明失败字段和 I/O 错误，
`retention.unavailable_fields` 标明省略的二进制数据。
快照用于审计，不能作为可续跑的模型会话。

`result.checkpoint` 和 `runner.run_checkpoint(result.run_id)` 均为 `None`。
SDK 不会重试工具，也不会在此失败后调用结果验证模型。
宿主负责审计持久化和 Workspace 发布；工具已产生的副作用不会回滚。
流处理示例见 `examples/streaming.py`。

内部投影计数与最终 HTTP 请求推理统计的区别，详见
[请求推理可观测性](model-context-and-reasoning.md#观察请求实际携带的推理)。
