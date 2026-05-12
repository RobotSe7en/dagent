# dagent Handoff

This file is for continuing work from another session or machine.

## Repository

- GitHub: https://github.com/RobotSe7en/dagent.git
- Main branch: `main`
- Current branch: `main`
- Base branch: `main`
- Latest commit: `0b9f738 fix DAG completion handling`

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
| `dagent/harness_runtime/runtime.py` | `HarnessRuntime` - top-level auto/direct/dag router and user-facing summarizer |
| `dagent/harness_runtime/agent_loop.py` | Single-agent loop primitive with runtime/control tools |
| `dagent/harness_runtime/dag_agent.py` | `DAGAgentLoop` - owns DAG messages, initial planning, review checkpoints, layer execution, observation, and replanning |
| `dagent/harness_runtime/dag_executor.py` | `DAGExecutor` - layer-by-layer DAG execution with placeholder injection |
| `dagent/harness_runtime/dag_validation.py` | Structural DAG validation (acyclic, no isolated nodes, tool required) |
| `dagent/harness_runtime/review_policy.py` | `ReviewPolicy` (`fast`/`careful`) + `effective_risk()` |
| `dagent/harness_runtime/trace_store.py` | Immutable trace storage for completed node records |
| `dagent/harness_runtime/loop_result.py` | `LoopResult` - unified contract between loops and the runtime |
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
    status: NodeStatus = "planned"    # planned | ready | running | completed | failed | skipped

class Boundary(BaseModel):
    mode: BoundaryMode = "read_only"  # read_only | write_limited | full
    allowed_paths: list[str] = []
    allowed_commands: list[str] = []
```

Removed fields (do NOT reintroduce): `risk_reason`, `title`, `goal`, `kind`, `agent`, `tools`, `skills`, `expected_output`, `max_steps`, `timeout_seconds`, `forbidden_tools`, `forbidden_commands`.

## Runtime Flow

```
User request �?DAGAgentLoop.run()
  ├── Initial planning (LLM via _request_dag, max_cycles budget)
  │     ├── DAG → review gate (fast=auto, careful=pause)
  │     └── Text → direct answer, return
  │
  ├── execute() loop (unified, max_cycles budget)
  │     └── while cycle < max_cycles:
  │           ├── execute_next_ready_layer() �?one layer
  │           ├── fmt observation (layer_completed / layer_failed / dag_executed)
  │           ├── _request_dag(observation) → LLM
  │           │     ├── new DAG → _apply_replan → loop
  │           │     ├── text → set message_markdown → break
  │           │     └── NO_CHANGE
  │           │           ├── DAG completed → break
  │           │           └── otherwise → continue
  │           └── review_required → break
  │
  └── _finalize_run → DAGAgentLoopResult → return to runtime

Runtime:
  handle_message / resume_dag → dasm_agent_loop.run() / resume() → pass through
  (no _continue_dag_loop, no _summarize_dag_run �?DAG loop self-contained)
```

The observe-replan loop is fully self-contained within DAGAgentLoop. Runtime only routes modes and adapts UI. `_continue_dag_loop` and `_summarize_dag_run` have been removed.

### Key Design Decisions

- **Tool-node-only DAG**: every node is a direct tool call. Intelligence lives in the replanner, not in node agents.
- **Placeholder injection (Level 1 replanning)**: `{{node_id.output}}` in args gets replaced with actual output. Happens automatically in `_execute_next_ready_layer`.
- **LLM-driven unified loop**: Initial planning and L2/L3 replanning use the same `_request_dag()` call — pass `dag_messages` history + current observation, get back a DAG (continue), final answer string (done), or `None` (NO_CHANGE). The execute loop has no code-level success/failure branching; every layer result goes to the LLM, which decides the strategy. Loop terminates when the LLM returns a final answer instead of a DAG. `dag_from_model_output()` returns `DAG | str | None` by trying DSL parsing first, falling back to final answer string if content is not valid DSL.
- **DAG agent session memory**: `dag_messages` (a `[user, assistant, ...]` list) persists across the entire plan �?execute �?replan lifecycle. Seeded with conversation history at `DAGAgentLoop.run()` time, then each internal DAG model call appends its prompt and response. This gives the LLM full context of its prior planning decisions during replanning, without needing a separate AgentLoop.
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

ReviewPolicy (2 levels)             # in review_policy.py
  fast     -> do not pause for DAG changes
  careful  -> pause when a DAG is created or changed
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
uv run --extra dev pytest --basetemp="D:\danny\llm\vibe_coding\dagent\.tmp_pytest"
# Expected: 97 passed, 2 skipped
```

