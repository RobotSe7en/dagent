"""Runtime process-boundary schemas."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from dagent.config import ProviderConfig, UserPythonToolConfig
from dagent.schemas.capability import MCPServerSnapshot
from dagent.schemas.dag import DAG, DAGSpec
from dagent.schemas.results import LoopStatus, RunState
from dagent.schemas.sandbox import SandboxConfig

if TYPE_CHECKING:
    from dagent.result import RunStreamEvent


RuntimeAction = Literal["run", "resume"]
RuntimeFrameType = Literal["hello", "spec", "event", "state_snapshot", "log", "bye"]
RuntimeTargetType = Literal["auto_agent", "tool_agent", "dag_agent", "dag_spec"]
RuntimeProcessStatus = Literal["completed", "failed"]
RuntimeReviewLevel = Literal["fast", "careful"]


class RuntimeWorkspaceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_root: str | None = None
    run_workspace_root: str | None = None
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
    review: RuntimeReviewLevel = "fast"
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
    review: RuntimeReviewLevel = "fast"
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
    review_level: RuntimeReviewLevel | None = None
    feedback: str | None = None

    def to_review_decision(self):
        from dagent.review import ReviewDecision

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
        if self.action == "resume" and self.state is None:
            raise ValueError("resume specs require state.")
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

    @model_validator(mode="after")
    def validate_terminal_status(self) -> "RuntimeByePayload":
        if self.process_status == "completed":
            if self.exit_code != 0:
                raise ValueError("completed worker processes require exit_code 0.")
            if self.error_type is not None or self.error is not None:
                raise ValueError("completed worker processes may not include errors.")
        if self.process_status == "failed" and self.exit_code == 0:
            raise ValueError("failed worker processes require a non-zero exit_code.")
        return self


_EVENT_ADAPTER: TypeAdapter[Any] | None = None


def _event_adapter() -> TypeAdapter[Any]:
    global _EVENT_ADAPTER
    if _EVENT_ADAPTER is None:
        from dagent.result import RunStreamEvent

        _EVENT_ADAPTER = TypeAdapter(RunStreamEvent)
    return _EVENT_ADAPTER


class RuntimeFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: RuntimeFrameType
    payload: Any = None

    @model_validator(mode="after")
    def validate_payload_for_type(self) -> "RuntimeFrame":
        if self.type == "spec":
            RuntimeRunSpec.model_validate(self.payload)
        elif self.type == "event":
            _event_adapter().validate_python(self.payload)
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

    def event_payload(self) -> "RunStreamEvent":
        if self.type != "event":
            raise TypeError("RuntimeFrame is not an event frame.")
        return _event_adapter().validate_python(self.payload)

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
