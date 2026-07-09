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

## 持久化 State

如果持久化完整 result payload，可以用 `RunResult.model_validate(...)` 恢复当前 SDK shape：

```python
saved_payload = result.model_dump(mode="json")
restored = dagent.RunResult.model_validate(saved_payload)

if restored.requires_review and restored.review is not None:
    result = await runner.resume(restored.review.approve(), state=restored.state)
else:
    messages.append({"role": "user", "content": "Continue."})
    result = await runner.run(agent, messages=messages, state=restored.state)
```

普通 continuation 使用 `run(..., state=...)`。如果保存的 state 正在等待 review，请用
`resume(..., state=...)` 继续该 checkpoint；`run(..., state=...)` 会拒绝 awaiting-review
states，避免绕过 review gates。

持久化的 `RunState` payload 包含 `schema_version: 1`。不含该字段的 payload
会按 version 1 读取。宿主遇到显式声明的不支持版本时应拒绝，而不是静默迁移。

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

如果 pending review 是重启后恢复出来的，传入保存的 state：

```python
restored = dagent.RunResult.model_validate(saved_payload)

if restored.requires_review and restored.review is not None:
    async for event in runner.resume_stream(
        restored.review.approve(),
        state=restored.state,
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