Frontend type check:

```powershell
cd web
npx tsc --noEmit
```

## Recent Changes (this branch)

### Current session

1. **Unified max_cycles budget**: Removed `max_node_retries` and `max_attempts`. Only `max_cycles` controls the entire DAG agent lifecycle (initial planning + execute loop). `_request_dag()` changed to single-shot — no internal retry loop.

2. **DAG completed detection**: `_dag_completed()` checks all nodes are in `record.node_results` with `completed=True`. NO_CHANGE after DAG completion → break (no infinite loop). Completed DAG observation uses `kind="dag_executed"` instead of `layer_completed`.

3. **Removed `_continue_dag_loop` / `_summarize_dag_run` from runtime**: DAGAgentLoop is now fully self-contained. After `execute()`, `run()` calls `_finalize_run()` to wrap the result. Runtime just passes through — no multi-segment DAG continuation, no external LLM summarization. Removed `_active_continuation_task_id`.

4. **Fixed replan downstream invalidation**: `_apply_replan` now cascades result invalidation to downstream nodes via `_invalidate_patch_results()` (was: only invalidated nodes with changed tool/args, missing dependents).

5. **Removed dead code**: `_node_by_id` from dag_executor.py (unused, dag_agent.py has own copy); `self.runs` from runtime.py (unreferenced); `_wrap_execute_result` from dag_agent.py (inlined into `_finalize_run`).

6. **`prepare_for_review()` no longer sets status**: Status ("approved" / "review_required") is now set by callers (`run()`, `_apply_replan`) explicitly.

7. **`_dag_created_review_message` deduplicated**: runtime.py imports it from dag_agent.py instead of redefining.

8. **Fixed `_visible_thinking_markup` in streaming**: Original function only collected `<think>` block content, skipping visible text before and after thinks. Now streams all content (visible text + think blocks), letting frontend's `splitThinking()` handle rendering.

### Previous session

9. **Unified risk computation**: Single `effective_risk(tool, args)` in `review_policy.py` replaces scattered risk inference. Supports static `tool.risk` and dynamic `tool.risk_fn(args)` callbacks.
2. **Tool-owned boundary/risk inference**: `boundary_fn` and `risk_fn` on Tool registration. `run_command` uses command whitelist (read-only commands→low risk, others→high). No more hardcoded tool names in `dag_agent.py`.
3. **Dead code cleanup**: Removed `apply_risk_overrides`, `_required_risk_for_node`, `_risk_rank`, `_max_risk`, `_record_tool_trace` from `dag_executor.py`. Removed `_infer_risk`, `_max_risk_str`, `_RISK_RANK` from `dag_agent.py`. Removed unused `task` param from `_compile_plan_node`.
4. **LLM no longer proposes risk**: Removed `risk` field from `PlanNodeSpec`. Risk is computed server-side via `effective_risk()`.
5. **Removed `auto_mode_tools.py`**: Routing is now a lightweight LLM classifier in `runtime.py._route()`, not a tool-based approach.
6. **DSL-only DAG creation**: Removed the legacy full-DAG compatibility path and updated tests to feed PlanSpec DSL fixtures.
7. **DAG agent session memory (`dag_messages`)**: Single `[user, assistant, ...]` message list on `TaskRecord`, seeded from conversation history at `DAGAgentLoop.run()` time. Each internal DAG model call appends its prompt and response. Removed `conversation_messages` parameter entirely �?all context flows through `dag_messages`.
8. **Complete DSL replan**: Replan always returns a complete PlanSpec DSL including both completed and pending nodes. Removed `_merge_completed_nodes` and all partial DSL merge logic from `runtime.py`.
9. **Simplified review modes**: `fast` runs DAG creation/replans without human checkpoints; `careful` pauses only when the DAG is created or changed. Execution failures and boundary violations feed back into LLM replanning instead of creating separate human review or permission gates.
10. **DAGAgentLoop consolidation**: Removed public `LLMDAGAgent`/`DAGAgent` and `aplan()` APIs. DAG creation, validation retries, execution, and replanning now live behind `DAGAgentLoop.run()` / `resume()` / `execute()`. `HarnessRuntime` only routes modes and summarizes DAG results.
11. **Event-style DAG observations**: Removed separate `_dag_planning_prompt` and `_replan_user_message` prompt builders. DAGAgentLoop now feeds history plus `_format_dag_observation()` messages for planning context, validation feedback, successful layers, and failed layers; `agent.md` remains the source of planning/replanning rules.
12. **Unified LLM-driven execute loop**: Removed `layer.completed` code-determined termination. Every layer result (success or failure) goes to the LLM via `_request_dag`. The LLM returns: a new DAG (continue), NO_CHANGE (execute next layer, or break if DAG completed), or a final answer string (loop ends). `dag_from_model_output` returns `DAG | str | None`.

