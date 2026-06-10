"""FastAPI application exposing the dagent harness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from dagent import (
    ArtifactUpload,
    AutoAgent,
    Boundary,
    CapabilityDefinition,
    DAG,
    DAGRun,
    DAGSpec,
    Dag,
    DagAgent,
    Node,
    ProfileStore,
    ReviewDecision,
    ReviewLevel,
    RiskLevel,
    Runner,
    RunState,
    SkillAmbiguousError,
    SkillNotFoundError,
    SkillPermissionError,
    SkillStore,
    SkillStoreError,
    default_skill_roots,
    ToolAgent,
    validate_dag_spec,
)
from dagent.config import load_config, resolve_config_path, resolve_config_relative_path
from dagent.capabilities.providers import template_capability_handler
from dagent.profiles import list_builtin_profiles
from dagent.schemas import Artifact, DAGEdge


MessageTarget = Literal["auto", "tool", "dag"]


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, Any]] = Field(min_length=1)
    state: RunState | None = None
    target: MessageTarget = "auto"
    review_level: ReviewLevel = "fast"
    capability_ids: list[str] | None = None
    skills: list[str] | None = None


class ResumeReviewRequest(BaseModel):
    review_id: str = Field(min_length=1)
    dag: DAG | None = None
    approved: bool = True
    review_level: ReviewLevel | None = None
    state: RunState | None = None


class CapabilityTestRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    boundary: dict[str, Any] | None = None


class DAGRunRequest(BaseModel):
    workspace_root: str | None = None
    graph_input: Any = None


class UserDAGNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    target: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    artifact_inputs: list[str] = Field(default_factory=list)
    artifact_outputs: list[str] = Field(default_factory=list)
    title: str = ""
    boundary: Boundary | None = None


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


class MCPServerRequest(BaseModel):
    name: str = Field(min_length=1)
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    enabled: bool = True
    risk: RiskLevel = "medium"
    connect_timeout: float = 30
    tool_timeout: float = 60
    include_tools: list[str] = Field(default_factory=list)
    exclude_tools: list[str] = Field(default_factory=list)


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

    def get_runner(self) -> Runner:
        if self.runner is None:
            self.runner = Runner.from_config(
                workspace=".",
                skill_roots=self.get_skill_roots(),
            )
            self._install_custom_capabilities()
            self.reload_custom_mcp()
        return self.runner

    def close_runner(self) -> None:
        if self.runner is not None:
            self.runner.close()
        self.runner = None
        self.custom_mcp_registered_names.clear()
        self.custom_mcp_errors.clear()

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

    def skill_store(self) -> SkillStore:
        return SkillStore(self.get_skill_roots(), managed_root=self.get_managed_skill_root())

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
        for name in list(self.custom_mcp_registered_names):
            runner.remove_mcp_server(name)
        self.custom_mcp_registered_names.clear()
        self.custom_mcp_errors.clear()
        for name, config in self.custom_mcp_servers.items():
            try:
                runner.add_mcp_server(name, config)
                self.custom_mcp_registered_names.add(name)
            except Exception as exc:
                self.custom_mcp_errors[name] = str(exc)


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
    runner.enable_validation = payload.get("enabled", False)
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
        validate_dag_spec(_compile_user_dag(dag))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state.dags[dag.id] = dag.model_copy(deep=True)
    _prune_dag_artifact_uploads(dag)
    return {"dag": dag.model_dump(mode="json")}


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
        return ".dagent-runs"
    return request.workspace_root.strip()


def _compile_user_dag(dag: UserDAG) -> DAGSpec:
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
            target=node.target.strip(),
            inputs=dict(node.inputs),
            artifact_inputs=list(node.artifact_inputs),
            artifact_outputs=list(node.artifact_outputs),
            title=node.title or None,
            boundary=node.boundary,
        ))
    for edge in dag.edges:
        builder.add_edge(edge.source, edge.target, reason=edge.reason)
    return builder.to_dag_spec()


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


@app.get("/dag-runs/{run_id}/artifacts")
async def get_dag_run_artifacts(run_id: str) -> dict[str, Any]:
    dag_run = _dag_run_from_state(run_id)
    if dag_run is None:
        raise HTTPException(status_code=404, detail="DAGRun not found.")
    return {
        "run_id": run_id,
        "artifacts": {
            artifact_id: artifact_state.model_dump(mode="json")
            for artifact_id, artifact_state in dag_run.trace.artifacts.items()
        },
    }


@app.get("/capabilities")
async def list_capabilities(kind: str | None = None) -> dict[str, Any]:
    runner = state.get_runner()
    return {
        "capabilities": [
            definition.model_dump(mode="json")
            for definition in runner.list_capabilities(kind=kind)
        ]
    }


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
    runner.remove_capability(capability_id)
    state.custom_capabilities.pop(capability_id, None)
    return {"status": "deleted"}


@app.post("/capabilities/{capability_id}/enable")
async def enable_capability(capability_id: str) -> dict[str, Any]:
    return _set_capability_enabled(capability_id, True)


@app.post("/capabilities/{capability_id}/disable")
async def disable_capability(capability_id: str) -> dict[str, Any]:
    return _set_capability_enabled(capability_id, False)


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
    state.custom_mcp_servers[server_name] = _mcp_server_config(request)
    try:
        state.reload_custom_mcp()
    except Exception as exc:
        state.custom_mcp_errors[server_name] = str(exc)
    return {"server": _mcp_server_payload(server_name, "memory", state.custom_mcp_servers[server_name], state.get_runner())}


@app.delete("/mcp/servers/{name}")
async def delete_mcp_server(name: str) -> dict[str, str]:
    server_name = _clean_name(name, field="MCP server name")
    if server_name not in state.custom_mcp_servers:
        raise HTTPException(status_code=404, detail="MCP server not found.")
    state.custom_mcp_servers.pop(server_name, None)
    state.reload_custom_mcp()
    return {"status": "deleted"}


@app.post("/mcp/reload")
async def reload_mcp_servers() -> dict[str, Any]:
    state.reload_custom_mcp()
    return await list_mcp_servers()


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
    profile_directory = state.get_profile_directory()
    profiles: list[dict[str, Any]] = [
        _profile_payload(profile, "builtin")
        for profile in list_builtin_profiles()
    ]
    warnings: list[dict[str, str]] = []
    if profile_directory is None:
        return {"profiles": profiles, "warnings": warnings}
    directory = Path(profile_directory)
    store = ProfileStore(directory)
    if not directory.exists():
        return {
            "profiles": profiles,
            "warnings": [{"name": str(directory), "error": "Profiles directory not found."}],
        }
    for profile_path in sorted(directory.glob("*.md"), key=lambda path: path.name):
        try:
            profiles.append(_profile_payload(store.load(profile_path.name), "user"))
        except Exception as exc:
            warnings.append({"name": profile_path.stem, "error": str(exc)})
    return {"profiles": profiles, "warnings": warnings}


def _profile_payload(profile, source: str) -> dict[str, Any]:
    return {
        **profile.model_dump(mode="json"),
        "id": f"{source}:{profile.name}",
        "source": source,
    }


@app.get("/sandbox/status")
async def sandbox_status() -> dict[str, Any]:
    return {
        "runner": "local-dev",
        "workspace_root": str(state.get_runner().workspace.resolve()),
        "container_ready": False,
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


def _agent_from_message(request: MessageRequest) -> AutoAgent | ToolAgent | DagAgent:
    skills = None if request.skills is None else tuple(_dedupe(request.skills))
    capability_ids: tuple[str, ...] | None = None
    if request.capability_ids is not None:
        capability_ids = tuple(_validated_capability_ids(request.capability_ids))

    if request.target == "auto":
        return AutoAgent(
            capabilities=capability_ids,
            skills=skills,
            review=request.review_level,
        )
    if request.target == "tool":
        return ToolAgent(
            profile="conversation",
            capabilities=capability_ids,
            skills=skills,
            review=request.review_level,
        )
    return DagAgent(
        capabilities=capability_ids,
        skills=skills,
        review=request.review_level,
    )


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
    boundary = Boundary.model_validate(request.boundary) if request.boundary is not None else None
    result = await runner.test_capability(capability_id, request.arguments, boundary=boundary)
    return {"result": result.model_dump(mode="json")}


@app.post("/messages/stream")
async def message_stream(request: MessageRequest) -> StreamingResponse:
    agent = _agent_from_message(request)

    async def events():
        sent_error = False
        try:
            async for event in state.get_runner().stream(
                agent,
                messages=request.messages,
                state=request.state,
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


@app.post("/messages/resume")
async def resume_message_stream(request: ResumeReviewRequest) -> StreamingResponse:
    async def events():
        decision = ReviewDecision(
            review_id=request.review_id,
            approved=request.approved,
            dag=request.dag,
            review_level=request.review_level,
        )
        sent_error = False
        try:
            async for event in state.get_runner().resume_stream(decision, state=request.state):
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


def _dag_run_from_state(run_id: str) -> DAGRun | None:
    if state.runner is None:
        return None
    run_state = state.runner.run_state(run_id)
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
    command = request.command.strip()
    if request.enabled and not command:
        raise HTTPException(status_code=400, detail="Enabled MCP servers require a command.")
    config: dict[str, Any] = {
        "command": command,
        "args": [str(arg) for arg in request.args],
        "env": {str(key): str(value) for key, value in request.env.items()},
        "enabled": request.enabled,
        "risk": request.risk,
        "connect_timeout": request.connect_timeout,
        "tool_timeout": request.tool_timeout,
    }
    if request.cwd:
        config["cwd"] = request.cwd
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
