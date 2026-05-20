# DAGSpec + Artifact Platform TODO

## Current State

- [x] Keep the existing local thread message flow unchanged for `ToolAgent` and `DAGAgent`.
- [x] Add public DAG platform models: `Artifact`, `ArtifactState`, `DAGSpec`, `DAGRun`.
- [x] Keep `DAGNode` as graph structure with `title`, `inputs`, and `outputs`; agent task text lives in capability `prompt` arguments.
- [x] Replace per-step DAG result DTOs with the unified `RunTrace` tree.
- [x] Add run workspace and artifact state helpers.
- [x] Compile and validate `DAGSpec` into executable `DAG`.
- [x] Run `DAGSpec` through the existing runtime task path instead of bypassing runtime state.
- [x] Add in-memory `DAGSpec` and `DAGRun` API endpoints.
- [x] Add regression coverage for artifact validation, DAGSpec API, run isolation, required artifact failure, and existing executor behavior.
- [x] Validate artifact producer/consumer contracts while keeping explicit edges as the scheduling source of truth.
- [x] Resolve `{{artifact.<id>.path}}` placeholders in capability arguments and boundary paths.

## Phase 2: Agent Node Runtime

- [x] Enhance `AgentCapabilityProvider` so `agent.*` capabilities call `ToolAgentLoop` internally.
- [x] Support agent capability profiles through capability config.
- [x] Support enabled capability scopes for agent nodes.
- [x] Support `max_steps`, workspace boundary, and artifact input/output awareness for agent nodes.
- [x] Use typed `DAGNode.payload` variants while keeping capability and agent execution unified through `CapabilityInvocation.kind` and `capability_id`.
- [x] Add tests proving a capability payload can invoke an agent capability without a separate agent node schema.
- [x] Add `/dag-specs/{id}/run/stream` so DAGSpec runs can emit live trace and node-scoped agent events.

## Phase 3: Persistent Platform APIs

- [ ] Introduce a persistence boundary for `DAGSpec`, `DAGRun`, `ArtifactState`, `CapabilityDefinition`, and execution trace records.
- [ ] Move the current in-memory API store behind a small repository/store interface.
- [ ] Add run lifecycle operations: resume, retry, cancel.
- [ ] Add artifact download and preview endpoints.
- [ ] Add DAGSpec version management.
- [ ] Stabilize the Python SDK public schema around `CapabilityDefinition`, `CapabilityInvocation`, `Artifact`, `ArtifactState`, `DAGSpec`, `DAGRun`, `DAG`, and `DAGNode`.

## Phase 4: Web DAG Builder

- [ ] Build frontend screens for creating and editing `DAGSpec`.
- [ ] Support capability selection and invocation argument editing.
- [x] Support artifact registry editing and node artifact binding through `inputs` and `outputs`.
- [x] Support configurable `workspace_root` when running a DAGSpec from Web.
- [ ] Support review checkpoints.
- [ ] Add DAGRun pages showing node status, traces, review state, and artifact outputs.

## Phase 5: Advanced Orchestration

- [ ] Add conditional edges.
- [ ] Add dynamic fan-out.
- [ ] Add sub-DAG execution.
- [ ] Add artifact versioning, diffing, and sign-off.
- [ ] Introduce sandbox runner management.
- [ ] Add MCP server management.
- [ ] Add team permissions.
- [ ] Add Trace DB.
- [ ] Let an LLM planner generate or repair a DAG inside `DAGSpec` constraints.

## Runtime Responsibility Cleanup Analysis

### Current Files

