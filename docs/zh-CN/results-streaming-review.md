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

## 静态 DAG Result Helpers

静态 DAG 在同一个 result object 上暴露 DAG-oriented helpers：

```python
result = await runner.run(dag, graph_input="dagent", workspace_root="runs")

print(result.workspace_path)
print(result.node_output("write_report"))
print(result.node_value("search"))
print(result.artifact_state("report").status)
```

`DAGRun` 仍然是 API projections 使用的 schema，并且可以通过静态 DAG runs 的
`result.dag_run` 访问。

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
