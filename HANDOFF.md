# dagent Handoff

This file is for continuing work from another session or machine.

## Repository

- GitHub: https://github.com/RobotSe7en/dagent.git
- Base branch: `main`
- Active branch: `codex/analyze-unified-node-plan-a`
- Latest notable code commits before this documentation refresh:
  - `e2ec1cf Unify dynamic DAG planning loop`
  - `aba6f02 refactor: tidy run-trace plumbing and settle awaiting-review nodes`
  - `0574e4b refactor: unify runtime trace and dag loop model`
  - `f25589d Implement agent node DAGSpec runtime`

If GitHub access is unstable, use the local Clash proxy:

```powershell
git -c http.proxy=http://127.0.0.1:7890 -c https.proxy=http://127.0.0.1:7890 push origin codex/runnable-platform
```

## Current Architecture

```text
dagent/
  api/              FastAPI API (SSE streaming)
  capabilities/     capability catalog, provider bootstrap, provider adapters,
                    LLM-visible capability toolsets
  harness_runtime/  runtime orchestration, tool/DAG loops, review, validation,
                    session state, trace recording, DAG execution
  providers/        OpenAI-compatible and mock providers
  schemas/          public data contracts: DAG, invocation, trace, results/outcomes
  state/            prompt assembly (PromptBuilder)
  tools/            legacy/builtin tool registry used by ToolCapabilityProvider
profiles/           editable agent profiles (conversation, dag_agent, validator_agent, feedback_learner)
web/                React + Vite frontend
tests/              pytest suite
```

Key imports: use `dagent.harness_runtime` for runtime behavior and `dagent.schemas`
for shared contracts. Do not reintroduce legacy `dagent/harness/`, `dagent/runtime/`,
or compatibility shim modules.

## Core Files

| File | Responsibility |
|------|----------------|
| `dagent/harness_runtime/runtime.py` | `HarnessRuntime`: auto/tool/dag routing, review continuation lookup, validation retries, final response |
| `dagent/harness_runtime/tool_agent.py` | `ToolAgent`: profile-backed system prompt and persistent tool-agent message thread; `ToolAgentLoop`: bounded tool-use loop |
| `dagent/harness_runtime/dag_agent.py` | `DAGAgent`: profile-backed system prompt and persistent DAG planner message thread; `DAGAgentLoop`: `run_dynamic(...)`, `run_static(...)`, review checkpoints, shared `execute(record, ...)`, observation, replanning |
| `dagent/harness_runtime/dag_executor.py` | `DAGExecutor`: ready-layer DAG execution, placeholder injection, run trace construction |
| `dagent/harness_runtime/dag_builder.py` | PlanSpec DSL parsing, DAG construction, DAG structural validation |
| `dagent/harness_runtime/task_record.py` | Mutable runtime task/session state |
| `dagent/harness_runtime/validator_agent.py` | LLM-backed `ValidatorAgent` and validation feedback formatting |
| `dagent/capabilities/catalog.py` | Session-owned `CapabilityCatalog`: definitions plus handlers |
| `dagent/capabilities/bootstrap.py` | Default session capability assembly |
| `dagent/capabilities/providers.py` | Providers for builtin tools, MCP, skill, shell, custom tool, agent, memory, file |
| `dagent/capabilities/toolsets.py` | `CapabilityToolAdapter` and `CapabilityToolset`: LLM function schemas and tool-call-to-capability mapping |
| `dagent/schemas/results.py` | `PendingReview`, `LoopOutcome`, `RuntimeResponse`, validation results |
| `dagent/schemas/run_trace.py` | `RunTrace`, `RunTraceNode`, capability execution leaves, unified process/result tree |
| `dagent/schemas/capability.py` | `CapabilityInvocation` shared by tool mode and DAG nodes |
| `dagent/schemas/common.py` | `Boundary`, boundary modes, risk levels |
| `dagent/schemas/dag.py` | `DAG`, `DAGSpec`, `DAGRun`; internal `PlanSpec` / `PlanNodeSpec` for LLM DSL compilation |
| `dagent/tools/registry.py` | Builtin tool registration source consumed by `ToolCapabilityProvider` |
| `dagent/tools/boundary.py` | Boundary enforcement |
| `dagent/api/app.py` | FastAPI app: `/messages/stream`, `/messages/resume`, `/tasks/{id}/trace` |
| `web/src/App.tsx` | Web UI for modes, review dialogs, DAG view, validation controls |