### Earlier changes (merged from `codex/llm-facing-planspec-dsl`):

10. **LLM-facing PlanSpec DSL**: DAGAgent consumes compact DSL output and parses it into PlanSpec/DAG.
11. **DAG conversation context**: forced DAG mode, auto dag_agent creation, continuation DAGs, and failure replanning now pass recent user/assistant history plus structured DAG execution context.
12. **Unknown tool feedback**: DAG validation checks node tools against the runtime registry and feeds unknown-tool errors back to DAGAgent for replanning.
13. **Execution recovery fixes**: failed parallel layers preserve successful sibling results, stale failed trace node ids are ignored after replanning, and failed DAG mode no longer loops through repeated approvals.
14. **Command failure semantics**: `run_command` now raises `ToolExecutionError` on non-zero exit codes, so DAG traces show `tool_failed`/`node_failed` instead of `tool_completed`.
15. **WebUI DAG fixes**: completed/failed DAG cards are no longer confirmable, and final assistant output is appended correctly after DAG review/execution.
16. **File tool safety**: `grep` skips heavy generated directories and caps large result sets.

## Replan Model

| Level | Description | Status |
|-------|-------------|--------|
| 1 - Placeholder Injection | `{{node_id.output}}` in args replaced with upstream output | Implemented (`_inject_placeholders` in `dag_executor.py`) |
| 2+3 - Unified LLM Loop | LLM decides: no change, new DAG, or final answer | Implemented (unified `_request_dag` in `dag_agent.py`) |

After each layer executes, L1 placeholder injection runs first (code, no LLM). Then
`_request_dag` formats the observation via `_format_dag_observation()` and calls the LLM.
`dag_from_model_output()` returns `DAG | str | None`:

- **DAG**: valid PlanSpec DSL. Applied via `_apply_replan`, loop continues.
- **str**: LLM final answer string, sets `record.message_markdown`, loop ends.
- **None**: `NO_CHANGE` signal. If DAG completed (`_dag_completed`) → break; otherwise continue executing.

Initial planning uses the same `_request_dag` with `allow_no_change=False` (NO_CHANGE is an error for initial planning).
A single `max_cycles` budget covers the entire lifecycle — no separate retry counts.

## Suggested Next Work
- Add `list_files` / `list_directory` as a proper registered tool (currently uses `run_command` + `dir`)
- Add persistent session/run storage (currently in-memory)
- Improve trace events for top AgentLoop tool calls
- Add WebUI node execution status indicators (running/completed/failed coloring)
- Install `gh` CLI for PR automation (`winget install GitHub.cli`)

## Notes

- Use `uv run --extra dev pytest` and `npx tsc --noEmit` before committing.
- Do not reintroduce `dagent/harness/` or `dagent/runtime/`.
- Canonical class names: `DAGAgentLoop`, `HarnessRuntime`, `DAGExecutor`.
- `PlanNodeSpec` fields: `id`, `tool`, `args`, `depends_on`. No `goal`, no `risk` (risk is computed server-side).
