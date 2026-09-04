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
    validate_extra_system_prompt,
    validate_runtime_directory,
)
from dagent.schemas.dag import DAG, DAGSpec
from dagent.schemas.artifact import ArtifactFileManifest
from dagent.schemas.capability import CapabilityInvocation
from dagent.schemas.run_trace import RunTrace
from dagent.schemas.sandbox import RunExecution
from dagent.schemas.context import ContextPolicy, ContextUsage, ResultStoragePolicy
from dagent.schemas.conversation import (
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

    schema_version: Literal[8] = 8
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
    context_window_tokens: int = Field(default=32768, ge=1024)
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


class PendingReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str
    kind: ReviewKind
    message: str
    proposed_dag: DAG | None = None
    proposed_dag_spec: DAGSpec | None = None
    rerun_nodes: tuple[str, ...] = ()
    capability_call: PendingCapabilityCall | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_review_payload(self) -> "PendingReview":
        if self.kind == "capability_review" and self.capability_call is None:
            raise ValueError("Capability reviews require capability_call.")
        if self.kind == "capability_review" and self.rerun_nodes:
            raise ValueError("Capability reviews cannot request DAG node reruns.")
        if len(set(self.rerun_nodes)) != len(self.rerun_nodes):
            raise ValueError("Pending review rerun_nodes must be unique.")
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

    schema_version: Literal[5] = 5
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
    pending_invocation: CapabilityInvocation | None = None
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

    schema_version: Literal[8] = 8
    state: RunState
    plan: ResolvedRunPlan
    usage: ExecutionUsage = Field(default_factory=ExecutionUsage)

    @model_validator(mode="after")
    def validate_checkpoint_consistency(self) -> "RunCheckpoint":
        self.plan.validate_fingerprint()
        if self.schema_version != self.plan.schema_version:
            raise ValueError("Checkpoint schema version does not match the resolved run plan.")
        expected_state_version = 5
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
        pending_invocation = self.state.pending_invocation
        if (self.state.status == "awaiting_review") != (pending_review is not None):
            raise ValueError(
                "Checkpoint awaiting_review status and pending review must agree."
            )
        if pending_review is not None and pending_review.kind == "capability_review":
            pending_call = pending_review.capability_call
            if pending_call is None or pending_invocation is None:
                raise ValueError(
                    "Capability review checkpoints require a pending invocation."
                )
            if (
                pending_call.invocation_id != pending_invocation.invocation_id
                or pending_call.capability_id != pending_invocation.capability_id
                or pending_call.arguments != pending_invocation.arguments
            ):
                raise ValueError(
                    "Checkpoint pending capability call and invocation do not match."
                )
            if pending_invocation.capability_id not in self.plan.capability_ids:
                raise ValueError(
                    "Checkpoint pending capability is outside the resolved scope."
                )
        elif pending_invocation is not None:
            raise ValueError(
                "Checkpoint pending invocation requires a capability review."
            )
        continuation = self.state.static_agent_continuation
        if continuation is not None:
            child = continuation.agent_state
            if (
                self.state.kind != "static_dag"
                or child.status != "awaiting_review"
                or child.pending_review != pending_review
                or child.pending_invocation != pending_invocation
            ):
                raise ValueError(
                    "Static agent continuation does not match its mirrored review state."
                )
            if continuation.invocation.capability_id not in self.plan.capability_ids:
                raise ValueError(
                    "Static agent continuation is outside the resolved scope."
                )
        elif self.state.kind == "static_dag" and pending_review is not None:
            raise ValueError(
                "Static DAG capability reviews require a static agent continuation."
            )
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
