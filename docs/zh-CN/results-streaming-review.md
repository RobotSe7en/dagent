# 结果、流式输出和 Review

`Runner.run(...)` 会为每个公开 target 返回 `RunResult`：`AutoAgent`、`ToolAgent`、
`DagAgent`、`Dag` 和 `DAGSpec`。

## Run Results

```python
messages = [{"role": "user", "content": "Write the report."}]
result = await runner.run(agent, messages=messages)

print(result.kind)
print(result.status)
print(result.output_text)
print(result.trace)
```

对于 agent targets，`result.messages` 只包含当前 run 生成的 messages。把它们追加到
调用方维护的 conversation 中：

```python
messages += result.messages
messages.append({"role": "user", "content": "Continue with one more detail."})
result = await runner.run(agent, messages=messages, state=result.state)
```

`result.state` 包含 dagent 可恢复的 internal thread、DAG、trace、pending review 和
static DAG metadata。`RunResult.output_text` 是规范的最终答案。

如果宿主在执行前已经创建了 run record，可以传入最终 run id：

```python
result = await runner.run(
    agent,
    messages=messages,
    run_id="enterprise_run_123",
)
```

同一个 `run_id` 会用于 `run.started`、每个 stream event envelope、最终 `RunState`
以及默认 run workspace 名称。宿主传入的 run id 必须是单个目录名。如果同时传入
`state`，`run_id` 必须匹配 `state.run_id`。

## 持久化 Checkpoint

`RunState` 继续表示普通 conversation continuation 使用的可变执行状态。跨进程 review
continuation 应使用 `RunCheckpoint`；它还包含 SDK 已解析的执行 plan 和累计 usage：

```python
checkpoint = result.checkpoint
if checkpoint is None:
    raise RuntimeError("SDK result did not contain a checkpoint")

saved_json = checkpoint.model_dump_json()

# 在之后的进程中，先构造兼容的 Runner：
restored = dagent.RunCheckpoint.model_validate_json(saved_json)
pending = restored.state.pending_review
if pending is not None:
    decision = dagent.ReviewHandle(pending).approve()
    result = await new_runner.resume(decision, checkpoint=restored)
else:
    messages.append({"role": "user", "content": "Continue."})
    result = await new_runner.run(agent, messages=messages, checkpoint=restored)
```

JSON 保存位置和方式由 host 决定。SDK 定义 checkpoint 的校验与 resume 语义，但不负责
数据库、租户生命周期、RBAC 或 retention policy。

Checkpoint resume 时，`Runner` 会验证精确的 capability、agent 和 skill IDs 是否可用，
根据已解析的 profiles 和局部 loop limits 重建目标专属 runtime，恢复累计 usage，然后才应用
review decision。缺失或禁用的 capabilities、缺失的 skills 都会 fail closed。同一个 runner
仍可通过内存中的 checkpoint cache 使用 `resume(decision)`。

Resolved plan 使用冻结的 profile snapshots 和 SDK 定义的 canonical SHA-256 fingerprint。
Fingerprint 用于发现意外 payload 修改，不是签名或 authentication boundary。

SDK 新生成的 checkpoint 使用 schema V2，并记录 `planner_frontend`。当 frontend 为
`sdk_builder` 时，resolved plan 还会冻结 mandatory、versioned `generate-dag` skill 的完整
内容和 SHA-256 digest。即使新 Runner 的全局 frontend 不同，resume 也会重建 checkpoint
中记录的 frontend 和 skill。Schema V1 checkpoint 继续可读，并始终表示 `typed_spec`。

同一个 `Runner` 上的普通 continuation 可以使用 `run(..., state=...)`；最新匹配的内存
checkpoint 会恢复 usage 和原 limits。跨进程 continuation 应向 `run(...)` 传入
`checkpoint=...`。过期 state 会被拒绝，恢复出的 limits 不能替换或扩大。
`resume(..., state=...)` 已弃用，因为单独的 `RunState` 无法恢复目标专属 profiles、limits
和精确执行语义。它暂时作为显式 legacy path 保留；新的持久化代码应保存
`result.checkpoint`。

