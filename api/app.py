"""FastAPI application exposing the dagent harness."""

from __future__ import annotations

import codecs
import json
import mimetypes
from pathlib import Path, PureWindowsPath
from typing import Any, Literal
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from api.agent_presets import (
    AgentPreset,
    AgentPresetStore,
    AgentPresetUpdateRequest,
    clean_agent_preset_name,
)
from dagent import (
    ArtifactUpload,
    AutoAgent,
    Boundary,
    CapabilityDefinition,
    CapabilityPolicy,
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
    load_config,
    resolve_config_path,
    resolve_config_relative_path,
)
from dagent.capabilities.providers import agent_capability_parameters, template_capability_handler
from dagent.profiles import AgentProfile, list_builtin_profiles, load_builtin_profile
from dagent.schemas import Artifact, DAGEdge

from api.stream_gate import gate_chat_display


MessageTarget = Literal["auto", "tool", "dag"]
AgentScope = Literal["none", "selected", "registered"]
CONFIG_MODEL_ID = "config"
ApiKeyAction = Literal["preserve", "replace", "clear"]
ModelProviderSource = Literal["config", "runtime"]
REDACTED_SECRET_VALUE = "[redacted]"
RunArtifactSource = Literal["dag_artifact", "run_file"]
RunArtifactPreviewKind = Literal["markdown", "code", "text"]
RUN_ARTIFACT_PREVIEW_BYTES = 200_000
RUN_ARTIFACT_SCAN_LIMIT = 500
RUN_ARTIFACT_SCAN_VISIT_LIMIT = 5_000
PROFILE_CONTENT_BYTES_LIMIT = 128 * 1024

_MARKDOWN_EXTENSIONS = {".md", ".markdown"}
_TEXT_EXTENSIONS = {".csv", ".log", ".txt", ".tsv"}
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
    ".py": "text/x-python",
    ".sh": "text/x-shellscript",
    ".ts": "text/typescript",
    ".tsx": "text/typescript-jsx",
    ".jsx": "text/jsx",
}


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, Any]] = Field(min_length=1)
    state: RunState | None = None
    target: MessageTarget = "auto"
    review_level: ReviewLevel = "fast"
    dynamic_adjust: bool = True
    capability_ids: list[str] | None = None
    skills: list[str] | None = None
    agent_scope: AgentScope = "none"
    agent_ids: list[str] | None = None
    workspace_root: str | None = None


class ResumeReviewRequest(BaseModel):
    review_id: str = Field(min_length=1)
    dag: DAG | None = None
    approved: bool = True
    review_level: ReviewLevel | None = None
    state: RunState | None = None
    feedback: str | None = None


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
    connect_timeout: float = 30
    tool_timeout: float = 60
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
    status: str = "created"
    error: str | None = None
    preview_url: str | None = None


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
    preview_kind: RunArtifactPreviewKind
    content: str
    size: int
    truncated: bool
    truncated_at: int = RUN_ARTIFACT_PREVIEW_BYTES


