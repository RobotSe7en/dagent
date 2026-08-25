# 非执行型 DAG 设计

当应用需要先获得类型化 DAG 候选，再自行决定是否以及保存或执行到哪里时，可使用
`Runner.design_dag(...)`。一次设计调用可以从自然语言创建 DAG、修改完整的现有
`DAGSpec`、报告无需修改，或回答有关图的问题。

该 API 与执行明确分离。它会调用一次已配置的 chat provider，但不会创建 Run、
`PendingReview`、checkpoint、workspace artifact 或 capability result，也绝不会调用
capability handler。

## 创建候选设计

```python
import dagent


@dagent.tool
def summarize(text: str) -> str:
    return text[:200]


runner = dagent.Runner(provider=provider, capabilities=[summarize])
result = await runner.design_dag(
    "创建一个汇总字符串输入的 DAG。",
    agent=dagent.DagAgent(capabilities=["tool.summarize"]),
)

if isinstance(result, dagent.DAGDesignProposal):
    candidate = result.candidate  # 完整且已校验的 DAGSpec
    print(result.summary)
elif isinstance(result, dagent.DAGDesignFailure):
    for diagnostic in result.diagnostics:
        print(diagnostic.code, diagnostic.message)
```

可运行的离线 `MockProvider` 示例见
[`examples/dag_design.py`](../../examples/dag_design.py)。

可选的 `agent` 是普通声明式 `DagAgent`。它的 profile、context policy、skills、已注册
子 agent 和 `capabilities` scope 与动态 DAG run 使用同样的解析方式。省略时使用 Runner
通常可见的 capability catalog。调用方不提交 capability definition 副本；Runner catalog
始终是权威事实来源。

## 修改现有 DAG

传入权威的当前 `DAGSpec`，必要时再提供中立的选择提示：

```python
result = await runner.design_dag(
    "只修改选中的 writer 步骤，让它输出 Markdown。",
    agent=agent,
    current=current_spec,
    selection=dagent.DAGDesignSelection(node_ids=("write_report",)),
    conversation=previous_result.conversation,
)
```

Proposal 总是包含完整候选 `DAGSpec`，不会返回 JSON Patch。修改时 SDK 会保留当前 spec
id 并递增 version；保留的节点和 artifact 维持当前顺序；未改边按当前顺序排列，新边随后
按候选顺序追加。当 node id、payload kind、capability id 和 arguments 均未改变时，会复用
invocation id。设计调用不会改写保留节点的 runtime status。

模型必须返回所有保留的顶层字段、artifact、节点、边和内嵌 DAG。因此
`input_schema`、`artifacts`、`output`、`metadata`、节点标题、边 reason 和其他公开字段都能
在编辑中保留。`DAGEdge` 没有 SDK id 字段；边的视觉身份、layout、semantic diff 和部分
采纳由 host 负责。

## 结果变体

`DAGDesignResult` 是用 `type` 区分的联合类型：

- `DAGDesignProposal`（`type="proposal"`）返回 `candidate`、`summary` 和 diagnostics。
- `DAGDesignNoChange`（`type="no_change"`）返回简短 `summary`，不复制返回当前候选。
- `DAGDesignAnswer`（`type="answer"`）在 `answer` 中返回说明。
- `DAGDesignFailure`（`type="failure"`）针对非法模型输出、非法候选或超出 catalog scope
  的 capability 返回类型化 diagnostics。

每种结果都返回新的 `ConversationState`、可选的 provider `ModelTokenUsage`（`usage`），以及
请求估算 `ContextUsage`（`context_usage`）。输入 conversation 会被复制，绝不会原地修改。
后续调用需显式传入返回的 conversation；不传时，各次调用彼此隔离。

## 校验与 catalog 权威性

模型响应由 `StructuredOutputFormat` 约束。Proposal 在返回前还会依次经过四个 fail-closed
边界：

1. 严格 JSON 解码与 DAGSpec JSON Schema 校验；
2. 严格 Pydantic 解析为公开 `DAGSpec` 图；
3. 依据已解析 Runner catalog 做 capability 查找和 arguments 解析；
4. 现有 `validate_dag_spec(...)` 结构和 dataflow 校验。

Capability `kind`、risk 和推导出的 boundary 均来自 catalog。模型候选 JSON 中的值不能扩大
或替换它们。未知、disabled 或超出 scope 的 capability 会产生带稳定诊断 code 的
`DAGDesignFailure`，不会调用 handler。

## 确定性检查

不需要模型时使用 `inspect_dag_spec(...)`：

```python
diagnostics = dagent.inspect_dag_spec(spec)
for item in diagnostics:
    print(item.severity, item.code, item.node_id, item.path, item.message)
```

它返回 `DAGDiagnostic` tuple。每项包含 `severity`、稳定 `code`、`message`，并在 validator
能够定位时包含 `node_id` 和 `path`。合法 spec 返回空 tuple。该函数完全确定，不调用
provider；`validate_dag_spec(...)` 保持现有 fail-fast 异常契约不变。

持久化、带 compare-and-swap 的 revision、用户或组织策略、layout、audit 和 review workflow
不属于此 SDK 设计 surface，应由 host 应用负责。
