# dagent Handoff

This file is for continuing work from another session or machine.

## Repository

- GitHub: https://github.com/RobotSe7en/dagent.git
- Base branch: `main`
- Active branch: `codex/runnable-platform`
- Latest notable code commits before this documentation refresh:
  - `02b2db6 Tighten capability toolset boundaries`
  - `376eb0c Add capability toolset adapter`
  - `b97d041 Refine capability runtime architecture`
  - `cb567f7 refactor: rename runnable platform to capabilities`

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
| `dagent/harness_runtime/dag_agent.py` | `DAGAgent`: profile-backed system prompt and persistent DAG planner message thread; `DAGAgentLoop`: DAG prompt/model parsing, review checkpoints, layer execution, observation, replanning |
| `dagent/harness_runtime/dag_executor.py` | `DAGExecutor`: ready-layer DAG execution, placeholder injection, execution records |
| `dagent/harness_runtime/dag_builder.py` | PlanSpec DSL parsing, DAG construction, DAG structural validation |
| `dagent/harness_runtime/task_record.py` | Mutable runtime task/session state and `CapabilityExecutionStore` |
| `dagent/harness_runtime/runtime_trace.py` | Trace event recording |
| `dagent/harness_runtime/validator_agent.py` | LLM-backed `ValidatorAgent` and validation feedback formatting |
| `dagent/capabilities/catalog.py` | Session-owned `CapabilityCatalog`: definitions plus handlers |
| `dagent/capabilities/bootstrap.py` | Default session capability assembly |
| `dagent/capabilities/providers.py` | Providers for builtin tools, MCP, skill, shell, custom tool, agent, memory, file |
| `dagent/capabilities/toolsets.py` | `CapabilityToolAdapter` and `CapabilityToolset`: LLM function schemas and tool-call-to-capability mapping |
| `dagent/schemas/results.py` | `DAGNodeResult`, `DAGRunResult`, `PendingReview`, `LoopOutcome`, `RuntimeResponse`, validation results |
| `dagent/schemas/capability.py` | `CapabilityInvocation` shared by tool mode and DAG nodes |
| `dagent/schemas/common.py` | `Boundary`, boundary modes, risk levels |
| `dagent/schemas/dag.py` | `DAG`, `PlanSpec`, `PlanNodeSpec` |
| `dagent/tools/registry.py` | Builtin tool registration source consumed by `ToolCapabilityProvider` |
| `dagent/tools/boundary.py` | Boundary enforcement |
| `dagent/api/app.py` | FastAPI app: `/messages/stream`, `/messages/resume`, `/tasks/{id}/trace` |
| `web/src/App.tsx` | Web UI for modes, review dialogs, DAG view, validation controls |

## Public Result Contracts

Runtime result/data contracts live in `dagent/schemas/results.py`:

```python
DAGNodeResult
DAGRunResult
ReviewKind
PendingReview
LoopStatus
LoopOutcome
RuntimeResponse
ValidationIssue
ValidationResult
```

`LoopOutcome` is the single loop-to-runtime contract. `ToolAgent.run()/resume_review()`
and `DAGAgent.run()/resume_review()` return it directly. Runtime converts that to
`RuntimeResponse` for API/UI consumption. There are no compatibility shims for older
`LoopResult`, `ToolAgentLoopResult`, `DAGAgentLoopResult`, or `run_result` names.

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
- `RuntimeTaskRecord` stores structured execution state: current DAG, node results,
  execution records, runs, final response, invocations, and pending review.

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
Current code has in-memory `TraceRecorder` and `CapabilityExecutionStore`; future work should
persist trace events, execution records, node outputs, and compact summaries into a
Trace DB for long-term audit and context-boundary use.

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
  -> DAGAgentLoop.run()
  -> _request_dag() sends DAGAgent.messages and parses PlanSpec DSL via dag_builder.py
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
  Intelligence lives in the planner/replanner, not inside node agents.
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
- **Unified execution records**: `CapabilityExecutionRecord` covers both top-level
  tool/capability loop calls and DAG node execution; `source` distinguishes
  `tool_loop` and `dag_node`.
- **Schemas as data contract layer**: shared result/outcome/review contracts are in
  `dagent.schemas`, while `harness_runtime` owns behavior and mutable session state.
- **Validator naming**: `validator_agent.py` exposes `ValidatorAgent`; profile and
  config key are `validator_agent`.
- **Agent-owned transcripts**: the runtime does not merge cross-mode history. A session
  is expected to use one active mode; follow-up context lives in the active agent's
  own message thread.
- **Validation evidence budget**: validation context uses larger evidence excerpts
  than display summaries. Tool and node result excerpts are capped at 4000 chars, the
  overall execution context at 16000 chars, and truncation is marked.
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

## Suggested Next Work

- Fix unknown/hallucinated tool calls in `ToolAgentLoop`: now that `extra_tools` is
  removed, adapter-owned calls are the only valid path, but an unexpected provider
  tool call can still surface as a `KeyError`. Prefer appending a protocol-correct
  `role="tool"` error message for that `tool_call_id` so the loop can recover.
- Decide whether `ToolCall` should move out of `dagent.providers` into `dagent.schemas`
  or a tiny protocol type. `dagent/capabilities/toolsets.py` currently imports the
  provider DTO, which is workable but still a mild boundary smell.
- Review DAG replanning DSL serialization helpers that still assume `tool.*` names.
  They are not the same bug as tool review resume, but future non-tool DAG capabilities
  may need adapter-based name rendering there too.
- Start wiring first real non-tool capability providers end-to-end: MCP discovery/call,
  skill execution, shell command templates, memory/file operations, and agent templates.
- Add API/web surfaces for capability/toolset listing, enabled toolsets, and node
  configuration so frontend DAG nodes select from capability functions rather than
  free-typing names.
- Add a dedicated validation evidence formatter if simple context budgets are still
  insufficient for long command/file outputs.
- Add persistent session/run storage; current runtime state is in-memory.
- Continue improving Web UI node execution status and trace display.
