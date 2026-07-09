# SDK Runtime Contracts Revised Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the neutral SDK/runtime capabilities needed by host-managed container workers to execute one complete dagent run inside an isolated runtime process.

**Architecture:** A host or enterprise worker owns queues, leases, authorization, Docker lifecycle, persistence, provider gateway credentials, usage, audit, and workspace synchronization. The SDK owns only reusable runtime contracts: typed run specs, typed frames, host-provided run ids, lazy MCP activation from trusted snapshots, validation settings, fail-closed Python tool loading, and a thin `python -m dagent.runtime` process entrypoint. Production communication uses a dedicated control channel such as a Unix socket; stdio JSONL is retained as a local test and simple-process fallback where the runtime process owns stdout.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, existing `Runner`, `HarnessRuntime`, MCP manager, Python tool loader, `RunState`, and `RunStreamEvent`.

---

## Why This Replaces The Earlier Plan

The previous draft is directionally right, but it should be tightened before implementation:

- It treats stdout JSONL as the main process protocol. That is unsafe for production container execution because user Python tools, MCP servers, hooks, or subprocesses can write arbitrary bytes to stdout. Use a dedicated control channel as the production path.
- It does not let a host provide the final `run_id`. Enterprise workers normally create a run record before starting the container, so SDK execution must accept that id instead of creating an unrelated internal one.
- It omits validation settings from the runtime spec, even though `Runner` already supports `enable_validation` and `max_validation_retries`.
- Its `bye.status` is ambiguous because it can mean process status or run status. Frames should distinguish process completion from `RunState.status`.
- Its top-level `capability_ids` and `skills` are underspecified. The SDK should keep capability and skill selection on the run target or registered agents, where `Runner` already understands it.
- It ignores Python tool install errors in the entrypoint. Runtime assembly must fail closed before the run starts.
- It puts too much pressure on the entrypoint to interpret metadata snapshots. SDK snapshots are useful for registration and validation, but executable behavior must still come from normal SDK mechanisms.

The revised design is still SDK-neutral. It does not add users, organizations, projects, roles, quotas, billing, audit, or worker orchestration to the open SDK.

## Boundary Decisions

- **Run inside the container.** The worker should start a container and run `python -m dagent.runtime` inside it. The worker should not drive the agent loop by repeatedly using Docker exec for each tool or shell command.
- **Docker SDK stays outside dagent SDK.** Docker client code belongs to the enterprise worker or another host integration. The open SDK should not manage images, mounts, networks, cgroups, or container cleanup.
- **Dedicated control channel is the production default.** Unix socket JSONL is the first production transport. Stdio JSONL is supported for local tests and simple hosts, with runtime-owned stdout.
- **Runtime frames are not logs.** Logs can be forwarded as explicit `log` frames or collected from Docker logs by the host. They must not be parsed as control frames.
- **Provider keys stay host-owned.** The runtime spec may contain a normal `ProviderConfig`, but enterprise usage should point to the host gateway or use an injected short-lived token. SDK must not learn tenant credential brokering.

## Current SDK Capabilities To Reuse

The latest SDK already has useful public capabilities and this plan should not duplicate them:

- `Runner.derive` with `inherit_local_tools` and `exclude_local_tool_ids`
- `Runner.reload_python_tool_sources`
- `Runner.reload_mcp_servers_with_snapshots`
- `Runner.mcp_server_snapshot`
- `Runner.list_mcp_server_snapshots`
- `Runner.catalog_view`
- `Runner.validate_tools_registerable`
- `Runner.validate_agent_registration`
- `Runner.stream`
- `Runner.resume_stream`
- `Runner.enable_validation`
- `runner.runtime.max_validation_retries`

The missing SDK capabilities are the process-boundary contracts and a few small public hooks into existing runtime behavior.

## File Structure

- Modify `dagent/schemas/results.py`: add `RunState.schema_version`; extend `ValidationIssue` with machine-readable fields.
- Modify `dagent/runner.py`: add host-provided `run_id` to `run`, `stream`, and dispatch paths; add side-effect-free capability reference validation.
- Modify `dagent/harness_runtime/runtime.py`: accept an optional run id for `handle_messages` and `run_dag_spec`.
- Modify `dagent/harness_runtime/artifacts.py`: expose run id validation so host-provided ids are checked even when `workspace_path` is explicit.
- Modify `dagent/capabilities/mcp/__init__.py`: allow trusted snapshot registration without connecting immediately.
- Modify `dagent/capabilities/mcp/manager.py`: start a single MCP server on first tool call when lazy registration was used.
- Create `dagent/schemas/runtime.py`: define `RuntimeRunSpec`, target specs, validation spec, frame models, and frame payloads.
- Modify `dagent/schemas/__init__.py`: export runtime schema models from `dagent.schemas` only.
- Create `dagent/runtime_io.py`: implement Unix socket JSONL and stdio JSONL frame transports.
- Create `dagent/runtime.py`: assemble a `Runner` from `RuntimeRunSpec` and stream frames through the selected transport.
- Add focused tests under `tests/`.
- Update English and Chinese docs in `docs/en/` and `docs/zh-CN/`.

---

### Task 1: Add Host-Provided Run IDs

**Files:**
- Modify: `dagent/runner.py`
- Modify: `dagent/harness_runtime/runtime.py`
- Modify: `dagent/harness_runtime/artifacts.py`
- Test: `tests/test_runtime_run_id.py`
- Docs: `docs/en/results-streaming-review.md`
- Docs: `docs/zh-CN/results-streaming-review.md`

- [ ] **Step 1: Write failing tests**

Create `tests/test_runtime_run_id.py`:

```python
from __future__ import annotations

import pytest

import dagent
from dagent.providers.base import ChatResponse
from dagent.providers.mock import MockProvider
from dagent.schemas import DAGNode, DAGSpec, RunState, StartNodePayload


@pytest.mark.asyncio
async def test_runner_stream_uses_host_run_id_for_tool_agent(tmp_path) -> None:
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([ChatResponse(content="done")]),
    )

    events = [
        event
        async for event in runner.stream(
            dagent.ToolAgent(profile="conversation"),
            messages=[{"role": "user", "content": "hi"}],
            run_id="enterprise_run_123",
        )
    ]

    assert events[0].type == "run.started"
    assert events[0].run_id == "enterprise_run_123"
    assert events[-1].type == "run.finished"
    assert events[-1].run_id == "enterprise_run_123"
    assert events[-1].data.result.state.run_id == "enterprise_run_123"
    runner.close()


@pytest.mark.asyncio
async def test_runner_stream_rejects_run_id_when_state_is_supplied(tmp_path) -> None:
    state = RunState(
        run_id="existing_run",
        kind="tool",
        status="completed",
    )
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([ChatResponse(content="done")]),
    )

    with pytest.raises(ValueError, match="run_id"):
        events = runner.stream(
            dagent.ToolAgent(profile="conversation"),
            messages=[{"role": "user", "content": "hi"}],
            state=state,
            run_id="different_run",
        )
        async for _event in events:
            pass

    runner.close()


@pytest.mark.asyncio
async def test_runner_stream_uses_host_run_id_for_static_dag_spec(tmp_path) -> None:
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([]),
    )
    spec = DAGSpec(
        id="spec_1",
        name="Start only",
        nodes=[DAGNode(id="start", payload=StartNodePayload(type="start"))],
    )

    events = [
        event
        async for event in runner.stream(
            spec,
            run_id="static_run_123",
        )
    ]

    assert events[0].type == "run.started"
    assert events[0].run_id == "static_run_123"
    assert events[-1].type == "run.finished"
    assert events[-1].data.result.state.run_id == "static_run_123"
    assert events[-1].data.result.state.kind == "static_dag"
    runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_run_id", ["../escape", "/tmp/run", ".", "nested/path"])
async def test_runner_stream_rejects_unsafe_host_run_id_even_with_explicit_workspace_path(
    tmp_path,
    bad_run_id: str,
) -> None:
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([ChatResponse(content="done")]),
    )

    with pytest.raises(ValueError, match="run_id"):
        events = runner.stream(
            dagent.ToolAgent(profile="conversation"),
            messages=[{"role": "user", "content": "hi"}],
            run_id=bad_run_id,
            workspace_path=tmp_path / "explicit-workspace",
        )
        async for _event in events:
            pass

    runner.close()
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
uv run --extra dev pytest tests/test_runtime_run_id.py -q
```