## Public Result Contracts

Runtime result/data contracts live in `dagent/schemas/results.py`:

```python
ReviewKind
PendingReview
LoopStatus
LoopOutcome
RuntimeResponse
ValidationIssue
ValidationResult
```

`RunTrace` is the unified execution/result tree. `DAGRun` is the public runtime snapshot for
custom DAGSpec runs and exposes a computed `status` from `trace.status`. `LoopOutcome` is the single loop-to-runtime contract.
`ToolAgent.run()/resume_review()` and `DAGAgent.run()/resume_review()` return it directly.
Runtime converts that to `RuntimeResponse` for API/UI consumption. There are no compatibility shims for older
`LoopResult`, `ToolAgentLoopResult`, loop-specific result dataclasses, `run_result`, `DAGNodeResult`, or
`DAGStepResult` names.

`CapabilityInvocation`, `CapabilityDefinition`, `CapabilityPolicy`, and
`CapabilityResult` are shared by tool mode, DAG nodes, and future capability
providers. LLM-visible OpenAI tool schemas are produced by `CapabilityToolAdapter`
from enabled toolsets, not directly from `ToolRegistry`.

## Message Ownership

- `ToolAgent.messages` is the persistent tool-agent thread:
  `system -> user -> assistant(tool_calls) -> tool(result/denied) -> assistant -> ...`
- `DAGAgent.messages` is the persistent DAG planner thread:
  `system -> user -> assistant(DAG DSL) -> user(DAG observation) -> assistant(...) -> ...`
- `HarnessRuntimeSession` owns task/review state only. It does not store global
  conversation history, `runtime_context`, `runtime_tasks`, or `dag_messages`.
- `RuntimeTaskRecord` stores resume/session state only: current DAG, latest trace,
  pending review, review level, runtime mode, spec id, and workspace path.

## Re-planning And Trace State

Three-level replanning is still part of the current architecture:

- **Level 1 - placeholder injection** lives in `DAGExecutor._inject_placeholders()`.
  Completed node results can fill `{{node.output}}`-style arguments before a tool call,
  without an LLM call.
- **Level 2 - local continuation / parameter reasoning** happens after a DAG observation.
  The DAG LLM can return `NO_CHANGE` or a revised PlanSpec that keeps structure mostly
  intact while changing pending arguments.
- **Level 3 - DAG revision** happens when the DAG LLM returns a revised PlanSpec whose
  nodes or edges changed. `_apply_replan()` invalidates changed/deleted nodes and
  downstream results, then applies review policy.

Trace DB is a target architecture component and should stay in architecture diagrams.
Current code builds in-memory `RunTrace` trees; future work should persist run traces,
node outputs, artifact states, and compact summaries into a Trace DB for long-term
audit and context-boundary use.

## Runtime Flow

```text
User request
  -> HarnessRuntime.handle_message()
     -> route: auto/tool/dag
     -> ToolAgent or DAGAgent returns LoopOutcome
     -> review gate if LoopOutcome.status == "awaiting_review"
     -> optional ValidatorAgent check
     -> retry once with validation feedback if needed
     -> RuntimeResponse
```

DAG mode:

```text
DAGAgent.run()
  -> appends user request to DAGAgent.messages
  -> DAGAgentLoop.run_dynamic()
  -> seed a start-only DAG and enter execute(entry_observation=request, replan=True)
  -> first _request_dag() parses PlanSpec DSL via dag_builder.py
     using CapabilityToolAdapter-provided function names
  -> prepare_for_review(): normalize + validate_dag + enabled capability/toolset check
  -> optional human review
  -> execute() loop
     -> DAGExecutor.execute_next_ready_layer()
     -> append DAG observation to DAGAgent.messages
     -> DAG, NO_CHANGE, or final answer
  -> LoopOutcome
```

