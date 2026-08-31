# Python SDK Reference Map

This page maps the current public Python SDK surface. It is intentionally a
reference map, not a full tutorial. Start with [Quick Start](quick-start.md) for
a first run, then use the feature guides linked below for details.

Install the package as `dagent-ai` and import it as `dagent`:

```bash
pip install dagent-ai
```

```python
import dagent
```

## Common Starting Points

| Task | Start with |
| --- | --- |
| Configure runtime, provider, MCP, profiles, validation | [Runner and Configuration](runner-and-configuration.md) |
| Register Python tools or MCP tools | [Capabilities](capabilities.md) |
| Choose an agent type | [Agents](agents.md) |
| Register subagents for single-level delegation | [Agents](agents.md#subagent-delegation) |
| Build static workflows in code | [Static DAGs](static-dag.md) |
| Design or inspect a DAG without execution | [DAG Design](dag-design.md) |
| Use skills and managed skill installs | [Skills](skills.md) |
| Persist, stream, steer, review, or resume runs | [Results, Streaming, and Review](results-streaming-review.md) |
| Run examples | [Examples](../../examples/README.md) |

## Public Surface

Most applications start with `Runner`, `@dagent.tool`, `ToolAgent`,
`AutoAgent`, `DagAgent`, `Dag`, and `SkillStore`.

| Area | Public SDK |
| --- | --- |
| Runner and tools | `Runner`, `tool`, `CapabilityBinding`; `dagent.capabilities.python_tools` provides configured Python tool source loading helpers |
| Agents | `AutoAgent`, `ToolAgent`, `DagAgent` |
| Static DAGs | `Dag`, `Node`, `ConditionNode`, `Case`, `MapNode`, `LoopNode`, `item`, `InputRef`, `NodeOutputRef`, `ItemRef`, `CompareRef`, `LogicalRef`, `all_of`, `any_of`, `not_`, `ArtifactRef`, `ArtifactValueRef`, `FormatRef`, `validate_dag_spec`, `validate_dag_input`, `DAGInputValidationError` |
| DAG design | `Runner.design_dag` (including optional `RunStreamEvent` observation), `DAGDesignSelection`, `DAGDesignResult`, `DAGDesignProposal`, `DAGDesignNoChange`, `DAGDesignAnswer`, `DAGDesignFailure`, `DAGDiagnostic`, `DAGDiagnosticSeverity`, `inspect_dag_spec` |
| Profiles | `AgentProfile`, `ProfileStore`, `load_builtin_profile`, `list_builtin_profiles` |
| Skills | `SkillStore`, `SkillEntry`, `SkillView`, `SkillAmbiguousError`, `SkillNotFoundError`, `SkillPermissionError`, `SkillStoreError`, `default_skill_roots`, `default_managed_skill_root` |
| Conversations and context | `ConversationState`, `UserMessage`, `AssistantMessage`, `ToolCallItem`, `ToolResultMessage`, `Attachment`, `InlineContent`, `ContentReference`, `ContextSummary`, `ContextPolicy`, `ContextUsage`, `ContextWindowExceeded`, `ModelTokenUsage`, `ResultStoragePolicy` |
| Reviews and results | `RunResult` (`output_text` plus structured static `output_value`), `RunState`, `RunCheckpoint`, `ResolvedRunPlan`, `PlannerFrontend`, `RunStreamEvent`, `ReviewHandle`, `ReviewDecision`, `ReviewLevel` |
| Tool-run steering | `Runner.steer`, `SteerReceipt`, `SteerError`, `RunNotActiveError`, `RunNotSteerableError`, `SteerQueueFullError` |
| Runtime schemas | `ArtifactFileRef`, `ArtifactFileManifest`, `ArtifactUpload`, `Boundary`, `CapabilityDefinition`, `CapabilityInvocation`, `CapabilityPolicy`, `CapabilityResult`, `CapabilityScope`, `DAG`, `DAGRun`, `DAGSpec`, `ExecutionLimits`, `ExecutionUsage`, `ExecutionLimitExceeded`, `PendingReview`, `RiskLevel`, `RunExecution`, `RunTrace`, `DockerSandboxConfig`, `SandboxBackend`, `SandboxConfig` |
| Providers | `Provider`; `dagent.providers` also exports `ChatProvider`, `ChatResponse`, `ChatStreamEvent`, `StructuredOutputFormat`, `MockProvider`, `OpenAICompatibleProvider`, and `ToolCall` for custom providers and tests |

## Minimal Runner

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

dagent is an in-process SDK. Construct and close `Runner` in the process you
control. Process commands, health, credentials, persistence, scheduling, and
container lifecycle are host responsibilities; the SDK intentionally exposes
no worker or service loop.

Set `extra_system_prompt` to a non-blank string when every execution agent and
dynamic DAG planner on this runner needs the same additional literal
instruction. Review checkpoints freeze the run's initial value. See
[Runner and Configuration](runner-and-configuration.md) for ordering, scope,
validation, and exclusions.

## Minimal Tool

```python
@dagent.tool
def search(q: str) -> str:
    return f"found:{q}"


runner.add_tool(search)
```

Python function tools use `tool.<name>` capability ids. MCP tools use
`mcp.<server>.<tool>` capability ids. The old `custom_tool.*` kind is not a
public compatibility alias.

## Minimal Agent Run

```python
agent = dagent.ToolAgent(profile="conversation", capabilities=["tool.search"])

result = await runner.run(
    agent,
    input="Search for dagent.",
)
print(result.output_text)
```

## Minimal Agent Delegation

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

Registered subagents are single-level delegates. Their own `agents` field must
be empty, and top-level runs expose them with `agents=[...]` or
`agents="registered"`.

## Minimal Static DAG Run

```python
dag = dagent.Dag("research", input=str)
node = dagent.Node("search", target=search, inputs={"q": dag.input})
dag.add_node(node)
dag.output = node.output

result = await runner.run(dag, graph_input="dagent")
print(result.output_text)
```

## Version-Aware Notes

- This project has released public SDK contracts. Treat documented behavior,
  capability ids, configuration semantics, and runnable examples as user-facing
  contracts.
- Public breaking changes should be documented in [Migration Notes](migration.md).
- Keep this page synchronized with `dagent/__init__.py`.
