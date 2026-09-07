"""Result and outcome schemas shared across dagent."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from dagent.profiles import AgentProfile
from dagent.schemas.common import (
    Boundary,
    RiskLevel,
    validate_extra_system_prompt,
    validate_runtime_directory,
)
from dagent.schemas.dag import DAG, DAGSpec
from dagent.schemas.artifact import ArtifactFileManifest
from dagent.schemas.capability import CapabilityInvocation, CapabilityResult
from dagent.schemas.run_trace import RunTrace
from dagent.schemas.sandbox import RunExecution
from dagent.schemas.context import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    ContextPolicy,
    ContextUsage,
    ResultStoragePolicy,
)
from dagent.schemas.conversation import (
    AssistantMessage,
    ToolCallItem,
    ToolResultMessage,
    ConversationItem,
    ConversationState,
)


ReviewKind = Literal["initial_dag", "dag_replan", "capability_review"]
LoopStatus = Literal["completed", "awaiting_review", "failed"]
RunStateKind = Literal["tool", "dynamic_dag", "static_dag"]
ReviewLevelValue = Literal["fast", "careful"]
RuntimeModeValue = Literal["auto", "tool", "dag", "dag_spec"]
PlannerFrontend = Literal["typed_spec", "sdk_builder"]


class ExecutionUsage(BaseModel):
    """Serializable operation counters consumed by a run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_operations: int = Field(default=0, ge=0)
    model_turns: int = Field(default=0, ge=0)
    capability_calls: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_total_operations(self) -> "ExecutionUsage":
        expected = self.model_turns + self.capability_calls
        if self.total_operations != expected:
            raise ValueError(
                "total_operations must equal model_turns + capability_calls."
            )
        return self


class _FrozenAgentProfile(AgentProfile):
    """Deeply immutable profile payload stored inside a resolved plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PlannerSkillSnapshot(BaseModel):
    """Frozen built-in planner skill included in resumable plans."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["generate-dag"]
    version: Literal[1]
    content: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_digest(self) -> "PlannerSkillSnapshot":
        digest = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.sha256 != digest:
            raise ValueError("Planner skill SHA-256 does not match its content.")
        return self


