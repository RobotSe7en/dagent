# DAGSpec + Artifact Platform TODO

## Current State

- [x] Keep the existing local thread message flow unchanged for `ToolAgent` and `DAGAgent`.
- [x] Add public DAG platform models: `Artifact`, `ArtifactState`, `DAGSpec`, `DAGRun`.
- [x] Extend `DAGNode` and internal `PlanNodeSpec` with `title`, `inputs`, and `outputs`.
- [x] Rename the internal per-step DAG execution DTO to `DAGStepResult`.
- [x] Keep a compatibility alias for older `DAGRunResult` imports while removing it from the public barrel export.
- [x] Add run workspace and artifact state helpers.
- [x] Compile and validate `DAGSpec` into executable `DAG`.
- [x] Run `DAGSpec` through the existing runtime task path instead of bypassing runtime state.
- [x] Add in-memory `DAGSpec` and `DAGRun` API endpoints.
- [x] Add regression coverage for artifact validation, DAGSpec API, run isolation, required artifact failure, and existing executor behavior.

## Phase 2: Agent Node Runtime

- [ ] Enhance `AgentCapabilityProvider` so `agent.*` capabilities call `ToolAgentLoop` internally.
- [ ] Support agent capability profiles through capability config.
- [ ] Support enabled capability scopes for agent nodes.
- [ ] Support `max_steps`, workspace boundary, and artifact input/output awareness for agent nodes.
- [ ] Keep node typing unchanged; distinguish execution behavior through `CapabilityInvocation.kind` and `capability_id`.
- [ ] Add tests proving a DAG node can invoke an agent capability without changing the `DAGNode` schema.

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
- [ ] Support artifact binding through node `inputs` and `outputs`.
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
- `dagent/harness_runtime/runtime_trace.py`: tiny trace recorder for one DAG run.
- `dagent/harness_runtime/runtime_events.py`: streaming adapters. It filters model tokens and maps trace/DAG objects into API event payloads.
- `dagent/harness_runtime/dag_executor.py`: DAG scheduling and node execution. It currently owns scheduling, placeholder injection, review-gate enforcement, trace recording, execution audit recording, and artifact state updates.
- `dagent/harness_runtime/dag_agent.py`: DAG lifecycle owner. It currently owns natural-language DAG planning, review, execution observation, and replanning, and should also own user-defined `DAGSpec` execution orchestration.
- `dagent/harness_runtime/artifacts.py`: artifact path validation, workspace creation, initial artifact state, and output-state updates.

### Overlap

- `runtime_session.py` and `task_record.py` overlap around "state ownership": the session owns dictionaries, while records own status transitions. This is acceptable, but `RuntimeTaskRecord.apply_outcome()` makes `task_record.py` more procedural than a pure model file.
- `runtime_trace.py` and `task_record.py` overlap around execution observability: `TraceRecorder` stores `TraceEvent`, while `CapabilityExecutionStore` stores `CapabilityExecutionRecord`. These are different concepts, but colocating `CapabilityExecutionStore` inside `task_record.py` makes that boundary unclear.
- `runtime_events.py` and `runtime_trace.py` sound similar but are not the same responsibility. `runtime_trace.py` records domain traces; `runtime_events.py` adapts runtime objects into stream events. The naming is currently the main source of confusion.
- `runtime.py` currently knows too much about `DAGSpec` execution details. The harness should call a DAG-domain entrypoint and persist the returned run state, not create run workspaces, compile specs, or drive executor loops itself.
- `dag_agent.py` and `dag_executor.py` both mutate DAG/task execution state, but at different levels. The agent should own the DAG lifecycle; the executor should own the mechanics of executing one approved DAG layer.

### Proposed Target Shape

- `runtime.py`: keep as the public harness facade only. It should route, validate, finish responses, resume reviews, and thinly delegate `DAGSpec` execution to `DAGAgent`.
- `runtime_session.py`: keep session-scoped in-memory state only: tasks and review continuations.
- `dag_agent.py`: own all DAG lifecycle orchestration. Keep `run(...)` for natural-language DAG tasks and add/own `run_spec(...)` for user-defined `DAGSpec` tasks.
- `dag_executor.py`: remain the execution kernel. It should validate and execute approved DAG layers, inject placeholders, call capabilities, update node output artifact states, and return `DAGStepResult`. It should not know `DAGSpec`, `DAGRun`, API stores, or run lifecycle business rules.
- `task_record.py`: keep task state dataclasses and task-state mutation helpers. Consider extracting execution audit helpers only if this file keeps growing.
- `runtime_trace.py`: keep as-is for now; it is small and focused despite the name.
- `runtime_events.py`: keep as-is for now; it is small and focused despite the name.
- `artifacts.py`: keep artifact path and state helpers here. Avoid adding API/store concerns to this file.

### Refactor Order

- [ ] Add `DAGAgent.run_spec(spec, workspace_root=...)`.
- [ ] Move the main `HarnessRuntime.run_dag_spec()` orchestration into `DAGAgent.run_spec()`: run id creation, workspace creation, artifact state initialization, `DAGSpec -> DAG` compilation, executor loop, required artifact failure handling, and `DAGRun` snapshot creation.
- [ ] Keep `HarnessRuntime.run_dag_spec()` as a thin wrapper that calls `self.dag_agent.run_spec(...)`, records the returned run in `self.tasks`, and preserves API compatibility.
- [ ] Keep `dag_executor.py` free of `DAGSpec` and `DAGRun`. Add only generic executor helpers if they are useful for both dynamic DAG and static DAGSpec flows.
- [ ] Leave `runtime_events.py` and `runtime_trace.py` unchanged unless they become materially larger.
- [ ] Revisit `task_record.py` only after the `DAGSpec` move; if it still feels heavy, extract execution audit helpers in a separate small refactor.
- [ ] Add regression tests proving `/messages/stream`, `/messages/resume`, `/dag-specs/{id}/run`, task trace lookup, and DAG review resume still behave the same.

### Naming Rules

- Use `DAG` for user-visible graph concepts; do not introduce `Workflow`.
- Use `DAGRun` only for a full run instance.
- Use `DAGStepResult` only for one executor step or agent execution snapshot.
- Use `TraceEvent` for chronological runtime events.
- Use `CapabilityExecutionRecord` for audit records of actual capability calls.
- Keep `RuntimeTaskRecord` as an internal session state object, not a public SDK model.