- `dagent/harness_runtime/runtime.py`: top-level harness facade. It routes messages, runs tool or DAG flows, performs review and validation orchestration, records outcomes, and currently still owns too much `DAGSpec` run orchestration.
- `dagent/harness_runtime/runtime_session.py`: session memory. It stores `RuntimeTaskRecord` instances and pending review continuations.
- `dagent/harness_runtime/task_record.py`: mutable task state plus execution-record storage. It currently mixes task snapshot models, review continuation DTOs, task outcome mutation, capability execution audit storage, and conversion from tool-loop messages into execution records.
- `dagent/harness_runtime/runtime_events.py`: streaming adapters. It filters model tokens and maps trace/DAG objects into API event payloads.
- `dagent/harness_runtime/dag_executor.py`: DAG scheduling and node execution. It currently owns scheduling, placeholder injection, review-gate enforcement, trace recording, execution audit recording, and artifact state updates.
- `dagent/harness_runtime/dag_agent.py`: DAG lifecycle owner. `run_dynamic(...)` owns natural-language DAG planning/replanning, `run_static(...)` owns user-defined `DAGSpec` compilation, and both share `execute(record, ...)`.
- `dagent/harness_runtime/artifacts.py`: artifact path validation, workspace creation, initial artifact state, and output-state updates.

### Overlap

- `runtime_session.py` owns dictionaries and review continuations; `RuntimeTaskRecord` is now a flat session-resume projection over the latest `RunTrace`.
- `runtime_events.py` adapts runtime objects into stream events; public execution observability is `RunTrace`.
- `runtime.py` now delegates `DAGSpec` execution to `DAGAgentLoop.run_static(...)`; keep it from growing new DAG execution details.
- `DAGAgentLoop` and `DAGExecutor` both mutate DAG/task execution state at different levels. The loop owns lifecycle decisions; the executor owns the mechanics of executing one approved DAG layer.

### Proposed Target Shape

- `runtime.py`: keep as the public harness facade only. It should route, validate, finish responses, resume reviews, and thinly delegate `DAGSpec` execution to `DAGAgentLoop.run_static(...)`.
- `runtime_session.py`: keep session-scoped in-memory state only: tasks and review continuations.
- `dag_agent.py`: own DAG lifecycle orchestration. Keep `DAGAgent.run(...)` for natural-language DAG tasks and delegate to `DAGAgentLoop.run_dynamic(...)`; keep `DAGAgentLoop.run_static(...)` for user-defined `DAGSpec` tasks.
- `dag_executor.py`: remain the execution kernel. It should validate and execute approved DAG layers, inject placeholders, call capabilities, update node output artifact states, and return cumulative `RunTrace`. It should not know `DAGSpec`, `DAGRun`, API stores, or run lifecycle business rules.
- `task_record.py`: keep task state dataclasses and task-state mutation helpers. Consider extracting execution audit helpers only if this file keeps growing.
- `runtime_events.py`: keep as-is for now; it is small and focused despite the name.
- `artifacts.py`: keep artifact path and state helpers here. Avoid adding API/store concerns to this file.

### Refactor Order

- [x] Add `DAGAgentLoop.run_static(spec, workspace_root=...)` returning `LoopOutcome`.
- [x] Move the main `HarnessRuntime.run_dag_spec()` orchestration into `DAGAgentLoop.run_static()`: run id creation, workspace creation, artifact state initialization, `DAGSpec -> DAG` compilation, executor loop, and required artifact failure handling.
- [x] Integrate `DAGSpec` execution into `HarnessRuntime._execute_loop(..., mode="dag_spec")` as an internal loop mode, then record the returned `LoopOutcome` and expose `DAGRun` for API compatibility.
- [x] Keep `dag_executor.py` free of `DAGSpec` and `DAGRun`. Add only generic executor helpers if they are useful for both dynamic DAG and static DAGSpec flows.
- [x] Remove the old `runtime_trace.py` event recorder after `RunTrace` became the execution source of truth.
- [ ] Revisit `task_record.py` only after the `DAGSpec` move; if it still feels heavy, extract execution audit helpers in a separate small refactor.
- [x] Add regression tests proving `/messages/stream`, `/messages/resume`, `/dag-specs/{id}/run`, task trace lookup, and DAG review resume still behave the same.

### Naming Rules

- Use `DAG` for user-visible graph concepts; do not introduce `Workflow`.
- Use `DAGRun` only for a full run instance.
- Use `RunTrace` for public process state and result data.
- Use `RunTraceNode(kind="capability_call")` with `{ invocation, result }` for capability leaves.
- Keep `RuntimeTaskRecord` as an internal session state object, not a public SDK model.