class ApiState:
    def __init__(self) -> None:
        self.runner: Runner | None = None
        self.dags: dict[str, UserDAG] = {}
        self.dag_artifact_uploads: dict[str, dict[str, list[ArtifactUpload]]] = {}
        self.profile_directory: str | None = None
        self.custom_capabilities: dict[str, CapabilityDefinition] = {}
        self.custom_mcp_servers: dict[str, dict[str, Any]] = {}
        self.custom_mcp_registered_names: set[str] = set()
        self.custom_mcp_errors: dict[str, str] = {}
        self.agent_preset_errors: dict[str, str] = {}
        self.custom_model_providers: dict[str, ModelProviderRequest] = {}
        self.active_model_id: str | None = None
        self.validation_override: bool | None = None

    def get_runner(self) -> Runner:
        if self.runner is None:
            self.runner = self._create_runner()
            self._install_custom_capabilities()
            self.reload_custom_mcp()
            self._install_agent_presets()
            if self.validation_override is not None:
                self.runner.enable_validation = self.validation_override
        return self.runner

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
        self.agent_preset_errors.clear()

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
            if self.runner.get_capability(definition.id) is None:
                self.runner.register_capability(definition, _handler_for_definition(definition))

    def reload_custom_mcp(self) -> None:
        if self.runner is None:
            return
        runner = self.runner
        registered_names, errors = runner.reload_mcp_servers(
            self.custom_mcp_servers,
            replace_names=self.custom_mcp_registered_names,
        )
        self.custom_mcp_registered_names = registered_names
        self.custom_mcp_errors = errors

    def _install_agent_presets(self) -> None:
        if self.runner is None:
            return
        presets, errors = _agent_presets_with_errors(self.agent_preset_store())
        self.agent_preset_errors = dict(errors)
        for preset in presets:
            try:
                self.runner.add_agent(_tool_agent_from_preset(preset))
            except Exception as exc:
                self.agent_preset_errors[preset.name] = str(exc)


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
    if artifact_id not in dag.artifacts:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required.")

    uploads: list[ArtifactUpload] = []
    for file in files:
        content = await file.read()
        uploads.append(
            ArtifactUpload(
                filename=file.filename or "upload",
                content=content,
            )
        )
    state.dag_artifact_uploads.setdefault(dag_id, {})[artifact_id] = uploads
    return {
        "artifact_id": artifact_id,
        "files": [upload.filename for upload in uploads],
    }


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


@app.post("/dags/{dag_id}/run/stream")
async def run_dag_stream(dag_id: str, request: DAGRunRequest | None = None) -> StreamingResponse:
    dag = state.dags.get(dag_id)
    if dag is None:
        raise HTTPException(status_code=404, detail="DAG not found.")
    workspace_root = _workspace_root_from_request(request)

    async def events():
        sent_error = False
        try:
            async for event in state.get_runner().stream(
                _compile_user_dag(dag),
                graph_input=None if request is None else request.graph_input,
                workspace_root=workspace_root,
                artifact_uploads=_artifact_uploads_for_dag(dag_id),
            ):
                if event.type == "run.failed":
                    sent_error = True
                yield _sse(event.model_dump(mode="json"))
        except Exception as exc:
            if not sent_error:
                yield _sse({
                    "type": "run.failed",
                    "data": {"message": str(exc), "error_type": type(exc).__name__},
                    "sequence": 0,
                    "run_id": None,
                })

    return StreamingResponse(events(), media_type="text/event-stream")


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


def _prune_dag_artifact_uploads(dag: UserDAG) -> None:
    uploads = state.dag_artifact_uploads.get(dag.id)
    if not uploads:
        return
    for artifact_id in list(uploads):
        if artifact_id not in dag.artifacts:
            del uploads[artifact_id]


@app.get("/dag-runs/{run_id}")
async def get_dag_run(run_id: str) -> dict[str, Any]:
    dag_run = _dag_run_from_state(run_id)
    if dag_run is None:
        raise HTTPException(status_code=404, detail="DAGRun not found.")
    return {"dag_run": dag_run.model_dump(mode="json")}


@app.get("/runs/{run_id}/artifacts")
async def get_run_artifacts(run_id: str) -> dict[str, Any]:
    run_state = _run_state_from_state(run_id)
    if run_state is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return _run_artifacts_response(run_state).model_dump(mode="json")


