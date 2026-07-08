"""FastAPI application exposing the dagent harness."""

from __future__ import annotations

import asyncio
import base64
import binascii
import codecs
import hashlib
import hmac
import json
import mimetypes
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, AsyncIterator, Literal
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from api.agent_presets import (
    AgentPreset,
    AgentPresetStore,
    AgentPresetUpdateRequest,
    clean_agent_preset_name,
)
from api.python_tools import discover_python_tool_names, load_python_tool_sources, read_python_tool_source
from api.storage import (
    Conversation,
    ConversationBusyError,
    ConversationMessage,
    OrchestrationSession,
    Project,
    Run,
    RunEvent,
    SavedDag,
    SQLiteStore,
    StorageConflictError,
    Store,
)
from api.workspaces import LocalWorkspaceStore
from dagent import (
    ArtifactUpload,
    AutoAgent,
    Boundary,
    CapabilityDefinition,
    CapabilityInvocation,
    CapabilityPolicy,
    CapabilityResult,
    DAG,
    DAGRun,
    Dag,
    DagAgent,
    Node,
    ProfileStore,
    ReviewDecision,
    ReviewLevel,
    RiskLevel,
    Runner,
    RunState,
    RunStreamEvent,
    SkillAmbiguousError,
    SkillNotFoundError,
    SkillPermissionError,
    SkillStore,
    SkillStoreError,
    Provider,
    default_skill_roots,
    ToolAgent,
    validate_dag_spec,
)
from dagent.config import (
    DEFAULT_RUNS_DIR,
    DEFAULT_WORKSPACE,
    UserDagentConfig,
    UserModelProviderConfig,
    UserOnlyOfficeConfig,
    UserPythonToolConfig,
    default_user_config_path,
    load_config,
    load_user_config,
    load_user_config_with_python_tool_errors,
    resolve_config_path,
    resolve_config_relative_path,
    save_user_config,
)
from dagent.capabilities.mcp.config import (
    DEFAULT_MCP_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_MCP_TOOL_TIMEOUT_SECONDS,
)
from dagent.capabilities.providers import agent_capability_parameters
from dagent.harness_runtime.artifacts import (
    ArtifactPathError,
    materialize_artifact_uploads,
    resolve_artifact_paths,
    validate_upload_filename,
)
from dagent.profiles import AgentProfile, list_builtin_profiles, load_builtin_profile
from dagent.schemas import Artifact, DAGEdge, validate_capability_id_segment

from api.stream_gate import gate_chat_display


MessageTarget = Literal["auto", "tool", "dag"]
AgentScope = Literal["none", "selected", "registered"]
CONFIG_MODEL_ID = "config"
ApiKeyAction = Literal["preserve", "replace", "clear"]
ModelProviderSource = Literal["config", "user"]
REDACTED_SECRET_VALUE = "[redacted]"
RunArtifactSource = Literal["dag_artifact", "run_file"]
RunArtifactTextPreviewKind = Literal["markdown", "code", "text"]
RunArtifactBrowserPreviewKind = Literal["pdf", "docx", "xlsx", "pptx"]
RunArtifactPreviewKind = RunArtifactTextPreviewKind | RunArtifactBrowserPreviewKind
RUN_ARTIFACT_PREVIEW_BYTES = 200_000
RUN_ARTIFACT_SCAN_LIMIT = 500
RUN_ARTIFACT_SCAN_VISIT_LIMIT = 5_000
ONLYOFFICE_TOKEN_SECONDS = 10 * 60
ONLYOFFICE_EDIT_TOKEN_SECONDS = 24 * 60 * 60
PROFILE_CONTENT_BYTES_LIMIT = 128 * 1024
_MANAGED_PROFILE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._-]*[A-Za-z0-9][A-Za-z0-9._-]*$")
_LOCAL_MCP_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_PROJECT_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_MARKDOWN_EXTENSIONS = {".md", ".markdown"}
_TEXT_EXTENSIONS = {".csv", ".log", ".txt", ".tsv"}
_BROWSER_PREVIEW_EXTENSIONS: dict[str, RunArtifactBrowserPreviewKind] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
}
_ONLYOFFICE_DOCUMENT_TYPES: dict[RunArtifactBrowserPreviewKind, Literal["word", "cell", "slide"]] = {
    "docx": "word",
    "xlsx": "cell",
    "pptx": "slide",
}
_CODE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".mjs",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".xml",
    ".yaml",
    ".yml",
}
_CODE_FILENAMES = {"Dockerfile", "Makefile"}
_MEDIA_TYPE_OVERRIDES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".py": "text/x-python",
    ".sh": "text/x-shellscript",
    ".ts": "text/typescript",
    ".tsx": "text/typescript-jsx",
    ".jsx": "text/jsx",
}
_TEXT_PREVIEW_KINDS: set[RunArtifactTextPreviewKind] = {"markdown", "code", "text"}


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, Any]] = Field(min_length=1)
    visible_message: str | None = None
    state: RunState | None = None
    target: MessageTarget = "auto"
    review_level: ReviewLevel = "fast"
    dynamic_adjust: bool = True
    capability_ids: list[str] | None = None
    skills: list[str] | None = None
    agent_scope: AgentScope = "none"
    agent_ids: list[str] | None = None
    workspace_root: str | None = None
    project_id: str | None = None
    conversation_id: str | None = None


class ResumeReviewRequest(BaseModel):
    review_id: str = Field(min_length=1)
    dag: DAG | None = None
    approved: bool = True
    review_level: ReviewLevel | None = None
    state: RunState | None = None
    feedback: str | None = None


class ProjectResumeReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dag: DAG | None = None
    approved: bool = True
    review_level: ReviewLevel | None = None
    feedback: str | None = None


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    slug: str | None = None
    description: str | None = None


class ProjectUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    slug: str | None = None
    description: str | None = None


class ConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    kind: Literal["chat", "dynamic_dag", "static_dag"] = "chat"


class ConversationUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)


class ProjectFolderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)


class ProjectFileMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    new_path: str = Field(min_length=1)


class ProjectFileDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)


class CapabilityTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any] = Field(default_factory=dict)
    boundary: Boundary | None = None


class DAGRunRequest(BaseModel):
    workspace_root: str | None = None
    graph_input: Any = None


class UserDAGAgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: list[str] | None = None
    skills: list[str] | None = None


class UserDAGNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    target: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    artifact_inputs: list[str] = Field(default_factory=list)
    artifact_outputs: list[str] = Field(default_factory=list)
    title: str = ""
    boundary: Boundary | None = None
    agent: UserDAGAgentConfig | None = None


class UserDAG(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    version: int = 1
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, Artifact] = Field(default_factory=dict)
    nodes: list[UserDAGNode] = Field(default_factory=list)
    edges: list[DAGEdge] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SavedDAGCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    name: str | None = None
    description: str = ""
    spec: UserDAG
    layout: dict[str, Any] = Field(default_factory=dict)


class SavedDAGUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    spec: UserDAG | None = None
    layout: dict[str, Any] | None = None
    expected_revision: int | None = None


class SavedDAGRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    conversation_id: str = Field(min_length=1)
    graph_input: Any = None


class OrchestrationSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1)
    project_id: str | None = None
    kind: Literal["dynamic_dag", "static_dag"]
    saved_dag_id: str | None = None
    draft_dag: dict[str, Any] | None = None
    ui_state: dict[str, Any] = Field(default_factory=dict)


class OrchestrationSessionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    saved_dag_id: str | None = None
    draft_dag: dict[str, Any] | None = None
    ui_state: dict[str, Any] = Field(default_factory=dict)


class DAGValidationIssue(BaseModel):
    severity: Literal["error", "warning"] = "error"
    code: str = "dag_validation_error"
    message: str
    node_id: str | None = None
    path: list[str | int] = Field(default_factory=list)


class DAGValidationResponse(BaseModel):
    valid: bool
    issues: list[DAGValidationIssue] = Field(default_factory=list)


class ProfileCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    content: str = Field(min_length=1)


class ProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)


class MCPServerRequest(BaseModel):
    name: str = Field(min_length=1)
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    risk: RiskLevel = "medium"
    connect_timeout: float = DEFAULT_MCP_CONNECT_TIMEOUT_SECONDS
    tool_timeout: float = DEFAULT_MCP_TOOL_TIMEOUT_SECONDS
    include_tools: list[str] = Field(default_factory=list)
    exclude_tools: list[str] = Field(default_factory=list)


class ModelProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key: str | None = None
    api_key_action: ApiKeyAction = "replace"
    api_key_env: str | None = None
    timeout_seconds: float = 60
    strip_thinking: bool = False
    reasoning: dict[str, Any] | None = None
    extra_request_args: dict[str, Any] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)


class ModelProviderPayload(BaseModel):
    id: str
    name: str
    source: ModelProviderSource
    active: bool
    base_url: str
    model: str
    api_key_env: str | None = None
    api_key_configured: bool
    api_key_saved: bool
    timeout_seconds: float
    strip_thinking: bool
    reasoning: dict[str, Any] | None = None
    extra_request_args: dict[str, Any] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)


class ModelListResponse(BaseModel):
    models: list[ModelProviderPayload]
    active_model_id: str


class ModelMutationResponse(BaseModel):
    model: ModelProviderPayload
    active_model_id: str


class ModelDeleteResponse(BaseModel):
    status: str
    active_model_id: str


class OnlyOfficeSettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    document_server_url: str | None = None
    public_api_base: str | None = None
    jwt_secret: str | None = None
    lang: str = "zh"
    project_file_edit_enabled: bool = False
    run_artifact_edit_enabled: bool = False


class OnlyOfficeCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: int
    forcesavetype: int | None = None
    url: str | None = None


class PythonToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    source: Literal["path", "managed", "module"] = "path"
    path: str | None = None
    module: str | None = None
    names: list[str] = Field(default_factory=list)
    enabled: bool = True


class PythonToolPayload(PythonToolRequest):
    status: Literal["loaded", "disabled", "error"]
    capabilities: list[str] = Field(default_factory=list)
    error: str | None = None


class RunArtifactFile(BaseModel):
    id: str
    artifact_id: str | None = None
    source: RunArtifactSource
    path: str
    name: str
    media_type: str
    preview_kind: RunArtifactPreviewKind | None = None
    previewable: bool
    size: int | None = None
    version: str | None = None
    status: str = "created"
    error: str | None = None
    preview_url: str | None = None
    download_url: str | None = None
    onlyoffice_config_url: str | None = None


class RunArtifactsResponse(BaseModel):
    run_id: str
    workspace_path: str | None = None
    artifacts: dict[str, Any] = Field(default_factory=dict)
    files: list[RunArtifactFile] = Field(default_factory=list)
    files_truncated: bool = False
    file_limit: int = RUN_ARTIFACT_SCAN_LIMIT
    visit_limit: int = RUN_ARTIFACT_SCAN_VISIT_LIMIT


class RunArtifactPreviewResponse(BaseModel):
    run_id: str
    path: str
    name: str
    media_type: str
    preview_kind: RunArtifactTextPreviewKind
    content: str
    size: int
    truncated: bool
    truncated_at: int = RUN_ARTIFACT_PREVIEW_BYTES


class ProjectFileItem(BaseModel):
    path: str
    name: str
    kind: Literal["file", "directory"]
    media_type: str | None = None
    preview_kind: RunArtifactPreviewKind | None = None
    previewable: bool = False
    size: int | None = None
    modified_at: int | None = None
    version: str | None = None
    preview_url: str | None = None
    download_url: str | None = None
    onlyoffice_config_url: str | None = None


class ProjectFileTreeItem(ProjectFileItem):
    children: list["ProjectFileTreeItem"] = Field(default_factory=list)


class ProjectFilesResponse(BaseModel):
    project_id: str
    path: str
    files: list[ProjectFileItem] = Field(default_factory=list)


class ProjectFileTreeResponse(ProjectFilesResponse):
    tree: list[ProjectFileTreeItem] = Field(default_factory=list)


class ProjectFilePreviewResponse(BaseModel):
    project_id: str
    path: str
    name: str
    media_type: str
    preview_kind: RunArtifactTextPreviewKind
    content: str
    size: int
    truncated: bool
    truncated_at: int = RUN_ARTIFACT_PREVIEW_BYTES


class RunArtifactOnlyOfficeConfigResponse(BaseModel):
    document_server_url: str
    script_url: str
    config: dict[str, Any]


@dataclass(frozen=True)
class PersistedMessageContext:
    project_id: str | None
    conversation_id: str
    conversation_kind: Literal["chat", "dynamic_dag", "static_dag"]
    workspace_uri: str
    workspace_path: Path
    run_state: RunState | None = None
    orchestration_session_id: str | None = None
    orchestration_surface: str | None = None


ORCHESTRATION_WORKSPACE_SURFACE = "orchestration_workspace"
SMART_WORKBENCH_SURFACE = "smart_workbench"


def _should_project_conversation_messages(context: PersistedMessageContext) -> bool:
    return context.conversation_kind == "chat" or (
        context.conversation_kind == "dynamic_dag"
        and context.orchestration_surface in {
            ORCHESTRATION_WORKSPACE_SURFACE,
            SMART_WORKBENCH_SURFACE,
        }
    )


class ConversationMessageProjection:
    def __init__(
        self,
        *,
        message_id: str,
        context: PersistedMessageContext,
        user_message_id: str | None = None,
        run_id: str | None = None,
        content: str = "",
        status: str = "running",
        timeline: list[dict[str, Any]] | None = None,
        dag: dict[str, Any] | None = None,
        trace: dict[str, Any] | None = None,
        pending_review: dict[str, Any] | None = None,
    ) -> None:
        self.message_id = message_id
        self.context = context
        self.user_message_id = user_message_id
        self.user_message_run_id: str | None = None
        self.run_id = run_id
        self.content = content
        self.status = status
        self.timeline = timeline or []
        self.dag = dag
        self.trace = trace
        self.pending_review = pending_review
        self._streamed_content = bool(content.strip())

    @classmethod
    async def start_for_message_request(
        cls,
        request: MessageRequest,
        context: PersistedMessageContext,
    ) -> "ConversationMessageProjection | None":
        if not _should_project_conversation_messages(context):
            return None
        store = state.get_store()
        visible_content = (
            request.visible_message
            if request.visible_message is not None
            else _visible_chat_message_content(request.messages[-1])
        )
        user_content = visible_content.strip()
        user_message_id = None
        if user_content:
            user_message = await run_in_threadpool(
                store.append_conversation_message,
                message_id=_new_api_id("msg"),
                conversation_id=context.conversation_id,
                project_id=context.project_id,
                role="user",
                content=user_content,
                status="completed",
            )
            user_message_id = user_message.id
        assistant = await run_in_threadpool(
            store.append_conversation_message,
            message_id=_new_api_id("msg"),
            conversation_id=context.conversation_id,
            project_id=context.project_id,
            role="assistant",
            content="",
            status="running",
            run_id=None,
        )
        return cls(
            message_id=assistant.id,
            context=context,
            user_message_id=user_message_id,
            run_id=assistant.run_id,
            content=assistant.content,
            status=assistant.status,
            timeline=_json_array(assistant.timeline_json),
            dag=_json_object_or_none(assistant.dag_json),
            trace=_json_object_or_none(assistant.trace_json),
            pending_review=_json_object_or_none(assistant.pending_review_json),
        )

    @classmethod
    async def resume_for_review(
        cls,
        run_state: RunState,
        context: PersistedMessageContext,
        decision: ReviewDecision,
    ) -> "ConversationMessageProjection | None":
        if not _should_project_conversation_messages(context):
            return None
        store = state.get_store()
        message = await run_in_threadpool(
            store.get_last_assistant_message_for_run,
            context.conversation_id,
            run_state.run_id,
        )
        if message is None:
            message = await run_in_threadpool(
                store.append_conversation_message,
                message_id=_new_api_id("msg"),
                conversation_id=context.conversation_id,
                project_id=context.project_id,
                role="assistant",
                content="",
                status="running",
                run_id=run_state.run_id,
            )
        projection = cls(
            message_id=message.id,
            context=context,
            run_id=message.run_id,
            content=message.content,
            status=message.status,
            timeline=_json_array(message.timeline_json),
            dag=_json_object_or_none(message.dag_json),
            trace=_json_object_or_none(message.trace_json),
            pending_review=_json_object_or_none(message.pending_review_json),
        )
        await projection.apply_review_decision(decision)
        return projection

    async def apply_review_decision(self, decision: ReviewDecision) -> None:
        if not self.pending_review:
            return
        if not decision.approved:
            capability_call = self.pending_review.get("capability_call")
            if isinstance(capability_call, dict):
                invocation_id = str(capability_call.get("invocation_id") or "")
                capability_id = str(capability_call.get("capability_id") or "")
                if invocation_id:
                    content = "人工审核已拒绝。"
                    if decision.feedback:
                        content = f"{content}\n\n反馈：{decision.feedback}"
                    result = {
                        "type": "capability.call.failed",
                        "invocation_id": invocation_id,
                        "capability_id": capability_id,
                        "arguments": capability_call.get("arguments") or {},
                        "content": content,
                    }
                    self._upsert_capability_result(invocation_id, result, status="rejected")
        self.pending_review = None
        self.status = "running"
        await self.save()

    async def handle_payload(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("type") or "")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        assert isinstance(data, dict)
        if payload.get("run_id"):
            self.run_id = str(payload["run_id"])
        if event_type == "run.started":
            self.status = "running"
            await self.bind_user_message_to_run()
        elif event_type == "response.reasoning.delta":
            self._append_reasoning(str(data.get("delta") or ""))
        elif event_type == "response.content.delta":
            self._append_text(str(data.get("delta") or ""))
        elif event_type == "capability.call.started":
            self._upsert_capability_started(_stream_timeline_event(payload))
        elif event_type == "capability.call.completed":
            event = _stream_timeline_event(payload)
            if str(event.get("content") or "").startswith("[PENDING_REVIEW]"):
                self._mark_capability_status(str(event.get("invocation_id") or ""), "awaiting_review")
                self.status = "awaiting_review"
            else:
                self._upsert_capability_result(str(event.get("invocation_id") or ""), event, status="completed")
        elif event_type == "capability.call.failed":
            event = _stream_timeline_event(payload)
            self._upsert_capability_result(str(event.get("invocation_id") or ""), event, status="failed")
        elif event_type == "dag.updated":
            dag = data.get("dag")
            if isinstance(dag, dict):
                self.dag = dag
                self._upsert_dag(dag)
        elif event_type == "trace.updated":
            trace = data.get("trace")
            if isinstance(trace, dict):
                self.trace = trace
        elif event_type == "validation.started":
            self.timeline.append({"type": "validating"})
        elif event_type in {"validation.passed", "validation.retry"}:
            self.timeline.append({"type": "validation", "event": _stream_timeline_event(payload)})
        elif event_type == "review.required":
            self.pending_review = data
            self.status = "awaiting_review"
            capability_call = data.get("capability_call")
            if isinstance(capability_call, dict):
                self._mark_capability_status(str(capability_call.get("invocation_id") or ""), "awaiting_review")
        elif event_type == "run.finished":
            result = data.get("result")
            if isinstance(result, dict):
                output_text = str(result.get("output_text") or "")
                if output_text and not self._streamed_content:
                    self._append_text(output_text)
                result_state = result.get("state")
                if isinstance(result_state, dict):
                    status = str(result_state.get("status") or "")
                    if status:
                        self.status = status
                    dag = result_state.get("dag")
                    if isinstance(dag, dict):
                        self.dag = dag
                    trace = result_state.get("trace")
                    if isinstance(trace, dict):
                        self.trace = trace
                    pending_review = result_state.get("pending_review")
                    self.pending_review = pending_review if isinstance(pending_review, dict) else None
        elif event_type == "run.failed":
            message = str(data.get("message") or "Run failed.")
            self.status = "failed"
            self._append_text(message)
        await self.save()

    async def bind_user_message_to_run(self) -> None:
        if not self.user_message_id or not self.run_id or self.user_message_run_id == self.run_id:
            return
        await run_in_threadpool(
            state.get_store().set_conversation_message_run_id,
            self.user_message_id,
            self.run_id,
        )
        self.user_message_run_id = self.run_id

    async def save(self) -> None:
        await run_in_threadpool(
            state.get_store().update_conversation_message,
            self.message_id,
            content=self.content,
            status=_message_status(self.status),
            timeline_json=json.dumps(self.timeline, ensure_ascii=False),
            run_id=self.run_id,
            dag_json=None if self.dag is None else json.dumps(self.dag, ensure_ascii=False),
            trace_json=None if self.trace is None else json.dumps(self.trace, ensure_ascii=False),
            pending_review_json=None if self.pending_review is None else json.dumps(self.pending_review, ensure_ascii=False),
        )

    def _append_reasoning(self, content: str) -> None:
        if not content:
            return
        last = self.timeline[-1] if self.timeline else None
        if isinstance(last, dict) and last.get("type") == "reasoning" and not last.get("closed"):
            last["content"] = f"{last.get('content') or ''}{content}"
        else:
            self.timeline.append({"type": "reasoning", "content": content, "closed": False})

    def _append_text(self, content: str) -> None:
        if not content:
            return
        self._close_reasoning()
        self.content = f"{self.content}{content}"
        self._streamed_content = True
        last = self.timeline[-1] if self.timeline else None
        if isinstance(last, dict) and last.get("type") == "text":
            last["content"] = f"{last.get('content') or ''}{content}"
        else:
            self.timeline.append({"type": "text", "content": content})

    def _close_reasoning(self) -> None:
        for item in reversed(self.timeline):
            if item.get("type") == "reasoning" and not item.get("closed"):
                item["closed"] = True
                return

    def _upsert_capability_started(self, event: dict[str, Any]) -> None:
        invocation_id = str(event.get("invocation_id") or "")
        existing = self._capability_item(invocation_id)
        if existing is not None:
            existing["event"] = event
            existing.setdefault("status", "running")
            return
        self._close_reasoning()
        self.timeline.append({"type": "capability", "status": "running", "event": event})

    def _upsert_capability_result(self, invocation_id: str, result: dict[str, Any], *, status: str) -> None:
        existing = self._capability_item(invocation_id)
        if existing is None:
            existing = {"type": "capability", "event": result}
            self.timeline.append(existing)
        existing["result"] = result
        existing["status"] = status

    def _mark_capability_status(self, invocation_id: str, status: str) -> None:
        existing = self._capability_item(invocation_id)
        if existing is not None:
            existing["status"] = status

    def _capability_item(self, invocation_id: str) -> dict[str, Any] | None:
        if not invocation_id:
            return None
        for item in reversed(self.timeline):
            if item.get("type") != "capability":
                continue
            event = item.get("event")
            if isinstance(event, dict) and event.get("invocation_id") == invocation_id:
                return item
        return None

    def _upsert_dag(self, dag: dict[str, Any]) -> None:
        dag_key = dag.get("task_id") or dag.get("dag_id")
        for index, item in enumerate(self.timeline):
            if item.get("type") != "dag":
                continue
            existing = item.get("dag")
            if isinstance(existing, dict) and (existing.get("task_id") or existing.get("dag_id")) == dag_key:
                self.timeline[index] = {"type": "dag", "dag": dag}
                return
        self.timeline.append({"type": "dag", "dag": dag})