Expected: fails because `Runner.stream` does not accept `run_id`.

- [ ] **Step 3: Expose reusable run id validation**

In `dagent/harness_runtime/artifacts.py`, rename the private helper to a public module-level helper and keep `create_run_workspace(...)` using it:

```python
def create_run_workspace(root: str | Path = DEFAULT_RUNS_DIR, *, run_id: str | None = None) -> Path:
    workspace_name = run_id if run_id is not None else f"run_{uuid4().hex}"
    validate_run_id(workspace_name)
    workspace = Path(root).resolve() / workspace_name
    workspace.mkdir(parents=True, exist_ok=False)
    return workspace


def validate_run_id(run_id: str) -> None:
    if not run_id or run_id in {".", ".."}:
        raise ValueError("run_id must be a single directory name.")
    path = Path(run_id)
    windows_path = PureWindowsPath(run_id)
    if (
        path.is_absolute()
        or windows_path.is_absolute()
        or path.name != run_id
        or windows_path.name != run_id
    ):
        raise ValueError("run_id must be a single directory name.")
```

Do not export this from package root `dagent`; it is an internal runtime helper.

- [ ] **Step 4: Add `run_id` to `Runner.run` and `Runner.stream`**

In `dagent/runner.py`, import `validate_run_id` from `dagent.harness_runtime.artifacts`, add `run_id: str | None = None` to both public methods, validate it before dispatch, and pass it through to `_run_dispatch`:

```python
    async def run(
        self,
        target: RunTarget,
        *,
        messages: list[dict[str, Any]] | None = None,
        state: RunState | None = None,
        graph_input: Any = None,
        review: ReviewLevel | None = None,
        dynamic_adjust: bool | None = None,
        execution: RunExecution = "local",
        workspace_root: str | Path = DEFAULT_RUNS_DIR,
        workspace_path: str | Path | None = None,
        run_id: str | None = None,
        input_uploads: list[ArtifactUpload] | None = None,
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult:
        if run_id is not None:
            validate_run_id(run_id)
        if state is not None:
            _ensure_run_state_can_continue(state)
            if run_id is not None and run_id != state.run_id:
                raise ValueError("run_id must match state.run_id when state is supplied.")
        resolved_workspace_path = _validated_workspace_path_for_state(state, workspace_path)
        resolved_execution = _resolve_run_execution(execution, state)
        if resolved_execution == "sandbox" and resolved_workspace_path is not None:
            _ensure_sandbox_workspace_path_is_mounted(
                resolved_workspace_path,
                self._runtime.capability_catalog.workspace_root,
            )
        if resolved_execution == "sandbox" and isinstance(target, (Dag, DAGSpec, DagAgent)):
            raise SandboxExecutionError(
                "Sandbox execution is not yet supported for DAG-based runs "
                f"({type(target).__name__}); use a tool agent or execution='local'."
            )
        skill_names = (
            _agent_skills(target)
            if isinstance(target, (AutoAgent, ToolAgent, DagAgent))
            else None
        )
        with self._run_scope(
            resolved_execution,
            skill_names=skill_names,
        ):
            return await self._run_dispatch(
                target,
                messages=messages,
                state=state,
                graph_input=graph_input,
                review=review,
                dynamic_adjust=dynamic_adjust,
                workspace_root=workspace_root,
                workspace_path=resolved_workspace_path,
                run_id=run_id,
                input_uploads=input_uploads,
                artifact_uploads=artifact_uploads,
                on_token=on_token,
                on_event=on_event,
            )
```

Add the same parameter to `Runner.stream` and pass it to `self.run`:

```python
    async def stream(
        self,
        target: RunTarget,
        *,
        messages: list[dict[str, Any]] | None = None,
        state: RunState | None = None,
        graph_input: Any = None,
        review: ReviewLevel | None = None,
        dynamic_adjust: bool | None = None,
        execution: RunExecution = "local",
        workspace_root: str | Path = DEFAULT_RUNS_DIR,
        workspace_path: str | Path | None = None,
        run_id: str | None = None,
        input_uploads: list[ArtifactUpload] | None = None,
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
    ) -> AsyncIterator[RunStreamEvent]:
        async def run_target(on_event: LoopEventHandler) -> RunResult:
            return await self.run(
                target,
                messages=messages,
                state=state,
                graph_input=graph_input,
                review=review,
                dynamic_adjust=dynamic_adjust,
                execution=execution,
                workspace_root=workspace_root,
                workspace_path=workspace_path,
                run_id=run_id,
                input_uploads=input_uploads,
                artifact_uploads=artifact_uploads,
                on_event=on_event,
            )

        async for event in self._stream_run(run_target):
            yield event
```

- [ ] **Step 5: Add `run_id` to dispatch and harness runtime**

In `dagent/runner.py`, add `run_id` to `_run_dispatch` and pass it to runtime calls:

```python
                run_id=run_id,
```

for `runtime.handle_messages` and `self._runtime.run_dag_spec`.

In `dagent/harness_runtime/runtime.py`, update `handle_messages`:

```python
    async def handle_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        run_state: RunState | None = None,
        mode: RuntimeMode = "auto",
        review_level: ReviewLevel = "fast",
        dynamic_adjust: bool = True,
        workspace_root: str | Path = DEFAULT_RUNS_DIR,
        workspace_path: str | Path | None = None,
        run_id: str | None = None,
        input_uploads: list[ArtifactUpload] | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult:
        if run_state is not None and run_id is not None and run_id != run_state.run_id:
            raise ValueError("run_id must match run_state.run_id when run_state is supplied.")
        user_request = _last_user_content(messages)
        loop_messages = _messages_for_run_state(run_state, user_request) if run_state else messages
        resolved_mode = mode
        if mode == "auto":
            resolved_mode = await self._route(user_request)
        resolved_run_id = run_state.run_id if run_state is not None else run_id or _new_run_id_for_mode(resolved_mode)
```

Use `resolved_run_id` for workspace creation, events, loop execution, and final state.

Update `run_dag_spec` similarly:

```python
    async def run_dag_spec(
        self,
        spec: DAGSpec,
        *,
        graph_input: Any = None,
        workspace_root: str | Path = DEFAULT_RUNS_DIR,
        workspace_path: str | Path | None = None,
        run_id: str | None = None,
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult:
        resolved_run_id = run_id or _new_run_id_for_mode("dag_spec")
```

- [ ] **Step 5: Verify**

Run:

```bash
uv run --extra dev pytest tests/test_runtime_run_id.py tests/test_runtime_state.py tests/test_stream_gate.py -q
git diff --check
```