Tool mode:

```text
ToolAgent.run()
  -> appends user request to ToolAgent.messages
  -> ToolAgentLoop.run()
  -> bounded chat/tool loop with tools from CapabilityToolAdapter.definitions()
  -> capability review gate for medium/high risk capabilities in careful mode
  -> LoopOutcome
```

Review resume:

```text
Tool review approve -> execute original capability -> replace pending tool marker with
                       the adapter-derived LLM function name -> continue ToolAgentLoop
Tool review reject  -> replace pending tool marker with [DENIED] -> continue ToolAgentLoop
DAG review approve/edit -> apply submitted DAG -> execute next layer
DAG review reject -> append "DAG observation: review_denied" -> continue DAGAgentLoop
```

## Key Design Decisions

- **Capability-node DAG**: every DAG node is a direct capability invocation.
  Intelligence lives in the planner/replanner. `DAGSpec` runtime can execute enabled
  agent capabilities, but the dynamic DAG planner prompt currently should not emit
  agent nodes.
- **Unknown tool calls recover through protocol messages**: if a provider returns a
  hallucinated or disabled tool name, `ToolAgentLoop` appends a protocol-correct
  `role="tool"` error for that `tool_call_id` and lets the model recover.
- **Toolset adapter is the only LLM tool schema path**: tool mode and DAG mode both
  use `CapabilityToolAdapter`; there is no `extra_tools` side channel.
- **No capability id guessing in DAG builder**: PlanSpec functions must match the
  adapter-provided visible function names. Unknown functions raise `DAGCreationError`
  and include available function names.
- **Direct DAG execution validates enabled toolsets**: `DAGAgentLoop.execute()`
  rejects records whose nodes reference capabilities outside the current enabled
  toolsets.
- **Three-level replanning**: placeholder injection, local observation-driven
  continuation/parameter adjustment, and DAG revision are all active design concepts.
- **PlanSpec DSL only**: the DAG agent emits compact DSL, compiled by `dag_builder.py`.
- **DAG build + validation together**: `dag_builder.py` owns model output parsing,
  PlanSpec compilation, and structural DAG validation.
- **Unified run trace**: every DAG node, agent loop, model call, and capability call
  is represented as a `RunTraceNode`; capability leaves carry the shared
  `{ invocation, result }` record.
- **Schemas as data contract layer**: shared result/outcome/review contracts are in
  `dagent.schemas`, while `harness_runtime` owns behavior and mutable session state.
- **Validator naming**: `validator_agent.py` exposes `ValidatorAgent`; profile and
  config key are `validator_agent`.
- **Agent-owned transcripts**: the runtime does not merge cross-mode history. A session
  is expected to use one active mode; follow-up context lives in the active agent's
  own message thread.
- **Observation/evidence budget**: DAG observations include recent node execution
  facts as node id, capability function, args, status, and content/error details.
  Tool and node result excerpts are capped at 4000 chars, capability args at 2000
  chars, the overall execution context at 16000 chars, and truncation is marked.
- **No compatibility shims**: old files and names were removed rather than aliased.

## Configuration

`config.yaml`:

```yaml
provider:
  base_url: "https://api.minimaxi.com/v1"
  model: "MiniMax-M2.1"
  api_key_env: "MINIMAX_API_KEY"
  timeout_seconds: 60
  strip_thinking: false
enable_result_validation: false
profiles:
  directory: "profiles"
  conversation: "conversation"
  dag_agent: "dag_agent"
  validator_agent: "validator_agent"
  feedback_learner: "feedback_learner"
```

Secrets live in `.env` as `MINIMAX_API_KEY=...`; do not commit secrets.

