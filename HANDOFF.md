# dagent Handoff

This file is for continuing work from another session or machine.

## Repository

- GitHub: https://github.com/RobotSe7en/dagent.git
- Main branch: `main`
- Current branch: `claude/friendly-bhaskara-959f3b`
- Base branch: `main`
- Latest commit: `11bca7c Rename control_tools.py to auto_mode_tools.py for clarity`

If GitHub access is unstable, use the local Clash proxy:

```powershell
git -c http.proxy=http://127.0.0.1:7890 -c https.proxy=http://127.0.0.1:7890 push
```

## Architecture

```text
dagent/
  api/              FastAPI API (SSE streaming)
  harness_runtime/  core runtime, agent loop, DAG creation/execution/review/trace
  providers/        OpenAI-compatible and mock providers
  schemas/          DAG, node, edge, trace schemas
  state/            prompt assembly (PromptBuilder)
  tools/            registry, executor, file tools, boundary enforcement
profiles/           editable agent profiles (Markdown/YAML)
web/                React + Vite + Tailwind frontend
tests/              pytest suite
```

Key imports: use `dagent.harness_runtime`, not legacy `dagent/harness/` or `dagent/runtime/`.

### Core Files

| File | Responsibility |
|------|----------------|
| `dagent/harness_runtime/runtime.py` | `HarnessRuntime` - top-level orchestrator, manages DAG lifecycle, execute_dag loop |
| `dagent/harness_runtime/agent_loop.py` | Single-agent loop primitive with runtime/control tools |
| `dagent/harness_runtime/dag_agent.py` | `LLMDAGAgent` - asks LLM to produce PlanSpec DSL, compiles to DAG, with JSON fallback |
| `dagent/harness_runtime/dag_executor.py` | `DAGExecutor` - layer-by-layer DAG execution with placeholder injection |
| `dagent/harness_runtime/dag_validation.py` | Structural DAG validation (acyclic, no isolated nodes, tool required) |
| `dagent/harness_runtime/review_policy.py` | `ReviewPolicy` (fast/balanced/careful/manual) + `effective_risk()` |
| `dagent/harness_runtime/trace_store.py` | Immutable trace storage for completed node records |
| `dagent/harness_runtime/auto_mode_tools.py` | `dag_agent` tool schema for top AgentLoop (auto mode routing) |
| `dagent/schemas/node.py` | `DAGNode` (6 fields: id, tool, args, boundary, risk, status) and `Boundary` |
| `dagent/schemas/dag.py` | `DAG`, `PlanSpec`, `PlanNodeSpec` |
| `dagent/tools/registry.py` | `ToolRegistry` with `all_tools()`, Tool has `boundary_fn`/`risk_fn` callbacks |
| `dagent/tools/boundary.py` | Boundary enforcement (path, command, action checks) |
| `dagent/api/app.py` | FastAPI app - `/messages/stream`, `/messages/resume`, `/tasks/{id}/trace` |
| `web/src/App.tsx` | WebUI with Auto/Direct/DAG modes, DAG review dialog |
| `profiles/dag_agent/agent.md` | DAG agent system prompt |

## DAGNode Schema (Current)

Every DAG node is a deterministic tool call - no agent nodes, no goals:

```python
class DAGNode(BaseModel):
    id: str
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    boundary: Boundary = Field(default_factory=Boundary)
    risk: RiskLevel = "low"           # low | medium | high
    status: NodeStatus = "planned"    # planned | ready | running | blocked_permission | completed | failed | skipped

class Boundary(BaseModel):
    mode: BoundaryMode = "read_only"  # read_only | write_limited | full
    allowed_paths: list[str] = []
    allowed_commands: list[str] = []
```

Removed fields (do NOT reintroduce): `risk_reason`, `title`, `goal`, `kind`, `agent`, `tools`, `skills`, `expected_output`, `max_steps`, `timeout_seconds`, `forbidden_tools`, `forbidden_commands`.

## Runtime Flow