Expected: tests pass and diff check has no output.

- [ ] **Step 6: Commit**

```bash
git add dagent/runner.py dagent/harness_runtime/runtime.py dagent/harness_runtime/artifacts.py tests/test_runtime_run_id.py docs/en/results-streaming-review.md docs/zh-CN/results-streaming-review.md
git commit -m "feat: allow host runtime run ids"
```

---

### Task 2: Version RunState And Add Validation Issue Codes

**Files:**
- Modify: `dagent/schemas/results.py`
- Test: `tests/test_runtime_state.py`
- Docs: `docs/en/results-streaming-review.md`
- Docs: `docs/zh-CN/results-streaming-review.md`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_runtime_state.py`:

```python
def test_run_state_defaults_missing_schema_version_to_v1() -> None:
    state = RunState.model_validate({
        "run_id": "run_1",
        "kind": "tool",
        "status": "completed",
    })

    assert state.schema_version == 1
    assert state.model_dump(mode="json")["schema_version"] == 1


def test_run_state_rejects_unsupported_schema_version() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        RunState.model_validate({
            "schema_version": 2,
            "run_id": "run_1",
            "kind": "tool",
            "status": "completed",
        })


def test_validation_issue_has_machine_readable_fields() -> None:
    issue = ValidationIssue(
        message="Capability is not registered.",
        capability_id="tool.missing",
        code="unknown_capability",
    )

    assert issue.capability_id == "tool.missing"
    assert issue.code == "unknown_capability"
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
uv run --extra dev pytest tests/test_runtime_state.py::test_run_state_defaults_missing_schema_version_to_v1 tests/test_runtime_state.py::test_run_state_rejects_unsupported_schema_version tests/test_runtime_state.py::test_validation_issue_has_machine_readable_fields -q
```

Expected: fails because the fields do not exist.

- [ ] **Step 3: Add fields**

In `dagent/schemas/results.py`:

```python
class RunState(BaseModel):
    """Serializable run state for display and cross-request resume."""

    schema_version: Literal[1] = 1
    run_id: str
    kind: RunStateKind
    status: LoopStatus
    internal_messages: list[dict[str, Any]] = Field(default_factory=list)
    input_message_count: int = 0
    dag: DAG | None = None
    trace: RunTrace | None = None
    pending_review: PendingReview | None = None
    pending_invocation: CapabilityInvocation | None = None
    user_request: str = ""
    review_level: ReviewLevelValue = "fast"
    runtime_mode: RuntimeModeValue = "auto"
    execution: RunExecution = "local"
    dynamic_adjust: bool = True
    capability_scope: RunCapabilityScope = Field(default_factory=RunCapabilityScope)
    spec_id: str | None = None
    workspace_path: str | None = None
    dag_boundary_approved_version: int | None = None


class ValidationIssue(BaseModel):
    message: str
    node_id: str | None = None
    capability_id: str | None = None
    code: str | None = None
```

- [ ] **Step 4: Document serialized state**

Add to the English and Chinese results/review docs:

```markdown
Persisted `RunState` payloads include `schema_version: 1`. Payloads without the
field are read as version 1. Hosts should reject unsupported explicit versions
instead of silently migrating them.
```

Chinese:

```markdown
持久化的 `RunState` payload 包含 `schema_version: 1`。不含该字段的 payload
会按 version 1 读取。宿主遇到显式声明的不支持版本时应拒绝，而不是静默迁移。
```

- [ ] **Step 5: Verify and commit**

Run:

```bash
uv run --extra dev pytest tests/test_runtime_state.py -q
git diff --check
```

Expected: tests pass and diff check has no output.

Commit:

```bash
git add dagent/schemas/results.py tests/test_runtime_state.py docs/en/results-streaming-review.md docs/zh-CN/results-streaming-review.md
git commit -m "feat: version runtime state payloads"
```

---

### Task 3: Add Side-Effect-Free Capability Reference Validation

**Files:**
- Modify: `dagent/runner.py`
- Test: `tests/test_runner_capability_validation.py`
- Docs: `docs/en/runner-and-configuration.md`
- Docs: `docs/zh-CN/runner-and-configuration.md`

- [ ] **Step 1: Write failing tests**

Create `tests/test_runner_capability_validation.py`:

```python
from __future__ import annotations

import dagent
from dagent.schemas import CapabilityDefinition, CapabilityResult


def _noop(invocation):
    return CapabilityResult.completed(invocation, "ok")


def test_validate_capability_refs_reports_unknown_ids_without_raising(tmp_path) -> None:
    runner = dagent.Runner(workspace=tmp_path)

    result = runner.validate_capability_refs(["tool.missing"])

    assert result.passed is False
    assert result.issues[0].capability_id == "tool.missing"
    assert result.issues[0].code == "unknown_capability"
    runner.close()


def test_validate_capability_refs_rejects_disabled_when_enabled_only(tmp_path) -> None:
    runner = dagent.Runner(workspace=tmp_path)
    runner.register_capability(
        CapabilityDefinition(id="tool.hidden", kind="tool", enabled=False),
        _noop,
    )

    result = runner.validate_capability_refs(["tool.hidden"])

    assert result.passed is False
    assert result.issues[0].capability_id == "tool.hidden"
    assert result.issues[0].code == "disabled_capability"
    runner.close()


def test_validate_capability_refs_does_not_register_bindings(tmp_path) -> None:
    @dagent.tool
    def local_tool() -> str:
        return "ok"

    runner = dagent.Runner(workspace=tmp_path)

    result = runner.validate_capability_refs([local_tool])

    assert result.passed is False
    assert runner.get_capability("tool.local_tool") is None
    assert result.issues[0].capability_id == "tool.local_tool"
    assert result.issues[0].code == "unknown_capability"
    runner.close()


def test_validate_capability_refs_none_checks_default_visible_capabilities(tmp_path) -> None:
    runner = dagent.Runner(workspace=tmp_path)
    runner.set_capability_enabled("tool.read_file", False)

    result = runner.validate_capability_refs(None)

    assert result.passed is False
    assert any(
        issue.capability_id == "tool.read_file" and issue.code == "disabled_capability"
        for issue in result.issues
    )
    runner.close()


