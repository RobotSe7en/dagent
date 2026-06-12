# Runnable Examples

These examples use the current public Python SDK. Run them from the repository
root with `uv run python -m examples.<module>`.

Most examples use `MockProvider`, so they do not require network access or model
credentials.

## Example Map

| Example | Demonstrates | Related docs |
| --- | --- | --- |
| `tool_agent.py` | Register a Python tool and run a profile-backed `ToolAgent`. | [Agents](../docs/agents.md), [Capabilities](../docs/capabilities.md) |
| `auto_agent.py` | Let the runtime choose direct tool use or dynamic DAG execution. | [Agents](../docs/agents.md) |
| `dynamic_dag_agent.py` | Run a `DagAgent` that plans, executes a tool node, and returns a final answer. | [Agents](../docs/agents.md), [Results, Streaming, and Review](../docs/results-streaming-review.md) |
| `static_dag.py` | Build and execute a static DAG with artifacts and a context-aware tool. | [Static DAGs](../docs/static-dag.md), [Capabilities](../docs/capabilities.md) |
| `control_flow.py` | Use conditional edges, map fan-out, an embedded subgraph, and a bounded loop in one static DAG. | [Static DAGs](../docs/static-dag.md) |
| `streaming.py` | Consume `Runner.stream(...)` typed events and read the final `RunResult`. | [Results, Streaming, and Review](../docs/results-streaming-review.md) |
| `runtime_registration_and_skills.py` | Add tools and skill roots at runtime, then use `SkillStore` directly. | [Runner and Configuration](../docs/runner-and-configuration.md), [Skills](../docs/skills.md) |
| `quickstart.py` | Stream a model-backed quickstart agent against a real provider. | [Quick Start](../docs/quick-start.md), [Installation](../docs/installation.md) |

## Run Examples

```bash
uv run python -m examples.tool_agent
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

It requires the optional MCP extra and a real stdio MCP server, so it is covered
in [Runner and Configuration](../docs/runner-and-configuration.md) rather than
exercised by these offline examples.
