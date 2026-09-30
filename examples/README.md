# Runnable Examples

These examples use the current public Python SDK. Run them from the repository
root with `uv run python -m examples.<module>`.

Most examples use `MockProvider`, so they do not require network access or model
credentials.

## Example Map

| Example | Demonstrates | Related docs |
| --- | --- | --- |
| `editable_uploads.py` | Upload, edit, and read current file contents across runs using the same workspace. | [Runner and Configuration](../docs/en/runner-and-configuration.md#editing-uploaded-files) |
| `batch_tool_review.py` | Exact replace-all editing, mixed batch review, saved-queue resume and shell execution | [Capabilities](../docs/en/capabilities.md), [Review](../docs/en/results-streaming-review.md) |
| `deepseek_replay.py` | Opt-in, bounded official DeepSeek tool-replay acceptance using synthetic data. | [Model context and reasoning](../docs/en/model-context-and-reasoning.md#deepseek-chat-tool-replay) |
| `tool_result_recovery.py` | Fit large tool results into a small model window, recover the saved report, inspect display cursors, page through searches, and retain shell logs offline. | [Tool result recovery](../docs/en/tool-result-recovery.md) |
| `tool_agent.py` | Register a Python tool and run a profile-backed `ToolAgent`. | [Agents](../docs/en/agents.md), [Capabilities](../docs/en/capabilities.md) |
| `model_failure.py` | Inspect retained tool errors and an inert reasoning-only final response. | [Model contracts](../docs/en/model-context-and-reasoning.md), [Results](../docs/en/results-streaming-review.md) |
| `agent_delegation.py` | Register a leaf subagent and expose it to a top-level `ToolAgent`. | [Agents](../docs/en/agents.md) |
| `auto_agent.py` | Let the runtime choose direct tool use or dynamic DAG execution. | [Agents](../docs/en/agents.md) |
| `dynamic_dag_agent.py` | Run a `DagAgent` that plans, executes a tool node, and returns a final answer. | [Agents](../docs/en/agents.md), [Results, Streaming, and Review](../docs/en/results-streaming-review.md) |
| `dynamic_dag_builder_agent.py` | Use the restricted SDK Builder planner frontend without executing generated Python. | [Agents](../docs/en/agents.md), [Runner and Configuration](../docs/en/runner-and-configuration.md) |
| `dag_design.py` | Observe provider reasoning while creating and validating a typed DAG candidate without creating a run or executing tools. | [DAG Design](../docs/en/dag-design.md) |
| `static_dag.py` | Build and execute a static DAG with artifacts and a context-aware tool. | [Static DAGs](../docs/en/static-dag.md), [Capabilities](../docs/en/capabilities.md) |
| `static_dag_artifact_files.py` | Materialize input artifact uploads and fan their safe file metadata out with a `MapNode`. | [Static DAGs](../docs/en/static-dag.md) |
| `static_rag.py` | Feed retrieval output into an agent node through optional reference content. | [Static DAGs](../docs/en/static-dag.md) |
| `control_flow.py` | Use an exclusive condition node, map fan-out, an embedded subgraph, and a bounded loop in one static DAG. | [Static DAGs](../docs/en/static-dag.md) |
| `streaming.py` | Consume `Runner.stream(...)` typed events, final results, and failure audit snapshots. | [Results, Streaming, and Review](../docs/en/results-streaming-review.md) |
| `steering.py` | Queue guidance for an active root `ToolAgent` and observe typed steer events. | [Results, Streaming, and Review](../docs/en/results-streaming-review.md#steer-an-active-tool-agent-run) |
| `runtime_registration_and_skills.py` | Add tools and skill roots at runtime, then use `SkillStore` directly. | [Runner and Configuration](../docs/en/runner-and-configuration.md), [Skills](../docs/en/skills.md) |
| `local_test_mcp.py` | Run a local stdio MCP server for registration and tool-call diagnostics. | [Runner and Configuration](../docs/en/runner-and-configuration.md), [Capabilities](../docs/en/capabilities.md) |
| `quickstart.py` | Stream a model-backed quickstart agent against a real provider. | [Quick Start](../docs/en/quick-start.md), [Installation](../docs/en/installation.md) |

## Run Examples

```bash
uv run python -m examples.tool_agent
uv run python -m examples.model_failure
uv run python -m examples.editable_uploads
uv run python -m examples.agent_delegation
uv run python -m examples.auto_agent
uv run python -m examples.dynamic_dag_agent
uv run python -m examples.dynamic_dag_builder_agent
uv run python -m examples.dag_design
uv run python -m examples.static_dag
uv run python -m examples.static_dag_artifact_files
uv run python -m examples.static_rag
uv run python -m examples.control_flow
uv run python -m examples.streaming
uv run python -m examples.steering
uv run python -m examples.runtime_registration_and_skills
```

`examples.quickstart` uses a real provider configuration and requires a matching
API key environment variable:

```bash
uv run python -m examples.quickstart
```

## MCP Note

MCP runtime registration is available through:

- `Runner.add_mcp_server(name, config)`
- `Runner.replace_mcp_server(name, config)`
- `Runner.remove_mcp_server(name)`

MCP requires the optional MCP extra. To test local stdio MCP registration from
the WebUI, add a server with command `uv` and args `--directory`, this
repository root, `run`, `python`, `-m`, `examples.local_test_mcp`. The test
server's `echo` tool intentionally waits 130 seconds before returning so MCP
tool timeout handling can be verified by setting `tool_timeout` below 130
seconds.