def test_validate_capability_refs_respects_allowed_kinds(tmp_path) -> None:
    runner = dagent.Runner(workspace=tmp_path)

    result = runner.validate_capability_refs(["skill.list"], allowed_kinds={"tool"})

    assert result.passed is False
    assert result.issues[0].capability_id == "skill.list"
    assert result.issues[0].code == "unsupported_kind"
    runner.close()
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
uv run --extra dev pytest tests/test_runner_capability_validation.py -q
```

Expected: fails because `validate_capability_refs` does not exist.

- [ ] **Step 3: Implement validation**

In `dagent/runner.py`, add:

```python
    def validate_capability_refs(
        self,
        refs: Iterable[CapabilityRef] | None,
        *,
        allowed_kinds: Iterable[str] | None = None,
        allow_agents: bool = False,
        enabled_only: bool = True,
    ) -> ValidationResult:
        """Validate capability references without registering tools or agents."""

        self._ensure_open()
        issues: list[ValidationIssue] = []
        allowed_kind_set = None if allowed_kinds is None else set(allowed_kinds)
        refs_to_check = self._default_visible_capability_ids() if refs is None else refs
        for ref in refs_to_check:
            if isinstance(ref, CapabilityBinding):
                capability_id = ref.definition.id
            elif isinstance(ref, str):
                capability_id = ref
            else:
                issues.append(ValidationIssue(
                    message="Capability refs must be capability id strings or @dagent.tool bindings.",
                    code="invalid_ref_type",
                ))
                continue
            definition = self._runtime.capability_catalog.get(capability_id)
            if definition is None:
                issues.append(ValidationIssue(
                    message=f"Capability '{capability_id}' is not registered.",
                    capability_id=capability_id,
                    code="unknown_capability",
                ))
                continue
            if enabled_only and not definition.enabled:
                issues.append(ValidationIssue(
                    message=f"Capability '{capability_id}' is disabled.",
                    capability_id=capability_id,
                    code="disabled_capability",
                ))
                continue
            if definition.kind == "agent" and not allow_agents:
                issues.append(ValidationIssue(
                    message=f"Capability '{capability_id}' is an agent capability.",
                    capability_id=capability_id,
                    code="agent_not_allowed",
                ))
                continue
            if allowed_kind_set is not None and definition.kind not in allowed_kind_set:
                issues.append(ValidationIssue(
                    message=f"Capability '{capability_id}' kind '{definition.kind}' is not allowed.",
                    capability_id=capability_id,
                    code="unsupported_kind",
                ))
        return ValidationResult(passed=not issues, issues=issues)
```

- [ ] **Step 4: Document host validation**

Add to runner/config docs:

```markdown
Hosts that persist user-selected tool ids can call
`Runner.validate_capability_refs` before constructing an agent or run
target. The method never registers `@dagent.tool` bindings and returns
`ValidationResult` issues with `capability_id` and `code` fields.
```

- [ ] **Step 5: Verify and commit**

Run:

```bash
uv run --extra dev pytest tests/test_runner_capability_validation.py tests/test_runner_capability_registration.py -q
git diff --check
```

Expected: tests pass and diff check has no output.

Commit:

```bash
git add dagent/runner.py tests/test_runner_capability_validation.py docs/en/runner-and-configuration.md docs/zh-CN/runner-and-configuration.md
git commit -m "feat: validate capability refs"
```

---

### Task 4: Add Lazy MCP Registration From Trusted Snapshots

**Files:**
- Modify: `dagent/capabilities/mcp/__init__.py`
- Modify: `dagent/capabilities/mcp/manager.py`
- Modify: `dagent/runner.py`
- Test: `tests/test_mcp_provider.py`
- Docs: `docs/en/capabilities.md`
- Docs: `docs/zh-CN/capabilities.md`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_mcp_provider.py`:

```python
def test_mcp_provider_registers_snapshot_without_starting_manager() -> None:
    definition = CapabilityDefinition(
        id="mcp.docs.search",
        kind="mcp",
        description="Search docs",
        config={"server": "docs", "tool": "search"},
    )
    snapshot = MCPServerSnapshot(
        name="docs",
        capability_ids=["mcp.docs.search"],
        tools=[MCPToolSnapshot(
            capability_id="mcp.docs.search",
            server="docs",
            tool="search",
            definition=definition,
        )],
    )
    manager = FakeMCPManager()
    provider = MCPCapabilityProvider(
        {"docs": {"command": "fake"}},
        manager=manager,
        snapshots={"docs": snapshot},
        lazy_connect=True,
    )
    catalog = CapabilityCatalog(workspace_root=".")

    provider.register_into(catalog)

    assert manager.started is False
    assert catalog.get("mcp.docs.search") is not None
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
uv run --extra dev pytest tests/test_mcp_provider.py::test_mcp_provider_registers_snapshot_without_starting_manager -q
```

Expected: fails because `snapshots` and `lazy_connect` are unsupported.

- [ ] **Step 3: Extend MCP provider constructor**

In `dagent/capabilities/mcp/__init__.py`:

```python
    def __init__(
        self,
        servers: dict[str, dict[str, Any]] | None = None,
        *,
        manager: Any | None = None,
        snapshots: dict[str, MCPServerSnapshot] | None = None,
        lazy_connect: bool = False,
    ) -> None:
        self.servers = servers or {}
        self.manager = manager
        self.snapshots = snapshots or {}
        self.lazy_connect = lazy_connect
        self.registration_errors: list[str] = []
```

- [ ] **Step 4: Register snapshot tools without connecting**

In `MCPCapabilityProvider.register_into`, branch before `manager.start()`:

```python
        if self.lazy_connect and self.snapshots:
            for server_name, snapshot in sorted(self.snapshots.items()):
                self._register_snapshot_tools(catalog, server_name, snapshot)
            if hasattr(catalog, "add_shutdown_hook") and hasattr(manager, "shutdown"):
                catalog.add_shutdown_hook(manager.shutdown)
            return
```

Add:

```python
    def _register_snapshot_tools(
        self,
        catalog: CapabilityCatalog,
        server_name: str,
        snapshot: MCPServerSnapshot,
    ) -> None:
        if snapshot.name != server_name:
            raise ValueError(
                f"MCP snapshot name '{snapshot.name}' does not match server '{server_name}'."
            )
        for tool in snapshot.tools:
            if tool.server != server_name:
                raise ValueError(
                    f"MCP snapshot tool '{tool.capability_id}' has server '{tool.server}', expected '{server_name}'."
                )
            definition = tool.definition.model_copy(
                update={"config": {**tool.definition.config, "server": server_name, "tool": tool.tool}},
                deep=True,
            )
            handler = make_mcp_tool_handler(
                self.manager,
                server_name=server_name,
                tool_name=tool.tool,
                timeout_seconds=float(
                    self.servers.get(server_name, {}).get("tool_timeout", DEFAULT_MCP_TOOL_TIMEOUT_SECONDS)
                ),
            )
            try:
                catalog.register(definition, handler)
            except ValueError as exc:
                self.registration_errors.append(str(exc))
```

- [ ] **Step 5: Start one MCP server on first tool call**

In `dagent/capabilities/mcp/manager.py`, add `ensure_started`:

```python
    def ensure_started(self, name: str) -> None:
        if not self.available:
            return
        if name in self.tasks:
            return
        if name not in self.servers:
            raise RuntimeError(f"MCP server '{name}' is not configured.")
        if self.servers[name].get("enabled", True) is False:
            raise RuntimeError(f"MCP server '{name}' is disabled.")
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._run_loop, name="dagent-mcp-manager", daemon=True)
            self._thread.start()
        task = MCPServerTask(name, self.servers[name])
        self.tasks[name] = task
        future = asyncio.run_coroutine_threadsafe(task.start(), self._loop)
        connect_timeout = _connect_timeout_seconds(self.servers[name])
        try:
            future.result(timeout=connect_timeout)
        except FutureTimeoutError:
            self.last_errors[name] = f"timed out after {_format_timeout(connect_timeout)} seconds."
            future.cancel()
            self._discard_task(name)
            raise RuntimeError(f"MCP server '{name}' failed to connect: {self.last_errors[name]}") from None
        except Exception as exc:
            self.last_errors[name] = str(exc)
            future.cancel()
            self._discard_task(name)
            raise RuntimeError(f"MCP server '{name}' failed to connect: {exc}") from exc
        self._started = True
```

In `call_tool_blocking`, call `self.ensure_started(server_name)` before fetching the task.

- [ ] **Step 6: Extend Runner MCP methods**

In `dagent/runner.py`, add optional parameters to `add_mcp_server`, `_add_mcp_server`, `reload_mcp_servers`, and `reload_mcp_servers_with_snapshots`:

