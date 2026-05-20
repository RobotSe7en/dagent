# Agent Node Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ToolAgentLoop-backed `agent.*` DAG nodes with scoped local thread messages and DAG runtime context.

**Architecture:** Keep DAG orchestration in `DAGAgent`/`DAGExecutor`, dispatch all node work through `CapabilityExecutor`, and implement agent behavior as a capability provider. Each agent node gets a message session keyed by `task_id + node_id`; the system prompt reuses `PromptBuilder` with profile, memory, tools, workspace, and artifact paths, while the first user message carries the node title, goal, and instructions.

**Tech Stack:** Python, dataclasses, Pydantic schemas, pytest, existing `ToolAgentLoop`, `PromptBuilder`, and capability catalog/runtime.

---

### Task 1: Capability Execution Context

**Files:**
- Modify: `dagent/harness_runtime/capability_executor.py`
- Modify: `dagent/capabilities/catalog.py`
- Test: `tests/test_capability_providers.py`

- [x] **Step 1: Write failing tests for async/context-aware capability execution**

Add a test that registers an async context-aware handler and calls the async `CapabilityExecutor.execute(...)` entrypoint with a `CapabilityExecutionContext`. The test must assert the handler sees `context.node.id` and returns a completed `CapabilityResult`.

- [x] **Step 2: Run the focused test and verify failure**

Run: `uv run pytest tests/test_capability_providers.py::test_capability_executor_passes_context_to_async_handler -q`

Expected: FAIL because `CapabilityExecutionContext` does not exist and `CapabilityExecutor.execute(...)` is still synchronous.

- [x] **Step 3: Implement context/callback dataclasses and async dispatch**

Add `CapabilityExecutionContext`, `CapabilityExecutionCallbacks`, context-aware registration support, and a single async `CapabilityExecutor.execute(...)` entrypoint that also supports existing synchronous handlers.

- [x] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_capability_providers.py -q`

Expected: PASS.

### Task 2: Agent Capability Provider

**Files:**
- Create: `dagent/capabilities/agent_provider.py`
- Modify: `dagent/capabilities/providers.py`
- Test: `tests/test_capability_providers.py`

- [x] **Step 1: Write failing tests for scoped agent node messages**

Add a test that registers `agent.helper`, executes the same `task_id/node_id` twice, and executes a second node once. Assert the same node reuses its saved messages, while the second node starts from a fresh system/user pair.

- [x] **Step 2: Run the focused test and verify failure**

Run: `uv run pytest tests/test_capability_providers.py::test_agent_provider_uses_scoped_node_messages -q`

Expected: FAIL because the old provider is synchronous chat-only and has no scoped node session.

- [x] **Step 3: Implement ToolAgentLoop-backed provider**

Create `AgentNodeSessionStore` and `AgentCapabilityProvider`. Build the system message with `PromptBuilder(PromptRequest(... context=...))`, append the first user message from node title/goal/instructions, call `ToolAgentLoop.run(messages=...)`, save `outcome.messages`, and return a `CapabilityResult`.

- [x] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_capability_providers.py -q`

Expected: PASS.

### Task 3: DAG Executor Context and Events

**Files:**
- Modify: `dagent/harness_runtime/artifacts.py`
- Modify: `dagent/harness_runtime/dag_executor.py`
- Modify: `dagent/harness_runtime/dag_agent.py`
- Modify: `dagent/harness_runtime/runtime.py`
- Test: `tests/test_dag_executor.py`
- Test: `tests/test_dag_artifacts.py`

- [x] **Step 1: Write failing DAG integration test**

Add a DAG node with `invocation.kind="agent"`, `goal`, `instructions`, input artifact id, and output artifact id. Assert the agent provider receives a system prompt containing workspace and artifact paths, and a user message containing only node request fields.

- [x] **Step 2: Run focused test and verify failure**

Run: `uv run pytest tests/test_dag_executor.py::test_executor_passes_node_context_to_agent_capability -q`

Expected: FAIL because `DAGExecutor` does not pass context or callbacks.

- [x] **Step 3: Implement DAG context creation**

Add artifact path resolution helper, make capability node execution async, pass context and callbacks to `CapabilityExecutor.execute(...)`, and wrap agent events with `task_id`, `dag_id`, `node_id`, and `capability_id`.

- [x] **Step 4: Run DAG tests**

Run: `uv run pytest tests/test_dag_executor.py tests/test_dag_artifacts.py -q`

Expected: PASS.

### Task 4: API Stream and Regression

**Files:**
- Modify: `dagent/api/app.py`
- Test: `tests/test_api.py`
- Test: `tests/test_harness_runtime.py`

- [x] **Step 1: Add DAGSpec stream API tests if needed by current surface**

If `POST /dag-specs/{id}/run/stream` is added in this slice, write a test that asserts SSE emits `trace`, node-scoped agent events, and final `done`.

- [x] **Step 2: Keep existing message stream behavior compatible**

Run: `uv run pytest tests/test_api.py tests/test_harness_runtime.py -q`

Expected: PASS.

### Task 5: Final Verification

**Files:**
- All modified files

- [x] **Step 1: Run focused suite**

Run: `uv run pytest tests/test_capability_providers.py tests/test_dag_executor.py tests/test_dag_artifacts.py tests/test_harness_runtime.py tests/test_api.py -q`

Expected: PASS.

- [x] **Step 2: Run full suite**

Run: `uv run pytest -q --basetemp .pytest-tmp-agent-node`

Expected: PASS.

- [x] **Step 3: Run static syntax check**

Run: `python -m compileall dagent`

Expected: PASS.

- [x] **Step 4: Check whitespace**

Run: `git diff --check`

Expected: no errors.
