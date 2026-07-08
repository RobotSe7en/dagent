# Runnable Examples

These examples use the current public Python SDK. Run them from the repository
root with `uv run python -m examples.<module>`.

Most examples use `MockProvider`, so they do not require network access or model
credentials.

## Example Map

| Example | Demonstrates | Related docs |
| --- | --- | --- |
| `tool_agent.py` | Register a Python tool and run a profile-backed `ToolAgent`. | [Agents](../docs/en/agents.md), [Capabilities](../docs/en/capabilities.md) |
| `agent_delegation.py` | Register a leaf subagent and expose it to a top-level `ToolAgent`. | [Agents](../docs/en/agents.md) |
| `auto_agent.py` | Let the runtime choose direct tool use or dynamic DAG execution. | [Agents](../docs/en/agents.md) |
| `dynamic_dag_agent.py` | Run a `DagAgent` that plans, executes a tool node, and returns a final answer. | [Agents](../docs/en/agents.md), [Results, Streaming, and Review](../docs/en/results-streaming-review.md) |
| `static_dag.py` | Build and execute a static DAG with artifacts and a context-aware tool. | [Static DAGs](../docs/en/static-dag.md), [Capabilities](../docs/en/capabilities.md) |
| `control_flow.py` | Use conditional edges, map fan-out, an embedded subgraph, and a bounded loop in one static DAG. | [Static DAGs](../docs/en/static-dag.md) |
| `streaming.py` | Consume `Runner.stream(...)` typed events and read the final `RunResult`. | [Results, Streaming, and Review](../docs/en/results-streaming-review.md) |
| `runtime_registration_and_skills.py` | Add tools and skill roots at runtime, then use `SkillStore` directly. | [Runner and Configuration](../docs/en/runner-and-configuration.md), [Skills](../docs/en/skills.md) |
| `local_test_mcp.py` | Run a local stdio MCP server for registration and tool-call diagnostics. | [Runner and Configuration](../docs/en/runner-and-configuration.md), [Capabilities](../docs/en/capabilities.md) |
| `quickstart.py` | Stream a model-backed quickstart agent against a real provider. | [Quick Start](../docs/en/quick-start.md), [Installation](../docs/en/installation.md) |

## Run Examples

```bash
uv run python -m examples.tool_agent
uv run python -m examples.agent_delegation
uv run python -m examples.auto_agent
uv run python -m examples.dynamic_dag_agent
uv run python -m examples.static_dag
uv run python -m examples.control_flow
uv run python -m examples.streaming
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
tool timeout handling can be verified.