```python
        snapshot: MCPServerSnapshot | None = None,
        lazy_connect: bool = False,
```

Instantiate:

```python
        provider = MCPCapabilityProvider(
            {name: config},
            manager=manager,
            snapshots={name: snapshot} if snapshot is not None else None,
            lazy_connect=lazy_connect,
        )
```

Default behavior remains eager connection because `lazy_connect=False`.

- [ ] **Step 7: Verify and commit**

Run:

```bash
uv run --extra dev pytest tests/test_mcp_provider.py tests/test_runner_capability_registration.py -q
git diff --check
```

Expected: existing eager MCP tests still pass and new lazy snapshot tests pass.

Commit:

```bash
git add dagent/capabilities/mcp/__init__.py dagent/capabilities/mcp/manager.py dagent/runner.py tests/test_mcp_provider.py docs/en/capabilities.md docs/zh-CN/capabilities.md
git commit -m "feat: support lazy mcp snapshots"
```

---

### Task 5: Add Runtime Schemas

**Files:**
- Create: `dagent/schemas/runtime.py`
- Modify: `dagent/schemas/__init__.py`
- Test: `tests/test_runtime_contracts.py`
- Docs: `docs/en/python-sdk.md`
- Docs: `docs/zh-CN/python-sdk.md`

- [ ] **Step 1: Write failing tests**

Create `tests/test_runtime_contracts.py`:

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from dagent.result import RunStartedData, RunStreamEvent
from dagent.schemas import RuntimeFrame, RuntimeRunSpec


def test_runtime_run_spec_rejects_unknown_host_fields() -> None:
    with pytest.raises(ValidationError):
        RuntimeRunSpec.model_validate({
            "schema_version": 1,
            "run_id": "run_1",
            "action": "run",
            "target": {"type": "tool_agent", "messages": [{"role": "user", "content": "hi"}]},
            "provider": {"base_url": "http://llm.test/v1", "model": "test"},
            "org_id": "org_1",
        })


def test_runtime_run_spec_includes_host_run_id_and_validation() -> None:
    spec = RuntimeRunSpec.model_validate({
        "schema_version": 1,
        "run_id": "run_1",
        "action": "run",
        "target": {"type": "tool_agent", "messages": [{"role": "user", "content": "hi"}]},
        "provider": {"base_url": "http://llm.test/v1", "model": "test"},
        "validation": {"enabled": True, "validator": "validator_agent", "max_retries": 2},
    })

    assert spec.run_id == "run_1"
    assert spec.validation.enabled is True
    assert spec.validation.max_retries == 2


def test_runtime_frame_validates_event_payload() -> None:
    event = RunStreamEvent(
        type="run.started",
        data=RunStartedData(kind="tool"),
        sequence=1,
        run_id="run_1",
    )

    frame = RuntimeFrame(type="event", payload=event.model_dump(mode="json"))

    assert frame.event_payload().type == "run.started"