```mermaid
flowchart TD
  U["User"] --> R["HarnessRuntime"]
  R --> A["Top AgentLoop"]

  A -->|"direct answer"| O["Return to user"]
  A -->|"runtime tool"| T["ToolExecutor"]
  A -->|"dag_agent"| D["LLMDAGAgent"]

  D -->|"review required"| UI["Return DAG for human review"]
  D -->|"auto approved (low risk)"| E["DAGExecutor"]

  UI -->|"approve/edit"| E

  E -->|"layer-by-layer"| N["execute_tool_node"]
  N --> T

  E --> DR["DAG result as tool output"]
  DR --> A
```

Modes:

- `auto`: top AgentLoop may call `dag_agent` when the task needs orchestration.
- `direct`: top AgentLoop cannot call `dag_agent`; no DAG created.
- `dag`: bypasses tool choice, invokes DAG planning directly.

### Key Design Decisions

- **Tool-node-only DAG**: every node is a direct tool call. Intelligence lives in the replanner, not in node agents.
- **Placeholder injection (Level 1 replanning)**: `{{node_id.output}}` in args gets replaced with actual output. Happens automatically in `_execute_next_ready_layer`.
- **Error-path replanning (Level 3)**: on node failure, `_create_next_dag_from_observation` asks the LLM for a revised DAG. Success-path replanning was removed - layers execute as planned without LLM intervention.
- **dag_start auto-complete**: synthetic start nodes (`tool="dag_start"`) are auto-completed in `execute_node` without calling ToolExecutor.
- **Dynamic tool injection**: `HarnessRuntime.__init__` injects `dag_executor.tool_executor.registry.all_tools()` into `dag_agent.tools`, which `PromptBuilder` renders as `## Available Tools` in the prompt.
- **Boundary enforcement**: whitelist approach - `allowed_paths` and `allowed_commands` constrain what each node can access.
- **Risk/boundary separation**: risk = "need approval?" (pre-execution), boundary = "allowed to execute?" (runtime constraint). `effective_risk(tool, args)` in `review_policy.py` is the single source of truth.
- **Tool-owned inference**: `boundary_fn` and `risk_fn` callbacks on Tool registration move domain logic (e.g. command whitelist) out of dag_agent.py into tool definitions (e.g. `command_tools.py`).

## Available Tools (registered in ToolRegistry)

| Tool | Action | Description |
|------|--------|-------------|
| `dag_start` | read | No-op start marker for connecting root nodes |
| `read_file` | read | Read a UTF-8 text file |
| `write_file` | write | Write UTF-8 text to a file |
| `grep` | read | Search files for a regex pattern |
| `run_command` | read/write | Run a shell command with boundary checks; has `boundary_fn` + `risk_fn` (whitelist→low, else→high) |

Tools are dynamically injected into the DAG agent prompt. Do NOT hardcode tool names in profile files.

### Risk & Boundary Architecture

```
effective_risk(tool, args)          # in review_policy.py
  ├── tool.risk_fn(args)            # dynamic (e.g. run_command checks whitelist)
  └── tool.risk                     # static fallback

_infer_boundary(tool, args)         # in dag_agent.py, at planning time
  ├── tool.boundary_fn(args)        # custom (e.g. run_command infers mode/paths/commands)
  └── tool.action + tool.path_args  # generic fallback

ReviewPolicy (4 levels)             # in review_policy.py
  fast     → only review medium/high risk DAGs
  balanced → review all DAGs, tool review only for high risk
  careful  → review all DAGs, tool review for medium+high
  manual   → review everything
```

All three modes (auto/direct/dag) go through `ToolExecutor.execute()` which enforces boundary constraints.

## Model Configuration

Config in `config.yaml`:

```yaml
provider:
  base_url: "https://api.minimaxi.com/v1"
  model: "MiniMax-M2.1"
  api_key_env: "MINIMAX_API_KEY"
```

Key lives in `.env` as `MINIMAX_API_KEY=...`; `.env` is gitignored. Do not commit secrets.

## Running

Backend:

```powershell
uv run uvicorn dagent.api.app:app --host 127.0.0.1 --port 8001
```

Frontend dev:

```powershell
cd web
npm install
$env:VITE_API_TARGET="http://127.0.0.1:8001"
npm run dev
# Open http://127.0.0.1:5173
```

Tests:

```powershell
uv run --extra dev pytest
# Expected: 107 passed, 2 skipped
```

Frontend type check:

```powershell
cd web
npx tsc --noEmit
```

## Recent Changes (this branch)

1. **Unified risk computation**: Single `effective_risk(tool, args)` in `review_policy.py` replaces scattered risk inference. Supports static `tool.risk` and dynamic `tool.risk_fn(args)` callbacks.
2. **Tool-owned boundary/risk inference**: `boundary_fn` and `risk_fn` on Tool registration. `run_command` uses command whitelist (read-only commands→low risk, others→high). No more hardcoded tool names in `dag_agent.py`.
3. **Dead code cleanup**: Removed `apply_risk_overrides`, `_required_risk_for_node`, `_risk_rank`, `_max_risk`, `_record_tool_trace` from `dag_executor.py`. Removed `_infer_risk`, `_max_risk_str`, `_RISK_RANK` from `dag_agent.py`. Removed unused `task` param from `_compile_plan_node`.
4. **LLM no longer proposes risk**: Removed `risk` field from `PlanNodeSpec`. Risk is computed server-side via `effective_risk()`.
5. **Renamed `control_tools.py` → `auto_mode_tools.py`** for clarity.
6. **Full DAG JSON path fix**: `_full_dag_from_payload` now calls `_apply_effective_risk(dag, tools)` to recompute risk consistently.

### Earlier changes (merged from `codex/llm-facing-planspec-dsl`):

7. **LLM-facing PlanSpec DSL**: DAGAgent now prefers compact DSL output, parses it into PlanSpec/DAG, and keeps JSON as fallback.
8. **DAG conversation context**: forced DAG mode, auto dag_agent creation, continuation DAGs, and failure replanning now pass recent user/assistant history plus structured DAG execution context.
9. **Unknown tool feedback**: DAG validation checks node tools against the runtime registry and feeds unknown-tool errors back to DAGAgent for replanning.
10. **Execution recovery fixes**: failed parallel layers preserve successful sibling results, stale failed trace node ids are ignored after replanning, and failed DAG mode no longer loops through repeated approvals.
11. **Command failure semantics**: `run_command` now raises `ToolExecutionError` on non-zero exit codes, so DAG traces show `tool_failed`/`node_failed` instead of `tool_completed`.
12. **WebUI DAG fixes**: completed/failed DAG cards are no longer confirmable, and final assistant output is appended correctly after DAG review/execution.
13. **File tool safety**: `grep` skips heavy generated directories and caps large result sets.

## Three-Level Replan Status

| Level | Description | Status |
|-------|-------------|--------|
| 1 - Placeholder Injection | `$ref(node_id)` in args replaced with upstream output | ✅ Implemented (`_inject_placeholders` in `dag_executor.py`) |
| 2 - Light Re-planner | Lightweight LLM adjusts next node params based on upstream output | ❌ Not implemented (`dag_executor.py:122` says "does not replan yet") |
| 3 - DAG Re-generator | Full LLM re-generation of pending subgraph | ⚠️ Error-path only (`_create_next_dag_from_observation` in `runtime.py`, triggers on node failure only) |

## Suggested Next Work

- **Implement Level 2 replanning** (light local param adjustment between layers, without full LLM re-generation)
- **Implement success-path Level 3** (proactive re-generation after each layer, not just on error)
- Add `list_files` / `list_directory` as a proper registered tool (currently uses `run_command` + `dir`)
- Add persistent session/run storage (currently in-memory)
- Improve trace events for top AgentLoop tool calls
- Add WebUI node execution status indicators (running/completed/failed coloring)
- Install `gh` CLI for PR automation (`winget install GitHub.cli`)

## Notes

- Use `uv run --extra dev pytest` and `npx tsc --noEmit` before committing.
- Do not reintroduce `dagent/harness/` or `dagent/runtime/`.
- Canonical class names: `LLMDAGAgent`, `DAGAgent`, `HarnessRuntime`, `DAGExecutor`.
- `PlanNodeSpec` fields: `id`, `tool`, `args`, `depends_on`. No `goal`, no `risk` (risk is computed server-side).
