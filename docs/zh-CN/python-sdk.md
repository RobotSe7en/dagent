# Python SDK 参考地图

本页映射当前公开 Python SDK surface。它是参考地图，不是完整教程。第一次运行请从
[快速开始](quick-start.md)开始，然后阅读下方链接的功能指南。

安装包名是 `dagent-ai`，导入名是 `dagent`：

```bash
pip install dagent-ai
```

```python
import dagent
```

## 常用入口

| 任务 | 从这里开始 |
| --- | --- |
| 配置 runtime、provider、MCP、profiles、validation | [Runner 和配置](runner-and-configuration.md) |
| 注册 Python tools 或 MCP tools | [Capabilities](capabilities.md) |
| 选择 agent 类型 | [Agents](agents.md) |
| 注册用于单层委派的子 agent | [Agents](agents.md#子-agent-委派) |
| 在代码中构建静态 workflow | [静态 DAGs](static-dag.md) |
| 使用 skills 和 managed skill installs | [Skills](skills.md) |
| 持久化、stream、review 或 resume runs | [结果、流式输出和 Review](results-streaming-review.md) |
| 运行示例 | [Examples](../../examples/README.md) |

## 公开 Surface

大多数应用会从 `Runner`、`@dagent.tool`、`ToolAgent`、`AutoAgent`、`DagAgent`、
`Dag` 和 `SkillStore` 开始。

| Area | Public SDK |
| --- | --- |
| Runner and tools | `Runner`, `tool`, `CapabilityBinding`；`dagent.capabilities.python_tools` 提供配置化 Python tool source loading helpers |
| Agents | `AutoAgent`, `ToolAgent`, `DagAgent` |
| Static DAGs | `Dag`, `Node`, `MapNode`, `LoopNode`, `item`, `InputRef`, `NodeOutputRef`, `ItemRef`, `CompareRef`, `ArtifactRef`, `ArtifactValueRef`, `FormatRef`, `validate_dag_spec`, `validate_dag_input`, `DAGInputValidationError` |
| Profiles | `AgentProfile`, `ProfileStore`, `load_builtin_profile`, `list_builtin_profiles` |
| Skills | `SkillStore`, `SkillEntry`, `SkillView`, `SkillAmbiguousError`, `SkillNotFoundError`, `SkillPermissionError`, `SkillStoreError`, `default_skill_roots`, `default_managed_skill_root` |
| Conversations and context | `ConversationState`, `UserMessage`, `AssistantMessage`, `ToolCallItem`, `ToolResultMessage`, `Attachment`, `InlineContent`, `ContentReference`, `ContextSummary`, `ContextPolicy`, `ContextUsage`, `ContextWindowExceeded`, `ModelTokenUsage`, `ResultStoragePolicy` |
| Reviews and results | `RunResult`（包含 `output_text` 和静态结构化 `output_value`）、`RunState`, `RunCheckpoint`, `ResolvedRunPlan`, `PlannerFrontend`, `RunStreamEvent`, `ReviewHandle`, `ReviewDecision`, `ReviewLevel` |
| Runtime schemas | `Boundary`, `CapabilityDefinition`, `CapabilityInvocation`, `CapabilityPolicy`, `CapabilityResult`, `CapabilityScope`, `DAG`, `DAGRun`, `DAGSpec`, `ExecutionLimits`, `ExecutionUsage`, `ExecutionLimitExceeded`, `PendingReview`, `RiskLevel`, `RunExecution`, `RunTrace`, `ArtifactUpload`, `DockerSandboxConfig`, `SandboxBackend`, `SandboxConfig` |
| Providers | `Provider`；`dagent.providers` 也导出 `ChatProvider`, `ChatResponse`, `ChatStreamEvent`, `StructuredOutputFormat`, `MockProvider`, `OpenAICompatibleProvider`, `ToolCall`，用于 custom providers 和 tests |

## 最小 Runner

```python
import dagent


provider = dagent.Provider(
    base_url="https://api.openai.com/v1",
    model="your-model",
    api_key_env="OPENAI_API_KEY",
)
runner = dagent.Runner(
    workspace="agent-workspace",
    runtime_directory=".runtime",
    provider=provider,
    extra_system_prompt=None,
)
```

dagent 是进程内 SDK。请在你控制的进程中构造并关闭 `Runner`。进程命令、健康检查、
凭证、持久化、调度和容器生命周期由 host 负责；SDK 不提供 worker 或 service loop。

如果这个 runner 上的所有执行 agent 和动态 DAG planner 都需要同一条额外字面指令，
可把 `extra_system_prompt` 设为非空字符串。Review checkpoint 会冻结当前 run 的初始值。
参数顺序、适用范围、校验和排除项见[Runner 和配置](runner-and-configuration.md)。

## 最小 Tool

```python
@dagent.tool
def search(q: str) -> str:
    return f"found:{q}"


runner.add_tool(search)
```

Python function tools 使用 `tool.<name>` capability ids。MCP tools 使用
`mcp.<server>.<tool>` capability ids。旧的 `custom_tool.*` kind 不是公开兼容别名。

## 最小 Agent Run

```python
agent = dagent.ToolAgent(profile="conversation", capabilities=["tool.search"])

result = await runner.run(
    agent,
    input="Search for dagent.",
)
print(result.output_text)
```

## 最小 Agent 委派

```python
helper = dagent.ToolAgent(
    profile="conversation",
    name="helper",
    capabilities=["tool.search"],
    review="fast",
)
runner.add_agent(helper)

agent = dagent.DagAgent(
    capabilities=["tool.read_file"],
    agents=["agent.helper"],
)
```

已注册子 agent 是单层委派目标。子 agent 自己的 `agents` 字段必须为空；顶层 run
通过 `agents=[...]` 或 `agents="registered"` 暴露这些子 agent。

## 最小静态 DAG Run

```python
dag = dagent.Dag("research", input=str)
node = dagent.Node("search", target=search, inputs={"q": dag.input})
dag.add_node(node)
dag.output = node.output

result = await runner.run(dag, graph_input="dagent")
print(result.output_text)
```

## 版本感知说明

- 本项目已经发布公开 SDK contracts。文档化行为、capability ids、配置语义和可运行示例
  都应视为面向用户的契约。
- 公开 breaking changes 应记录在[迁移说明](migration.md)中。
- 本页应与 `dagent/__init__.py` 保持同步。