def test_runtime_bye_distinguishes_process_and_run_status() -> None:
    frame = RuntimeFrame(
        type="bye",
        payload={"process_status": "completed", "run_status": "awaiting_review", "exit_code": 0},
    )

    assert frame.bye_payload().process_status == "completed"
    assert frame.bye_payload().run_status == "awaiting_review"
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
uv run --extra dev pytest tests/test_runtime_contracts.py -q
```

Expected: import fails because runtime schemas do not exist.

- [ ] **Step 3: Create schema module**

Create `dagent/schemas/runtime.py`:

```python
"""Runtime process-boundary schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from dagent.config import ProviderConfig, UserPythonToolConfig
from dagent.result import RunStreamEvent
from dagent.review import ReviewDecision, ReviewLevel
from dagent.schemas.capability import CapabilityDefinition, MCPServerSnapshot
from dagent.schemas.dag import DAG, DAGSpec
from dagent.schemas.results import LoopStatus, RunState
from dagent.schemas.sandbox import SandboxConfig


RuntimeAction = Literal["run", "resume"]
RuntimeFrameType = Literal["hello", "spec", "event", "state_snapshot", "log", "bye"]
RuntimeTargetType = Literal["auto_agent", "tool_agent", "dag_agent", "dag_spec"]
RuntimeProcessStatus = Literal["completed", "failed"]


class RuntimeWorkspaceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_root: str | None = None
    workspace_path: str | None = None


class RuntimeValidationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    validator: str | None = None
    max_retries: int | None = None


class RuntimeAgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_agent"] = "tool_agent"
    profile: str = "conversation"
    name: str | None = None
    max_steps: int = 8
    capabilities: list[str] | None = None
    skills: list[str] | None = None
    agents: list[str] | Literal["registered"] | None = None
    review: ReviewLevel = "fast"
    description: str = ""


class RuntimeRunTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: RuntimeTargetType
    messages: list[dict[str, Any]] | None = None
    graph_input: Any = None
    profile: str = "conversation"
    planner_profile: str = "dag_agent"
    name: str | None = None
    max_steps: int = 8
    max_cycles: int = 6
    capabilities: list[str] | None = None
    skills: list[str] | None = None
    agents: list[str] | Literal["registered"] | None = None
    review: ReviewLevel = "fast"
    dynamic_adjust: bool = True
    dag_spec: DAGSpec | None = None

    @model_validator(mode="after")
    def validate_target_payload(self) -> "RuntimeRunTarget":
        if self.type in {"auto_agent", "tool_agent", "dag_agent"} and self.messages is None:
            raise ValueError(f"{self.type} targets require messages.")
        if self.type == "dag_spec" and self.dag_spec is None:
            raise ValueError("dag_spec targets require dag_spec.")
        return self


class RuntimeReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str
    approved: bool
    dag: DAG | None = None
    review_level: ReviewLevel | None = None
    feedback: str | None = None

    def to_review_decision(self) -> ReviewDecision:
        return ReviewDecision(
            review_id=self.review_id,
            approved=self.approved,
            dag=self.dag,
            review_level=self.review_level,
            feedback=self.feedback,
        )


class RuntimeRunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    sdk_version: str | None = None
    run_id: str | None = None
    action: RuntimeAction = "run"
    target: RuntimeRunTarget | None = None
    review_decision: RuntimeReviewDecision | None = None
    provider: ProviderConfig
    workspace: RuntimeWorkspaceSpec = Field(default_factory=RuntimeWorkspaceSpec)
    validation: RuntimeValidationSpec = Field(default_factory=RuntimeValidationSpec)
    capability_definitions: list[CapabilityDefinition] = Field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    mcp_snapshots: list[MCPServerSnapshot] = Field(default_factory=list)
    lazy_mcp: bool = False
    python_tools: list[UserPythonToolConfig] = Field(default_factory=list)
    python_tool_user_config_dir: str | None = None
    python_tool_managed_root: str | None = None
    skill_roots: list[str] = Field(default_factory=list)
    profile_root: str | None = None
    registered_agents: list[RuntimeAgentSpec] = Field(default_factory=list)
    sandbox: SandboxConfig | None = None
    state: RunState | None = None

    @model_validator(mode="after")
    def validate_action_payload(self) -> "RuntimeRunSpec":
        if self.action == "run" and self.target is None:
            raise ValueError("run specs require target.")
        if self.action == "resume" and self.review_decision is None:
            raise ValueError("resume specs require review_decision.")
        if self.state is not None and self.run_id is not None and self.run_id != self.state.run_id:
            raise ValueError("run_id must match state.run_id when state is supplied.")
        if self.python_tools and self.python_tool_user_config_dir is None:
            raise ValueError("python_tools require python_tool_user_config_dir.")
        return self


class RuntimeLogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["stdout", "stderr"]
    text: str


class RuntimeByePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    process_status: RuntimeProcessStatus
    run_status: LoopStatus | None = None
    exit_code: int = 0
    error_type: str | None = None
    error: str | None = None


_EVENT_ADAPTER = TypeAdapter(RunStreamEvent)


class RuntimeFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: RuntimeFrameType
    payload: Any = None

    @model_validator(mode="after")
    def validate_payload_for_type(self) -> "RuntimeFrame":
        if self.type == "spec":
            RuntimeRunSpec.model_validate(self.payload)
        elif self.type == "event":
            _EVENT_ADAPTER.validate_python(self.payload)
        elif self.type == "state_snapshot":
            RunState.model_validate(self.payload)
        elif self.type == "log":
            RuntimeLogPayload.model_validate(self.payload)
        elif self.type == "bye":
            RuntimeByePayload.model_validate(self.payload)
        elif self.type == "hello" and self.payload is not None and not isinstance(self.payload, dict):
            raise ValueError("hello payload must be an object when provided.")
        return self

    def spec_payload(self) -> RuntimeRunSpec:
        if self.type != "spec":
            raise TypeError("RuntimeFrame is not a spec frame.")
        return RuntimeRunSpec.model_validate(self.payload)

    def event_payload(self) -> RunStreamEvent:
        if self.type != "event":
            raise TypeError("RuntimeFrame is not an event frame.")
        return _EVENT_ADAPTER.validate_python(self.payload)

    def state_payload(self) -> RunState:
        if self.type != "state_snapshot":
            raise TypeError("RuntimeFrame is not a state_snapshot frame.")
        return RunState.model_validate(self.payload)

    def log_payload(self) -> RuntimeLogPayload:
        if self.type != "log":
            raise TypeError("RuntimeFrame is not a log frame.")
        return RuntimeLogPayload.model_validate(self.payload)

    def bye_payload(self) -> RuntimeByePayload:
        if self.type != "bye":
            raise TypeError("RuntimeFrame is not a bye frame.")
        return RuntimeByePayload.model_validate(self.payload)
```

`capability_definitions` is metadata only. It must not create executable handlers in the runtime process.

- [ ] **Step 4: Export from `dagent.schemas` only**

Modify `dagent/schemas/__init__.py`:

```python
from dagent.schemas.runtime import (
    RuntimeAgentSpec,
    RuntimeByePayload,
    RuntimeFrame,
    RuntimeLogPayload,
    RuntimeReviewDecision,
    RuntimeRunSpec,
    RuntimeRunTarget,
    RuntimeValidationSpec,
    RuntimeWorkspaceSpec,
)
```

Add the same names to `__all__`. Do not export them from package root `dagent/__init__.py`.

- [ ] **Step 5: Verify and commit**

Run:

```bash
uv run --extra dev pytest tests/test_runtime_contracts.py tests/test_agent_sdk_public_api.py -q
git diff --check
```

Expected: tests pass and package-root exports remain unchanged.

Commit:

```bash
git add dagent/schemas/runtime.py dagent/schemas/__init__.py tests/test_runtime_contracts.py docs/en/python-sdk.md docs/zh-CN/python-sdk.md
git commit -m "feat: add runtime contract schemas"
```

---

### Task 6: Add Runtime Frame Transports

**Files:**
- Create: `dagent/runtime_io.py`
- Test: `tests/test_runtime_io.py`
- Docs: `docs/en/runner-and-configuration.md`
- Docs: `docs/zh-CN/runner-and-configuration.md`

- [ ] **Step 1: Write failing tests**

Create `tests/test_runtime_io.py`:

```python
from __future__ import annotations

import io
import json
import socket

from dagent.runtime_io import StdioJsonlTransport, UnixSocketJsonlTransport
from dagent.schemas import RuntimeFrame


def test_stdio_jsonl_transport_round_trips_frames() -> None:
    input_stream = io.StringIO(json.dumps({"type": "hello", "payload": {"ok": True}}) + "\n")
    output_stream = io.StringIO()
    transport = StdioJsonlTransport(input_stream=input_stream, output_stream=output_stream)

    frame = transport.read_frame()
    transport.write_frame(RuntimeFrame(type="bye", payload={"process_status": "completed", "exit_code": 0}))

    assert frame.type == "hello"
    assert json.loads(output_stream.getvalue().splitlines()[0])["type"] == "bye"


def test_unix_socket_jsonl_transport_round_trips_frames() -> None:
    left, right = socket.socketpair()
    try:
        transport = UnixSocketJsonlTransport.from_socket(left)
        right.sendall(json.dumps({"type": "hello", "payload": {"ok": True}}).encode() + b"\n")

        frame = transport.read_frame()
        transport.write_frame(RuntimeFrame(type="bye", payload={"process_status": "completed", "exit_code": 0}))

        data = right.recv(4096).decode()
        assert frame.type == "hello"
        assert json.loads(data.splitlines()[0])["type"] == "bye"
    finally:
        right.close()
        transport.close()
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
uv run --extra dev pytest tests/test_runtime_io.py -q
```

Expected: fails because `dagent.runtime_io` does not exist.

- [ ] **Step 3: Implement transports**

Create `dagent/runtime_io.py`:

```python
"""Frame transports for the dagent runtime entrypoint."""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from typing import Protocol, TextIO

from dagent.schemas import RuntimeFrame


class RuntimeFrameTransport(Protocol):
    def read_frame(self) -> RuntimeFrame:
        """Read one JSONL runtime frame."""

    def write_frame(self, frame: RuntimeFrame) -> None:
        """Write one JSONL runtime frame."""

    def close(self) -> None:
        """Close transport resources."""


class StdioJsonlTransport:
    """JSONL transport over caller-provided text streams."""

    def __init__(
        self,
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
    ) -> None:
        self._input = input_stream or sys.stdin
        self._output = output_stream or sys.stdout

    def read_frame(self) -> RuntimeFrame:
        line = self._input.readline()
        if not line:
            raise EOFError("runtime control channel closed before a frame was received.")
        return RuntimeFrame.model_validate_json(line)

    def write_frame(self, frame: RuntimeFrame) -> None:
        self._output.write(frame.model_dump_json() + "\n")
        self._output.flush()

    def close(self) -> None:
        return None


class UnixSocketJsonlTransport:
    """JSONL transport over a connected Unix domain socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._socket = sock
        self._reader = sock.makefile("r", encoding="utf-8")
        self._writer = sock.makefile("w", encoding="utf-8")

    @classmethod
    def connect(cls, path: str | Path) -> "UnixSocketJsonlTransport":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(path))
        return cls(sock)

    @classmethod
    def from_socket(cls, sock: socket.socket) -> "UnixSocketJsonlTransport":
        return cls(sock)

    def read_frame(self) -> RuntimeFrame:
        line = self._reader.readline()
        if not line:
            raise EOFError("runtime control socket closed before a frame was received.")
        return RuntimeFrame.model_validate_json(line)

    def write_frame(self, frame: RuntimeFrame) -> None:
        self._writer.write(frame.model_dump_json() + "\n")
        self._writer.flush()

    def close(self) -> None:
        self._reader.close()
        self._writer.close()
        self._socket.close()