@app.get("/runs/{run_id}/artifacts/preview")
async def preview_run_artifact(run_id: str, path: str) -> dict[str, Any]:
    run_state = _run_state_from_state(run_id)
    if run_state is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    file_path = _resolve_run_artifact_path(run_state, path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Artifact file not found.")
    preview_kind = _preview_kind_for_path(path)
    if preview_kind is None:
        raise HTTPException(status_code=415, detail="Artifact file type is not previewable.")
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


@app.get("/dag-runs/{run_id}/artifacts")
async def get_dag_run_artifacts(run_id: str) -> dict[str, Any]:
    run_state = _run_state_from_state(run_id)
    if run_state is None or run_state.kind != "static_dag":
        raise HTTPException(status_code=404, detail="DAGRun not found.")
    return _run_artifacts_response(run_state).model_dump(mode="json")


@app.get("/capabilities")
async def list_capabilities(kind: str | None = None) -> dict[str, Any]:
    runner = state.get_runner()
    definitions = list(runner.list_capabilities(kind=kind))
    if kind in (None, "agent"):
        existing_ids = {definition.id for definition in definitions}
        definitions.extend(
            definition
            for definition in _profile_agent_capabilities()
            if definition.id not in existing_ids
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
    presets, errors = _agent_presets_with_errors(store)
    names = set(store.list_names())
    registration_errors = {
        name: error
        for name, error in state.agent_preset_errors.items()
        if name in names and name not in errors
    }
    state.agent_preset_errors = {**errors, **registration_errors}
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
    saved = store.save(preset)
    try:
        state.get_runner().add_agent(tool_agent)
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
    return [
        CapabilityDefinition(
            id=f"agent.{profile.name}",
            name=profile.name,
            kind="agent",
            description=profile.description,
            parameters=agent_capability_parameters(),
            policy=CapabilityPolicy(risk="medium", sandbox_required=True),
            config={"profile": profile.name, "source": source},
        )
        for source, profile in profiles
    ]


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
            capability_ids=(
                None if request.capability_ids is None
                else _validated_agent_node_capabilities(request.capability_ids)
            ),
            skills=(
                None if request.skills is None
                else _validated_agent_node_skills(request.skills)
            ),
        )
        _resolve_agent_profile(preset.profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return preset


def _tool_agent_from_preset(preset: AgentPreset) -> ToolAgent:
    return ToolAgent(
        profile=_resolve_agent_profile(preset.profile),
        name=preset.name,
        max_steps=preset.max_steps,
        capabilities=None if preset.capability_ids is None else tuple(preset.capability_ids),
        skills=None if preset.skills is None else tuple(preset.skills),
        review="fast",
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
    return {"capability": runner.get_capability(definition.id).model_dump(mode="json")}


@app.put("/capabilities/{capability_id}")
async def update_capability(capability_id: str, definition: CapabilityDefinition) -> dict[str, Any]:
    runner = state.get_runner()
    if capability_id != definition.id:
        raise HTTPException(status_code=400, detail="Capability id mismatch.")
    if runner.get_capability(capability_id) is None:
        raise HTTPException(status_code=404, detail="Capability not found.")
    try:
        runner.replace_capability(definition, _handler_for_definition(definition))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state.custom_capabilities[definition.id] = definition.model_copy(deep=True)
    return {"capability": runner.get_capability(definition.id).model_dump(mode="json")}


@app.delete("/capabilities/{capability_id}")
async def delete_capability(capability_id: str) -> dict[str, str]:
    runner = state.get_runner()
    if runner.get_capability(capability_id) is None:
        raise HTTPException(status_code=404, detail="Capability not found.")
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


@app.get("/models", response_model=ModelListResponse)
async def list_models() -> ModelListResponse:
    return ModelListResponse(
        models=_model_provider_payloads(),
        active_model_id=_active_model_id(),
    )


@app.post("/models", response_model=ModelMutationResponse)
async def create_model_provider(request: ModelProviderRequest) -> ModelMutationResponse:
    model_id = _clean_model_id(request.id)
    if model_id in state.custom_model_providers:
        raise HTTPException(status_code=400, detail=f"Model '{model_id}' already exists.")
    if request.api_key_action == "preserve":
        raise HTTPException(status_code=400, detail="Cannot preserve an API key when creating a model.")
    model = _normalized_model_provider(request, model_id=model_id)
    if request.api_key_action == "clear":
        model = model.model_copy(update={"api_key": None}, deep=True)
    state.custom_model_providers[model_id] = model
    return ModelMutationResponse(
        model=_runtime_model_payload(model, active=_active_model_id() == model_id),
        active_model_id=_active_model_id(),
    )


@app.put("/models/{model_id}", response_model=ModelMutationResponse)
async def update_model_provider(model_id: str, request: ModelProviderRequest) -> ModelMutationResponse:
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
    if state.active_model_id == clean_model_id:
        state.close_runner()
    return ModelMutationResponse(
        model=_runtime_model_payload(model, active=_active_model_id() == clean_model_id),
        active_model_id=_active_model_id(),
    )


@app.delete("/models/{model_id}", response_model=ModelDeleteResponse)
async def delete_model_provider(model_id: str) -> ModelDeleteResponse:
    clean_model_id = _clean_model_id(model_id)
    if clean_model_id not in state.custom_model_providers:
        raise HTTPException(status_code=404, detail="Model not found.")
    state.custom_model_providers.pop(clean_model_id, None)
    if state.active_model_id == clean_model_id:
        state.active_model_id = None
        state.close_runner()
    return ModelDeleteResponse(status="deleted", active_model_id=_active_model_id())


@app.post("/models/{model_id}/activate", response_model=ModelMutationResponse)
async def activate_model_provider(model_id: str) -> ModelMutationResponse:
    clean_model_id = _clean_model_id(model_id, allow_config=True)
    if clean_model_id == CONFIG_MODEL_ID:
        state.active_model_id = None
        state.close_runner()
        return ModelMutationResponse(model=_config_model_payload(active=True), active_model_id=CONFIG_MODEL_ID)
    model = state.custom_model_providers.get(clean_model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found.")
    state.active_model_id = clean_model_id
    state.close_runner()
    return ModelMutationResponse(model=_runtime_model_payload(model, active=True), active_model_id=clean_model_id)


@app.get("/mcp/servers")
async def list_mcp_servers() -> dict[str, Any]:
    return {"servers": _mcp_server_payloads(state.get_runner())}


@app.post("/mcp/servers")
async def create_mcp_server(request: MCPServerRequest) -> dict[str, Any]:
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
    return {"server": _mcp_server_payload(name, "memory", state.custom_mcp_servers[name], state.get_runner())}


@app.put("/mcp/servers/{name}")
async def update_mcp_server(name: str, request: MCPServerRequest) -> dict[str, Any]:
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
    return {"server": _mcp_server_payload(server_name, "memory", state.custom_mcp_servers[server_name], state.get_runner())}


@app.delete("/mcp/servers/{name}")
async def delete_mcp_server(name: str) -> dict[str, str]:
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
    return {"status": "deleted"}


@app.post("/mcp/reload")
async def reload_mcp_servers() -> dict[str, Any]:
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
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if (
        not name[0].isalpha()
        or any(char not in allowed for char in name)
    ):
        raise HTTPException(
            status_code=400,
            detail="Profile name may contain only letters, numbers, underscores, and dashes, and must start with a letter.",
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
    return template_capability_handler(str(definition.config.get("template", "")))


def _set_capability_enabled(capability_id: str, enabled: bool) -> dict[str, Any]:
    runner = state.get_runner()
    if runner.get_capability(capability_id) is None:
        raise HTTPException(status_code=404, detail="Capability not found.")
    updated = runner.set_capability_enabled(capability_id, enabled)
    return {"capability": updated.model_dump(mode="json")}


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


def _model_provider_payloads() -> list[ModelProviderPayload]:
    active_id = _active_model_id()
    return [
        _config_model_payload(active=active_id == CONFIG_MODEL_ID),
        *[
            _runtime_model_payload(model, active=model.id == active_id)
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


def _runtime_model_payload(model: ModelProviderRequest, *, active: bool) -> ModelProviderPayload:
    return ModelProviderPayload(
        id=model.id,
        name=model.name,
        source="runtime",
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
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    if any(char not in allowed for char in model_id) or not any(char.isalnum() for char in model_id):
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
async def message_stream(request: MessageRequest) -> StreamingResponse:
    agent = _agent_from_message(request)
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


def _workspace_root_from_message(request: MessageRequest) -> str:
    if request.workspace_root is None or not request.workspace_root.strip():
        return DEFAULT_RUNS_DIR
    return _clean_workspace_root(request.workspace_root)


def _clean_workspace_root(value: str) -> str:
    root = value.strip()
    if root.startswith("~"):
        raise HTTPException(status_code=400, detail="workspace_root cannot use '~' expansion.")
    path = Path(root)
    if not path.is_absolute() and (".." in path.parts or ".." in PureWindowsPath(root).parts):
        raise HTTPException(status_code=400, detail="workspace_root cannot contain '..' in a relative path.")
    return root


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


def _run_state_from_state(run_id: str) -> RunState | None:
    if state.runner is None:
        return None
    return state.runner.run_state(run_id)


def _dag_run_from_state(run_id: str) -> DAGRun | None:
    run_state = _run_state_from_state(run_id)
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
    files, files_truncated = _run_artifact_files(run_state)
    return RunArtifactsResponse(
        run_id=run_state.run_id,
        workspace_path=run_state.workspace_path,
        artifacts=artifact_states,
        files=files,
        files_truncated=files_truncated,
    )


def _run_artifact_files(run_state: RunState) -> tuple[list[RunArtifactFile], bool]:
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
) -> RunArtifactFile:
    file_path = _resolve_workspace_path(workspace, path)
    preview_kind = _preview_kind_for_path(path)
    size: int | None = None
    path_error = error
    previewable = False
    if file_path is None:
        path_error = path_error or "Artifact path escapes run workspace."
    elif file_path.is_file():
        size = file_path.stat().st_size
        previewable = preview_kind is not None and _looks_like_utf8_text(file_path)
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
        status=status,
        error=path_error,
        preview_url=_preview_url(run_id, path) if previewable else None,
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
    path_obj = Path(normalized_path)
    windows_path = PureWindowsPath(normalized_path)
    if path_obj.is_absolute() or windows_path.is_absolute():
        return None
    if ".." in path_obj.parts or ".." in windows_path.parts:
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


def _preview_kind_for_path(path: str) -> RunArtifactPreviewKind | None:
    name = Path(path).name
    suffix = Path(path).suffix.lower()
    if suffix in _MARKDOWN_EXTENSIONS or name.upper() == "README":
        return "markdown"
    if suffix in _CODE_EXTENSIONS or name in _CODE_FILENAMES:
        return "code"
    if suffix in _TEXT_EXTENSIONS:
        return "text"
    return None


def _media_type_for_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in _MEDIA_TYPE_OVERRIDES:
        return _MEDIA_TYPE_OVERRIDES[suffix]
    guessed_type = mimetypes.guess_type(path)[0]
    if guessed_type is not None:
        return guessed_type
    return "text/plain" if _preview_kind_for_path(path) is not None else "application/octet-stream"


def _looks_like_utf8_text(path: Path) -> bool:
    try:
        with path.open("rb") as file:
            sample = file.read(2048)
    except OSError:
        return False
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


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
    if state.runner is not None:
        trace = state.runner.run_trace(run_id)
        if trace is not None:
            return {"run_id": run_id, "trace": trace.model_dump(mode="json")}

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


def _mcp_server_payloads(runner: Runner) -> list[dict[str, Any]]:
    servers: dict[str, tuple[str, dict[str, Any]]] = {}
    try:
        for name, config in load_config().mcp_servers.items():
            servers[str(name)] = ("config", dict(config))
    except Exception:
        pass
    for name, config in state.custom_mcp_servers.items():
        servers[str(name)] = ("memory", dict(config))
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
    return text


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


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