class ResolvedRunPlan(BaseModel):
    """Immutable, serializable execution semantics resolved by ``Runner``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[9] = 9
    runtime_kind: RunStateKind
    tool_profile: AgentProfile
    planner_profile: AgentProfile
    max_steps: int | None = Field(default=None, ge=1)
    review_level: ReviewLevelValue = "fast"
    dynamic_adjust: bool = True
    capability_ids: tuple[str, ...] = ()
    capability_fingerprints: dict[str, str] = Field(default_factory=dict)
    skill_ids: tuple[str, ...] = ()
    agent_ids: tuple[str, ...] = ()
    validation_enabled: bool = False
    validator_profile: AgentProfile | None = None
    max_validation_retries: int = Field(default=1, ge=0)
    planner_frontend: PlannerFrontend = "typed_spec"
    planner_skill: PlannerSkillSnapshot | None = None
    context_policy: ContextPolicy = Field(default_factory=ContextPolicy)
    result_storage_policy: ResultStoragePolicy = Field(default_factory=ResultStoragePolicy)
    runtime_directory: str
    context_window_tokens: int = Field(default=DEFAULT_CONTEXT_WINDOW_TOKENS, ge=1024)
    max_output_tokens: int | None = Field(default=None, ge=1)
    extra_system_prompt: str | None = None
    fingerprint: str = ""

    @field_validator(
        "tool_profile",
        "planner_profile",
        "validator_profile",
        mode="before",
    )
    @classmethod
    def freeze_profiles(cls, value: Any) -> _FrozenAgentProfile | None:
        if value is None:
            return None
        payload = value.model_dump() if isinstance(value, AgentProfile) else value
        return _FrozenAgentProfile.model_validate(payload)

    @field_validator("capability_ids", "skill_ids", "agent_ids", mode="before")
    @classmethod
    def canonicalize_ids(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            raise ValueError("Resolved run plan ids must be a collection of strings.")
        ids = tuple(str(item).strip() for item in (value or ()))
        if any(not item for item in ids):
            raise ValueError("Resolved run plan ids must not be empty.")
        return tuple(sorted(set(ids)))

    @field_validator("runtime_directory", mode="before")
    @classmethod
    def validate_runtime_directory(cls, value: Any) -> str:
        return validate_runtime_directory(value)

    @field_validator("extra_system_prompt", mode="before")
    @classmethod
    def validate_extra_system_prompt_value(cls, value: Any) -> str | None:
        return validate_extra_system_prompt(value)

    @model_validator(mode="after")
    def validate_resolved_configuration(self) -> "ResolvedRunPlan":
        if self.runtime_kind == "static_dag" and self.max_steps is not None:
            raise ValueError("Static DAG plans cannot contain max_steps.")
        if self.runtime_kind != "static_dag" and self.max_steps is None:
            raise ValueError("Agent run plans require max_steps.")
        if self.planner_frontend == "sdk_builder" and self.planner_skill is None:
            raise ValueError("sdk_builder plans require a frozen planner skill.")
        if self.planner_frontend == "typed_spec" and self.planner_skill is not None:
            raise ValueError("typed_spec plans cannot include a builder planner skill.")
        if (
            self.max_output_tokens is not None
            and self.max_output_tokens >= self.context_window_tokens
        ):
            raise ValueError(
                "max_output_tokens must be smaller than context_window_tokens."
            )
        if self.validation_enabled and self.validator_profile is None:
            raise ValueError(
                "validator_profile is required when validation_enabled is true."
            )
        expected_agent_ids = tuple(
            capability_id
            for capability_id in self.capability_ids
            if capability_id.startswith("agent.")
        )
        if self.agent_ids != expected_agent_ids:
            raise ValueError(
                "agent_ids must exactly match agent.* entries in capability_ids."
            )
        if set(self.capability_fingerprints) != set(self.capability_ids):
            raise ValueError(
                "capability_fingerprints must exactly match capability_ids."
            )
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in self.capability_fingerprints.values()
        ):
            raise ValueError(
                "capability_fingerprints values must be lowercase SHA-256 digests."
            )
        expected_fingerprint = self.canonical_fingerprint()
        if self.fingerprint and self.fingerprint != expected_fingerprint:
            raise ValueError("Resolved run plan fingerprint does not match its payload.")
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", expected_fingerprint)
        return self

    @model_serializer(mode="wrap")
    def serialize_current_execution_fields(self, serializer):
        payload = serializer(self)
        if self.runtime_kind == "static_dag" and self.max_steps is None:
            payload.pop("max_steps", None)
        return payload

    def canonical_fingerprint(self) -> str:
        """Return the SDK-defined SHA-256 fingerprint for this plan payload."""

        payload = self.model_dump(mode="json", exclude={"fingerprint"})
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def validate_fingerprint(self) -> None:
        if self.fingerprint != self.canonical_fingerprint():
            raise ValueError("Resolved run plan fingerprint does not match its payload.")

    def effective_max_steps(self) -> int:
        """Return the active local loop bound."""

        return self.max_steps or 888


class RunCapabilityScope(BaseModel):
    """Serializable capability visibility for a resumable run."""

    model_config = ConfigDict(extra="forbid")

    capability_ids: tuple[str, ...] | None = None
    skills: tuple[str, ...] | None = None


class PendingCapabilityCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invocation_id: str
    capability_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class PendingCapabilityReviewItem(PendingCapabilityCall):
    """One review requirement, independent of its single/batch presentation."""

    message: str = ""
    risk: RiskLevel = "low"
    reason: Literal["risk", "boundary_violation"] = "risk"
    boundary_paths: tuple[str, ...] = ()
    error: str | None = None

    def single_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"capability_id": self.capability_id, "risk": self.risk}
        if self.reason == "boundary_violation":
            payload.update(reason=self.reason, error=self.error, boundary_paths=list(self.boundary_paths))
        return payload

    def call(self) -> PendingCapabilityCall:
        return PendingCapabilityCall(**self.model_dump(include={
            "invocation_id", "capability_id", "tool_name", "arguments",
        }))


class PendingReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str
    kind: ReviewKind
    message: str
    proposed_dag: DAG | None = None
    proposed_dag_spec: DAGSpec | None = None
    rerun_nodes: tuple[str, ...] = ()
    capability_call: PendingCapabilityCall | None = None
    capability_calls: tuple[PendingCapabilityReviewItem, ...] = ()
    queued_call_count: int = Field(default=0, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def capability_items(self) -> tuple[PendingCapabilityReviewItem, ...]:
        if self.capability_call is None:
            return self.capability_calls
        return (PendingCapabilityReviewItem(
            **self.capability_call.model_dump(), message=self.message,
            risk=self.payload.get("risk", "low"),
            reason=self.payload.get("reason", "risk"),
            boundary_paths=self.payload.get("boundary_paths", ()),
            error=self.payload.get("error"),
        ),)

    @model_validator(mode="after")
    def validate_review_payload(self) -> "PendingReview":
        if self.kind == "capability_review":
            if (self.capability_call is not None) == bool(self.capability_calls):
                raise ValueError("Capability reviews require exactly one single or batch presentation.")
            if self.capability_calls and len(self.capability_calls) < 2:
                raise ValueError("Batch capability reviews require at least two calls.")
            ids = [item.invocation_id for item in self.capability_items]
            if len(ids) != len(set(ids)):
                raise ValueError("Capability review invocation ids must be unique.")
            if self.rerun_nodes:
                raise ValueError("Capability reviews cannot request DAG node reruns.")
        elif self.capability_call is not None or self.capability_calls:
            raise ValueError("DAG reviews cannot contain capability calls.")
        if len(set(self.rerun_nodes)) != len(self.rerun_nodes):
            raise ValueError("Pending review rerun_nodes must be unique.")
        return self


class _PendingToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call: ToolCallItem
    invocation: CapabilityInvocation | None = None
    review: PendingCapabilityReviewItem | None = None
    error: str | None = None
    error_result: CapabilityResult | None = None
    approved: bool | None = None
    feedback: str | None = None
    started: bool = False

    @model_validator(mode="after")
    def validate_call(self) -> "_PendingToolCall":
        invocation = self.invocation
        if invocation is None:
            if self.error is None or self.review is not None or self.approved is not None:
                raise ValueError("Unresolved tool calls require a preflight error and cannot be approved.")
            return self
        if invocation.invocation_id != self.call.id or invocation.arguments != self.call.arguments:
            raise ValueError("Pending tool call and invocation do not match.")
        if self.review is not None and (
            self.review.call() != PendingCapabilityCall(
                invocation_id=self.call.id, capability_id=invocation.capability_id,
                tool_name=self.call.name, arguments=self.call.arguments,
            )
        ):
            raise ValueError("Pending capability review does not match its invocation.")
        if self.approved is not None and self.review is None:
            raise ValueError("Only reviewed tool calls can carry a decision.")
        return self


class _PendingToolBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calls: tuple[_PendingToolCall, ...]
    next_index: int = Field(default=0, ge=0)
    boundary: Boundary

    @model_validator(mode="after")
    def validate_batch(self) -> "_PendingToolBatch":
        ids = [item.call.id for item in self.calls]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("Pending tool batch requires unique call ids.")
        if self.next_index >= len(self.calls):
            raise ValueError("Pending tool batch cursor is outside its calls.")
        return self


class _StaticDagAgentContinuation(BaseModel):
    """Internal checkpoint data for one suspended direct static-DAG agent node."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    invocation: CapabilityInvocation
    agent_state: "RunState"
    graph_input: Any = None