```

- [ ] **Step 4: Document transport rule**

Add:

```markdown
`python -m dagent.runtime` supports Unix socket JSONL and stdio JSONL transports.
Production hosts should prefer Unix sockets or another dedicated control channel.
Stdio is intended for local tests and simple process hosts where dagent owns stdout.
Container stdout and stderr should be treated as logs, not the control protocol.
```

- [ ] **Step 5: Verify and commit**

Run:

```bash
uv run --extra dev pytest tests/test_runtime_io.py -q
git diff --check
```

Expected: tests pass and diff check has no output.

Commit:

```bash
git add dagent/runtime_io.py tests/test_runtime_io.py docs/en/runner-and-configuration.md docs/zh-CN/runner-and-configuration.md
git commit -m "feat: add runtime frame transports"
```

---

### Task 7: Add Runtime Entrypoint

**Files:**
- Create: `dagent/runtime.py`
- Test: `tests/test_runtime_entrypoint.py`
- Docs: `docs/en/runner-and-configuration.md`
- Docs: `docs/zh-CN/runner-and-configuration.md`

- [ ] **Step 1: Write failing tests**

Create `tests/test_runtime_entrypoint.py`:

```python
from __future__ import annotations

import json
import subprocess
import sys


def test_runtime_entrypoint_rejects_non_spec_first_frame() -> None:
    process = subprocess.run(
        [sys.executable, "-m", "dagent.runtime", "--transport", "stdio"],
        input=json.dumps({"type": "hello", "payload": {}}) + "\n",
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert process.returncode == 2
    assert "first frame must be spec" in process.stderr


def test_runtime_entrypoint_rejects_empty_stdin() -> None:
    process = subprocess.run(
        [sys.executable, "-m", "dagent.runtime", "--transport", "stdio"],
        input="",
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert process.returncode == 2
    assert "first frame must be spec" in process.stderr
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
uv run --extra dev pytest tests/test_runtime_entrypoint.py -q
```

Expected: fails because `dagent.runtime` does not exist.

- [ ] **Step 3: Create entrypoint and transport selection**

Create `dagent/runtime.py`:

```python
"""Runtime process entrypoint for executing RuntimeRunSpec payloads."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from pydantic import ValidationError

from dagent import __version__
from dagent.agent import AutoAgent, DagAgent, ToolAgent
from dagent.config import UserPythonToolConfig
from dagent.providers.openai_compatible import OpenAICompatibleProvider
from dagent.result import RunStreamEvent
from dagent.runner import Runner
from dagent.runtime_io import RuntimeFrameTransport, StdioJsonlTransport, UnixSocketJsonlTransport
from dagent.schemas import RuntimeAgentSpec, RuntimeFrame, RuntimeRunSpec, RuntimeRunTarget


def _transport_from_args(args: argparse.Namespace) -> RuntimeFrameTransport:
    if args.transport == "stdio":
        return StdioJsonlTransport()
    if args.transport == "unix-socket":
        if not args.socket_path:
            raise ValueError("--socket-path is required for unix-socket transport.")
        return UnixSocketJsonlTransport.connect(args.socket_path)
    raise ValueError(f"Unsupported runtime transport: {args.transport}")


def _read_spec(transport: RuntimeFrameTransport) -> RuntimeRunSpec:
    frame = transport.read_frame()
    if frame.type != "spec":
        raise ValueError("first frame must be spec.")
    return frame.spec_payload()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a dagent RuntimeRunSpec over a JSONL control channel.")
    parser.add_argument("--transport", choices=["stdio", "unix-socket"], default="stdio")
    parser.add_argument("--socket-path")
    args = parser.parse_args(argv)
    transport: RuntimeFrameTransport | None = None
    try:
        transport = _transport_from_args(args)
        spec = _read_spec(transport)
        asyncio.run(_run_spec(spec, transport))
        return 0
    except (EOFError, ValidationError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        if transport is not None:
            transport.write_frame(RuntimeFrame(
                type="bye",
                payload={
                    "process_status": "failed",
                    "run_status": None,
                    "exit_code": 1,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            ))
        return 1
    finally:
        if transport is not None:
            transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Add runner assembly**

Add before `main`:

```python
def _runner_from_spec(spec: RuntimeRunSpec) -> Runner:
    validator = spec.validation.validator if spec.validation.enabled else None
    runner = Runner(
        workspace=spec.workspace.workspace_root or ".dagent",
        provider=OpenAICompatibleProvider(spec.provider),
        validator=validator,
        skill_roots=[Path(root) for root in spec.skill_roots],
        profile_root=None if spec.profile_root is None else Path(spec.profile_root),
        sandbox=spec.sandbox,
    )
    try:
        runner.enable_validation = spec.validation.enabled
        if spec.validation.max_retries is not None:
            runner.runtime.max_validation_retries = spec.validation.max_retries
        snapshots = {snapshot.name: snapshot for snapshot in spec.mcp_snapshots}
        for name, config in spec.mcp_servers.items():
            runner.add_mcp_server(
                name,
                config,
                snapshot=snapshots.get(name),
                lazy_connect=spec.lazy_mcp,
            )
        if spec.python_tools:
            if spec.python_tool_user_config_dir is None:
                raise ValueError("python_tools require python_tool_user_config_dir.")
            result = runner.reload_python_tool_sources(
                [UserPythonToolConfig.model_validate(item) for item in spec.python_tools],
                user_config_dir=Path(spec.python_tool_user_config_dir),
                managed_root=None if spec.python_tool_managed_root is None else Path(spec.python_tool_managed_root),
                replace_ids=set(),
            )
            if result.errors:
                raise ValueError(f"Python tool registration failed: {result.errors}")
        for agent in spec.registered_agents:
            runner.add_agent(_tool_agent_from_runtime_agent(agent))
    except Exception:
        runner.close()
        raise
    return runner


def _tool_agent_from_runtime_agent(agent: RuntimeAgentSpec) -> ToolAgent:
    return ToolAgent(
        profile=agent.profile,
        name=agent.name,
        max_steps=agent.max_steps,
        capabilities=agent.capabilities,
        skills=agent.skills,
        agents=agent.agents,
        review=agent.review,
        description=agent.description,
    )
```

- [ ] **Step 5: Add target conversion and execution**

Add:

```python
def _target_from_spec(target: RuntimeRunTarget):
    if target.type == "auto_agent":
        return AutoAgent(
            profile=target.profile,
            planner_profile=target.planner_profile,
            name=target.name,
            max_steps=target.max_steps,
            max_cycles=target.max_cycles,
            capabilities=target.capabilities,
            skills=target.skills,
            agents=target.agents,
            review=target.review,
            dynamic_adjust=target.dynamic_adjust,
        )
    if target.type == "tool_agent":
        return ToolAgent(
            profile=target.profile,
            name=target.name,
            max_steps=target.max_steps,
            capabilities=target.capabilities,
            skills=target.skills,
            agents=target.agents,
            review=target.review,
        )
    if target.type == "dag_agent":
        return DagAgent(
            planner_profile=target.planner_profile,
            name=target.name,
            max_cycles=target.max_cycles,
            capabilities=target.capabilities,
            skills=target.skills,
            agents=target.agents,
            review=target.review,
            dynamic_adjust=target.dynamic_adjust,
        )
    if target.type == "dag_spec":
        if target.dag_spec is None:
            raise ValueError("dag_spec target requires dag_spec.")
        return target.dag_spec
    raise ValueError(f"Unsupported runtime target type: {target.type}")


async def _run_spec(spec: RuntimeRunSpec, transport: RuntimeFrameTransport) -> None:
    runner = _runner_from_spec(spec)
    final_run_status = None
    try:
        transport.write_frame(RuntimeFrame(type="hello", payload={"sdk_version": __version__}))
        if spec.action == "resume":
            if spec.review_decision is None:
                raise ValueError("resume specs require review_decision.")
            events = runner.resume_stream(
                spec.review_decision.to_review_decision(),
                state=spec.state,
            )
        else:
            if spec.target is None:
                raise ValueError("run specs require target.")
            events = runner.stream(
                _target_from_spec(spec.target),
                messages=spec.target.messages,
                graph_input=spec.target.graph_input,
                state=spec.state,
                workspace_root=spec.workspace.workspace_root or "runs",
                workspace_path=spec.workspace.workspace_path,
                run_id=spec.run_id,
            )
        async for event in events:
            transport.write_frame(RuntimeFrame(type="event", payload=event.model_dump(mode="json")))
            if event.type == "run.finished":
                result = event.data.result
                final_run_status = result.state.status
                transport.write_frame(RuntimeFrame(
                    type="state_snapshot",
                    payload=result.state.model_dump(mode="json"),
                ))
        transport.write_frame(RuntimeFrame(
            type="bye",
            payload={"process_status": "completed", "run_status": final_run_status, "exit_code": 0},
        ))
    finally:
        runner.close()
```

- [ ] **Step 6: Add focused helper tests**

Append:

```python
from dagent.runtime import _target_from_spec
from dagent.schemas import RuntimeRunTarget


def test_runtime_target_builds_tool_agent() -> None:
    target = _target_from_spec(RuntimeRunTarget(
        type="tool_agent",
        messages=[{"role": "user", "content": "hi"}],
        capabilities=["tool.read_file"],
        skills=["docs/readme"],
    ))

    assert target.profile == "conversation"
    assert target.capabilities == ("tool.read_file",)
    assert target.skills == ("docs/readme",)
```

- [ ] **Step 7: Verify and commit**

Run:

```bash
uv run --extra dev pytest tests/test_runtime_entrypoint.py tests/test_runtime_contracts.py tests/test_runtime_io.py -q
python -m dagent.runtime --transport stdio < /dev/null ; test $? -eq 2
git diff --check
```

Expected: tests pass; empty stdin exits with code 2.

Commit:

```bash
git add dagent/runtime.py tests/test_runtime_entrypoint.py docs/en/runner-and-configuration.md docs/zh-CN/runner-and-configuration.md
git commit -m "feat: add runtime process entrypoint"
```

---

### Task 8: Public API Guardrails And Documentation

**Files:**
- Modify: `tests/test_agent_sdk_public_api.py`
- Modify: `docs/en/python-sdk.md`
- Modify: `docs/zh-CN/python-sdk.md`
- Modify: `docs/en/runner-and-configuration.md`
- Modify: `docs/zh-CN/runner-and-configuration.md`
- Modify: `docs/en/capabilities.md`
- Modify: `docs/zh-CN/capabilities.md`

- [ ] **Step 1: Add package-root export guard**

Add to `tests/test_agent_sdk_public_api.py`:

```python
def test_runtime_contracts_are_schema_exports_not_package_root_exports() -> None:
    import dagent
    import dagent.schemas as schemas

    assert hasattr(schemas, "RuntimeRunSpec")
    assert hasattr(schemas, "RuntimeFrame")
    assert hasattr(schemas, "RuntimeValidationSpec")
    assert not hasattr(dagent, "RuntimeRunSpec")
    assert not hasattr(dagent, "RuntimeFrame")
    assert not hasattr(dagent, "RuntimeValidationSpec")
```

- [ ] **Step 2: Add boundary statement to docs**

Use this English text:

```markdown
Runtime contracts are process-boundary contracts for hosts that already know how
to prepare workspaces and credentials. They do not include users, organizations,
projects, RBAC, authorization filtering, persistence, queue claims, leases,
rate limits, audit, usage, billing, provider key brokering, Docker lifecycle, or
worker orchestration.
```

Use this Chinese text:

```markdown
Runtime contracts 是给宿主进程使用的进程边界契约，前提是宿主已经准备好 workspace
和凭证。它们不包含用户、组织、项目、RBAC、授权过滤、持久化、队列领取、租约、
限流、审计、用量、计费、provider key 代理、Docker 生命周期或 worker 编排。
```

- [ ] **Step 3: Add runtime usage note**

Document:

```markdown
Container hosts should start one `python -m dagent.runtime` process for one run
or resume operation. The process reads one `RuntimeFrame(type="spec")`, emits
`event`, `state_snapshot`, `log`, and `bye` frames, and exits. Long-lived
workers, queue loops, and Docker clients belong outside the SDK.
```

- [ ] **Step 4: Run full verification**

Run:

```bash
uv run --extra dev pytest
git diff --check
```

Expected: all Python tests pass and diff check has no output.

- [ ] **Step 5: Commit**

```bash
git add tests/test_agent_sdk_public_api.py docs/en/python-sdk.md docs/zh-CN/python-sdk.md docs/en/runner-and-configuration.md docs/zh-CN/runner-and-configuration.md docs/en/capabilities.md docs/zh-CN/capabilities.md
git commit -m "docs: document runtime contract boundary"
```

---

## Enterprise Coverage Check

This SDK plan supports the enterprise multi-user runtime path because it gives the enterprise worker these neutral building blocks:

- Build `RuntimeRunSpec` from enterprise EffectiveConfig without leaking user, org, project, RBAC, quota, audit, or billing concepts into SDK.
- Start a container and run a single SDK runtime process inside it.
- Use Docker SDK in enterprise code for image, mount, resource, network, and lifecycle management.
- Send a typed spec over a dedicated control socket.
- Keep the host-created run id through all stream events and final `RunState`.
- Load Python tools from host-materialized files and fail before execution if registration fails.
- Register MCP tools from trusted snapshots and connect only when the run actually invokes a tool.
- Pass SDK validation settings into the isolated runtime.
- Stream typed events and state snapshots back to enterprise storage.
- Resume review from serialized `RunState` plus `RuntimeReviewDecision`.

The plan deliberately does not solve enterprise-only work:

- Authentication and authorization.
- User, organization, project, and role models.
- EffectiveConfig composition and precedence.
- Queue claims, leases, cancellation ownership, and retries.
- Docker image selection, network policy, mounted volumes, and cleanup.
- Provider gateway token issuing and secret brokering.
- Quotas, usage accounting, audit logs, and billing.
- Web API and UI behavior.

## Self-Review

- Spec coverage: covers process-boundary schemas, host run id, validation settings, lazy MCP snapshots, Python tool fail-closed loading, controlled transport, runtime entrypoint, review resume, docs, and public API guardrails.
- Boundary control: no task adds users, organizations, projects, RBAC, EffectiveConfig, queues, leases, usage, audit, billing, Docker lifecycle, or worker orchestration to the SDK.
- Existing SDK reuse: keeps `Runner`, Python tool reload, MCP snapshots, `Runner.stream`, `Runner.resume_stream`, `catalog_view`, validation toggles, and agent registration as the execution owners.
- Protocol correction: stdout is no longer the production control protocol. Stdio remains a fallback only when the runtime process controls stdout.
- Ambiguity correction: `bye` distinguishes `process_status` from `run_status`.
- Enterprise fit: enough for the worker to run dagent inside a container without reimplementing the agent loop or tool/MCP semantics in enterprise backend code.