## Running

Backend:

```powershell
uv run uvicorn dagent.api.app:app --host 127.0.0.1 --port 8001
```

Frontend:

```powershell
cd web
npm install
$env:VITE_API_TARGET="http://127.0.0.1:8001"
npm run dev
```

Tests used on this branch:

```powershell
python -m compileall dagent
uv run pytest -q --basetemp .pytest-tmp-full
cd web
npm run build
```

## Recent Commits On This Branch

- `02b2db6 Tighten capability toolset boundaries`
- `376eb0c Add capability toolset adapter`
- `b97d041 Refine capability runtime architecture`
- `cb567f7 refactor: rename runnable platform to capabilities`
- `896908d refactor: unify runnable execution boundaries`
- `563ab02 feat: introduce runnable capability platform`
- `3cd6753 fix: avoid stopping DAG execution after start node`
- `7273bae fix: preserve DAG task id during review validation retry`
- `a3d5e1c refactor: make runtime session own DAG task records`

## Capability Platform Roadmap

This roadmap follows the original full-platform `Runnable` plan, but uses the
current `Capability` / `Toolset` naming and module structure.

1. **Close the capability core**
   Finish the remaining boundary cleanup so tool mode, DAG mode, review, trace,
   and execution records all flow through capability definitions/invocations/results.
   Remove old tool-only assumptions instead of adding compatibility shims.

2. **Wire first-stage capability providers**
   Connect MCP, skill, shell, custom tool, memory, file, and agent template as
   capability providers. Keep the first pass minimal but complete: discovery/list,
   schema, execute, policy, execution record, and failure audit.

3. **Add toolset and session capability configuration**
   Model workspace/session enabled toolsets explicitly. A session gets a fixed
   capability set at startup; system prompts, tool mode, and DAG planning all use
   the same toolset snapshot. Do not rely on dynamic update/delete inside a live
   session.

4. **Platformize the API**
   Add capability APIs for list/test/enable/disable, MCP server configuration,
   skill scanning, sandbox status, execution records, trace lookup, artifacts, and
   logs. Runtime should keep consuming outcomes/records and should not assemble
   providers directly.

5. **Build the web DAG capability panel**
   Move DAG node configuration from free-typed tool names to selectable capability
   functions. Render parameter-schema forms, permission/risk metadata, review
   details, execution trace, artifacts, stdout/stderr, and clear error states.

6. **Introduce sandbox runners**
   Route shell, skill, custom tool, local MCP servers, and agent nodes through a
   sandbox runner interface. A dev-only local runner is acceptable, but the code
   path should remain the same as the target sandbox runner path.

7. **Persist execution and audit state**
   Add durable session/run/task/execution/trace storage. Persist capability inputs,
   outputs, policy decisions, artifacts, stdout/stderr, errors, retries, and DAG
   node state for audit and resumability.

8. **Deepen agent, memory, and file capabilities**
   Agent capabilities should own independent threads, budgets, and trace output.
   Memory and file capabilities should stay explicit through capability calls or
   agent policy, not hidden context injection.

## Suggested Next Work

- Decide whether `ToolCall` should move out of `dagent.providers` into `dagent.schemas`
  or a tiny protocol type. `dagent/capabilities/toolsets.py` currently imports the
  provider DTO, which is workable but still a mild boundary smell.
- Start wiring first real non-tool capability providers end-to-end: MCP discovery/call,
  skill execution, shell command templates, memory/file operations, and agent templates.
  Keep dynamic DAG planner support for agent nodes out of scope until the planner
  prompt, review surface, budgets, and trace display are designed together.
- Add API/web surfaces for capability/toolset listing, enabled toolsets, and node
  configuration so frontend DAG nodes select from capability functions rather than
  free-typing names.
- Add a dedicated validation evidence formatter if simple context budgets are still
  insufficient for long command/file outputs.
- Add persistent session/run storage; current runtime state is in-memory.
- Continue improving Web UI node execution status and trace display.