def _visible_chat_message_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text")
                if isinstance(value, str):
                    parts.append(value)
                else:
                    value = item.get("content")
                    if isinstance(value, str):
                        parts.append(value)
        return "\n".join(part for part in parts if part)
    return ""


def _json_array(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _json_object_or_none(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _stream_timeline_event(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    assert isinstance(data, dict)
    return {"type": payload.get("type"), **data}


def _message_status(status: str) -> Literal["created", "running", "awaiting_review", "completed", "failed", "rejected"]:
    if status in {"created", "running", "awaiting_review", "completed", "failed", "rejected"}:
        return status  # type: ignore[return-value]
    return "running"


class ApiState:
    def __init__(self) -> None:
        self.runner: Runner | None = None
        self.dags: dict[str, UserDAG] = {}
        self.dag_artifact_uploads: dict[str, dict[str, list[ArtifactUpload]]] = {}
        self.profile_directory: str | None = None
        self.custom_capabilities: dict[str, CapabilityDefinition] = {}
        self.custom_mcp_servers: dict[str, dict[str, Any]] = {}
        self.custom_mcp_conflicts: dict[str, dict[str, Any]] = {}
        self.custom_mcp_registered_names: set[str] = set()
        self.custom_mcp_errors: dict[str, str] = {}
        self.custom_mcp_conflict_errors: dict[str, str] = {}
        self.agent_preset_errors: dict[str, str] = {}
        self.agent_preset_registered_names: set[str] = set()
        self.custom_model_providers: dict[str, ModelProviderRequest] = {}
        self.active_model_id: str | None = None
        self.custom_python_tools: list[UserPythonToolConfig] = []
        self.custom_python_tool_config_errors: dict[str, str] = {}
        self.custom_python_tool_errors: dict[str, str] = {}
        self.custom_python_tool_capabilities: dict[str, list[str]] = {}
        self.custom_python_tool_capability_ids: set[str] = set()
        self.python_tool_lock = threading.Lock()
        self.onlyoffice_token_secret = secrets.token_bytes(32)
        self.validation_override: bool | None = None
        self.store: Store | None = None
        self.workspaces: LocalWorkspaceStore | None = None

    def get_runner(self) -> Runner:
        if self.runner is None:
            self.sync_user_config()
            self.runner = self._create_runner()
            self._install_custom_capabilities()
            self._install_python_tools()
            self.reload_custom_mcp()
            self._install_agent_presets()
            if self.validation_override is not None:
                self.runner.enable_validation = self.validation_override
        return self.runner

    def get_store(self) -> Store:
        if self.store is None:
            self.store = SQLiteStore(self.get_user_config_path().parent / "api.sqlite3")
        return self.store

    def get_workspaces(self) -> LocalWorkspaceStore:
        if self.workspaces is None:
            self.workspaces = LocalWorkspaceStore(self.get_user_config_path().parent / "projects")
        return self.workspaces

    def _create_runner(self) -> Runner:
        active_model = self.active_model()
        if active_model is None:
            return Runner.from_config(
                workspace=DEFAULT_WORKSPACE,
                skill_roots=self.get_skill_roots(),
            )
        return _runner_from_model_provider(active_model, skill_roots=self.get_skill_roots())

    def active_model(self) -> ModelProviderRequest | None:
        if self.active_model_id is None:
            return None
        model = self.custom_model_providers.get(self.active_model_id)
        if model is None:
            self.active_model_id = None
        return model

    def close_runner(self) -> None:
        if self.runner is not None:
            self.runner.close()
        self.runner = None
        self.custom_mcp_registered_names.clear()
        self.custom_mcp_errors.clear()
        self.custom_mcp_conflict_errors.clear()
        self.custom_python_tool_config_errors.clear()
        self.custom_python_tool_errors.clear()
        self.custom_python_tool_capabilities.clear()
        self.custom_python_tool_capability_ids.clear()
        self.agent_preset_errors.clear()
        self.agent_preset_registered_names.clear()

    def get_profile_directory(self) -> str | None:
        if self.profile_directory is not None:
            return self.profile_directory
        try:
            config_path = resolve_config_path()
            directory = resolve_config_relative_path(load_config(config_path).profiles.directory, config_path)
            return str(directory) if directory is not None else None
        except Exception:
            return None

    def get_skill_roots(self) -> list[Path]:
        return default_skill_roots()

    def get_managed_skill_root(self) -> Path:
        return Path.home() / ".dagent" / "skills"

    def get_managed_profile_root(self) -> Path:
        return Path.home() / ".dagent" / "profiles"

    def get_agent_preset_root(self) -> Path:
        return Path.home() / ".dagent" / "agents"

    def get_managed_python_tool_root(self) -> Path:
        return Path.home() / ".dagent" / "python-tools"

    def get_user_config_path(self) -> Path:
        return default_user_config_path()

    def sync_user_config(self) -> None:
        config, python_tool_config_errors = _load_user_config_for_webui(self.get_user_config_path())
        self.custom_model_providers = {
            model_id: _model_request_from_user_config(model_id, model)
            for model_id, model in config.model_providers.items()
        }
        self.active_model_id = (
            config.active_model
            if config.active_model in self.custom_model_providers
            else None
        )
        configured_mcp_names = _configured_mcp_server_names()
        user_mcp_servers: dict[str, dict[str, Any]] = {}
        user_mcp_conflicts: dict[str, dict[str, Any]] = {}
        conflict_errors: dict[str, str] = {}
        for name, server_config in config.mcp_servers.items():
            server_name = str(name)
            if server_name in configured_mcp_names:
                conflict_errors[server_name] = (
                    f"MCP server '{server_name}' is defined in both project config and user config."
                )
                user_mcp_conflicts[server_name] = dict(server_config)
                continue
            user_mcp_servers[server_name] = dict(server_config)
        self.custom_mcp_servers = user_mcp_servers
        self.custom_mcp_conflicts = user_mcp_conflicts
        self.custom_mcp_errors = {
            name: error
            for name, error in self.custom_mcp_errors.items()
            if name in user_mcp_servers
        }
        self.custom_mcp_conflict_errors = conflict_errors
        self.custom_python_tool_config_errors = python_tool_config_errors
        self.custom_python_tools = list(config.python_tools)

    def persist_user_models(self) -> None:
        config = self._current_user_config()
        config.model_providers = {
            model_id: _user_model_provider_config(model)
            for model_id, model in sorted(self.custom_model_providers.items())
        }
        config.active_model = (
            self.active_model_id
            if self.active_model_id in self.custom_model_providers
            else None
        )
        save_user_config(config, self.get_user_config_path())

    def persist_user_mcp_servers(self) -> None:
        config = self._current_user_config()
        mcp_servers = {
            name: dict(server_config)
            for name, server_config in sorted(self.custom_mcp_conflicts.items())
        }
        mcp_servers.update({
            name: _mcp_storage_config(server_config)
            for name, server_config in sorted(self.custom_mcp_servers.items())
        })
        config.mcp_servers = mcp_servers
        save_user_config(config, self.get_user_config_path())

    def persist_user_python_tools(self) -> None:
        config = self._current_user_config()
        config.python_tools = list(self.custom_python_tools)
        save_user_config(config, self.get_user_config_path())

    def _current_user_config(self) -> UserDagentConfig:
        config, _ = _load_user_config_for_webui(self.get_user_config_path())
        return config

    def skill_store(self) -> SkillStore:
        return SkillStore(self.get_skill_roots(), managed_root=self.get_managed_skill_root())

    def managed_profile_store(self) -> ProfileStore:
        return ProfileStore(self.get_managed_profile_root())

    def agent_preset_store(self) -> AgentPresetStore:
        return AgentPresetStore(self.get_agent_preset_root())

    def _install_custom_capabilities(self) -> None:
        if self.runner is None:
            return
        for definition in self.custom_capabilities.values():
            self.runner.register_capability(definition, _handler_for_definition(definition))

    def _install_python_tools(
        self,
        *,
        replace_existing: bool = False,
        refresh_agent_presets: bool = False,
    ) -> None:
        if self.runner is None:
            return
        result = load_python_tool_sources(
            self.custom_python_tools,
            user_config_dir=self.get_user_config_path().parent,
            managed_root=self.get_managed_python_tool_root(),
        )
        groups = {
            status.config.id: status.bindings
            for status in result.statuses
            if status.error is None and status.config.enabled
        }
        replace_ids = self.custom_python_tool_capability_ids if replace_existing else set()
        registered, install_errors = self.runner.reload_tools(groups, replace_ids=replace_ids)
        source_install_errors = {
            source_id: error
            for source_id, error in install_errors.items()
            if source_id in groups
        }
        self.custom_python_tool_errors = {
            **self.custom_python_tool_config_errors,
            **result.errors,
            **source_install_errors,
        }
        self.custom_python_tool_capabilities = {
            status.config.id: list(status.capability_ids)
            for status in result.statuses
        }
        self.custom_python_tool_capability_ids = {
            definition.id
            for definitions in registered.values()
            for definition in definitions
        }
        for source_id in source_install_errors:
            self.custom_python_tool_capabilities[source_id] = []
        if refresh_agent_presets:
            self._install_agent_presets()

    def reload_custom_mcp(self) -> None:
        if self.runner is None:
            return
        runner = self.runner
        registered_names, errors = runner.reload_mcp_servers(
            self.custom_mcp_servers,
            replace_names=self.custom_mcp_registered_names,
        )
        self.custom_mcp_registered_names = registered_names
        self.custom_mcp_errors = {**errors, **self.custom_mcp_conflict_errors}
        self._install_agent_presets()

    def _install_agent_presets(self) -> None:
        if self.runner is None:
            return
        presets, errors = _agent_presets_with_errors(self.agent_preset_store())
        previous_registered = set(self.agent_preset_registered_names)
        registered_names: set[str] = set()
        self.agent_preset_errors = dict(errors)
        for name in previous_registered:
            self._remove_agent_preset_capability(name)
        for preset in presets:
            try:
                self.runner.add_agent(_tool_agent_from_preset(preset))
                registered_names.add(preset.name)
            except Exception as exc:
                self.agent_preset_errors[preset.name] = str(exc)
        self.agent_preset_registered_names = registered_names

    def _remove_agent_preset_capability(self, name: str) -> None:
        if self.runner is None:
            return
        capability_id = f"agent.{name}"
        if self.runner.get_capability(capability_id) is not None:
            self.runner.remove_capability(capability_id)


state = ApiState()
app = FastAPI(title="dagent API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/projects")
async def create_project(request: ProjectCreateRequest) -> dict[str, Any]:
    project_id = _new_api_id("proj")
    slug = _clean_project_slug(request.slug or request.name)
    name = _clean_required_text(request.name, field="Project name")
    description = None if request.description is None else request.description.strip()
    workspaces = state.get_workspaces()
    workspace_uri = workspaces.project_workspace_uri(project_id)
    await run_in_threadpool(workspaces.local_path_for, workspace_uri)
    try:
        project = await run_in_threadpool(
            state.get_store().create_project,
            project_id=project_id,
            slug=slug,
            name=name,
            description=description,
            workspace_uri=workspace_uri,
        )
    except StorageConflictError as exc:
        raise HTTPException(status_code=400, detail=f"Project slug '{slug}' already exists.") from exc
    return {"project": project.model_dump(mode="json")}


@app.get("/projects")
async def list_projects() -> dict[str, Any]:
    projects = await run_in_threadpool(state.get_store().list_projects)
    return {"projects": [project.model_dump(mode="json") for project in projects]}


@app.get("/projects/{project_id}")
async def get_project(project_id: str) -> dict[str, Any]:
    project = await run_in_threadpool(state.get_store().get_project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return {"project": project.model_dump(mode="json")}


@app.patch("/projects/{project_id}")
async def update_project(project_id: str, request: ProjectUpdateRequest) -> dict[str, Any]:
    store = state.get_store()
    project = await run_in_threadpool(store.get_project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    name = project.name
    if request.name is not None:
        name = _clean_required_text(request.name, field="Project name")
    slug = project.slug
    if request.slug is not None:
        slug = _clean_project_slug(request.slug)
    description = project.description
    if "description" in request.model_fields_set:
        description = None if request.description is None else request.description.strip()
    try:
        updated = await run_in_threadpool(
            store.update_project,
            project_id,
            slug=slug,
            name=name,
            description=description,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    except StorageConflictError as exc:
        raise HTTPException(status_code=400, detail=f"Project slug '{slug}' already exists.") from exc
    return {"project": updated.model_dump(mode="json")}


@app.delete("/projects/{project_id}")
async def delete_project(project_id: str) -> dict[str, str]:
    store = state.get_store()
    project = await run_in_threadpool(store.get_project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    conversations = await run_in_threadpool(store.list_conversations, project.id)
    locks = []
    owner = _new_api_id("delete")
    try:
        for conversation in conversations:
            try:
                locks.append(await run_in_threadpool(
                    store.acquire_conversation_lock,
                    conversation.id,
                    owner=owner,
                ))
            except ConversationBusyError as exc:
                raise HTTPException(status_code=409, detail="Project has active conversations.") from exc
        runs = await run_in_threadpool(store.list_runs, project_id=project.id)
        run_states: dict[str, RunState | None] = {}
        for run in runs:
            run_states[run.id] = await run_in_threadpool(store.get_run_state, run.id)
        saved_dags = await run_in_threadpool(store.list_saved_dags, project.id)
        workspace_path = await run_in_threadpool(state.get_workspaces().local_path_for, project.workspace_uri)
        for run in runs:
            await run_in_threadpool(_delete_run_files, run, run_states[run.id])
        for saved_dag in saved_dags:
            await run_in_threadpool(shutil.rmtree, _saved_dag_artifact_root(saved_dag.id), ignore_errors=True)
        await run_in_threadpool(_delete_project_workspace, project, workspace_path)
        deleted = await run_in_threadpool(store.delete_project, project.id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Project not found.")
    finally:
        for lock in locks:
            await run_in_threadpool(lock.release)
    return {"status": "deleted"}


@app.get("/projects/{project_id}/files")
async def list_project_files(project_id: str, path: str = "", tree: bool = False) -> dict[str, Any]:
    project, workspace = await _project_workspace(project_id)
    directory = _resolve_project_file_path(workspace, path, allow_empty=True)
    if not directory.exists():
        raise HTTPException(status_code=404, detail="Project directory not found.")
    if not directory.is_dir():
        raise HTTPException(status_code=400, detail="Project file path is not a directory.")
    normalized_path = _normalize_project_file_path(path, allow_empty=True)
    onlyoffice_config = _configured_onlyoffice_config()
    files = await run_in_threadpool(
        _project_file_items,
        project.id,
        workspace,
        directory,
        onlyoffice_config,
    )
    if tree:
        tree_items = await run_in_threadpool(
            _project_file_tree_items,
            project.id,
            workspace,
            directory,
            onlyoffice_config,
        )
        return ProjectFileTreeResponse(
            project_id=project.id,
            path=normalized_path,
            files=files,
            tree=tree_items,
        ).model_dump(mode="json")
    return ProjectFilesResponse(project_id=project.id, path=normalized_path, files=files).model_dump(mode="json")


@app.post("/projects/{project_id}/files/upload")
async def upload_project_files(
    project_id: str,
    path: str = Form(""),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    project, workspace = await _project_workspace(project_id)
    directory = _resolve_project_file_path(workspace, path, allow_empty=True)
    if directory.exists() and not directory.is_dir():
        raise HTTPException(status_code=400, detail="Upload path is not a directory.")
    await run_in_threadpool(directory.mkdir, parents=True, exist_ok=True)
    uploaded: list[ProjectFileItem] = []
    for file in files:
        filename = str(file.filename or "upload").replace("\\", "/")
        try:
            validate_upload_filename(filename)
        except ArtifactPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        target = _resolve_project_file_path(
            workspace,
            "/".join(part for part in (path, filename) if part),
            allow_empty=False,
        )
        content = await file.read()
        await run_in_threadpool(target.parent.mkdir, parents=True, exist_ok=True)
        await run_in_threadpool(target.write_bytes, content)
        uploaded.append(_project_file_item(
            project.id,
            workspace,
            target,
            onlyoffice_config=_configured_onlyoffice_config(),
        ))
    return {"files": [item.model_dump(mode="json") for item in uploaded]}


@app.post("/projects/{project_id}/files/folder")
async def create_project_folder(project_id: str, request: ProjectFolderRequest) -> dict[str, Any]:
    project, workspace = await _project_workspace(project_id)
    folder = _resolve_project_file_path(workspace, request.path)
    if folder.exists() and not folder.is_dir():
        raise HTTPException(status_code=400, detail="Project path exists and is not a directory.")
    await run_in_threadpool(folder.mkdir, parents=True, exist_ok=True)
    return {"file": _project_file_item(
        project.id,
        workspace,
        folder,
        onlyoffice_config=_configured_onlyoffice_config(),
    ).model_dump(mode="json")}


@app.patch("/projects/{project_id}/files")
async def move_project_file(project_id: str, request: ProjectFileMoveRequest) -> dict[str, Any]:
    project, workspace = await _project_workspace(project_id)
    source = _resolve_project_file_path(workspace, request.path)
    target = _resolve_project_file_path(workspace, request.new_path)
    if not source.exists():
        raise HTTPException(status_code=404, detail="Project file not found.")
    if target.exists():
        raise HTTPException(status_code=409, detail="Project destination already exists.")
    if source.is_dir() and _path_contains(source.resolve(), target.resolve()):
        raise HTTPException(status_code=400, detail="Project directory cannot be moved into its own descendant.")
    try:
        await run_in_threadpool(target.parent.mkdir, parents=True, exist_ok=True)
        await run_in_threadpool(source.rename, target)
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"Project file move failed: {exc}") from exc
    return {"file": _project_file_item(
        project.id,
        workspace,
        target,
        onlyoffice_config=_configured_onlyoffice_config(),
    ).model_dump(mode="json")}


@app.delete("/projects/{project_id}/files")
async def delete_project_file(project_id: str, request: ProjectFileDeleteRequest) -> dict[str, str]:
    _project, workspace = await _project_workspace(project_id)
    target = _resolve_project_file_path(workspace, request.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Project file not found.")
    if target.is_dir() and not target.is_symlink():
        await run_in_threadpool(shutil.rmtree, target)
    else:
        await run_in_threadpool(target.unlink)
    return {"status": "deleted"}


@app.get("/projects/{project_id}/files/preview")
async def preview_project_file(project_id: str, path: str) -> dict[str, Any]:
    project, workspace = await _project_workspace(project_id)
    file_path = _resolve_project_file_path(workspace, path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Project file not found.")
    normalized_path = _normalize_project_file_path(path)
    preview_kind = _preview_kind_for_path(normalized_path)
    if preview_kind is None:
        raise HTTPException(status_code=415, detail="Project file type is not previewable.")
    if preview_kind not in _TEXT_PREVIEW_KINDS:
        raise HTTPException(status_code=415, detail="Project file type uses binary browser preview.")
    content, truncated, size = _read_text_preview(file_path)
    return ProjectFilePreviewResponse(
        project_id=project.id,
        path=normalized_path,
        name=file_path.name,
        media_type=_media_type_for_path(normalized_path),
        preview_kind=preview_kind,
        content=content,
        size=size,
        truncated=truncated,
    ).model_dump(mode="json")


@app.get("/projects/{project_id}/files/onlyoffice/config")
async def get_project_file_onlyoffice_config(project_id: str, path: str) -> dict[str, Any]:
    project, workspace = await _project_workspace(project_id)
    return _project_file_onlyoffice_config_response(project, workspace, path).model_dump(mode="json")


@app.get("/projects/{project_id}/files/download")
async def download_project_file(project_id: str, path: str) -> FileResponse:
    _project, workspace = await _project_workspace(project_id)
    file_path = _resolve_project_file_path(workspace, path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Project file not found.")
    normalized_path = _normalize_project_file_path(path)
    return FileResponse(
        file_path,
        filename=file_path.name,
        media_type=_media_type_for_path(normalized_path),
    )


@app.post("/conversations")
async def create_conversation(request: ConversationCreateRequest) -> dict[str, Any]:
    conversation_id = _new_api_id("conv")
    title = _clean_required_text(request.title, field="Conversation title")
    workspaces = state.get_workspaces()
    workspace_uri = workspaces.conversation_workspace_uri(conversation_id)
    await run_in_threadpool(workspaces.local_path_for, workspace_uri)
    try:
        conversation = await run_in_threadpool(
            state.get_store().create_conversation,
            conversation_id=conversation_id,
            project_id=None,
            title=title,
            workspace_uri=workspace_uri,
            kind=request.kind,
        )
    except StorageConflictError as exc:
        raise HTTPException(status_code=400, detail="Conversation already exists.") from exc
    return {"conversation": conversation.model_dump(mode="json")}


@app.get("/conversations")
async def list_conversations(kind: Literal["chat", "dynamic_dag", "static_dag"] | None = None) -> dict[str, Any]:
    conversations = await run_in_threadpool(
        state.get_store().list_conversations,
        standalone=True,
        kind=kind,
    )
    return {"conversations": [conversation.model_dump(mode="json") for conversation in conversations]}


async def _update_conversation_title(conversation: Conversation, title: str) -> Conversation:
    clean_title = _clean_required_text(title, field="Conversation title")
    try:
        return await run_in_threadpool(
            state.get_store().update_conversation,
            conversation.id,
            title=clean_title,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found.") from exc


@app.patch("/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, request: ConversationUpdateRequest) -> dict[str, Any]:
    conversation = await run_in_threadpool(state.get_store().get_conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if conversation.project_id is not None:
        raise HTTPException(status_code=400, detail="Project conversations must be updated through the project route.")
    updated = await _update_conversation_title(conversation, request.title)
    return {"conversation": updated.model_dump(mode="json")}


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str) -> dict[str, str]:
    store = state.get_store()
    conversation = await run_in_threadpool(store.get_conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if conversation.project_id is not None:
        raise HTTPException(status_code=400, detail="Project conversations must be deleted through the project route.")
    await _delete_conversation(conversation)
    return {"status": "deleted"}


@app.post("/projects/{project_id}/conversations")
async def create_project_conversation(
    project_id: str,
    request: ConversationCreateRequest,
) -> dict[str, Any]:
    project = await run_in_threadpool(state.get_store().get_project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    conversation_id = _new_api_id("conv")
    title = _clean_required_text(request.title, field="Conversation title")
    workspaces = state.get_workspaces()
    workspace_uri = project.workspace_uri
    await run_in_threadpool(workspaces.local_path_for, workspace_uri)
    try:
        conversation = await run_in_threadpool(
            state.get_store().create_conversation,
            conversation_id=conversation_id,
            project_id=project.id,
            title=title,
            workspace_uri=workspace_uri,
            kind=request.kind,
        )
    except StorageConflictError as exc:
        raise HTTPException(status_code=400, detail="Conversation already exists.") from exc
    return {"conversation": conversation.model_dump(mode="json")}


@app.get("/projects/{project_id}/conversations")
async def list_project_conversations(
    project_id: str,
    kind: Literal["chat", "dynamic_dag", "static_dag"] | None = None,
) -> dict[str, Any]:
    project = await run_in_threadpool(state.get_store().get_project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    conversations = await run_in_threadpool(
        state.get_store().list_conversations,
        project_id,
        kind=kind,
    )
    return {"conversations": [conversation.model_dump(mode="json") for conversation in conversations]}


@app.get("/projects/{project_id}/conversations/{conversation_id}")
async def get_project_conversation(project_id: str, conversation_id: str) -> dict[str, Any]:
    project = await run_in_threadpool(state.get_store().get_project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    conversation = await run_in_threadpool(state.get_store().get_conversation, conversation_id)
    if conversation is None or conversation.project_id != project.id:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"conversation": conversation.model_dump(mode="json")}


@app.patch("/projects/{project_id}/conversations/{conversation_id}")
async def update_project_conversation(
    project_id: str,
    conversation_id: str,
    request: ConversationUpdateRequest,
) -> dict[str, Any]:
    project = await run_in_threadpool(state.get_store().get_project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    conversation = await run_in_threadpool(state.get_store().get_conversation, conversation_id)
    if conversation is None or conversation.project_id != project.id:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    updated = await _update_conversation_title(conversation, request.title)
    return {"conversation": updated.model_dump(mode="json")}


@app.delete("/projects/{project_id}/conversations/{conversation_id}")
async def delete_project_conversation(project_id: str, conversation_id: str) -> dict[str, str]:
    store = state.get_store()
    project = await run_in_threadpool(store.get_project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    conversation = await run_in_threadpool(store.get_conversation, conversation_id)
    if conversation is None or conversation.project_id != project.id:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    await _delete_conversation(conversation)
    return {"status": "deleted"}


async def _delete_conversation(conversation: Conversation) -> None:
    store = state.get_store()
    runs = await run_in_threadpool(
        store.list_runs,
        conversation_id=conversation.id,
    )
    run_states: list[RunState] = []
    for run in runs:
        run_state = await run_in_threadpool(store.get_run_state, run.id)
        if run_state is not None:
            run_states.append(run_state)
    conversation_workspace = await run_in_threadpool(
        state.get_workspaces().local_path_for,
        conversation.workspace_uri,
    )
    delete_conversation_workspace = True
    if conversation.project_id is not None:
        project = await run_in_threadpool(store.get_project, conversation.project_id)
        if project is not None:
            project_workspace = await run_in_threadpool(
                state.get_workspaces().local_path_for,
                project.workspace_uri,
            )
            delete_conversation_workspace = conversation_workspace.resolve() != project_workspace.resolve()
    await run_in_threadpool(
        _delete_conversation_files,
        run_states,
        conversation_workspace,
        delete_conversation_workspace=delete_conversation_workspace,
    )
    for run in runs:
        await run_in_threadpool(store.delete_run, run.id)
    deleted = await run_in_threadpool(store.delete_conversation, conversation.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found.")


async def _conversation_run_summaries(
    conversation: Conversation,
    *,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    runs = await run_in_threadpool(
        state.get_store().list_runs,
        project_id=project_id,
        conversation_id=conversation.id,
    )
    return [_run_summary_payload(run) for run in runs]


@app.get("/conversations/{conversation_id}/runs")
async def list_conversation_runs(conversation_id: str) -> dict[str, Any]:
    conversation = await run_in_threadpool(state.get_store().get_conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if conversation.project_id is not None:
        raise HTTPException(
            status_code=400,
            detail="Project conversation runs must be listed through the project route.",
        )
    return {"runs": await _conversation_run_summaries(conversation)}


@app.get("/projects/{project_id}/conversations/{conversation_id}/runs")
async def list_project_conversation_runs(project_id: str, conversation_id: str) -> dict[str, Any]:
    project = await run_in_threadpool(state.get_store().get_project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    conversation = await run_in_threadpool(state.get_store().get_conversation, conversation_id)
    if conversation is None or conversation.project_id != project.id:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"runs": await _conversation_run_summaries(conversation, project_id=project.id)}


@app.get("/conversations/{conversation_id}/messages")
async def list_conversation_messages(conversation_id: str) -> dict[str, Any]:
    conversation = await run_in_threadpool(state.get_store().get_conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if conversation.project_id is not None:
        raise HTTPException(
            status_code=400,
            detail="Project conversation messages must be listed through the project route.",
        )
    messages = await run_in_threadpool(state.get_store().list_conversation_messages, conversation.id)
    return {"messages": [_conversation_message_payload(message) for message in messages]}


@app.get("/projects/{project_id}/conversations/{conversation_id}/messages")
async def list_project_conversation_messages(project_id: str, conversation_id: str) -> dict[str, Any]:
    project = await run_in_threadpool(state.get_store().get_project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    conversation = await run_in_threadpool(state.get_store().get_conversation, conversation_id)
    if conversation is None or conversation.project_id != project.id:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    messages = await run_in_threadpool(state.get_store().list_conversation_messages, conversation.id)
    return {"messages": [_conversation_message_payload(message) for message in messages]}


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    run = await run_in_threadpool(state.get_store().get_run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return {"run": _run_summary_payload(run)}


@app.delete("/runs/{run_id}")
async def delete_run(run_id: str) -> dict[str, str]:
    store = state.get_store()
    run = await run_in_threadpool(store.get_run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    if run.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="Active runs cannot be deleted.")
    run_state = await run_in_threadpool(store.get_run_state, run.id)
    await run_in_threadpool(_delete_run_files, run, run_state)
    deleted = await run_in_threadpool(store.delete_run, run.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Run not found.")
    return {"status": "deleted"}


@app.get("/runs/{run_id}/events")
async def get_run_events(run_id: str, after_event_id: int = 0) -> dict[str, Any]:
    run = await run_in_threadpool(state.get_store().get_run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    events = await run_in_threadpool(
        state.get_store().list_run_events,
        run_id,
        after_event_id=after_event_id,
    )
    return {"events": [_run_event_payload(event) for event in events]}


@app.get("/settings/validation")
async def get_validation_status() -> dict[str, bool]:
    return {"enabled": state.get_runner().enable_validation}


@app.post("/settings/validation")
async def toggle_validation(payload: dict[str, bool]) -> dict[str, bool]:
    runner = state.get_runner()
    state.validation_override = payload.get("enabled", False)
    runner.enable_validation = state.validation_override
    return {"enabled": runner.enable_validation}


@app.post("/session/reset")
async def reset_session() -> dict[str, str]:
    state.close_runner()
    state.dags.clear()
    state.dag_artifact_uploads.clear()
    state.custom_capabilities.clear()
    state.custom_mcp_servers.clear()
    state.custom_mcp_conflicts.clear()
    state.custom_mcp_conflict_errors.clear()
    state.custom_python_tools.clear()
    state.custom_python_tool_config_errors.clear()
    state.custom_python_tool_errors.clear()
    state.custom_python_tool_capabilities.clear()
    state.custom_python_tool_capability_ids.clear()
    return {"status": "ok"}


@app.get("/dags")
async def list_dags() -> dict[str, Any]:
    return {
        "dags": [
            dag.model_dump(mode="json")
            for dag in sorted(state.dags.values(), key=lambda item: item.id)
        ]
    }


@app.post("/dags")
async def create_dag(dag: UserDAG) -> dict[str, Any]:
    try:
        validate_dag_spec(_compile_user_dag(dag).to_dag_spec())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state.dags[dag.id] = dag.model_copy(deep=True)
    _prune_dag_artifact_uploads(dag)
    return {"dag": dag.model_dump(mode="json")}


@app.post("/dags/validate")
async def validate_user_dag(dag: UserDAG) -> dict[str, Any]:
    try:
        validate_dag_spec(_compile_user_dag(dag).to_dag_spec())
    except Exception as exc:
        return DAGValidationResponse(
            valid=False,
            issues=[DAGValidationIssue(message=str(exc))],
        ).model_dump(mode="json")
    return DAGValidationResponse(valid=True).model_dump(mode="json")


@app.post("/saved-dags")
async def create_saved_dag(request: SavedDAGCreateRequest) -> dict[str, Any]:
    if request.project_id is not None:
        project = await run_in_threadpool(state.get_store().get_project, request.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
    _validate_user_dag_or_raise(request.spec)
    name = _clean_required_text(request.name or request.spec.name, field="DAG name")
    description = request.description.strip()
    try:
        saved = await run_in_threadpool(
            state.get_store().create_saved_dag,
            dag_id=_new_api_id("dag"),
            project_id=request.project_id,
            name=name,
            description=description,
            spec_json=_user_dag_json(request.spec),
            layout_json=_json_object(request.layout),
        )
    except StorageConflictError as exc:
        raise HTTPException(status_code=400, detail="Saved DAG already exists.") from exc
    return {"saved_dag": _saved_dag_payload(saved)}


@app.get("/saved-dags")
async def list_saved_dags(project_id: str | None = None) -> dict[str, Any]:
    if project_id is not None:
        project = await run_in_threadpool(state.get_store().get_project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
    saved_dags = await run_in_threadpool(state.get_store().list_saved_dags, project_id)
    return {"saved_dags": [_saved_dag_payload(saved) for saved in saved_dags]}


@app.get("/saved-dags/{dag_id}")
async def get_saved_dag(dag_id: str) -> dict[str, Any]:
    saved = await run_in_threadpool(state.get_store().get_saved_dag, dag_id)
    if saved is None:
        raise HTTPException(status_code=404, detail="Saved DAG not found.")
    return {"saved_dag": _saved_dag_payload(saved)}


@app.get("/saved-dags/{dag_id}/runs")
async def list_saved_dag_runs(dag_id: str) -> dict[str, Any]:
    saved = await run_in_threadpool(state.get_store().get_saved_dag, dag_id)
    if saved is None:
        raise HTTPException(status_code=404, detail="Saved DAG not found.")
    runs = await run_in_threadpool(
        state.get_store().list_runs,
        saved_dag_id=saved.id,
    )
    return {"runs": [_run_summary_payload(run) for run in runs]}


@app.patch("/saved-dags/{dag_id}")
async def update_saved_dag(dag_id: str, request: SavedDAGUpdateRequest) -> dict[str, Any]:
    existing = await run_in_threadpool(state.get_store().get_saved_dag, dag_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Saved DAG not found.")
    spec = _user_dag_from_saved(existing) if request.spec is None else request.spec
    _validate_user_dag_or_raise(spec)
    name = existing.name if request.name is None else _clean_required_text(request.name, field="DAG name")
    description = existing.description if request.description is None else request.description.strip()
    layout_json = existing.layout_json if request.layout is None else _json_object(request.layout)
    try:
        saved = await run_in_threadpool(
            state.get_store().update_saved_dag,
            dag_id,
            name=name,
            description=description,
            spec_json=_user_dag_json(spec),
            layout_json=layout_json,
            expected_revision=request.expected_revision,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Saved DAG not found.") from exc
    except StorageConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"saved_dag": _saved_dag_payload(saved)}


@app.delete("/saved-dags/{dag_id}")
async def delete_saved_dag(dag_id: str) -> dict[str, str]:
    deleted = await run_in_threadpool(state.get_store().archive_saved_dag, dag_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Saved DAG not found.")
    await run_in_threadpool(shutil.rmtree, _saved_dag_artifact_root(dag_id), ignore_errors=True)
    return {"status": "deleted"}


@app.post("/saved-dags/{dag_id}/run/stream")
async def run_saved_dag_stream(dag_id: str, request: SavedDAGRunRequest) -> StreamingResponse:
    saved = await run_in_threadpool(state.get_store().get_saved_dag, dag_id)
    if saved is None:
        raise HTTPException(status_code=404, detail="Saved DAG not found.")
    context = await _persisted_context_from_conversation(
        request.project_id,
        request.conversation_id,
        expected_kind="static_dag",
    )
    if saved.project_id != context.project_id:
        raise HTTPException(status_code=404, detail="Saved DAG not found.")
    dag = _user_dag_from_saved(saved)
    stream_id = _new_api_id("stream")
    try:
        lock = await run_in_threadpool(
            state.get_store().acquire_conversation_lock,
            context.conversation_id,
            owner=stream_id,
        )
    except ConversationBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StreamingResponse(
        _persisted_static_dag_stream_events(
            dag,
            request.graph_input,
            saved,
            context,
            stream_id,
            lock,
        ),
        media_type="text/event-stream",
    )


@app.post("/orchestration-sessions")
async def create_orchestration_session(request: OrchestrationSessionCreateRequest) -> dict[str, Any]:
    context = await _persisted_context_from_conversation(
        request.project_id,
        request.conversation_id,
        include_orchestration_session=False,
    )
    if context.conversation_kind != request.kind:
        raise HTTPException(status_code=400, detail="Conversation kind does not match orchestration session kind.")
    if request.saved_dag_id is not None:
        saved = await run_in_threadpool(state.get_store().get_saved_dag, request.saved_dag_id)
        if saved is None or saved.project_id != context.project_id:
            raise HTTPException(status_code=404, detail="Saved DAG not found.")
    try:
        session = await run_in_threadpool(
            state.get_store().create_orchestration_session,
            session_id=_new_api_id("orch"),
            conversation_id=context.conversation_id,
            project_id=context.project_id,
            kind=request.kind,
            saved_dag_id=request.saved_dag_id,
            draft_dag_json=None if request.draft_dag is None else _json_object(request.draft_dag),
            ui_state_json=_json_object(request.ui_state),
        )
    except StorageConflictError as exc:
        raise HTTPException(status_code=409, detail="Orchestration session already exists for conversation.") from exc
    return {"session": _orchestration_session_payload(session)}


@app.get("/orchestration-sessions/{session_id}")
async def get_orchestration_session(session_id: str) -> dict[str, Any]:
    session = await run_in_threadpool(state.get_store().get_orchestration_session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Orchestration session not found.")
    return {"session": _orchestration_session_payload(session)}


@app.get("/orchestration-sessions/{session_id}/runs")
async def list_orchestration_session_runs(session_id: str) -> dict[str, Any]:
    session = await run_in_threadpool(state.get_store().get_orchestration_session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Orchestration session not found.")
    runs = await run_in_threadpool(
        state.get_store().list_runs,
        conversation_id=session.conversation_id,
    )
    return {"runs": [_run_summary_payload(run) for run in runs]}


@app.get("/conversations/{conversation_id}/orchestration-session")
async def get_orchestration_session_by_conversation(conversation_id: str) -> dict[str, Any]:
    session = await run_in_threadpool(
        state.get_store().get_orchestration_session_by_conversation,
        conversation_id,
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Orchestration session not found.")
    return {"session": _orchestration_session_payload(session)}


@app.patch("/orchestration-sessions/{session_id}")
async def update_orchestration_session(
    session_id: str,
    request: OrchestrationSessionUpdateRequest,
) -> dict[str, Any]:
    existing = await run_in_threadpool(state.get_store().get_orchestration_session, session_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Orchestration session not found.")
    update_saved_dag_id = "saved_dag_id" in request.model_fields_set
    update_draft_dag = "draft_dag" in request.model_fields_set
    update_ui_state = "ui_state" in request.model_fields_set
    if update_saved_dag_id and request.saved_dag_id is not None:
        saved = await run_in_threadpool(state.get_store().get_saved_dag, request.saved_dag_id)
        if saved is None or saved.project_id != existing.project_id:
            raise HTTPException(status_code=404, detail="Saved DAG not found.")
    try:
        session = await run_in_threadpool(
            state.get_store().update_orchestration_session,
            session_id,
            saved_dag_id=request.saved_dag_id,
            draft_dag_json=None if request.draft_dag is None else _json_object(request.draft_dag),
            ui_state_json=_json_object(request.ui_state),
            update_saved_dag_id=update_saved_dag_id,
            update_draft_dag=update_draft_dag,
            update_ui_state=update_ui_state,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Orchestration session not found.") from exc
    return {"session": _orchestration_session_payload(session)}


@app.get("/dags/{dag_id}")
async def get_dag(dag_id: str) -> dict[str, Any]:
    dag = state.dags.get(dag_id)
    if dag is None:
        raise HTTPException(status_code=404, detail="DAG not found.")
    return {"dag": dag.model_dump(mode="json")}


@app.post("/dags/{dag_id}/artifacts/{artifact_id}/upload")
async def upload_dag_artifact(
    dag_id: str,
    artifact_id: str,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    dag = state.dags.get(dag_id)
    if dag is None:
        raise HTTPException(status_code=404, detail="DAG not found.")
    return await _upload_dag_artifact_files(dag_id, dag, artifact_id, files)


@app.post("/saved-dags/{dag_id}/artifacts/{artifact_id}/upload")
async def upload_saved_dag_artifact(
    dag_id: str,
    artifact_id: str,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    saved = await run_in_threadpool(state.get_store().get_saved_dag, dag_id)
    if saved is None:
        raise HTTPException(status_code=404, detail="Saved DAG not found.")
    dag = _user_dag_from_saved(saved)
    uploads = await _validated_artifact_uploads(dag, artifact_id, files)
    root = _saved_dag_artifact_root(saved.id)
    await run_in_threadpool(_replace_saved_dag_artifact_uploads, root, dag, artifact_id, uploads)
    return {
        "artifact_id": artifact_id,
        "files": [upload.filename for upload in uploads],
    }


async def _upload_dag_artifact_files(
    dag_id: str,
    dag: UserDAG,
    artifact_id: str,
    files: list[UploadFile],
) -> dict[str, Any]:
    uploads = await _validated_artifact_uploads(dag, artifact_id, files)
    state.dag_artifact_uploads.setdefault(dag_id, {})[artifact_id] = uploads
    return {
        "artifact_id": artifact_id,
        "files": [upload.filename for upload in uploads],
    }


async def _validated_artifact_uploads(
    dag: UserDAG,
    artifact_id: str,
    files: list[UploadFile],
) -> list[ArtifactUpload]:
    if artifact_id not in dag.artifacts:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required.")

    uploads: list[ArtifactUpload] = []
    for file in files:
        filename = file.filename or "upload"
        try:
            validate_upload_filename(filename)
        except ArtifactPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        content = await file.read()
        uploads.append(
            ArtifactUpload(
                filename=filename,
                content=content,
            )
        )
    return uploads


@app.post("/dags/{dag_id}/run")
async def run_dag(dag_id: str, request: DAGRunRequest | None = None) -> dict[str, Any]:
    dag = state.dags.get(dag_id)
    if dag is None:
        raise HTTPException(status_code=404, detail="DAG not found.")

    try:
        result = await state.get_runner().run(
            _compile_user_dag(dag),
            graph_input=None if request is None else request.graph_input,
            workspace_root=_workspace_root_from_request(request),
            artifact_uploads=_artifact_uploads_for_dag(dag_id),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"result": result.model_dump(mode="json")}


def _workspace_root_from_request(request: DAGRunRequest | None) -> str:
    if request is None or request.workspace_root is None or not request.workspace_root.strip():
        return DEFAULT_RUNS_DIR
    return _clean_workspace_root(request.workspace_root)


def _compile_user_dag(dag: UserDAG) -> Dag:
    builder = Dag(
        dag.id,
        name=dag.name,
        description=dag.description,
        version=dag.version,
        input_schema=dict(dag.input_schema),
        metadata=dict(dag.metadata),
    )
    for artifact in dag.artifacts.values():
        builder.add_artifact(artifact)
    for node in dag.nodes:
        builder.add_node(Node(
            node.id,
            target=_compile_user_dag_target(node),
            inputs=dict(node.inputs),
            artifact_inputs=list(node.artifact_inputs),
            artifact_outputs=list(node.artifact_outputs),
            title=node.title or None,
            boundary=node.boundary,
        ))
    for edge in dag.edges:
        builder.add_edge(edge.source, edge.target, reason=edge.reason)
    return builder


def _compile_user_dag_target(node: UserDAGNode) -> str | ToolAgent:
    capability_id = node.target.strip()
    if not capability_id.startswith("agent."):
        if node.agent is not None:
            raise ValueError(f"Node '{node.id}' has agent config but does not target an agent capability.")
        return capability_id
    definition = state.get_runner().get_capability(capability_id)
    if definition is not None:
        if definition.kind != "agent":
            raise ValueError(f"Capability '{capability_id}' is not an agent capability.")
        if node.agent is not None:
            raise ValueError(
                f"Node '{node.id}' targets registered agent capability '{capability_id}' "
                "and cannot include node-level agent config."
            )
        return capability_id
    profile_name = _clean_agent_profile_name(capability_id.removeprefix("agent."))
    agent_config = node.agent
    capabilities = (
        None if agent_config is None or agent_config.capabilities is None
        else tuple(_validated_agent_node_capabilities(agent_config.capabilities))
    )
    skills = (
        None if agent_config is None or agent_config.skills is None
        else tuple(_validated_agent_node_skills(agent_config.skills))
    )
    return ToolAgent(
        profile=_resolve_agent_profile(profile_name),
        name=profile_name,
        capabilities=capabilities,
        skills=skills,
        review="fast",
    )


def _validated_agent_node_capabilities(capability_ids: list[str]) -> list[str]:
    runner = state.get_runner()
    validated: list[str] = []
    for capability_id in _dedupe(capability_ids):
        definition = runner.get_capability(capability_id)
        if definition is None:
            raise ValueError(f"Capability '{capability_id}' was not found.")
        if not definition.enabled:
            raise ValueError(f"Capability '{capability_id}' is disabled.")
        if definition.kind in {"agent", "skill"}:
            raise ValueError(
                f"Capability '{capability_id}' cannot be used in an agent node capability scope."
            )
        validated.append(capability_id)
    return validated


def _validated_agent_node_skills(skills: list[str]) -> list[str]:
    store = state.skill_store()
    validated: list[str] = []
    for skill in _dedupe(skills):
        try:
            store.view(skill)
        except SkillStoreError as exc:
            raise ValueError(str(exc)) from exc
        validated.append(skill)
    return validated


def _clean_agent_profile_name(value: str) -> str:
    try:
        return _clean_managed_profile_name(value)
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc


def _resolve_agent_profile(name: str) -> AgentProfile:
    managed_store = state.managed_profile_store()
    if name in managed_store.list_names():
        return managed_store.load(name)
    config_directory = state.get_profile_directory()
    if config_directory is not None:
        config_store = ProfileStore(config_directory)
        if name in config_store.list_names():
            return config_store.load(name)
    try:
        return load_builtin_profile(name)
    except FileNotFoundError as exc:
        raise ValueError(f"Agent profile '{name}' was not found.") from exc


def _artifact_uploads_for_dag(dag_id: str) -> dict[str, list[ArtifactUpload]]:
    return {
        artifact_id: list(uploads)
        for artifact_id, uploads in state.dag_artifact_uploads.get(dag_id, {}).items()
    }


def _saved_dag_artifact_root(dag_id: str) -> Path:
    return state.get_user_config_path().parent / "saved-dag-artifacts" / dag_id


def _replace_saved_dag_artifact_uploads(
    root: Path,
    dag: UserDAG,
    artifact_id: str,
    uploads: list[ArtifactUpload],
) -> None:
    artifact = dag.artifacts[artifact_id]
    staging_root = root.parent / f".{root.name}.upload-{uuid4().hex}"
    try:
        target_paths = resolve_artifact_paths(artifact, root)
        staging_root.mkdir(parents=True, exist_ok=False)
        materialize_artifact_uploads(
            {artifact_id: uploads},
            artifacts=dag.artifacts,
            workspace_path=staging_root,
        )
        staged_paths = resolve_artifact_paths(artifact, staging_root)
        for target in target_paths:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        for staged_path in staged_paths:
            if not staged_path.exists():
                continue
            target = root / staged_path.relative_to(staging_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged_path), str(target))
    except ArtifactPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _saved_dag_artifact_uploads(saved: SavedDag, dag: UserDAG) -> dict[str, list[ArtifactUpload]]:
    root = _saved_dag_artifact_root(saved.id)
    uploads: dict[str, list[ArtifactUpload]] = {}
    for artifact_id, artifact in dag.artifacts.items():
        try:
            target_paths = resolve_artifact_paths(artifact, root)
        except ArtifactPathError:
            continue
        artifact_uploads: list[ArtifactUpload] = []
        for target in target_paths:
            if target.is_file():
                artifact_uploads.append(ArtifactUpload(filename=target.name, content=target.read_bytes()))
            elif target.is_dir():
                for file_path in sorted(path for path in target.rglob("*") if path.is_file()):
                    artifact_uploads.append(
                        ArtifactUpload(
                            filename=str(file_path.relative_to(target)),
                            content=file_path.read_bytes(),
                        )
                    )
        if artifact_uploads:
            uploads[artifact_id] = artifact_uploads
    return uploads


def _validate_user_dag_or_raise(dag: UserDAG) -> None:
    try:
        validate_dag_spec(_compile_user_dag(dag).to_dag_spec())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _user_dag_json(dag: UserDAG) -> str:
    return dag.model_dump_json()


def _json_object(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def _user_dag_from_saved(saved: SavedDag) -> UserDAG:
    try:
        return UserDAG.model_validate_json(saved.spec_json)
    except ValidationError as exc:
        raise HTTPException(status_code=500, detail="Stored DAG spec is invalid.") from exc


def _empty_user_dag_payload(saved: SavedDag) -> dict[str, Any]:
    return UserDAG(id=saved.id, name=saved.name or "Untitled DAG").model_dump(mode="json")


def _saved_dag_payload(saved: SavedDag) -> dict[str, Any]:
    payload = saved.model_dump(mode="json")
    payload.pop("spec_json")
    payload.pop("layout_json")
    payload["spec"] = _json_from_storage(saved.spec_json, fallback=_empty_user_dag_payload(saved))
    payload["layout"] = _json_from_storage(saved.layout_json, fallback={})
    return payload


def _orchestration_session_payload(session: OrchestrationSession) -> dict[str, Any]:
    payload = session.model_dump(mode="json")
    payload.pop("draft_dag_json")
    payload.pop("ui_state_json")
    payload["draft_dag"] = (
        None
        if session.draft_dag_json is None
        else _json_from_storage(session.draft_dag_json, fallback=None)
    )
    payload["ui_state"] = _json_from_storage(session.ui_state_json, fallback={})
    return payload


def _orchestration_session_surface(session: OrchestrationSession | None) -> str | None:
    if session is None:
        return None
    ui_state = _json_from_storage(session.ui_state_json, fallback={})
    if not isinstance(ui_state, dict):
        return None
    surface = ui_state.get("surface")
    return surface if isinstance(surface, str) else None


def _json_from_storage(value: str, *, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _prune_dag_artifact_uploads(dag: UserDAG) -> None:
    uploads = state.dag_artifact_uploads.get(dag.id)
    if not uploads:
        return
    for artifact_id in list(uploads):
        if artifact_id not in dag.artifacts:
            del uploads[artifact_id]


@app.get("/dag-runs/{run_id}")
async def get_dag_run(run_id: str) -> dict[str, Any]:
    dag_run = await _dag_run_from_state(run_id)
    if dag_run is None:
        raise HTTPException(status_code=404, detail="DAGRun not found.")
    return {"dag_run": dag_run.model_dump(mode="json")}


@app.get("/runs/{run_id}/artifacts")
async def get_run_artifacts(run_id: str) -> dict[str, Any]:
    run_state = await _run_state_from_state(run_id)
    if run_state is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return _run_artifacts_response(run_state).model_dump(mode="json")


@app.get("/runs/{run_id}/artifacts/preview")
async def preview_run_artifact(run_id: str, path: str) -> dict[str, Any]:
    run_state = await _run_state_from_state(run_id)
    if run_state is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    file_path = _resolve_run_artifact_path(run_state, path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Artifact file not found.")
    preview_kind = _preview_kind_for_path(path)
    if preview_kind is None:
        raise HTTPException(status_code=415, detail="Artifact file type is not previewable.")
    if preview_kind not in _TEXT_PREVIEW_KINDS:
        raise HTTPException(status_code=415, detail="Artifact file type uses binary browser preview.")
    content, truncated, size = _read_text_preview(file_path)
    return RunArtifactPreviewResponse(
        run_id=run_id,
        path=_normalize_run_artifact_path(path),
        name=file_path.name,
        media_type=_media_type_for_path(path),
        preview_kind=preview_kind,
        content=content,
        size=size,
        truncated=truncated,
    ).model_dump(mode="json")


@app.get("/runs/{run_id}/artifacts/download")
async def download_run_artifact(run_id: str, path: str) -> FileResponse:
    run_state = await _run_state_from_state(run_id)
    if run_state is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    file_path = _resolve_run_artifact_path(run_state, path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Artifact file not found.")
    normalized_path = _normalize_run_artifact_path(path)
    return FileResponse(
        file_path,
        filename=file_path.name,
        media_type=_media_type_for_path(normalized_path),
    )


@app.get("/runs/{run_id}/artifacts/onlyoffice/config")
async def get_run_artifact_onlyoffice_config(run_id: str, path: str) -> dict[str, Any]:
    run_state = await _run_state_from_state(run_id)
    if run_state is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return _onlyoffice_config_response(run_state, path).model_dump(mode="json")


@app.get("/onlyoffice/files/{token}")
async def get_onlyoffice_file(token: str) -> FileResponse:
    payload = _onlyoffice_token_payload(token)
    file_path, not_found_detail = await _onlyoffice_file_path_for_payload(payload)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=not_found_detail)
    return FileResponse(
        file_path,
        filename=file_path.name,
        media_type=_media_type_for_path(payload["path"]),
    )


@app.post("/onlyoffice/callback/{token}")
async def onlyoffice_callback(token: str, request: OnlyOfficeCallbackRequest) -> dict[str, int]:
    payload = _onlyoffice_token_payload(token)
    if request.status != 6 or request.forcesavetype != 1:
        return {"error": 0}
    if not payload["editable"]:
        raise HTTPException(status_code=403, detail="OnlyOffice callback is not authorized to edit this file.")
    if not request.url:
        raise HTTPException(status_code=400, detail="OnlyOffice callback save URL is required.")
    file_path, not_found_detail = await _onlyoffice_file_path_for_payload(payload)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=not_found_detail)
    content = await _download_onlyoffice_callback_file(request.url)
    await run_in_threadpool(_replace_file_bytes, file_path, content)
    return {"error": 0}


@app.get("/dag-runs/{run_id}/artifacts")
async def get_dag_run_artifacts(run_id: str) -> dict[str, Any]:
    run_state = await _run_state_from_state(run_id)
    if run_state is None or run_state.kind != "static_dag":
        raise HTTPException(status_code=404, detail="DAGRun not found.")
    return _run_artifacts_response(run_state).model_dump(mode="json")


@app.get("/capabilities")
async def list_capabilities(kind: str | None = None) -> dict[str, Any]:
    runner = state.get_runner()
    definitions = list(runner.list_capabilities(kind=kind))
    if kind in (None, "agent"):
        definitions.extend(
            definition
            for definition in _profile_agent_capabilities()
            if _capability_can_be_listed_as_profile_agent(definition)
        )
    return {
        "capabilities": [
            definition.model_dump(mode="json")
            for definition in sorted(definitions, key=lambda item: item.id)
        ]
    }


@app.get("/agents")
async def list_agents() -> dict[str, Any]:
    store = state.agent_preset_store()
    presets, errors = _agent_presets_available_for_registration(store)
    state.agent_preset_errors = errors
    return {
        "agents": [
            _agent_preset_payload(preset)
            for preset in presets
        ],
        "errors": dict(state.agent_preset_errors),
    }


@app.post("/agents")
async def create_agent(request: AgentPreset) -> dict[str, Any]:
    name = _clean_agent_preset_name(request.name)
    store = state.agent_preset_store()
    if name in store.list_names():
        raise HTTPException(status_code=400, detail=f"Agent preset '{name}' already exists.")
    _ensure_agent_preset_name_available(name)
    preset = _agent_preset_from_request(request, name=name)
    tool_agent = _tool_agent_from_preset(preset)
    try:
        state.get_runner().validate_agent_registration(tool_agent)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved = store.save(preset)
    try:
        state.get_runner().add_agent(tool_agent)
        state.agent_preset_registered_names.add(name)
    except Exception as exc:
        store.delete(name)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"agent": _agent_preset_payload(saved)}


@app.put("/agents/{name}")
async def update_agent(name: str, request: AgentPresetUpdateRequest) -> dict[str, Any]:
    preset_name = _clean_agent_preset_name(name)
    store = state.agent_preset_store()
    if preset_name not in store.list_names():
        raise HTTPException(status_code=404, detail="Agent preset not found.")
    _ensure_agent_preset_name_available(preset_name)
    previous = store.load(preset_name)
    preset = _agent_preset_from_request(request, name=preset_name)
    try:
        state.get_runner().validate_agent_registration(_tool_agent_from_preset(preset), replacing=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved = store.save(preset)
    state.close_runner()
    state.get_runner()
    error = state.agent_preset_errors.get(preset_name)
    if error is not None:
        store.save(previous)
        state.close_runner()
        raise HTTPException(status_code=400, detail=error)
    return {"agent": _agent_preset_payload(saved)}


@app.delete("/agents/{name}")
async def delete_agent(name: str) -> dict[str, str]:
    preset_name = _clean_agent_preset_name(name)
    store = state.agent_preset_store()
    if preset_name not in store.list_names():
        raise HTTPException(status_code=404, detail="Agent preset not found.")
    store.delete(preset_name)
    state.close_runner()
    return {"status": "deleted"}


def _profile_agent_capabilities() -> list[CapabilityDefinition]:
    profiles = _agent_profile_candidates()
    definitions: list[CapabilityDefinition] = []
    for source, profile in profiles:
        try:
            name = clean_agent_preset_name(profile.name)
        except ValueError:
            continue
        definitions.append(CapabilityDefinition(
            id=f"agent.{name}",
            kind="agent",
            description=profile.description,
            parameters=agent_capability_parameters(),
            policy=CapabilityPolicy(risk="medium", sandbox_required=True),
            config={"profile": profile.name, "source": source},
        ))
    return definitions


def _capability_can_be_listed_as_profile_agent(definition: CapabilityDefinition) -> bool:
    try:
        state.get_runner().validate_capability_registerable(definition)
    except ValueError:
        return False
    return True


def _agent_preset_payload(preset: AgentPreset) -> dict[str, Any]:
    return {
        **preset.model_dump(mode="json"),
        "id": f"agent.{preset.name}",
    }


def _agent_preset_from_request(
    request: AgentPreset | AgentPresetUpdateRequest,
    *,
    name: str,
) -> AgentPreset:
    try:
        preset = AgentPreset(
            name=name,
            profile=str(request.profile).strip(),
            description=str(request.description or ""),
            max_steps=request.max_steps,
            capabilities=(
                None if request.capabilities is None
                else _validated_agent_node_capabilities(request.capabilities)
            ),
            skills=(
                None if request.skills is None
                else _validated_agent_node_skills(request.skills)
            ),
            agents=None if request.agents is None else [str(agent) for agent in request.agents],
            review=request.review,
        )
        _resolve_agent_profile(preset.profile)
        if preset.agents:
            raise ValueError(f"Registered subagent 'agent.{name}' cannot expose subagents.")
        if preset.review != "fast":
            raise ValueError("Registered subagents must use review=\"fast\".")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return preset


def _tool_agent_from_preset(preset: AgentPreset) -> ToolAgent:
    return ToolAgent(
        profile=_resolve_agent_profile(preset.profile),
        name=preset.name,
        max_steps=preset.max_steps,
        capabilities=None if preset.capabilities is None else tuple(preset.capabilities),
        skills=None if preset.skills is None else tuple(preset.skills),
        agents=None if preset.agents is None else tuple(preset.agents),
        review=preset.review,
        description=preset.description,
    )


def _clean_agent_preset_name(value: str) -> str:
    try:
        return clean_agent_preset_name(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _agent_presets_with_errors(store: AgentPresetStore) -> tuple[list[AgentPreset], dict[str, str]]:
    presets, errors = store.list_valid()
    profile_names = {profile.name for _, profile in _agent_profile_candidates()}
    valid: list[AgentPreset] = []
    for preset in presets:
        if preset.name in profile_names:
            errors[preset.name] = f"Agent preset '{preset.name}' conflicts with an agent profile."
            continue
        valid.append(preset)
    return valid, errors


def _agent_presets_available_for_registration(store: AgentPresetStore) -> tuple[list[AgentPreset], dict[str, str]]:
    presets, errors = _agent_presets_with_errors(store)
    if not presets:
        return [], errors
    runner = state.get_runner()
    valid: list[AgentPreset] = []
    for preset in presets:
        try:
            runner.validate_agent_registration(_tool_agent_from_preset(preset), replacing=True)
        except Exception as exc:
            errors[preset.name] = str(exc)
            continue
        valid.append(preset)
    return valid, errors


def _ensure_agent_preset_name_available(name: str) -> None:
    profile_names = {profile.name for _, profile in _agent_profile_candidates()}
    if name in profile_names:
        raise HTTPException(status_code=400, detail=f"Agent preset '{name}' conflicts with an agent profile.")


def _agent_profile_candidates() -> list[tuple[str, AgentProfile]]:
    candidates: dict[str, tuple[str, AgentProfile]] = {}
    for profile in list_builtin_profiles():
        candidates[profile.name] = ("builtin", profile)
    config_directory = state.get_profile_directory()
    if config_directory is not None:
        config_store = ProfileStore(config_directory)
        for name in config_store.list_names():
            try:
                candidates[name] = ("config", config_store.load(name))
            except Exception:
                continue
    managed_store = state.managed_profile_store()
    for name in managed_store.list_names():
        try:
            candidates[name] = ("managed", managed_store.load(name))
        except Exception:
            continue
    return sorted(candidates.values(), key=lambda item: item[1].name)


@app.post("/capabilities")
async def create_capability(definition: CapabilityDefinition) -> dict[str, Any]:
    runner = state.get_runner()
    try:
        runner.register_capability(definition, _handler_for_definition(definition))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state.custom_capabilities[definition.id] = definition.model_copy(deep=True)
    state._install_agent_presets()
    return {"capability": runner.get_capability(definition.id).model_dump(mode="json")}


@app.put("/capabilities/{capability_id}")
async def update_capability(capability_id: str, definition: CapabilityDefinition) -> dict[str, Any]:
    runner = state.get_runner()
    if capability_id != definition.id:
        raise HTTPException(status_code=400, detail="Capability id mismatch.")
    existing = runner.get_capability(capability_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Capability not found.")
    _ensure_generic_capability_mutation_allowed(existing)
    try:
        runner.replace_capability(definition, _handler_for_definition(definition))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state.custom_capabilities[definition.id] = definition.model_copy(deep=True)
    state._install_agent_presets()
    return {"capability": runner.get_capability(definition.id).model_dump(mode="json")}


@app.delete("/capabilities/{capability_id}")
async def delete_capability(capability_id: str) -> dict[str, str]:
    runner = state.get_runner()
    definition = runner.get_capability(capability_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Capability not found.")
    _ensure_generic_capability_mutation_allowed(definition)
    try:
        runner.remove_capability(capability_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state.custom_capabilities.pop(capability_id, None)
    return {"status": "deleted"}


@app.post("/capabilities/{capability_id}/enable")
async def enable_capability(capability_id: str) -> dict[str, Any]:
    return _set_capability_enabled(capability_id, True)


@app.post("/capabilities/{capability_id}/disable")
async def disable_capability(capability_id: str) -> dict[str, Any]:
    return _set_capability_enabled(capability_id, False)


@app.get("/python-tools")
async def list_python_tools() -> dict[str, Any]:
    state.get_runner()
    return {"tools": [_python_tool_payload(config) for config in state.custom_python_tools]}


@app.post("/python-tools")
async def create_python_tool(request: PythonToolRequest) -> dict[str, Any]:
    with state.python_tool_lock:
        state.sync_user_config()
        config = _python_tool_config_from_request(request)
        if _python_tool_config_index(config.id) is not None:
            raise HTTPException(status_code=400, detail=f"Python tool source '{config.id}' already exists.")
        state.custom_python_tools.append(config)
        state.persist_user_python_tools()
        _reload_python_tools()
        return {"tool": _python_tool_payload(config)}


@app.post("/python-tools/validate")
async def validate_python_tool(request: PythonToolRequest) -> dict[str, Any]:
    config = _python_tool_config_from_request(request)
    result = load_python_tool_sources(
        [config],
        user_config_dir=state.get_user_config_path().parent,
        managed_root=state.get_managed_python_tool_root(),
    )
    status = result.statuses[0] if result.statuses else None
    if status is not None and status.error is None and status.config.enabled:
        runner = state.get_runner()
        try:
            runner.validate_tools_registerable(
                status.bindings,
                ignore_ids=state.custom_python_tool_capabilities.get(config.id, []),
            )
        except Exception as exc:
            status.error = str(exc)
            status.capability_ids.clear()
    return {
        "tool": _python_tool_payload(
            config,
            capabilities=[] if status is None else status.capability_ids,
            error=None if status is None else status.error,
        )
    }


@app.post("/python-tools/discover")
async def discover_python_tools(request: Request) -> dict[str, list[str]]:
    source_text = await _python_tool_discovery_source(request)
    try:
        names = discover_python_tool_names(source_text)
    except SyntaxError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Python tool source could not be parsed: {exc.msg}.",
        ) from exc
    return {"names": names}


@app.post("/python-tools/upload")
async def upload_python_tool(
    file: UploadFile = File(...),
    id: str = Form(...),
    names: str = Form(...),
    enabled: bool = Form(True),
) -> dict[str, Any]:
    source_id = _clean_python_tool_source_id(id)
    filename = file.filename or ""
    if Path(filename).suffix != ".py":
        raise HTTPException(status_code=400, detail="Python tool uploads must use the .py extension.")
    name_list = _parse_python_tool_names(names)
    content = await file.read()
    with state.python_tool_lock:
        state.sync_user_config()
        if _python_tool_config_index(source_id) is not None:
            raise HTTPException(status_code=400, detail=f"Python tool source '{source_id}' already exists.")
        managed_root = state.get_managed_python_tool_root()
        managed_root.mkdir(parents=True, exist_ok=True)
        target = managed_root / f"{source_id}.py"
        target.write_bytes(content)
        previous_tools = list(state.custom_python_tools)
        try:
            relative_path = _python_tool_storage_path(target)
            request = PythonToolRequest(
                id=source_id,
                source="managed",
                path=relative_path,
                names=name_list,
                enabled=enabled,
            )
            config = _python_tool_config_from_request(request, allow_managed=True)
            state.custom_python_tools.append(config)
            state.persist_user_python_tools()
            _reload_python_tools()
            return {"tool": _python_tool_payload(config)}
        except Exception:
            state.custom_python_tools = previous_tools
            state.persist_user_python_tools()
            target.unlink(missing_ok=True)
            state.close_runner()
            raise


@app.post("/python-tools/reload")
async def reload_python_tools() -> dict[str, Any]:
    with state.python_tool_lock:
        _reload_python_tools()
        return await list_python_tools()


@app.put("/python-tools/{tool_id}")
async def update_python_tool(tool_id: str, request: PythonToolRequest) -> dict[str, Any]:
    with state.python_tool_lock:
        state.sync_user_config()
        source_id = _clean_python_tool_source_id(tool_id)
        index = _python_tool_config_index(source_id)
        if index is None:
            raise HTTPException(status_code=404, detail="Python tool source not found.")
        existing = state.custom_python_tools[index]
        config = _python_tool_config_from_request(
            request,
            allow_managed=existing.source == "managed",
        )
        if source_id != config.id:
            raise HTTPException(status_code=400, detail="Python tool source id mismatch.")
        if existing.source == "managed" and (config.source != "managed" or config.path != existing.path):
            raise HTTPException(
                status_code=400,
                detail="Uploaded Python tool sources cannot change their managed path.",
            )
        state.custom_python_tools[index] = config
        state.persist_user_python_tools()
        _reload_python_tools()
        return {"tool": _python_tool_payload(config)}


@app.delete("/python-tools/{tool_id}")
async def delete_python_tool(tool_id: str) -> dict[str, str]:
    with state.python_tool_lock:
        state.sync_user_config()
        source_id = _clean_python_tool_source_id(tool_id)
        index = _python_tool_config_index(source_id)
        if index is None:
            raise HTTPException(status_code=404, detail="Python tool source not found.")
        config = state.custom_python_tools.pop(index)
        _delete_managed_python_tool_file(config)
        state.persist_user_python_tools()
        _reload_python_tools()
        return {"status": "deleted"}


@app.get("/models", response_model=ModelListResponse)
async def list_models() -> ModelListResponse:
    return ModelListResponse(
        models=_model_provider_payloads(),
        active_model_id=_active_model_id(),
    )


@app.post("/models", response_model=ModelMutationResponse)
async def create_model_provider(request: ModelProviderRequest) -> ModelMutationResponse:
    state.sync_user_config()
    model_id = _clean_model_id(request.id)
    if model_id in state.custom_model_providers:
        raise HTTPException(status_code=400, detail=f"Model '{model_id}' already exists.")
    if request.api_key_action == "preserve":
        raise HTTPException(status_code=400, detail="Cannot preserve an API key when creating a model.")
    model = _normalized_model_provider(request, model_id=model_id)
    if request.api_key_action == "clear":
        model = model.model_copy(update={"api_key": None}, deep=True)
    state.custom_model_providers[model_id] = model
    state.persist_user_models()
    return ModelMutationResponse(
        model=_user_model_payload(model, active=_active_model_id() == model_id),
        active_model_id=_active_model_id(),
    )


@app.put("/models/{model_id}", response_model=ModelMutationResponse)
async def update_model_provider(model_id: str, request: ModelProviderRequest) -> ModelMutationResponse:
    state.sync_user_config()
    clean_model_id = _clean_model_id(model_id)
    body_model_id = _clean_model_id(request.id)
    if clean_model_id != body_model_id:
        raise HTTPException(status_code=400, detail="Model id mismatch.")
    if clean_model_id not in state.custom_model_providers:
        raise HTTPException(status_code=404, detail="Model not found.")
    existing = state.custom_model_providers[clean_model_id]
    model = _normalized_model_provider(request, model_id=clean_model_id, existing=existing)
    if request.api_key_action == "preserve":
        model = model.model_copy(update={"api_key": existing.api_key}, deep=True)
    elif request.api_key_action == "clear":
        model = model.model_copy(update={"api_key": None}, deep=True)
    state.custom_model_providers[clean_model_id] = model
    state.persist_user_models()
    if state.active_model_id == clean_model_id:
        state.close_runner()
    return ModelMutationResponse(
        model=_user_model_payload(model, active=_active_model_id() == clean_model_id),
        active_model_id=_active_model_id(),
    )


@app.delete("/models/{model_id}", response_model=ModelDeleteResponse)
async def delete_model_provider(model_id: str) -> ModelDeleteResponse:
    state.sync_user_config()
    clean_model_id = _clean_model_id(model_id)
    if clean_model_id not in state.custom_model_providers:
        raise HTTPException(status_code=404, detail="Model not found.")
    state.custom_model_providers.pop(clean_model_id, None)
    if state.active_model_id == clean_model_id:
        state.active_model_id = None
        state.close_runner()
    state.persist_user_models()
    return ModelDeleteResponse(status="deleted", active_model_id=_active_model_id())


@app.post("/models/{model_id}/activate", response_model=ModelMutationResponse)
async def activate_model_provider(model_id: str) -> ModelMutationResponse:
    state.sync_user_config()
    clean_model_id = _clean_model_id(model_id, allow_config=True)
    if clean_model_id == CONFIG_MODEL_ID:
        state.active_model_id = None
        state.persist_user_models()
        state.close_runner()
        return ModelMutationResponse(model=_config_model_payload(active=True), active_model_id=CONFIG_MODEL_ID)
    model = state.custom_model_providers.get(clean_model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found.")
    state.active_model_id = clean_model_id
    state.persist_user_models()
    state.close_runner()
    return ModelMutationResponse(model=_user_model_payload(model, active=True), active_model_id=clean_model_id)


@app.get("/system/onlyoffice", response_model=OnlyOfficeSettingsPayload)
async def get_onlyoffice_settings() -> OnlyOfficeSettingsPayload:
    return _onlyoffice_settings_payload(state._current_user_config().onlyoffice)


@app.put("/system/onlyoffice", response_model=OnlyOfficeSettingsPayload)
async def update_onlyoffice_settings(request: OnlyOfficeSettingsPayload) -> OnlyOfficeSettingsPayload:
    config = state._current_user_config()
    config.onlyoffice = UserOnlyOfficeConfig(
        enabled=request.enabled,
        document_server_url=_clean_optional_text(request.document_server_url),
        public_api_base=_clean_optional_text(request.public_api_base),
        jwt_secret=_clean_optional_text(request.jwt_secret),
        lang=_clean_optional_text(request.lang) or "zh",
        project_file_edit_enabled=request.project_file_edit_enabled,
        run_artifact_edit_enabled=request.run_artifact_edit_enabled,
    )
    save_user_config(config, state.get_user_config_path())
    return _onlyoffice_settings_payload(config.onlyoffice)


@app.get("/mcp/servers")
async def list_mcp_servers() -> dict[str, Any]:
    return {"servers": _mcp_server_payloads(state.get_runner())}


@app.post("/mcp/servers")
async def create_mcp_server(request: MCPServerRequest) -> dict[str, Any]:
    state.sync_user_config()
    name = _clean_name(request.name, field="MCP server name")
    if name in _configured_mcp_server_names():
        raise HTTPException(status_code=400, detail=f"MCP server '{name}' is already configured.")
    if name in state.custom_mcp_servers:
        raise HTTPException(status_code=400, detail=f"MCP server '{name}' already exists.")
    state.custom_mcp_servers[name] = _mcp_server_config(request)
    try:
        state.reload_custom_mcp()
    except ValueError as exc:
        state.custom_mcp_servers.pop(name, None)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        state.custom_mcp_errors[name] = str(exc)
    state.persist_user_mcp_servers()
    return {"server": _mcp_server_payload(name, "user", state.custom_mcp_servers[name], state.get_runner())}


@app.put("/mcp/servers/{name}")
async def update_mcp_server(name: str, request: MCPServerRequest) -> dict[str, Any]:
    state.sync_user_config()
    server_name = _clean_name(name, field="MCP server name")
    body_name = _clean_name(request.name, field="MCP server name")
    if body_name != server_name:
        raise HTTPException(status_code=400, detail="MCP server name mismatch.")
    if server_name not in state.custom_mcp_servers:
        raise HTTPException(status_code=404, detail="MCP server not found.")
    _ensure_mcp_server_removable(server_name)
    previous = dict(state.custom_mcp_servers[server_name])
    state.custom_mcp_servers[server_name] = _mcp_server_config(request)
    try:
        state.reload_custom_mcp()
    except ValueError as exc:
        state.custom_mcp_servers[server_name] = previous
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state.persist_user_mcp_servers()
    return {"server": _mcp_server_payload(server_name, "user", state.custom_mcp_servers[server_name], state.get_runner())}


@app.delete("/mcp/servers/{name}")
async def delete_mcp_server(name: str) -> dict[str, str]:
    state.sync_user_config()
    server_name = _clean_name(name, field="MCP server name")
    if server_name not in state.custom_mcp_servers:
        raise HTTPException(status_code=404, detail="MCP server not found.")
    _ensure_mcp_server_removable(server_name)
    previous = state.custom_mcp_servers.pop(server_name)
    try:
        state.reload_custom_mcp()
    except ValueError as exc:
        state.custom_mcp_servers[server_name] = previous
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state.persist_user_mcp_servers()
    return {"status": "deleted"}


@app.post("/mcp/reload")
async def reload_mcp_servers() -> dict[str, Any]:
    state.sync_user_config()
    try:
        state.reload_custom_mcp()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await list_mcp_servers()


def _ensure_mcp_server_removable(name: str) -> None:
    try:
        state.get_runner().ensure_mcp_server_removable(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/skills")
async def list_skills() -> dict[str, Any]:
    try:
        return {"skills": [skill.as_list_item() for skill in state.skill_store().list()]}
    except SkillStoreError as exc:
        raise _skill_http_exception(exc) from exc


@app.get("/skills/{name:path}")
async def get_skill(name: str, file_path: str | None = None) -> dict[str, Any]:
    try:
        return state.skill_store().view(name, file_path=file_path).as_payload()
    except SkillStoreError as exc:
        raise _skill_http_exception(exc) from exc


@app.post("/skills/install")
async def install_skill(
    file: UploadFile | None = File(None),
    content: str | None = Form(None),
    name: str | None = Form(None),
    description: str | None = Form(None),
    category: str | None = Form(None),
) -> dict[str, Any]:
    if file is None and not content:
        raise HTTPException(status_code=400, detail="Skill content or file is required.")
    try:
        if file is not None:
            view = state.skill_store().install(
                await file.read(),
                filename=file.filename,
                name=name or None,
                description=description or None,
                category=category or None,
            )
        else:
            view = state.skill_store().install(
                content or "",
                filename=None,
                name=name or None,
                description=description or None,
                category=category or None,
            )
    except SkillStoreError as exc:
        raise _skill_http_exception(exc) from exc
    return {"skill": view.as_payload()}


@app.delete("/skills/{name:path}")
async def delete_skill(name: str) -> dict[str, str]:
    try:
        state.skill_store().delete(name)
    except SkillStoreError as exc:
        raise _skill_http_exception(exc) from exc
    return {"status": "deleted"}


@app.get("/profiles")
async def list_profiles() -> dict[str, Any]:
    profiles, warnings = _profile_payloads()
    return {"profiles": profiles, "warnings": warnings}


@app.post("/profiles")
async def create_profile(request: ProfileCreateRequest) -> dict[str, Any]:
    name = _clean_managed_profile_name(request.name)
    _validate_profile_content(request.content)
    _ensure_managed_profile_name_available(name)
    profile = state.managed_profile_store().save(name, request.content)
    state.close_runner()
    return {"profile": _profile_payload(profile, "managed")}


@app.put("/profiles/{name}")
async def update_profile(name: str, request: ProfileUpdateRequest) -> dict[str, Any]:
    profile_name = _clean_managed_profile_name(name)
    _validate_profile_content(request.content)
    store = state.managed_profile_store()
    if profile_name not in store.list_names():
        raise HTTPException(status_code=404, detail="Managed profile not found.")
    profile = store.save(profile_name, request.content)
    state.close_runner()
    return {"profile": _profile_payload(profile, "managed")}


@app.delete("/profiles/{name}")
async def delete_profile(name: str) -> dict[str, str]:
    profile_name = _clean_managed_profile_name(name)
    store = state.managed_profile_store()
    if profile_name not in store.list_names():
        raise HTTPException(status_code=404, detail="Managed profile not found.")
    store.delete(profile_name)
    state.close_runner()
    return {"status": "deleted"}


def _profile_payload(profile: AgentProfile, source: str) -> dict[str, Any]:
    return {
        **profile.model_dump(mode="json"),
        "id": f"{source}:{profile.name}",
        "source": source,
        "editable": source == "managed",
        "deletable": source == "managed",
    }


def _profile_payloads() -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    profiles: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    for profile in list_builtin_profiles():
        profiles.append(_profile_payload(profile, "builtin"))
    managed_store = state.managed_profile_store()
    for name in managed_store.list_names():
        try:
            profiles.append(_profile_payload(managed_store.load(name), "managed"))
        except Exception as exc:
            warnings.append({"name": name, "error": str(exc)})
    config_directory = state.get_profile_directory()
    if config_directory is None:
        return profiles, warnings
    directory = Path(config_directory)
    if not directory.exists():
        warnings.append({"name": str(directory), "error": "Profiles directory not found."})
        return profiles, warnings
    config_store = ProfileStore(directory)
    for name in config_store.list_names():
        try:
            profiles.append(_profile_payload(config_store.load(name), "config"))
        except Exception as exc:
            warnings.append({"name": name, "error": str(exc)})
    return profiles, warnings


def _clean_managed_profile_name(value: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Profile name is required.")
    if _MANAGED_PROFILE_NAME_RE.fullmatch(name) is None:
        raise HTTPException(
            status_code=400,
            detail="Profile name may contain only letters, numbers, and underscores, and must start with a letter.",
        )
    return name


def _validate_profile_content(content: str) -> None:
    if not content.strip():
        raise HTTPException(status_code=400, detail="Profile content is required.")
    if len(content.encode("utf-8")) > PROFILE_CONTENT_BYTES_LIMIT:
        raise HTTPException(status_code=400, detail="Profile content is too large.")


def _ensure_managed_profile_name_available(name: str) -> None:
    if name in {profile.name for profile in list_builtin_profiles()}:
        raise HTTPException(status_code=400, detail=f"Profile '{name}' is built in.")
    if name in state.agent_preset_store().list_names():
        raise HTTPException(status_code=400, detail=f"Profile '{name}' conflicts with an agent preset.")
    managed_store = state.managed_profile_store()
    if name in managed_store.list_names():
        raise HTTPException(status_code=400, detail=f"Profile '{name}' already exists.")
    config_directory = state.get_profile_directory()
    if config_directory is not None and name in ProfileStore(config_directory).list_names():
        raise HTTPException(status_code=400, detail=f"Profile '{name}' already exists in the configured profile directory.")


@app.get("/sandbox/status")
async def sandbox_status() -> dict[str, Any]:
    runner = state.get_runner()
    status = runner.sandbox_status()
    return {
        "runner": "local-dev",
        "workspace_root": str(runner.runtime.capability_catalog.workspace_root),
        "container_ready": bool(status.get("docker_available")),
        **status,
    }


def _handler_for_definition(definition: CapabilityDefinition):
    if definition.kind != "tool":
        raise HTTPException(
            status_code=400,
            detail="Only tool capabilities can be created through this endpoint.",
        )
    if not definition.id.startswith("tool."):
        raise HTTPException(
            status_code=400,
            detail="Tool capability ids must start with 'tool.'.",
        )
    return _template_capability_handler(str(definition.config.get("template", "")))


def _template_capability_handler(template: str):
    def execute(invocation: CapabilityInvocation) -> CapabilityResult:
        policy_decision = invocation.boundary.policy_decision()
        try:
            content = template.format(**invocation.arguments) if template else ""
        except Exception as exc:
            return CapabilityResult.failed(
                invocation,
                str(exc),
                stop_reason=type(exc).__name__,
                policy_decision=policy_decision,
            )
        return CapabilityResult.completed(
            invocation,
            content,
            policy_decision=policy_decision,
        )

    return execute


def _set_capability_enabled(capability_id: str, enabled: bool) -> dict[str, Any]:
    runner = state.get_runner()
    definition = runner.get_capability(capability_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Capability not found.")
    _ensure_generic_capability_mutation_allowed(definition)
    try:
        updated = runner.set_capability_enabled(capability_id, enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if enabled:
        state._install_agent_presets()
    return {"capability": updated.model_dump(mode="json")}


def _ensure_generic_capability_mutation_allowed(definition: CapabilityDefinition) -> None:
    if definition.kind == "agent":
        raise HTTPException(
            status_code=400,
            detail="Agent capabilities are managed through /agents.",
        )
    if definition.id in state.custom_python_tool_capability_ids:
        raise HTTPException(
            status_code=400,
            detail="Python tool capabilities must be managed through their Python tool source.",
        )


def _load_user_config_for_webui(path: Path) -> tuple[UserDagentConfig, dict[str, str]]:
    return load_user_config_with_python_tool_errors(path)


def _reload_python_tools() -> None:
    state.sync_user_config()
    if state.runner is None:
        state.get_runner()
        return
    state._install_python_tools(replace_existing=True, refresh_agent_presets=True)


async def _python_tool_discovery_source(request: Request) -> str:
    content_type = request.headers.get("content-type", "")
    try:
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            file_value = form.get("file")
            if file_value is not None:
                return await _uploaded_python_tool_source(file_value)
            source = str(form.get("source") or "path")
            path = form.get("path")
            return _path_python_tool_source(source=source, path=None if path is None else str(path))

        payload = await request.json()
    except HTTPException:
        raise
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Python tool source must be UTF-8 text.") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Python tool discovery request must be an object.")
    return _path_python_tool_source(
        source=str(payload.get("source") or "path"),
        path=None if payload.get("path") is None else str(payload.get("path")),
    )


async def _uploaded_python_tool_source(file_value: Any) -> str:
    filename = str(getattr(file_value, "filename", "") or "")
    if Path(filename).suffix != ".py":
        raise HTTPException(status_code=400, detail="Python tool uploads must use the .py extension.")
    reader = getattr(file_value, "read", None)
    if reader is None:
        raise HTTPException(status_code=400, detail="Python tool file is required.")
    content = await reader()
    try:
        return content.decode("utf-8") if isinstance(content, bytes) else str(content)
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Python tool source must be UTF-8 text.") from exc


def _path_python_tool_source(*, source: str, path: str | None) -> str:
    if source not in {"path", "managed"}:
        raise HTTPException(status_code=400, detail="Python tool discovery supports path and uploaded sources.")
    if not path or not path.strip():
        raise HTTPException(status_code=400, detail="Python tool path is required.")
    try:
        return read_python_tool_source(
            UserPythonToolConfig(
                id="discovery",
                source=source,
                path=path.strip(),
                names=["discovery"],
            ),
            user_config_dir=state.get_user_config_path().parent,
            managed_root=state.get_managed_python_tool_root(),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _python_tool_payload(
    config: UserPythonToolConfig,
    *,
    capabilities: list[str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    resolved_error = state.custom_python_tool_errors.get(config.id) if error is None else error
    resolved_capabilities = (
        list(state.custom_python_tool_capabilities.get(config.id, []))
        if capabilities is None
        else list(capabilities)
    )
    status: Literal["loaded", "disabled", "error"]
    if resolved_error:
        status = "error"
    elif not config.enabled:
        status = "disabled"
    else:
        status = "loaded"
    return PythonToolPayload(
        id=config.id,
        source=config.source,
        path=config.path,
        module=config.module,
        names=list(config.names),
        enabled=config.enabled,
        status=status,
        capabilities=resolved_capabilities,
        error=resolved_error,
    ).model_dump(mode="json")


def _python_tool_config_from_request(
    request: PythonToolRequest,
    *,
    allow_managed: bool = False,
) -> UserPythonToolConfig:
    source_id = _clean_python_tool_source_id(request.id)
    names = [_clean_python_tool_name(name) for name in request.names]
    if not names:
        raise HTTPException(status_code=400, detail="Python tool names are required.")
    path = request.path.strip() if request.path is not None else None
    module = request.module.strip() if request.module is not None else None
    if request.source == "managed" and not allow_managed:
        raise HTTPException(
            status_code=400,
            detail="Managed Python tool sources must be uploaded through /python-tools/upload.",
        )
    if request.source in {"path", "managed"} and not path:
        raise HTTPException(status_code=400, detail="Python tool path is required.")
    if request.source == "module" and not module:
        raise HTTPException(status_code=400, detail="Python tool module is required.")
    return UserPythonToolConfig(
        id=source_id,
        source=request.source,
        path=path,
        module=module,
        names=names,
        enabled=request.enabled,
    )


def _clean_python_tool_source_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Python tool source id is required.")
    try:
        return validate_capability_id_segment(text, label="Python tool source ids")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _clean_python_tool_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Python tool names cannot be empty.")
    try:
        return validate_capability_id_segment(text, label="Python tool names")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _parse_python_tool_names(value: str) -> list[str]:
    names = [
        item.strip()
        for item in re.split(r"[\n,]+", value)
        if item.strip()
    ]
    if not names:
        raise HTTPException(status_code=400, detail="Python tool names are required.")
    return [_clean_python_tool_name(name) for name in names]


def _python_tool_config_index(source_id: str) -> int | None:
    for index, config in enumerate(state.custom_python_tools):
        if config.id == source_id:
            return index
    return None


def _python_tool_storage_path(path: Path) -> str:
    config_dir = state.get_user_config_path().parent.resolve()
    resolved = path.resolve(strict=False)
    try:
        return resolved.relative_to(config_dir).as_posix()
    except ValueError:
        return str(resolved)


def _delete_managed_python_tool_file(config: UserPythonToolConfig) -> None:
    if config.source != "managed" or not config.path:
        return
    config_dir = state.get_user_config_path().parent.resolve()
    managed_root = state.get_managed_python_tool_root().resolve()
    candidate = (config_dir / config.path).resolve(strict=False)
    try:
        candidate.relative_to(managed_root)
    except ValueError:
        return
    if candidate != managed_root / f"{config.id}.py":
        return
    candidate.unlink(missing_ok=True)


def _runner_from_model_provider(model: ModelProviderRequest, *, skill_roots: list[Path]) -> Runner:
    config_path = resolve_config_path()
    config = load_config(config_path)
    validator = "validator_agent" if config.enable_result_validation else None
    profile_root = resolve_config_relative_path(config.profiles.directory, config_path)
    return Runner(
        workspace=DEFAULT_WORKSPACE,
        provider=Provider(**_provider_kwargs(model)),
        validator=validator,
        skill_roots=skill_roots,
        mcp_servers=dict(config.mcp_servers),
        profile_root=profile_root,
        sandbox=config.sandbox,
    )


def _provider_kwargs(model: ModelProviderRequest) -> dict[str, Any]:
    return {
        "base_url": model.base_url,
        "model": model.model,
        "api_key": model.api_key,
        "api_key_env": model.api_key_env,
        "timeout_seconds": model.timeout_seconds,
        "strip_thinking": model.strip_thinking,
        "reasoning": model.reasoning,
        "extra_request_args": dict(model.extra_request_args),
        "extra_body": dict(model.extra_body),
    }


def _model_request_from_user_config(model_id: str, model: UserModelProviderConfig) -> ModelProviderRequest:
    api_key = (
        model.api_key
        if "api_key" in model.model_fields_set and model.api_key != "not-needed"
        else None
    )
    return ModelProviderRequest(
        id=model_id,
        name=model.name or model_id,
        base_url=model.base_url,
        model=model.model,
        api_key=api_key,
        api_key_env=model.api_key_env,
        timeout_seconds=model.timeout_seconds,
        strip_thinking=model.strip_thinking,
        reasoning=model.reasoning.model_dump(mode="json") if model.reasoning is not None else None,
        extra_request_args=dict(model.extra_request_args),
        extra_body=dict(model.extra_body),
    )


def _user_model_provider_config(model: ModelProviderRequest) -> UserModelProviderConfig:
    return UserModelProviderConfig(
        name=model.name,
        base_url=model.base_url,
        model=model.model,
        api_key=model.api_key,
        api_key_env=model.api_key_env,
        timeout_seconds=model.timeout_seconds,
        strip_thinking=model.strip_thinking,
        reasoning=model.reasoning,
        extra_request_args=dict(model.extra_request_args),
        extra_body=dict(model.extra_body),
    )


def _onlyoffice_settings_payload(config: UserOnlyOfficeConfig) -> OnlyOfficeSettingsPayload:
    return OnlyOfficeSettingsPayload(
        enabled=config.enabled,
        document_server_url=config.document_server_url,
        public_api_base=config.public_api_base,
        jwt_secret=config.jwt_secret,
        lang=config.lang,
        project_file_edit_enabled=config.project_file_edit_enabled,
        run_artifact_edit_enabled=config.run_artifact_edit_enabled,
    )


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _model_provider_payloads() -> list[ModelProviderPayload]:
    state.sync_user_config()
    active_id = _active_model_id()
    return [
        _config_model_payload(active=active_id == CONFIG_MODEL_ID),
        *[
            _user_model_payload(model, active=model.id == active_id)
            for model in sorted(state.custom_model_providers.values(), key=lambda item: (item.name.lower(), item.id))
        ],
    ]


def _active_model_id() -> str:
    return state.active_model_id if state.active_model() is not None else CONFIG_MODEL_ID


def _config_model_payload(*, active: bool) -> ModelProviderPayload:
    config = load_config()
    provider = config.provider
    return ModelProviderPayload(
        id=CONFIG_MODEL_ID,
        name="config.yaml",
        source="config",
        active=active,
        base_url=provider.base_url,
        model=provider.model,
        api_key_env=provider.api_key_env,
        api_key_configured=_api_key_configured(provider.api_key, provider.api_key_env),
        api_key_saved=bool(provider.api_key),
        timeout_seconds=provider.timeout_seconds,
        strip_thinking=provider.strip_thinking,
        reasoning=provider.reasoning.model_dump(mode="json") if provider.reasoning is not None else None,
        extra_request_args=_redact_json_secrets(provider.extra_request_args),
        extra_body=_redact_json_secrets(provider.extra_body),
    )


def _user_model_payload(model: ModelProviderRequest, *, active: bool) -> ModelProviderPayload:
    return ModelProviderPayload(
        id=model.id,
        name=model.name,
        source="user",
        active=active,
        base_url=model.base_url,
        model=model.model,
        api_key_env=model.api_key_env,
        api_key_configured=_api_key_configured(model.api_key, model.api_key_env),
        api_key_saved=bool(model.api_key),
        timeout_seconds=model.timeout_seconds,
        strip_thinking=model.strip_thinking,
        reasoning=model.reasoning,
        extra_request_args=_redact_json_secrets(model.extra_request_args),
        extra_body=_redact_json_secrets(model.extra_body),
    )


def _normalized_model_provider(
    request: ModelProviderRequest,
    *,
    model_id: str,
    existing: ModelProviderRequest | None = None,
) -> ModelProviderRequest:
    extra_request_args = dict(request.extra_request_args)
    extra_body = dict(request.extra_body)
    if existing is not None:
        extra_request_args = _merge_redacted_json(existing.extra_request_args, extra_request_args)
        extra_body = _merge_redacted_json(existing.extra_body, extra_body)
    return request.model_copy(update={
        "id": model_id,
        "name": _required_text(request.name, field="Model name"),
        "base_url": _required_text(request.base_url, field="Base URL"),
        "model": _required_text(request.model, field="Model"),
        "api_key": _optional_secret(request.api_key),
        "api_key_env": _optional_text(request.api_key_env),
        "extra_request_args": extra_request_args,
        "extra_body": extra_body,
    }, deep=True)


def _redact_json_secrets(value: Any, *, key: str = "") -> Any:
    if _is_sensitive_json_key(key):
        return REDACTED_SECRET_VALUE
    if isinstance(value, dict):
        return {name: _redact_json_secrets(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact_json_secrets(item) for item in value]
    return value


def _merge_redacted_json(existing: Any, incoming: Any, *, key: str = "") -> Any:
    if incoming == REDACTED_SECRET_VALUE and _is_sensitive_json_key(key):
        return existing
    if isinstance(existing, dict) and isinstance(incoming, dict):
        return {
            child_key: _merge_redacted_json(existing.get(child_key), value, key=str(child_key))
            for child_key, value in incoming.items()
        }
    if isinstance(existing, list) and isinstance(incoming, list):
        return [
            _merge_redacted_json(existing[index], value, key=key) if index < len(existing) else value
            for index, value in enumerate(incoming)
        ]
    return incoming


def _is_sensitive_json_key(key: str) -> bool:
    normalized = "".join(char for char in key.lower() if char.isalnum())
    return any(marker in normalized for marker in ("apikey", "authorization", "token", "secret", "password", "credential"))


def _clean_model_id(value: str, *, allow_config: bool = False) -> str:
    model_id = str(value or "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="Model id is required.")
    if model_id == CONFIG_MODEL_ID:
        if allow_config:
            return model_id
        raise HTTPException(status_code=400, detail=f"Model id '{CONFIG_MODEL_ID}' is reserved.")
    if _MODEL_ID_RE.fullmatch(model_id) is None:
        raise HTTPException(
            status_code=400,
            detail="Model id may contain only letters, numbers, dots, underscores, and dashes.",
        )
    return model_id


def _api_key_configured(api_key: str | None, api_key_env: str | None) -> bool:
    return bool(api_key_env or (api_key and api_key != "not-needed"))


def _optional_text(value: str | None) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _optional_secret(value: str | None) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _required_text(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail=f"{field} is required.")
    return text


def _agent_from_message(request: MessageRequest) -> AutoAgent | ToolAgent | DagAgent:
    skills = None if request.skills is None else tuple(_dedupe(request.skills))
    capability_ids: tuple[str, ...] | None = None
    if request.capability_ids is not None:
        capability_ids = tuple(_validated_capability_ids(request.capability_ids))
    agents = _message_agent_refs(request)

    if request.target == "auto":
        return AutoAgent(
            capabilities=capability_ids,
            skills=skills,
            agents=agents,
            review=request.review_level,
            dynamic_adjust=request.dynamic_adjust,
        )
    if request.target == "tool":
        return ToolAgent(
            profile="conversation",
            capabilities=capability_ids,
            skills=skills,
            agents=agents,
            review=request.review_level,
        )
    return DagAgent(
        capabilities=capability_ids,
        skills=skills,
        agents=agents,
        review=request.review_level,
        dynamic_adjust=request.dynamic_adjust,
    )


def _message_agent_refs(request: MessageRequest) -> tuple[str, ...] | str | None:
    agent_ids = request.agent_ids or []
    if request.agent_scope == "none":
        if agent_ids:
            raise HTTPException(status_code=400, detail="agent_ids require agent_scope='selected'.")
        return None
    if request.agent_scope == "registered":
        if agent_ids:
            raise HTTPException(status_code=400, detail="agent_ids are not accepted when agent_scope='registered'.")
        return "registered"
    if not agent_ids:
        return ()
    return tuple(_validated_agent_ids(agent_ids))


def _validated_agent_ids(agent_ids: list[str]) -> list[str]:
    runner = state.get_runner()
    validated: list[str] = []
    for agent_id in _dedupe(agent_ids):
        if not agent_id.startswith("agent."):
            raise HTTPException(status_code=400, detail="agent_ids must use the 'agent.<name>' capability id form.")
        definition = runner.get_capability(agent_id)
        if definition is None:
            raise HTTPException(status_code=400, detail=f"Agent capability '{agent_id}' was not found.")
        if not definition.enabled:
            raise HTTPException(status_code=400, detail=f"Agent capability '{agent_id}' is disabled.")
        if definition.kind != "agent":
            raise HTTPException(status_code=400, detail=f"Capability '{agent_id}' is not an agent.")
        validated.append(agent_id)
    return validated


def _validated_capability_ids(capability_ids: list[str]) -> list[str]:
    runner = state.get_runner()
    validated: list[str] = []
    for capability_id in _dedupe(capability_ids):
        definition = runner.get_capability(capability_id)
        if definition is None:
            raise HTTPException(status_code=400, detail=f"Capability '{capability_id}' was not found.")
        if not definition.enabled:
            raise HTTPException(status_code=400, detail=f"Capability '{capability_id}' is disabled.")
        if definition.kind == "agent":
            raise HTTPException(
                status_code=400,
                detail="Agent capabilities must be selected through agent_scope and agent_ids.",
            )
        validated.append(capability_id)
    return validated


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _skill_http_exception(exc: SkillStoreError) -> HTTPException:
    if isinstance(exc, SkillNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, SkillAmbiguousError):
        return HTTPException(
            status_code=400,
            detail={
                "error": str(exc),
                "matches": [match.as_list_item() for match in exc.matches],
            },
        )
    if isinstance(exc, SkillPermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@app.post("/capabilities/{capability_id}/test")
async def test_capability(capability_id: str, request: CapabilityTestRequest) -> dict[str, Any]:
    runner = state.get_runner()
    if runner.get_capability(capability_id) is None:
        raise HTTPException(status_code=404, detail="Capability not found.")
    result = await runner.test_capability(capability_id, request.arguments, boundary=request.boundary)
    return {"result": result.model_dump(mode="json")}


@app.post("/messages/stream")
async def message_stream(http_request: Request) -> StreamingResponse:
    request, input_uploads = await _message_request_from_http(http_request)
    persisted_context = await _persisted_context_from_message(request)
    agent = _agent_from_message(request)
    if persisted_context is not None:
        stream_id = _new_api_id("stream")
        try:
            lock = await run_in_threadpool(
                state.get_store().acquire_conversation_lock,
                persisted_context.conversation_id,
                owner=stream_id,
            )
        except ConversationBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return StreamingResponse(
            _persisted_message_stream_events(request, agent, input_uploads, persisted_context, stream_id, lock),
            media_type="text/event-stream",
        )
    workspace_root = _workspace_root_from_message(request)

    async def events():
        sent_error = False
        try:
            runner = state.get_runner()
            async for event in gate_chat_display(
                runner.stream(
                    agent,
                    messages=request.messages,
                    state=request.state,
                    workspace_root=workspace_root,
                    input_uploads=input_uploads,
                ),
                validation_enabled=runner.enable_validation,
            ):
                if event.type == "run.failed":
                    sent_error = True
                yield _sse(_chat_stream_event_payload(event, runner))
        except Exception as exc:
            if not sent_error:
                yield _sse({
                    "type": "run.failed",
                    "data": {"message": str(exc), "error_type": type(exc).__name__},
                    "sequence": 0,
                    "run_id": None,
                })

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/projects/{project_id}/reviews/{review_id}/resume")
async def resume_project_review_stream(
    project_id: str,
    review_id: str,
    request: ProjectResumeReviewRequest,
) -> StreamingResponse:
    return await _resume_persisted_review_stream(review_id, request, project_id=project_id)


@app.post("/reviews/{review_id}/resume")
async def resume_review_stream(
    review_id: str,
    request: ProjectResumeReviewRequest,
) -> StreamingResponse:
    return await _resume_persisted_review_stream(review_id, request, project_id=None)


async def _resume_persisted_review_stream(
    review_id: str,
    request: ProjectResumeReviewRequest,
    *,
    project_id: str | None,
) -> StreamingResponse:
    store = state.get_store()
    project = None
    if project_id is not None:
        project = await run_in_threadpool(store.get_project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
    review = await run_in_threadpool(store.get_review, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    if project is None and review.project_id is not None:
        raise HTTPException(status_code=400, detail="Project reviews must be resumed through the project route.")
    if project is not None and review.project_id != project.id:
        raise HTTPException(status_code=404, detail="Review not found.")
    if review.status != "pending":
        raise HTTPException(status_code=409, detail="Review is already resolved.")
    run = await run_in_threadpool(store.get_run, review.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    if run.conversation_id is None:
        raise HTTPException(status_code=400, detail="Review run is not attached to a conversation.")
    conversation = await run_in_threadpool(store.get_conversation, run.conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if project is not None and conversation.project_id != project.id:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    run_state = await run_in_threadpool(store.get_run_state, run.id)
    if run_state is None:
        raise HTTPException(status_code=404, detail="Run state not found.")
    workspace_path = await run_in_threadpool(state.get_workspaces().local_path_for, run.workspace_uri)
    orchestration_session = None
    if conversation.kind != "chat":
        orchestration_session = await run_in_threadpool(
            store.get_orchestration_session_by_conversation,
            conversation.id,
        )
    context = PersistedMessageContext(
        project_id=conversation.project_id,
        conversation_id=conversation.id,
        conversation_kind=conversation.kind,
        workspace_uri=run.workspace_uri,
        workspace_path=workspace_path,
        orchestration_session_id=None if orchestration_session is None else orchestration_session.id,
        orchestration_surface=_orchestration_session_surface(orchestration_session),
    )
    stream_id = _new_api_id("stream")
    try:
        lock = await run_in_threadpool(
            store.acquire_conversation_lock,
            conversation.id,
            owner=stream_id,
        )
    except ConversationBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    decision = ReviewDecision(
        review_id=review_id,
        approved=request.approved,
        dag=request.dag,
        review_level=request.review_level,
        feedback=request.feedback,
    )
    try:
        decision_json = _review_decision_json(decision)
        message_projection = await ConversationMessageProjection.resume_for_review(run_state, context, decision)
        await _persist_review_resume_checkpoint(
            store,
            run=run,
            run_state=run_state,
            stream_id=stream_id,
            review_id=review_id,
            decision_json=decision_json,
            message_projection=message_projection,
        )
    except BaseException:
        await run_in_threadpool(lock.release)
        raise
    return StreamingResponse(
        _LockReleasingAsyncIterator(
            _persisted_review_resume_stream_events(
                decision,
                run_state,
                context,
                stream_id,
                message_projection=message_projection,
                decision_json=decision_json,
            ),
            lock,
        ),
        media_type="text/event-stream",
    )


async def _persisted_message_stream_events(
    request: MessageRequest,
    agent: ToolAgent | DagAgent | AutoAgent,
    input_uploads: list[ArtifactUpload],
    context: PersistedMessageContext,
    stream_id: str,
    lock: Any,
):
    try:
        runner = state.get_runner()
        message_projection = await ConversationMessageProjection.start_for_message_request(request, context)
        event_source = gate_chat_display(
            runner.stream(
                agent,
                messages=request.messages,
                state=context.run_state,
                workspace_path=context.workspace_path,
                input_uploads=input_uploads,
            ),
            validation_enabled=runner.enable_validation,
        )
        async for payload in _persisted_run_events(
            event_source,
            runner=runner,
            context=context,
            stream_id=stream_id,
            run_kind=context.run_state.kind if context.run_state is not None else request.target,
            create_run=context.run_state is None,
            existing_run_id=None if context.run_state is None else context.run_state.run_id,
            message_projection=message_projection,
        ):
            yield _sse(payload)
    finally:
        await run_in_threadpool(lock.release)


async def _persisted_review_resume_stream_events(
    decision: ReviewDecision,
    run_state: RunState,
    context: PersistedMessageContext,
    stream_id: str,
    *,
    message_projection: ConversationMessageProjection | None = None,
    decision_json: str | None = None,
):
    runner = state.get_runner()
    event_source = gate_chat_display(
        runner.resume_stream(decision, state=run_state),
        validation_enabled=runner.enable_validation,
    )
    async for payload in _persisted_run_events(
        event_source,
        runner=runner,
        context=context,
        stream_id=stream_id,
        run_kind=run_state.kind,
        create_run=False,
        existing_run_id=run_state.run_id,
        resolve_review_id=decision.review_id,
        decision_json=decision_json or _review_decision_json(decision),
        message_projection=message_projection,
    ):
        yield _sse(payload)


class _LockReleasingAsyncIterator:
    def __init__(self, source: AsyncIterator[Any], lock: Any) -> None:
        self._source = source
        self._lock = lock
        self._released = False

    def __aiter__(self) -> "_LockReleasingAsyncIterator":
        return self

    async def __anext__(self) -> Any:
        try:
            return await self._source.__anext__()
        except StopAsyncIteration:
            await self.aclose()
            raise
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        close = getattr(self._source, "aclose", None)
        if callable(close):
            await close()
        if self._released:
            return
        self._released = True
        await run_in_threadpool(self._lock.release)


async def _persist_review_resume_checkpoint(
    store: Store,
    *,
    run: Run,
    run_state: RunState,
    stream_id: str,
    review_id: str,
    decision_json: str,
    message_projection: ConversationMessageProjection | None,
) -> None:
    checkpoint_state = run_state.model_copy(update={
        "status": "failed",
        "pending_review": None,
        "pending_invocation": None,
    })
    payload = {
        "type": "run.finished",
        "data": {
            "result": {
                "state": checkpoint_state.model_dump(mode="json"),
                "output_text": run.output_text,
            },
        },
        "sequence": 0,
        "run_id": run_state.run_id,
    }
    if message_projection is not None:
        await message_projection.handle_payload(payload)
    await run_in_threadpool(
        store.append_run_event,
        run_id=run_state.run_id,
        stream_id=f"{stream_id}_checkpoint",
        event_type="run.finished",
        payload_json=json.dumps(payload, ensure_ascii=False),
    )
    await run_in_threadpool(
        store.save_run_state,
        run_state.run_id,
        checkpoint_state.model_dump_json(),
        run.output_text,
    )
    await run_in_threadpool(store.update_run_status, run.id, "failed")
    await run_in_threadpool(store.resolve_review, review_id, decision_json)


async def _persisted_static_dag_stream_events(
    dag: UserDAG,
    graph_input: Any,
    saved: SavedDag,
    context: PersistedMessageContext,
    stream_id: str,
    lock: Any,
):
    try:
        runner = state.get_runner()
        event_source = runner.stream(
            _compile_user_dag(dag),
            graph_input=graph_input,
            workspace_path=context.workspace_path,
            artifact_uploads=_saved_dag_artifact_uploads(saved, dag),
        )
        async for payload in _persisted_run_events(
            event_source,
            runner=runner,
            context=context,
            stream_id=stream_id,
            run_kind="static_dag",
            create_run=True,
            saved_dag_id=saved.id,
        ):
            yield _sse(payload)
    finally:
        await run_in_threadpool(lock.release)


async def _persisted_run_events(
    event_source: AsyncIterator[RunStreamEvent],
    *,
    runner: Runner,
    context: PersistedMessageContext,
    stream_id: str,
    run_kind: str,
    create_run: bool,
    existing_run_id: str | None = None,
    resolve_review_id: str | None = None,
    decision_json: str | None = None,
    saved_dag_id: str | None = None,
    message_projection: ConversationMessageProjection | None = None,
) -> AsyncIterator[dict[str, Any]]:
    store = state.get_store()
    sent_error = False
    run_id = existing_run_id
    run_created = existing_run_id is not None
    stream_created = False
    terminal_event_persisted = False
    try:
        async for event in event_source:
            payload = _chat_stream_event_payload(event, runner)
            if event.run_id is not None and run_id is None:
                run_id = event.run_id
            if event.type == "run.started":
                run_kind = str(getattr(event.data, "kind", run_kind) or run_kind)
                if run_id is not None and create_run and not run_created:
                    await run_in_threadpool(
                        store.create_run,
                        run_id=run_id,
                        project_id=context.project_id,
                        conversation_id=context.conversation_id,
                        user_id="default",
                        kind=run_kind,
                        status="running",
                        workspace_uri=context.workspace_uri,
                        saved_dag_id=saved_dag_id,
                    )
                    run_created = True
                if run_id is not None and run_created and not stream_created:
                    await run_in_threadpool(
                        store.create_run_stream,
                        stream_id=stream_id,
                        run_id=run_id,
                        project_id=context.project_id,
                        conversation_id=context.conversation_id,
                        user_id="default",
                        kind=run_kind,
                        status="running",
                    )
                    await run_in_threadpool(store.update_run_status, run_id, "running", started_at=int(time.time()))
                    stream_created = True
            if event.type == "run.failed":
                sent_error = True
            if message_projection is not None:
                await message_projection.handle_payload(payload)
            if event.type == "dag.updated" and context.orchestration_session_id is not None:
                data = payload.get("data")
                if isinstance(data, dict) and isinstance(data.get("dag"), dict):
                    await run_in_threadpool(
                        store.update_orchestration_session,
                        context.orchestration_session_id,
                        draft_dag_json=json.dumps(data["dag"], ensure_ascii=False),
                        update_draft_dag=True,
                    )
            if run_id is not None and run_created:
                persisted = await run_in_threadpool(
                    store.append_run_event,
                    run_id=run_id,
                    stream_id=stream_id,
                    event_type=event.type,
                    payload_json=json.dumps(payload, ensure_ascii=False),
                )
                payload["stream_sequence"] = payload.get("sequence", event.sequence)
                payload["sequence"] = persisted.event_id
            if event.type == "run.failed" and run_id is not None and run_created:
                error_json = json.dumps(payload.get("data") or {}, ensure_ascii=False)
                await run_in_threadpool(
                    store.save_run_error,
                    run_id,
                    error_json,
                )
                if stream_created:
                    await run_in_threadpool(
                        store.finish_run_stream,
                        stream_id,
                        "failed",
                        error_json=error_json,
                        completed_at=int(time.time()),
                    )
                terminal_event_persisted = True
            if event.type == "run.finished":
                result = getattr(event.data, "result", None)
                if result is not None and run_id is not None:
                    completed_at = int(time.time())
                    if context.orchestration_session_id is not None and result.state.dag is not None:
                        await run_in_threadpool(
                            store.update_orchestration_session,
                            context.orchestration_session_id,
                            draft_dag_json=json.dumps(result.state.dag.model_dump(mode="json"), ensure_ascii=False),
                            update_draft_dag=True,
                        )
                    await run_in_threadpool(
                        store.save_run_state,
                        run_id,
                        result.state.model_dump_json(),
                        result.output_text,
                    )
                    await run_in_threadpool(
                        store.update_run_status,
                        run_id,
                        result.status,
                        completed_at=completed_at if result.status != "awaiting_review" else None,
                    )
                    if stream_created:
                        await run_in_threadpool(
                            store.finish_run_stream,
                            stream_id,
                            result.status,
                            completed_at=completed_at,
                        )
                    if resolve_review_id is not None:
                        await run_in_threadpool(
                            store.resolve_review,
                            resolve_review_id,
                            decision_json or "{}",
                        )
                    if result.pending_review is not None:
                        await run_in_threadpool(
                            store.upsert_review,
                            review_id=result.pending_review.review_id,
                            run_id=run_id,
                            project_id=context.project_id,
                            kind=result.pending_review.kind,
                        )
                    terminal_event_persisted = True
            yield payload
    except asyncio.CancelledError:
        if not terminal_event_persisted:
            await _persist_interrupted_run(
                store,
                run_id=run_id,
                run_created=run_created,
                stream_created=stream_created,
                stream_id=stream_id,
                resolve_review_id=resolve_review_id,
                decision_json=decision_json,
                message_projection=message_projection,
            )
        raise
    except Exception as exc:
        if run_id is not None and run_created:
            error_payload = {
                "type": "run.failed",
                "data": {"message": str(exc), "error_type": type(exc).__name__},
                "sequence": 0,
                "run_id": run_id,
            }
            if message_projection is not None:
                await message_projection.handle_payload(error_payload)
            persisted = await run_in_threadpool(
                store.append_run_event,
                run_id=run_id,
                stream_id=stream_id,
                event_type="run.failed",
                payload_json=json.dumps(error_payload, ensure_ascii=False),
            )
            error_payload["sequence"] = persisted.event_id
            await run_in_threadpool(
                store.save_run_error,
                run_id,
                json.dumps(error_payload["data"], ensure_ascii=False),
            )
            if stream_created:
                await run_in_threadpool(
                    store.finish_run_stream,
                    stream_id,
                    "failed",
                    error_json=json.dumps(error_payload["data"], ensure_ascii=False),
                    completed_at=int(time.time()),
                )
            if not sent_error:
                yield error_payload
        elif not sent_error:
            error_payload = {
                "type": "run.failed",
                "data": {"message": str(exc), "error_type": type(exc).__name__},
                "sequence": 0,
                "run_id": None,
            }
            if message_projection is not None:
                await message_projection.handle_payload(error_payload)
            yield error_payload


async def _persist_interrupted_run(
    store: Store,
    *,
    run_id: str | None,
    run_created: bool,
    stream_created: bool,
    stream_id: str,
    resolve_review_id: str | None,
    decision_json: str | None,
    message_projection: ConversationMessageProjection | None,
) -> None:
    if run_id is None or not run_created:
        return
    run = await run_in_threadpool(store.get_run, run_id)
    if run is None:
        return
    if run.status not in {"queued", "running", "awaiting_review"}:
        if resolve_review_id is not None:
            await run_in_threadpool(store.resolve_review, resolve_review_id, decision_json or "{}")
        return
    completed_at = int(time.time())
    error_payload = {
        "type": "run.failed",
        "data": {
            "message": "Stream interrupted by client.",
            "error_type": "ClientDisconnect",
        },
        "sequence": 0,
        "run_id": run_id,
    }
    if message_projection is not None:
        await message_projection.handle_payload(error_payload)
    persisted = await run_in_threadpool(
        store.append_run_event,
        run_id=run_id,
        stream_id=stream_id,
        event_type="run.failed",
        payload_json=json.dumps(error_payload, ensure_ascii=False),
    )
    error_payload["sequence"] = persisted.event_id
    error_json = json.dumps(error_payload["data"], ensure_ascii=False)
    stored_state = await run_in_threadpool(store.get_run_state, run_id)
    if stored_state is not None:
        interrupted_state = stored_state.model_copy(update={
            "status": "failed",
            "pending_review": None,
            "pending_invocation": None,
        })
        await run_in_threadpool(
            store.save_run_state,
            run_id,
            interrupted_state.model_dump_json(),
            run.output_text,
        )
    await run_in_threadpool(store.save_run_error, run_id, error_json)
    await run_in_threadpool(store.update_run_status, run_id, "failed", completed_at=completed_at)
    if stream_created:
        await run_in_threadpool(
            store.finish_run_stream,
            stream_id,
            "failed",
            error_json=error_json,
            completed_at=completed_at,
        )
    if resolve_review_id is not None:
        await run_in_threadpool(store.resolve_review, resolve_review_id, decision_json or "{}")


async def _message_request_from_http(http_request: Request) -> tuple[MessageRequest, list[ArtifactUpload]]:
    content_type = http_request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/form-data"):
        form = await http_request.form()
        payload = form.get("payload")
        if not isinstance(payload, str):
            raise HTTPException(status_code=422, detail="Multipart message requests require a JSON payload field.")
        try:
            raw_request = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Multipart message payload must be valid JSON.") from exc
        return _validate_message_request(raw_request), await _artifact_uploads_from_form_files(form.getlist("files"))

    try:
        raw_request = await http_request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Message request body must be valid JSON.") from exc
    return _validate_message_request(raw_request), []


def _validate_message_request(raw_request: Any) -> MessageRequest:
    try:
        return MessageRequest.model_validate(raw_request)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


async def _artifact_uploads_from_form_files(files: list[Any]) -> list[ArtifactUpload]:
    uploads: list[ArtifactUpload] = []
    for file in files:
        read = getattr(file, "read", None)
        if not callable(read):
            raise HTTPException(status_code=400, detail="Upload entries must be file parts.")
        filename = str(getattr(file, "filename", "") or "upload")
        try:
            validate_upload_filename(filename)
        except ArtifactPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        content = await read()
        uploads.append(ArtifactUpload(filename=filename, content=content))
    return uploads


def _workspace_root_from_message(request: MessageRequest) -> str:
    if request.workspace_root is None or not request.workspace_root.strip():
        return DEFAULT_RUNS_DIR
    return _clean_workspace_root(request.workspace_root)


async def _persisted_context_from_message(request: MessageRequest) -> PersistedMessageContext | None:
    if request.project_id is None and request.conversation_id is None:
        return None
    if request.conversation_id is None:
        raise HTTPException(
            status_code=400,
            detail="conversation_id must be provided for persisted message streams.",
        )
    if request.state is not None:
        raise HTTPException(status_code=400, detail="Persisted message streams do not accept client state.")
    if request.workspace_root is not None:
        raise HTTPException(status_code=400, detail="Persisted message streams do not accept workspace_root.")
    expected_kind = "dynamic_dag" if request.target == "dag" else "chat"
    return await _persisted_context_from_conversation(
        request.project_id,
        request.conversation_id,
        expected_kind=expected_kind,
    )


async def _persisted_context_from_conversation(
    project_id: str | None,
    conversation_id: str,
    *,
    expected_kind: Literal["chat", "dynamic_dag", "static_dag"] | None = None,
    include_orchestration_session: bool = True,
) -> PersistedMessageContext:
    store = state.get_store()
    project = None
    if project_id is not None:
        project = await run_in_threadpool(store.get_project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
    conversation = await run_in_threadpool(store.get_conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if expected_kind is not None and conversation.kind != expected_kind:
        if expected_kind == "chat":
            detail = "Chat streams require a chat conversation."
        elif expected_kind == "dynamic_dag":
            detail = "Dynamic DAG streams require a dynamic DAG conversation."
        else:
            detail = "Static DAG runs require a static DAG conversation."
        raise HTTPException(status_code=400, detail=detail)
    if project is None and conversation.project_id is not None:
        raise HTTPException(status_code=400, detail="project_id must be provided for project conversations.")
    if project is not None and conversation.project_id != project.id:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if conversation.project_id is not None:
        if project is None:
            project = await run_in_threadpool(store.get_project, conversation.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
        workspace_uri = project.workspace_uri
    else:
        workspace_uri = conversation.workspace_uri
    workspace_path = await run_in_threadpool(state.get_workspaces().local_path_for, workspace_uri)
    orchestration_session = None
    if include_orchestration_session and conversation.kind != "chat":
        orchestration_session = await run_in_threadpool(
            store.get_orchestration_session_by_conversation,
            conversation.id,
        )
    orchestration_surface = _orchestration_session_surface(orchestration_session)
    previous_run_state = None
    if conversation.last_run_id is not None:
        stored_run_state = await run_in_threadpool(store.get_run_state, conversation.last_run_id)
        if stored_run_state is not None and stored_run_state.status == "awaiting_review":
            raise HTTPException(
                status_code=409,
                detail="Conversation is awaiting review; resume the pending review before sending a new message.",
            )
        if not (
            conversation.kind == "dynamic_dag"
            and orchestration_surface == ORCHESTRATION_WORKSPACE_SURFACE
        ):
            previous_run_state = stored_run_state
    return PersistedMessageContext(
        project_id=conversation.project_id,
        conversation_id=conversation.id,
        conversation_kind=conversation.kind,
        workspace_uri=workspace_uri,
        workspace_path=workspace_path,
        run_state=previous_run_state,
        orchestration_session_id=None if orchestration_session is None else orchestration_session.id,
        orchestration_surface=orchestration_surface,
    )


def _clean_workspace_root(value: str) -> str:
    root = value.strip()
    if root.startswith("~"):
        raise HTTPException(status_code=400, detail="workspace_root cannot use '~' expansion.")
    path = Path(root)
    if not path.is_absolute() and (".." in path.parts or ".." in PureWindowsPath(root).parts):
        raise HTTPException(status_code=400, detail="workspace_root cannot contain '..' in a relative path.")
    return root


def _new_api_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _clean_required_text(value: str, *, field: str) -> str:
    text = value.strip()
    if not text:
        raise HTTPException(status_code=400, detail=f"{field} cannot be empty.")
    return text


def _clean_project_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip().lower()).strip("-_")
    if not slug:
        raise HTTPException(status_code=400, detail="Project slug cannot be empty.")
    if _PROJECT_SLUG_RE.fullmatch(slug) is None:
        raise HTTPException(
            status_code=400,
            detail="Project slug may contain only letters, numbers, hyphens, and underscores.",
        )
    return slug


@app.post("/messages/resume")
async def resume_message_stream(request: ResumeReviewRequest) -> StreamingResponse:
    async def events():
        decision = ReviewDecision(
            review_id=request.review_id,
            approved=request.approved,
            dag=request.dag,
            review_level=request.review_level,
            feedback=request.feedback,
        )
        sent_error = False
        try:
            runner = state.get_runner()
            async for event in gate_chat_display(
                runner.resume_stream(decision, state=request.state),
                validation_enabled=runner.enable_validation,
            ):
                if event.type == "run.failed":
                    sent_error = True
                yield _sse(_chat_stream_event_payload(event, runner))
        except Exception as exc:
            if not sent_error:
                yield _sse({
                    "type": "run.failed",
                    "data": {"message": str(exc), "error_type": type(exc).__name__},
                    "sequence": 0,
                    "run_id": None,
                })

    return StreamingResponse(events(), media_type="text/event-stream")


async def _run_state_from_state(run_id: str) -> RunState | None:
    run_state = await run_in_threadpool(state.get_store().get_run_state, run_id)
    if run_state is not None:
        return run_state
    if state.runner is None:
        return None
    return state.runner.run_state(run_id)


async def _dag_run_from_state(run_id: str) -> DAGRun | None:
    run_state = await _run_state_from_state(run_id)
    if (
        run_state is None
        or run_state.kind != "static_dag"
        or run_state.dag is None
        or run_state.trace is None
    ):
        return None
    return DAGRun(
        run_id=run_state.run_id,
        spec_id=run_state.spec_id,
        workspace_path=run_state.workspace_path or "",
        dag=run_state.dag,
        trace=run_state.trace,
    )


def _run_artifacts_response(run_state: RunState) -> RunArtifactsResponse:
    artifact_states = {
        artifact_id: artifact_state.model_dump(mode="json")
        for artifact_id, artifact_state in (run_state.trace.artifacts if run_state.trace else {}).items()
    }
    files, files_truncated = _run_artifact_files(
        run_state,
        onlyoffice_config=_configured_onlyoffice_config(),
    )
    return RunArtifactsResponse(
        run_id=run_state.run_id,
        workspace_path=run_state.workspace_path,
        artifacts=artifact_states,
        files=files,
        files_truncated=files_truncated,
    )


def _run_artifact_files(
    run_state: RunState,
    *,
    onlyoffice_config: UserOnlyOfficeConfig | None,
) -> tuple[list[RunArtifactFile], bool]:
    workspace = _run_workspace(run_state)
    if workspace is None:
        return [], False
    declared_paths: set[str] = set()
    files: list[RunArtifactFile] = []
    artifact_states = run_state.trace.artifacts if run_state.trace else {}
    for artifact_id, artifact_state in sorted(artifact_states.items()):
        for path in artifact_state.paths:
            normalized_path = _normalize_run_artifact_path(path)
            declared_paths.add(normalized_path)
            files.append(
                _run_artifact_file(
                    run_id=run_state.run_id,
                    workspace=workspace,
                    path=normalized_path,
                    artifact_id=artifact_id,
                    source="dag_artifact",
                    status=artifact_state.status,
                    error=artifact_state.error,
                    onlyoffice_config=onlyoffice_config,
                )
            )

    workspace_paths, files_truncated = _workspace_file_paths(workspace, exclude=declared_paths)
    for path in workspace_paths:
        files.append(
            _run_artifact_file(
                run_id=run_state.run_id,
                workspace=workspace,
                path=path,
                artifact_id=None,
                source="run_file",
                status="created",
                error=None,
                onlyoffice_config=onlyoffice_config,
            )
        )
    return sorted(files, key=lambda item: (item.source, item.path, item.id)), files_truncated


def _run_artifact_file(
    *,
    run_id: str,
    workspace: Path,
    path: str,
    artifact_id: str | None,
    source: RunArtifactSource,
    status: str,
    error: str | None,
    onlyoffice_config: UserOnlyOfficeConfig | None,
) -> RunArtifactFile:
    file_path = _resolve_workspace_path(workspace, path)
    preview_kind = _preview_kind_for_path(path)
    size: int | None = None
    version: str | None = None
    path_error = error
    previewable = False
    if file_path is None:
        path_error = path_error or "Artifact path escapes run workspace."
    elif file_path.is_file():
        file_stat = file_path.stat()
        size = file_stat.st_size
        version = _file_version(file_stat)
        previewable = preview_kind is not None and (
            preview_kind in _BROWSER_PREVIEW_EXTENSIONS.values()
            or preview_kind in _TEXT_PREVIEW_KINDS
        )
    elif path_error is None and status == "created":
        path_error = "Artifact file not found."
    return RunArtifactFile(
        id=_run_artifact_file_id(source=source, artifact_id=artifact_id, path=path),
        artifact_id=artifact_id,
        source=source,
        path=path,
        name=Path(path).name,
        media_type=_media_type_for_path(path),
        preview_kind=preview_kind if previewable else None,
        previewable=previewable,
        size=size,
        version=version,
        status=status,
        error=path_error,
        preview_url=_preview_url(run_id, path) if previewable and preview_kind in _TEXT_PREVIEW_KINDS else None,
        download_url=_download_url(run_id, path) if file_path is not None and file_path.is_file() else None,
        onlyoffice_config_url=(
            _onlyoffice_config_url(run_id, path)
            if previewable and _onlyoffice_document_type(preview_kind) is not None and _onlyoffice_is_configured(onlyoffice_config)
            else None
        ),
    )


def _workspace_file_paths(workspace: Path, *, exclude: set[str]) -> tuple[list[str], bool]:
    paths: list[str] = []
    directories = [workspace]
    visited = 0
    truncated = False
    while directories:
        directory = directories.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for candidate in entries:
            visited += 1
            if visited > RUN_ARTIFACT_SCAN_VISIT_LIMIT:
                return paths, True
            try:
                resolved = candidate.resolve()
                resolved.relative_to(workspace)
            except (OSError, ValueError):
                continue
            if candidate.is_dir() and not candidate.is_symlink():
                directories.append(candidate)
                continue
            if not resolved.is_file():
                continue
            relative_path = candidate.relative_to(workspace).as_posix()
            if relative_path in exclude:
                continue
            paths.append(relative_path)
            if len(paths) >= RUN_ARTIFACT_SCAN_LIMIT:
                truncated = True
                return paths, truncated
    return paths, truncated


def _run_workspace(run_state: RunState) -> Path | None:
    if not run_state.workspace_path:
        return None
    return Path(run_state.workspace_path).resolve()


def _delete_conversation_files(
    run_states: list[RunState],
    conversation_workspace: Path,
    *,
    delete_conversation_workspace: bool,
) -> None:
    conversation_workspace = conversation_workspace.resolve()
    for run_state in run_states:
        if not run_state.workspace_path:
            continue
        candidate = Path(run_state.workspace_path).resolve()
        if candidate == conversation_workspace:
            continue
        if not _should_delete_run_workspace(candidate, conversation_workspace):
            continue
        shutil.rmtree(candidate)
    if delete_conversation_workspace:
        _delete_workspace_root(conversation_workspace)


def _delete_run_files(run: Run, run_state: RunState | None) -> None:
    if run_state is None or not run_state.workspace_path:
        return
    try:
        parent_workspace = state.get_workspaces().local_path_for_existing(run.workspace_uri).resolve()
    except ValueError:
        return
    candidate = Path(run_state.workspace_path).resolve()
    if candidate == parent_workspace:
        return
    if not _should_delete_run_workspace(candidate, parent_workspace):
        return
    shutil.rmtree(candidate)


def _delete_workspace_root(workspace_path: Path) -> None:
    workspace_path = workspace_path.resolve()
    target = workspace_path.parent if workspace_path.name == "workspace" else workspace_path
    if target.exists() and target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)


def _should_delete_run_workspace(candidate: Path, project_workspace: Path) -> bool:
    if candidate == project_workspace:
        return False
    if _path_contains(candidate, project_workspace):
        return False
    if not candidate.exists() or candidate.is_symlink() or not candidate.is_dir():
        return False
    name = candidate.name
    if candidate.parent.name == "runs":
        return True
    return name.startswith(("run_", ".run_", "tool_run_", "dag_run_", "task_"))


def _path_contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


async def _project_workspace(project_id: str) -> tuple[Project, Path]:
    project = await run_in_threadpool(state.get_store().get_project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    workspace = await run_in_threadpool(state.get_workspaces().local_path_for, project.workspace_uri)
    return project, workspace.resolve()


def _delete_project_workspace(project: Project, workspace_path: Path) -> None:
    workspace_path = workspace_path.resolve()
    project_root = workspace_path.parent if workspace_path.name == "workspace" else workspace_path
    if project_root.name != project.id:
        project_root = workspace_path
    if project_root.exists() and project_root.is_dir() and not project_root.is_symlink():
        shutil.rmtree(project_root)


def _project_file_items(
    project_id: str,
    workspace: Path,
    directory: Path,
    onlyoffice_config: UserOnlyOfficeConfig | None,
) -> list[ProjectFileItem]:
    items: list[ProjectFileItem] = []
    for candidate in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        try:
            items.append(_project_file_item(
                project_id,
                workspace,
                candidate,
                onlyoffice_config=onlyoffice_config,
            ))
        except HTTPException:
            continue
    return items


def _project_file_tree_items(
    project_id: str,
    workspace: Path,
    directory: Path,
    onlyoffice_config: UserOnlyOfficeConfig | None,
    visited: set[Path] | None = None,
) -> list[ProjectFileTreeItem]:
    items: list[ProjectFileTreeItem] = []
    seen = set(visited or set())
    seen.add(directory.resolve())
    for candidate in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        try:
            item = _project_file_item(
                project_id,
                workspace,
                candidate,
                onlyoffice_config=onlyoffice_config,
            )
            resolved_candidate = candidate.resolve()
            children = (
                _project_file_tree_items(
                    project_id,
                    workspace,
                    candidate,
                    onlyoffice_config,
                    seen | {resolved_candidate},
                )
                if item.kind == "directory" and resolved_candidate not in seen
                else []
            )
            items.append(ProjectFileTreeItem(**item.model_dump(), children=children))
        except HTTPException:
            continue
    return items


def _project_file_item(
    project_id: str,
    workspace: Path,
    path: Path,
    *,
    onlyoffice_config: UserOnlyOfficeConfig | None,
) -> ProjectFileItem:
    workspace = workspace.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(workspace)
        relative_path = path.relative_to(workspace).as_posix()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Project file path escapes workspace.") from exc
    stat = path.stat()
    if path.is_dir():
        return ProjectFileItem(
            path=relative_path,
            name=path.name,
            kind="directory",
            modified_at=int(stat.st_mtime),
        )
    preview_kind = _preview_kind_for_path(relative_path)
    previewable = preview_kind is not None
    return ProjectFileItem(
        path=relative_path,
        name=path.name,
        kind="file",
        media_type=_media_type_for_path(relative_path),
        preview_kind=preview_kind if previewable else None,
        previewable=previewable,
        size=stat.st_size,
        modified_at=int(stat.st_mtime),
        version=_file_version(stat),
        preview_url=(
            _project_file_preview_url(project_id, relative_path)
            if preview_kind in _TEXT_PREVIEW_KINDS
            else None
        ),
        download_url=_project_file_download_url(project_id, relative_path),
        onlyoffice_config_url=(
            _project_file_onlyoffice_config_url(project_id, relative_path)
            if _onlyoffice_document_type(preview_kind) is not None
            and _onlyoffice_is_configured(onlyoffice_config)
            else None
        ),
    )


def _resolve_project_file_path(workspace: Path, path: str, *, allow_empty: bool = False) -> Path:
    normalized_path = _normalize_project_file_path(path, allow_empty=allow_empty)
    workspace = workspace.resolve()
    candidate = workspace if not normalized_path else workspace / normalized_path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Project file path escapes workspace.") from exc
    return candidate


def _normalize_project_file_path(path: str, *, allow_empty: bool = False) -> str:
    raw_path = path.strip().replace("\\", "/")
    if not raw_path:
        if allow_empty:
            return ""
        raise HTTPException(status_code=400, detail="Project file path is required.")
    windows_path = PureWindowsPath(raw_path)
    if raw_path.startswith("/") or Path(raw_path).is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise HTTPException(status_code=400, detail="Project file path must be relative.")
    parts = [part for part in raw_path.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise HTTPException(status_code=400, detail="Project file path cannot contain '..'.")
    normalized_path = "/".join(parts)
    if not normalized_path and not allow_empty:
        raise HTTPException(status_code=400, detail="Project file path is required.")
    return normalized_path


def _project_file_preview_url(project_id: str, path: str) -> str:
    return f"/projects/{quote(project_id, safe='')}/files/preview?path={quote(path, safe='/')}"


def _project_file_download_url(project_id: str, path: str) -> str:
    return f"/projects/{quote(project_id, safe='')}/files/download?path={quote(path, safe='/')}"


def _project_file_onlyoffice_config_url(project_id: str, path: str) -> str:
    return f"/projects/{quote(project_id, safe='')}/files/onlyoffice/config?path={quote(path, safe='')}"


def _resolve_run_artifact_path(run_state: RunState, path: str) -> Path:
    workspace = _run_workspace(run_state)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Run workspace not found.")
    try:
        normalized_path = _normalize_run_artifact_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    file_path = _resolve_workspace_path(workspace, normalized_path)
    if file_path is None:
        raise HTTPException(status_code=400, detail="Artifact path escapes run workspace.")
    return file_path


def _resolve_workspace_path(workspace: Path, path: str) -> Path | None:
    try:
        normalized_path = _normalize_run_artifact_path(path)
    except ValueError:
        return None
    if not _is_safe_relative_artifact_path(normalized_path):
        return None
    resolved = (workspace / normalized_path).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError:
        return None
    return resolved


def _normalize_run_artifact_path(path: str) -> str:
    normalized_path = path.strip().replace("\\", "/")
    if not normalized_path:
        raise ValueError("Artifact path is required.")
    return normalized_path


def _is_safe_relative_artifact_path(path: str) -> bool:
    path_obj = Path(path)
    windows_path = PureWindowsPath(path)
    if path_obj.is_absolute() or windows_path.is_absolute():
        return False
    return ".." not in path_obj.parts and ".." not in windows_path.parts


def _run_artifact_file_id(
    *,
    source: RunArtifactSource,
    artifact_id: str | None,
    path: str,
) -> str:
    if source == "dag_artifact":
        return f"dag:{artifact_id}:{path}"
    return f"run:{path}"


def _preview_url(run_id: str, path: str) -> str:
    return f"/runs/{quote(run_id, safe='')}/artifacts/preview?path={quote(path, safe='')}"


def _download_url(run_id: str, path: str) -> str:
    return f"/runs/{quote(run_id, safe='')}/artifacts/download?path={quote(path, safe='')}"


def _onlyoffice_config_url(run_id: str, path: str) -> str:
    return f"/runs/{quote(run_id, safe='')}/artifacts/onlyoffice/config?path={quote(path, safe='')}"


def _onlyoffice_config_response(run_state: RunState, path: str) -> RunArtifactOnlyOfficeConfigResponse:
    try:
        normalized_path = _normalize_run_artifact_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    file_path = _resolve_run_artifact_path(run_state, normalized_path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Artifact file not found.")
    return _onlyoffice_config_response_for_file(
        scope="run",
        owner_id=run_state.run_id,
        normalized_path=normalized_path,
        file_path=file_path,
    )


def _project_file_onlyoffice_config_response(
    project: Project,
    workspace: Path,
    path: str,
) -> RunArtifactOnlyOfficeConfigResponse:
    file_path = _resolve_project_file_path(workspace, path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Project file not found.")
    normalized_path = _normalize_project_file_path(path)
    return _onlyoffice_config_response_for_file(
        scope="project",
        owner_id=project.id,
        normalized_path=normalized_path,
        file_path=file_path,
    )


def _onlyoffice_config_response_for_file(
    *,
    scope: Literal["run", "project"],
    owner_id: str,
    normalized_path: str,
    file_path: Path,
) -> RunArtifactOnlyOfficeConfigResponse:
    onlyoffice_config = _configured_onlyoffice_config()
    if not _onlyoffice_is_configured(onlyoffice_config):
        raise HTTPException(status_code=404, detail="OnlyOffice preview is not configured.")
    preview_kind = _preview_kind_for_path(normalized_path)
    document_type = _onlyoffice_document_type(preview_kind)
    if document_type is None:
        raise HTTPException(status_code=415, detail="File type is not supported by OnlyOffice preview.")

    document_server_url = _clean_onlyoffice_url(onlyoffice_config.document_server_url)
    public_api_base = _clean_onlyoffice_url(onlyoffice_config.public_api_base)
    editable = (
        scope == "project" and onlyoffice_config.project_file_edit_enabled
    ) or (
        scope == "run" and onlyoffice_config.run_artifact_edit_enabled
    )
    file_token = (
        _onlyoffice_file_token(run_id=owner_id, path=normalized_path, editable=editable)
        if scope == "run"
        else _onlyoffice_file_token(project_id=owner_id, path=normalized_path, editable=editable)
    )
    file_type = Path(normalized_path).suffix.lower().lstrip(".")
    editor_config: dict[str, Any] = {
        "documentType": document_type,
        "type": "desktop",
        "document": {
            "fileType": file_type,
            "key": _onlyoffice_document_key(f"{scope}:{owner_id}", normalized_path, file_path),
            "title": file_path.name,
            "url": f"{public_api_base}/onlyoffice/files/{file_token}",
            "permissions": {
                "chat": False,
                "comment": False,
                "download": True,
                "edit": editable,
                "fillForms": False,
                "modifyContentControl": False,
                "modifyFilter": False,
                "print": True,
                "protect": False,
                "review": False,
            },
        },
        "editorConfig": {
            "callbackUrl": f"{public_api_base}/onlyoffice/callback/{file_token}",
            "coEditing": {
                "change": False,
                "mode": "strict",
            },
            "customization": {
                "autosave": False,
                "forcesave": editable,
                "macros": False,
                "macrosMode": "disable",
                "plugins": False,
            },
            "lang": onlyoffice_config.lang,
            "mode": "edit" if editable else "view",
        },
    }
    jwt_secret = _clean_optional_text(onlyoffice_config.jwt_secret)
    if jwt_secret:
        editor_config["token"] = _onlyoffice_jwt_token(editor_config, jwt_secret)
    return RunArtifactOnlyOfficeConfigResponse(
        document_server_url=document_server_url,
        script_url=f"{document_server_url}/web-apps/apps/api/documents/api.js",
        config=editor_config,
    )


def _configured_onlyoffice_config() -> UserOnlyOfficeConfig | None:
    return state._current_user_config().onlyoffice


def _onlyoffice_is_configured(config: UserOnlyOfficeConfig | None) -> bool:
    return bool(
        config
        and config.enabled
        and _clean_onlyoffice_url(config.document_server_url)
        and _clean_onlyoffice_url(config.public_api_base)
    )


def _clean_onlyoffice_url(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")


def _onlyoffice_document_type(
    preview_kind: RunArtifactPreviewKind | None,
) -> Literal["word", "cell", "slide"] | None:
    if preview_kind not in _ONLYOFFICE_DOCUMENT_TYPES:
        return None
    return _ONLYOFFICE_DOCUMENT_TYPES[preview_kind]


def _onlyoffice_document_key(run_id: str, path: str, file_path: Path) -> str:
    file_stat = file_path.stat()
    raw_key = f"{run_id}\0{path}\0{_file_version(file_stat)}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:48]


def _file_version(file_stat: Any) -> str:
    return f"{file_stat.st_size}:{file_stat.st_mtime_ns}"


def _onlyoffice_jwt_token(payload: dict[str, Any], secret: str) -> str:
    header_segment = _base64url_json({"alg": "HS256", "typ": "JWT"})
    payload_segment = _base64url_json(payload)
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_segment}.{payload_segment}.{_base64url_bytes(signature)}"


def _base64url_json(value: dict[str, Any]) -> str:
    return _base64url_bytes(
        json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )


def _base64url_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _onlyoffice_file_token(
    *,
    path: str,
    run_id: str | None = None,
    project_id: str | None = None,
    editable: bool = False,
) -> str:
    if (run_id is None) == (project_id is None):
        raise ValueError("OnlyOffice file token requires exactly one owner.")
    payload = {
        "editable": editable,
        "exp": int(time.time()) + (ONLYOFFICE_EDIT_TOKEN_SECONDS if editable else ONLYOFFICE_TOKEN_SECONDS),
        "path": path,
    }
    if run_id is not None:
        payload.update({"run_id": run_id, "scope": "run"})
    else:
        payload.update({"project_id": project_id, "scope": "project"})
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(state.onlyoffice_token_secret, raw, hashlib.sha256).digest()
    return f"{_base64_url_encode(raw)}.{_base64_url_encode(signature)}"


def _onlyoffice_token_payload(token: str) -> dict[str, Any]:
    try:
        raw_value, signature_value = token.split(".", 1)
        raw = _base64_url_decode(raw_value)
        signature = _base64_url_decode(signature_value)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=403, detail="Invalid OnlyOffice file token.") from exc
    expected = hmac.new(state.onlyoffice_token_secret, raw, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=403, detail="Invalid OnlyOffice file token.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=403, detail="Invalid OnlyOffice file token.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=403, detail="Invalid OnlyOffice file token.")
    try:
        expires_at = int(payload.get("exp") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Invalid OnlyOffice file token.") from exc
    if expires_at < int(time.time()):
        raise HTTPException(status_code=403, detail="OnlyOffice file token expired.")
    scope = str(payload.get("scope") or ("run" if payload.get("run_id") else ""))
    path = str(payload.get("path") or "")
    editable = payload.get("editable", False)
    if not isinstance(editable, bool):
        raise HTTPException(status_code=403, detail="Invalid OnlyOffice file token.")
    if scope not in {"run", "project"} or not path:
        raise HTTPException(status_code=403, detail="Invalid OnlyOffice file token.")
    if scope == "project":
        owner_key = "project_id"
        owner_id = str(payload.get("project_id") or "")
        try:
            normalized_path = _normalize_project_file_path(path)
        except HTTPException as exc:
            raise HTTPException(status_code=403, detail="Invalid OnlyOffice file token.") from exc
    else:
        owner_key = "run_id"
        owner_id = str(payload.get("run_id") or "")
        try:
            normalized_path = _normalize_run_artifact_path(path)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="Invalid OnlyOffice file token.") from exc
        if not _is_safe_relative_artifact_path(normalized_path):
            raise HTTPException(status_code=403, detail="Invalid OnlyOffice file token.")
    if not owner_id:
        raise HTTPException(status_code=403, detail="Invalid OnlyOffice file token.")
    if _onlyoffice_document_type(_preview_kind_for_path(normalized_path)) is None:
        raise HTTPException(status_code=403, detail="Invalid OnlyOffice file token.")
    return {"scope": scope, owner_key: owner_id, "path": normalized_path, "editable": editable}


async def _onlyoffice_file_path_for_payload(payload: dict[str, Any]) -> tuple[Path, str]:
    if payload["scope"] == "project":
        _project, workspace = await _project_workspace(payload["project_id"])
        return _resolve_project_file_path(workspace, payload["path"]), "Project file not found."
    run_state = await _run_state_from_state(payload["run_id"])
    if run_state is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return _resolve_run_artifact_path(run_state, payload["path"]), "Artifact file not found."


async def _download_onlyoffice_callback_file(url: str) -> bytes:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"OnlyOffice edited file download failed: {exc}") from exc


def _replace_file_bytes(path: Path, content: bytes) -> None:
    temp_path = path.with_name(f".{path.name}.onlyoffice-{uuid4().hex}.tmp")
    try:
        temp_path.write_bytes(content)
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _base64_url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64_url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def _preview_kind_for_path(path: str) -> RunArtifactPreviewKind | None:
    name = Path(path).name
    suffix = Path(path).suffix.lower()
    if suffix in _MARKDOWN_EXTENSIONS or name.upper() == "README":
        return "markdown"
    if suffix in _CODE_EXTENSIONS or name in _CODE_FILENAMES:
        return "code"
    if suffix in _TEXT_EXTENSIONS:
        return "text"
    if suffix in _BROWSER_PREVIEW_EXTENSIONS:
        return _BROWSER_PREVIEW_EXTENSIONS[suffix]
    return None


def _media_type_for_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in _MEDIA_TYPE_OVERRIDES:
        return _MEDIA_TYPE_OVERRIDES[suffix]
    guessed_type = mimetypes.guess_type(path)[0]
    if guessed_type is not None:
        return guessed_type
    return "text/plain" if _preview_kind_for_path(path) is not None else "application/octet-stream"


def _read_text_preview(path: Path) -> tuple[str, bool, int]:
    try:
        size = path.stat().st_size
        with path.open("rb") as file:
            content = file.read(RUN_ARTIFACT_PREVIEW_BYTES + 1)
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Artifact file not found.") from exc
    truncated = len(content) > RUN_ARTIFACT_PREVIEW_BYTES
    content = content[:RUN_ARTIFACT_PREVIEW_BYTES]
    if b"\x00" in content:
        raise HTTPException(status_code=415, detail="Artifact file is not text.")
    try:
        text = _decode_utf8_preview(content, truncated=truncated)
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=415, detail="Artifact file is not UTF-8 text.") from exc
    return text, truncated, size


def _decode_utf8_preview(content: bytes, *, truncated: bool) -> str:
    if not truncated:
        return content.decode("utf-8")
    decoder = codecs.getincrementaldecoder("utf-8")()
    return decoder.decode(content, final=False)


@app.get("/runs/{run_id}/trace")
async def get_run_trace(run_id: str) -> dict[str, Any]:
    run_state = await _run_state_from_state(run_id)
    if run_state is not None and run_state.trace is not None:
        return {"run_id": run_id, "trace": run_state.trace.model_dump(mode="json")}

    raise HTTPException(status_code=404, detail="Run not found.")



def _configured_mcp_server_names() -> set[str]:
    try:
        return set(load_config().mcp_servers)
    except Exception:
        return set()


def _mcp_server_config(request: MCPServerRequest) -> dict[str, Any]:
    config: dict[str, Any] = {
        "transport": request.transport,
        "enabled": request.enabled,
        "risk": request.risk,
        "connect_timeout": request.connect_timeout,
        "tool_timeout": request.tool_timeout,
    }
    if request.transport == "stdio":
        command = request.command.strip()
        if request.enabled and not command:
            raise HTTPException(status_code=400, detail="Enabled stdio MCP servers require a command.")
        config["command"] = command
        config["args"] = [str(arg) for arg in request.args]
        config["env"] = {str(key): str(value) for key, value in request.env.items()}
        if request.cwd:
            config["cwd"] = request.cwd
    else:
        url = request.url.strip()
        if request.enabled and not url:
            raise HTTPException(status_code=400, detail="Enabled HTTP MCP servers require a url.")
        config["url"] = url
        config["headers"] = {str(key): str(value) for key, value in request.headers.items()}
    if request.include_tools:
        config["include_tools"] = [str(tool) for tool in request.include_tools]
    if request.exclude_tools:
        config["exclude_tools"] = [str(tool) for tool in request.exclude_tools]
    return config


def _mcp_storage_config(config: dict[str, Any]) -> dict[str, Any]:
    stored = dict(config)
    for key in ("args", "env", "headers", "include_tools", "exclude_tools"):
        if not stored.get(key):
            stored.pop(key, None)
    if stored.get("cwd") is None:
        stored.pop("cwd", None)
    return stored


def _mcp_server_payloads(runner: Runner) -> list[dict[str, Any]]:
    servers: dict[str, tuple[str, dict[str, Any]]] = {}
    try:
        for name, config in load_config().mcp_servers.items():
            servers[str(name)] = ("config", dict(config))
    except Exception:
        pass
    for name, config in state.custom_mcp_servers.items():
        servers.setdefault(str(name), ("user", dict(config)))
    capability_servers = {
        str(definition.config.get("server"))
        for definition in runner.list_capabilities(kind="mcp")
        if definition.config.get("server")
    }
    for name in capability_servers:
        servers.setdefault(name, ("runtime", {}))
    return [
        _mcp_server_payload(name, source, config, runner)
        for name, (source, config) in sorted(servers.items())
    ]


def _mcp_server_payload(name: str, source: str, config: dict[str, Any], runner: Runner) -> dict[str, Any]:
    tools = [
        definition.model_dump(mode="json")
        for definition in runner.list_capabilities(kind="mcp")
        if definition.config.get("server") == name
    ]
    error = state.custom_mcp_errors.get(name)
    enabled = bool(config.get("enabled", True))
    status = "disabled" if not enabled else "connected" if tools else "error" if error else "pending"
    return {
        "name": name,
        "source": source,
        "config": config,
        "status": status,
        "error": error,
        "tools": tools,
    }


def _clean_name(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail=f"{field} is required.")
    if "/" in text or "\\" in text:
        raise HTTPException(status_code=400, detail=f"{field} cannot contain path separators.")
    if field == "MCP server name":
        if _LOCAL_MCP_SERVER_NAME_RE.fullmatch(text) is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "MCP server name is a local workspace key and may contain only "
                    "letters, numbers, and underscores."
                ),
            )
    return text


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _run_summary_payload(run: Run) -> dict[str, Any]:
    payload = run.model_dump(mode="json")
    state_json = payload.pop("state_json")
    error_json = payload.pop("error_json")
    payload["has_state"] = state_json is not None
    payload["has_error"] = error_json is not None
    return payload


def _run_event_payload(event: RunEvent) -> dict[str, Any]:
    payload = event.model_dump(mode="json")
    raw_payload = payload.pop("payload_json")
    try:
        event_payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        event_payload = {"raw": raw_payload}
    if isinstance(event_payload, dict):
        event_payload["sequence"] = event.event_id
    payload["payload"] = event_payload
    return payload


def _conversation_message_payload(message: ConversationMessage) -> dict[str, Any]:
    payload = message.model_dump(mode="json")
    payload["timeline"] = _json_array(payload.pop("timeline_json"))
    payload["dag"] = _json_object_or_none(payload.pop("dag_json"))
    payload["trace"] = _json_object_or_none(payload.pop("trace_json"))
    payload["pending_review"] = _json_object_or_none(payload.pop("pending_review_json"))
    return payload


def _review_decision_json(decision: ReviewDecision) -> str:
    return json.dumps(
        {
            "review_id": decision.review_id,
            "approved": decision.approved,
            "dag": None if decision.dag is None else decision.dag.model_dump(mode="json"),
            "review_level": decision.review_level,
            "feedback": decision.feedback,
        },
        ensure_ascii=False,
    )


def _chat_stream_event_payload(event: RunStreamEvent, runner: Runner) -> dict[str, Any]:
    payload = event.model_dump(mode="json")
    data = payload.get("data")
    if (
        event.type != "review.required"
        or event.run_id is None
        or not isinstance(data, dict)
        or data.get("kind") != "capability_review"
    ):
        return payload
    run_state = runner.run_state(event.run_id)
    pending_review = None if run_state is None else run_state.pending_review
    if pending_review is None or pending_review.review_id != data.get("review_id"):
        return payload
    review_payload = pending_review.model_dump(mode="json")
    if review_payload.get("capability_call") is not None:
        data["capability_call"] = review_payload["capability_call"]
    data["payload"] = review_payload.get("payload") or {}
    return payload
