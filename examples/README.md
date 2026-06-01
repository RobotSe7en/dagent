# Public SDK Examples

These examples use the current public Python SDK. Run them from the repository
root with `uv run python -m examples.<module>`.

## Public Surface

| Area | Public names |
| --- | --- |
| Runner and tools | `dagent.Runner`, `dagent.tool`, `dagent.CapabilityBinding` |
| Agents | `dagent.ToolAgent`, `dagent.DagAgent` |
| Static DAGs | `dagent.Dag`, `dagent.InputRef`, `dagent.NodeRef`, `dagent.NodeOutputRef`, `dagent.ArtifactRef`, `dagent.ArtifactValueRef`, `dagent.FormatRef`, `dagent.validate_dag_spec` |
| Profiles | `dagent.AgentProfile`, `dagent.ProfileStore` |
| Skills | `dagent.SkillStore`, `dagent.SkillEntry`, `dagent.SkillView`, skill store errors, `default_skill_roots`, `default_managed_skill_root` |
| Reviews and results | `dagent.RunResult`, `dagent.ReviewHandle`, `dagent.ReviewDecision`, `dagent.ReviewLevel` |
| Runtime schemas | `dagent.Boundary`, `dagent.CapabilityDefinition`, `dagent.CapabilityInvocation`, `dagent.CapabilityPolicy`, `dagent.CapabilityResult`, `dagent.DAG`, `dagent.DAGSpec`, `dagent.DAGRun`, `dagent.PendingReview`, `dagent.RunTrace`, `dagent.RuntimeMode`, `dagent.RuntimeResponse`, `dagent.RiskLevel`, `dagent.ArtifactUpload`, `dagent.CapabilityScope`, `dagent.ValueBinding`, `dagent.ValueExpr` |
| Providers | `dagent.OpenAICompatibleProvider`; test/provider helpers from `dagent.providers` include `ChatProvider`, `ChatResponse`, `ChatStreamEvent`, `MockProvider`, and `ToolCall` |

## Files

- `tool_agent.py`: register a Python tool and run a profile-backed `ToolAgent`.
- `dynamic_dag_agent.py`: run a `DagAgent` that plans, executes a tool node, and returns a final answer.
- `static_dag.py`: build and execute a static DAG with artifacts and a context-aware tool.
- `runtime_registration_and_skills.py`: add tools and skill roots at runtime, and use `SkillStore` directly.

MCP runtime registration is available through `Runner.add_mcp_server(name, config)`.
It requires the optional MCP extra and a real stdio MCP server, so it is documented
in the root README rather than exercised by these offline examples.