class RunState(BaseModel):
    """Serializable same-run state embedded in results and checkpoints."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[6] = 6
    run_id: str
    kind: RunStateKind
    status: LoopStatus
    conversation: ConversationState | None = None
    model_thread: ConversationState | None = None
    context_usage: list[ContextUsage] = Field(default_factory=list)
    dag: DAG | None = None
    dag_spec: DAGSpec | None = None
    trace: RunTrace | None = None
    pending_review: PendingReview | None = None
    pending_tool_batch: _PendingToolBatch | None = None
    user_request: str = ""
    review_level: ReviewLevelValue = "fast"
    runtime_mode: RuntimeModeValue = "auto"
    execution: RunExecution = "local"
    dynamic_adjust: bool = True
    planner_frontend: PlannerFrontend = "typed_spec"
    capability_scope: RunCapabilityScope = Field(default_factory=RunCapabilityScope)
    spec_id: str | None = None
    workspace_path: str | None = None
    dag_boundary_approved_version: int | None = None
    static_agent_continuation: _StaticDagAgentContinuation | None = None
    input_artifact_files: tuple[ArtifactFileManifest, ...] = ()

    @model_validator(mode="after")
    def validate_input_artifact_files(self) -> "RunState":
        manifests = self.input_artifact_files
        artifact_ids = [manifest.artifact_id for manifest in manifests]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("Run input artifact manifests must have unique artifact ids.")
        if artifact_ids != sorted(artifact_ids):
            raise ValueError("Run input artifact manifests must be sorted by artifact id.")
        if not manifests:
            return self
        if self.kind != "static_dag" or self.dag_spec is None:
            raise ValueError(
                "Input artifact file manifests require a static DAG run with a DAGSpec."
            )
        for manifest in manifests:
            artifact = self.dag_spec.artifacts.get(manifest.artifact_id)
            if artifact is None:
                raise ValueError(
                    f"Input artifact manifest references unknown artifact '{manifest.artifact_id}'."
                )
            for file in manifest.files:
                if not _artifact_declares_file_path(artifact.paths, file.path):
                    raise ValueError(
                        f"Artifact file '{file.path}' is outside declared artifact "
                        f"'{manifest.artifact_id}' paths."
                    )
        return self


class RunCheckpoint(BaseModel):
    """Portable continuation snapshot generated by the SDK."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[9] = 9
    state: RunState
    plan: ResolvedRunPlan
    usage: ExecutionUsage = Field(default_factory=ExecutionUsage)

    @model_validator(mode="after")
    def validate_checkpoint_consistency(self) -> "RunCheckpoint":
        self.plan.validate_fingerprint()
        if self.schema_version != self.plan.schema_version:
            raise ValueError("Checkpoint schema version does not match the resolved run plan.")
        expected_state_version = 6
        if self.state.schema_version != expected_state_version:
            raise ValueError(
                f"Checkpoint V{self.schema_version} requires RunState V{expected_state_version}."
            )
        if self.state.planner_frontend != self.plan.planner_frontend:
            raise ValueError("Checkpoint planner frontend does not match the resolved run plan.")
        if self.state.kind != self.plan.runtime_kind:
            raise ValueError("Checkpoint state kind does not match the resolved run plan.")
        expected_runtime_mode = {
            "tool": "tool",
            "dynamic_dag": "dag",
            "static_dag": "dag_spec",
        }[self.state.kind]
        if self.state.runtime_mode != expected_runtime_mode:
            raise ValueError(
                "Checkpoint runtime mode does not match the run state kind."
            )
        if self.state.review_level != self.plan.review_level:
            raise ValueError(
                "Checkpoint review level does not match the resolved run plan."
            )
        if self.state.dynamic_adjust != self.plan.dynamic_adjust:
            raise ValueError(
                "Checkpoint dynamic_adjust does not match the resolved run plan."
            )
        if self.state.capability_scope.capability_ids != self.plan.capability_ids:
            raise ValueError(
                "Checkpoint capability scope does not match the resolved run plan."
            )
        if self.state.capability_scope.skills != self.plan.skill_ids:
            raise ValueError("Checkpoint skill scope does not match the resolved run plan.")
        pending_review = self.state.pending_review
        if (self.state.status == "awaiting_review") != (pending_review is not None):
            raise ValueError("Checkpoint awaiting_review status and pending review must agree.")
        continuation = self.state.static_agent_continuation
        tool_state = self.state
        if continuation is not None:
            tool_state = continuation.agent_state
            if (
                self.state.kind != "static_dag"
                or tool_state.status != "awaiting_review"
                or tool_state.pending_review != pending_review
                or self.state.pending_tool_batch is not None
            ):
                raise ValueError("Static agent continuation does not match its mirrored review state.")
            if continuation.invocation.capability_id not in self.plan.capability_ids:
                raise ValueError("Static agent continuation is outside the resolved scope.")
        elif self.state.kind == "static_dag" and pending_review is not None:
            raise ValueError("Static DAG capability reviews require a static agent continuation.")
        batch = tool_state.pending_tool_batch
        if pending_review is not None and pending_review.kind == "capability_review":
            if batch is None:
                raise ValueError("Capability review checkpoints require a pending tool batch.")
            expected = tuple(item.review for item in batch.calls[batch.next_index:]
                             if item.review is not None and item.approved is None)
            if pending_review.capability_items != expected:
                raise ValueError("Checkpoint pending capability calls and batch do not match.")
            scope = (self.plan.capability_ids if continuation is None
                     else tool_state.capability_scope.capability_ids)
            for item in batch.calls:
                if item.invocation is not None and scope is not None and item.invocation.capability_id not in scope:
                    raise ValueError("Checkpoint pending capability is outside the resolved scope.")
            thread = tool_state.model_thread or tool_state.conversation
            assistant = next((item for item in reversed(thread.items if thread else ())
                              if isinstance(item, AssistantMessage)), None)
            if assistant is None or assistant.tool_calls != tuple(item.call for item in batch.calls):
                raise ValueError("Checkpoint tool batch does not match its model reply.")
            results = [item.call_id for item in (thread.items if thread else ())
                       if isinstance(item, ToolResultMessage) and item.call_id in {c.call.id for c in batch.calls}]
            if results != [item.call.id for item in batch.calls[:batch.next_index]]:
                raise ValueError("Checkpoint tool results do not match its batch cursor.")
        elif batch is not None:
            raise ValueError("Pending tool batch requires a capability review.")
        return self


def _artifact_declares_file_path(paths: list[str], file_path: str) -> bool:
    candidate = PurePosixPath(file_path)
    for declared_path in paths:
        normalized = declared_path.replace("\\", "/").rstrip("/")
        if not normalized:
            continue
        declared = PurePosixPath(normalized)
        if candidate == declared or declared in candidate.parents:
            return True
    return False


_StaticDagAgentContinuation.model_rebuild()


class LoopOutcome(BaseModel):
    """Common contract between loops and runtime orchestration."""

    state: RunState
    output_text: str = ""
    execution_context: str = ""
    new_items: tuple[ConversationItem, ...] = ()


class ValidationIssue(BaseModel):
    message: str
    node_id: str | None = None
    capability_id: str | None = None
    code: str | None = None


class ValidationResult(BaseModel):
    passed: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    summary: str = ""