Checkpoint review decision 在同一个 `Runner` 中只能消费一次。如果 continuation 失败，
`Runner.run_checkpoint(run_id)` 会返回 terminal checkpoint。如果 resume 期间抛出
`ExecutionLimitExceeded`，同一 checkpoint 和更新后的 usage 也可通过
`error.checkpoint`、`error.usage` 获取。Durable host 必须在执行前原子认领 review ID，
以避免不同进程间重放；无持久化 SDK 无法单独保证 exactly-once side effects。

`RunResult.model_dump(...)` 继续保持原有的 `state` 和 `output_text` shape。需要跨进程
review resume 时，请单独持久化 `result.checkpoint`。

新持久化的 `RunState` payload 包含 `schema_version: 2`。不含该字段的 payload 会按
version 1 读取，V1 使用 `typed_spec` 语义。宿主遇到显式声明的不支持版本时应拒绝，
而不是静默迁移。

## Run-Wide Execution Budget

当 host 需要 root agent、DAG nodes、map/loop/subgraph、validation 和 subagents 共享同一个
上限时，使用 `ExecutionLimits`：

```python
result = await runner.run(
    agent,
    messages=messages,
    limits=dagent.ExecutionLimits(
        max_total_operations=40,
        max_model_turns=24,
        max_capability_calls=20,
    ),
)

print(result.usage.model_turns)
print(result.usage.capability_calls)
```

每次模型请求都会原子预留一个 model turn，包括 route、planning、validation、retry 和
subagent turns。每次 tool、MCP、memory 或 agent capability 执行都会预留一个 capability
call；两者都会增加 total operations。并发工作会在开始前原子预留。超过限制时，SDK 会在
不允许的外部操作开始前抛出 `ExecutionLimitExceeded`。

`ToolAgent.max_steps` 和 `DagAgent.max_cycles` 仍是局部 loop limits，run-wide budget 不会
改变其含义。Limits 和 usage 会写入 checkpoint；review resume 和普通 checkpoint
continuation 都不能重置或扩大它们。

## 静态 DAG Result Helpers

静态 DAG 在同一个 result object 上暴露 DAG-oriented helpers：

```python
result = await runner.run(dag, graph_input="dagent", workspace_root="runs")

print(result.workspace_path)
print(result.node_output("write_report"))
print(result.node_value("search"))
print(result.artifact_state("report").status)
```

当 `workspace_root` 不是绝对路径时，它会相对 runner workspace 解析。使用默认
runner workspace 时，运行 artifact 会写到 `.dagent/runs/<run_id>`。

`DAGRun` 仍然是 API projections 使用的 schema，并且可以通过静态 DAG runs 的
`result.dag_run` 访问。

本地 WebUI 会把 run workspace 中的文件列为运行 artifacts。文本、Markdown 和代码
artifacts 使用文本预览接口；PDF artifacts 会通过 artifact download 接口拉取文件，
并在浏览器中渲染。如果 `~/.dagent/config.yaml` 中启用了 `onlyoffice` 配置，DOCX、
XLSX 和 PPTX artifacts 会通过 ONLYOFFICE Document Server 以 view mode 打开。如果同时
启用 `run_artifact_edit_enabled`，这些 artifacts 会以关闭 autosave 的 edit mode 打开；
只有用户显式点击 Save 时才会覆盖 run workspace 中的文件，并且不会修改历史 trace。
artifact 文件元数据会包含 `version`，WebUI 据此在文件变化时让 Office 预览缓存失效。
未配置 ONLYOFFICE 时，这些 Office 文件会回退到内置的浏览器渲染器。

WebUI 可以通过持久化运行历史查看历史编排 runs。选择某个历史动态或静态编排 run
会恢复其 trace、输出和 artifacts 供检查，但不会修改当前动态草稿或已保存的静态 DAG。

## Streaming

`Runner.stream(...)` 运行 target，并 yield 类型化的 `RunStreamEvent` objects。事件
envelope 统一包含：`type`、`data`、`sequence` 和 `run_id`。

```python
async for event in runner.stream(agent, messages=messages):
    if event.type == "response.content.delta":
        print(event.data.delta, end="")
    elif event.type == "trace.updated":
        print(event.data.trace.status)
    elif event.type == "review.required":
        print(event.data.message)
    elif event.type == "run.finished":
        print(event.data.result.output_text)
```

如果需要从另一个 task 停止 streamed run，请保存 `run.started` envelope 中的
`run_id`，并传给 `Runner.cancel(...)`：

```python
cancelled = await runner.cancel(run_id)
```

活动 run 接受取消时返回 `True`；run 已不再活动时返回 `False`。取消信号会从 runtime
继续传递到 async capability call、MCP call 和内置 shell 的进程组。Python 无法强制终止
线程中正在执行的任意同步用户代码，因此长时间运行的自定义 function tool 仍应设置明确
边界，并在它自己的外部操作被取消后及时返回。

运行离线 streaming 示例：

```bash
uv run python -m examples.streaming
```

## Event Protocol

| Event type | 主要字段 |
| --- | --- |
| `run.started` | `event.data.kind`；envelope `run_id` 是最终 run id |
| `response.started` | response identity fields |
| `response.reasoning.delta` | `event.data.delta`，结构化 provider reasoning 或 `<think>...</think>` 中的文本 |
| `response.content.delta` | `event.data.delta`，reasoning 外的 assistant answer text |
| `response.finished` | response identity fields |
| `capability.call.started` | `event.data.invocation_id`, `event.data.capability_id`, `event.data.arguments`，可选 DAG context fields |
| `capability.call.completed` / `capability.call.failed` | invocation fields、result content、可选 DAG context fields |
| `dag.updated` | `event.data.dag`，仅当 DAG 改变时发出 |
| `trace.updated` | `event.data.trace`，仅当 trace 改变时发出 |
| `validation.started` / `validation.passed` / `validation.retry` | `event.data` |
| `review.required` | `event.data.review_id`, `event.data.kind`, `event.data.message` |
| `run.finished` | `event.data.result` |
| `run.failed` | `event.data.message`, `event.data.error_type` |

每个 streamed text source 都由 `response.started` 和 `response.finished` 包围。按
`response_id` 聚合 deltas，不要只依赖 ordering 或 `model_step`。

## Review 和 Resume

当 run 需要 review 时，`RunResult.requires_review` 为 true，`RunResult.review` 包含
handle：

```python
first = await runner.run(
    agent,
    messages=[{"role": "user", "content": "Write the report."}],
)

if first.requires_review and first.review is not None:
    result = await runner.resume(first.review.approve())
```

批准和拒绝都可以携带 reviewer feedback。拒绝时可以用它说明原因，或引导 agent
改用另一条执行路径：

```python
if first.requires_review and first.review is not None:
    result = await runner.resume(
        first.review.reject(feedback="不要读取该路径，改为总结 README.md。")
    )
```

Streaming resume 使用相同 event contract：

```python
if first.requires_review and first.review is not None:
    async for event in runner.resume_stream(first.review.approve()):
        if event.type == "response.content.delta":
            print(event.data.delta, end="")
        elif event.type == "run.finished":
            print(event.data.result.output_text)
```

如果 pending review 是重启后恢复出来的，传入保存的 checkpoint：

```python
restored = dagent.RunCheckpoint.model_validate_json(saved_json)
pending = restored.state.pending_review

if pending is not None:
    async for event in runner.resume_stream(
        dagent.ReviewHandle(pending).approve(),
        checkpoint=restored,
    ):
        ...
```

`review.required` stream event 是轻量信号。Review UI 应基于后续 `run.finished` result
中携带的完整 pending review 构建。

DAG review 会授权已批准 DAG 版本的执行。DAG review 批准后，提交审核的这张 DAG
中的所有节点都可以按审核时展示的 boundary 执行。Replan 会生成新的 DAG 版本；当所选
review level 要求审核时，变更后的 DAG 需要重新 review。Boundary 授权绑定到人工批准
的 DAG 版本，而不是所有 lifecycle status 为 `approved` 的 DAG object；静态 DAG 和
fast no-review revision 如果节点越过 boundary，仍会 fail closed。

Capability review 可以由 risk policy 触发，也可以在 tool-agent 执行过程中由 boundary
override 请求触发。Boundary override review 使用 `kind == "capability_review"`，并在
payload 中包含 `payload.reason == "boundary_violation"` 以及原始错误。批准只会执行这一次
pending capability call，不会扩大后续调用的 run boundary；拒绝会把 denial 消息反馈给
agent。如果 review decision 携带 `feedback`，该文本会进入 agent 的后续上下文。
