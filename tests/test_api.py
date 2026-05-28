import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from fastapi.testclient import TestClient

from dagent.api.app import app, state
from dagent.capabilities.mcp import MCPCapabilityProvider
from dagent.runner import Runner
from dagent.capabilities.providers import ToolCapabilityProvider
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.capabilities.tools.registry import ToolRegistry
from dagent.schemas import CapabilityDefinition, CapabilityPolicy, CapabilityResult


def test_api_state_owns_runner_without_runtime_shim() -> None:
    assert not hasattr(state, "harness_runtime")


def test_api_skills_use_file_scanner(monkeypatch, tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "writing" / "summarize"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: summarize\ndescription: Summarize text.\n---\nBody.",
        encoding="utf-8",
    )
    monkeypatch.setattr(state, "get_skill_roots", lambda: [tmp_path / "skills"])
    client = TestClient(app)

    listed = client.get("/skills")
    viewed = client.get("/skills/writing/summarize")

    assert listed.status_code == 200
    assert listed.json()["skills"] == [
        {
            "name": "summarize",
            "description": "Summarize text.",
            "category": "writing",
            "path": str(skill_dir / "SKILL.md"),
        }
    ]
    assert viewed.status_code == 200
    assert viewed.json()["content"] == "Body."
    assert viewed.json()["skill_dir"] == str(skill_dir.resolve())


def test_api_imports_skill_with_frontmatter_and_serves_through_capability() -> None:
    state.close_runner()
    state.imported_skills.clear()
    state.runner = Runner(
        provider=MockProvider([ChatResponse(content="unused")]),
        imported_skills=state.imported_skills,
    )
    client = TestClient(app)

    import_response = client.post(
        "/skills/import",
        json={
            "content": "---\nname: summarize\ndescription: Summarize carefully.\n---\nUse concise summaries.",
            "category": "writing",
        },
    )
    list_response = client.get("/skills")
    view_response = client.get("/skills/writing/summarize")
    capability_response = client.post(
        "/capabilities/skill.view/test",
        json={"arguments": {"name": "writing/summarize"}},
    )

    assert import_response.status_code == 200
    assert list_response.status_code == 200
    assert {
        "name": "summarize",
        "description": "Summarize carefully.",
        "category": "writing",
        "path": "memory://skills/writing/summarize",
    } in list_response.json()["skills"]
    assert view_response.status_code == 200
    assert view_response.json()["content"] == "Use concise summaries."
    payload = json.loads(capability_response.json()["result"]["content"])
    assert payload["content"] == "Use concise summaries."


def test_api_imports_plain_markdown_skill_and_rejects_name_collision(monkeypatch, tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "plain"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: existing\ndescription: Existing skill.\n---\nBody.",
        encoding="utf-8",
    )
    state.close_runner()
    state.imported_skills.clear()
    monkeypatch.setattr(state, "get_skill_roots", lambda: [tmp_path / "skills"])
    client = TestClient(app)

    imported = client.post(
        "/skills/import",
        json={
            "name": "draft",
            "description": "Draft skill.",
            "content": "# Draft Skill\n\nUse this temporary instruction.",
        },
    )
    collision = client.post(
        "/skills/import",
        json={
            "name": "existing",
            "content": "Body.",
        },
    )

    assert imported.status_code == 200
    assert imported.json()["skill"]["content"].startswith("# Draft Skill")
    assert collision.status_code == 400
    assert "collides" in collision.json()["detail"]


def test_api_imports_zip_skill_package(monkeypatch, tmp_path) -> None:
    state.close_runner()
    state.imported_skills.clear()
    managed_root = tmp_path / "managed-skills"
    monkeypatch.setattr(state, "get_managed_skill_root", lambda: managed_root)
    monkeypatch.setattr(state, "get_skill_roots", lambda: [managed_root])
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(
            "bundle/SKILL.md",
            "---\n"
            "name: summarize\n"
            "description: Summarize with a helper.\n"
            "category: writing\n"
            "---\n"
            "Use the helper script.",
        )
        archive.writestr("bundle/scripts/run.py", "print('ok')\n")
        archive.writestr("bundle/references/style.md", "Keep it short.\n")
    client = TestClient(app)

    imported = client.post(
        "/skills/import/package",
        files={"file": ("bundle.zip", buffer.getvalue(), "application/zip")},
    )
    viewed = client.get("/skills/writing/summarize")
    script = client.get("/skills/writing/summarize", params={"file_path": "scripts/run.py"})

    assert imported.status_code == 200
    assert imported.json()["skill"]["skill_dir"] == str((managed_root / "writing" / "summarize").resolve())
    assert imported.json()["skill"]["linked_files"] == {
        "references": ["references/style.md"],
        "scripts": ["scripts/run.py"],
    }
    assert viewed.status_code == 200
    assert script.status_code == 200
    assert script.json()["content"] == "print('ok')\n"


def test_api_memory_mcp_server_reload_updates_runtime_catalog() -> None:
    class FakeMCPProvider:
        def __init__(self, servers):
            self.servers = servers
            self.manager = SimpleNamespace(last_errors={})

        def register_into(self, catalog):
            for name, config in self.servers.items():
                if config.get("enabled", True) is False:
                    continue
                catalog.register(
                    CapabilityDefinition(
                        id=f"mcp.{name}.lookup",
                        name=f"mcp_{name}__lookup",
                        kind="mcp",
                        description="Lookup.",
                        policy=CapabilityPolicy(risk=config.get("risk", "medium")),
                        config={"server": name, "tool": "lookup"},
                    ),
                    lambda invocation: CapabilityResult(
                        invocation_id=invocation.invocation_id,
                        capability_id=invocation.capability_id,
                        kind=invocation.kind,
                        status="completed",
                        content="found",
                    ),
                )

    state.close_runner()
    state.custom_mcp_servers.clear()
    state.custom_mcp_capability_ids.clear()
    state.custom_mcp_errors.clear()
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    previous_factory = state.mcp_provider_factory
    state.mcp_provider_factory = FakeMCPProvider
    client = TestClient(app)
    try:
        create_response = client.post(
            "/mcp/servers",
            json={"name": "mock", "command": "fake", "risk": "low"},
        )
        capability_ids = {item["id"] for item in client.get("/capabilities").json()["capabilities"]}
        delete_response = client.delete("/mcp/servers/mock")
        capability_ids_after_delete = {item["id"] for item in client.get("/capabilities").json()["capabilities"]}
    finally:
        state.mcp_provider_factory = previous_factory

    assert create_response.status_code == 200
    assert create_response.json()["server"]["status"] == "connected"
    assert "mcp.mock.lookup" in capability_ids
    assert delete_response.status_code == 200
    assert "mcp.mock.lookup" not in capability_ids_after_delete


def test_api_session_reset_clears_in_memory_workbench_state() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    client.post(
        "/capabilities",
        json={
            "id": "tool.temp",
            "name": "temp",
            "kind": "tool",
            "config": {"template": "temp"},
        },
    )
    client.post(
        "/skills/import",
        json={"name": "temp_skill", "content": "Use temporary guidance."},
    )
    state.custom_mcp_servers["temp"] = {"command": "fake"}

    response = client.post("/session/reset")

    assert response.status_code == 200
    assert state.custom_capabilities == {}
    assert state.imported_skills == {}
    assert state.custom_mcp_servers == {}


def test_api_message_stream_can_return_tool_answer_without_dag() -> None:
    state.runner = _runner(MockProvider([
        ChatResponse(content="tool"),            # _route()
        ChatResponse(content="hello there"),     # ToolAgentLoop
    ]))
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "hello", "mode": "auto"},
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    assert events[-1]["type"] == "done"
    assert events[-1]["dag"] is None
    assert events[-1]["final_answer"] == "hello there"


def test_api_message_stream_scopes_capabilities_and_skills() -> None:
    state.close_runner()
    state.imported_skills.clear()
    provider = MockProvider([ChatResponse(content="scoped answer")])
    state.runner = _runner(provider)
    client = TestClient(app)

    import_response = client.post(
        "/skills/import",
        json={"name": "brief", "content": "Use brief responses."},
    )
    response = client.post(
        "/messages/stream",
        json={
            "message": "hello",
            "mode": "tool",
            "capability_ids": ["tool.echo"],
            "skill_names": ["brief"],
        },
    )

    assert import_response.status_code == 200
    assert response.status_code == 200
    assert [tool["function"]["name"] for tool in provider.requests[0]["tools"]] == ["echo"]
    system_content = provider.requests[0]["messages"][0]["content"]
    assert "Use brief responses." in system_content
    assert "fail_tool" not in system_content


def test_api_message_stream_rejects_unknown_capability_scope() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "hello", "mode": "tool", "capability_ids": ["tool.missing"]},
    )

    assert response.status_code == 400
    assert "tool.missing" in response.json()["detail"]


def test_api_message_stream_rejects_dag_spec_as_public_mode() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "hello", "mode": "dag_spec"},
    )

    assert response.status_code == 422


def test_api_trace_endpoint_reads_tool_mode_run_trace() -> None:
    state.runner = _runner(MockProvider([
        ChatResponse(
            content="",
            tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hello"})],
        ),
        ChatResponse(content="done"),
    ]))
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "echo hello", "mode": "tool"},
    )
    events = _sse_events(response.text)
    task_id = events[-1]["task_id"]

    trace_response = client.get(f"/tasks/{task_id}/trace")

    assert trace_response.status_code == 200
    trace = trace_response.json()["trace"]
    capability = _capability_trace(trace, "tool.echo")
    assert capability["input"] == {"text": "hello"}
    assert capability["output"] == "echo:hello"
    assert capability["capability_execution"]["result"]["status"] == "completed"


def test_api_message_stream_creates_dag_and_waits_for_review() -> None:
    state.runner = _runner(
        MockProvider([
            ChatResponse(content="dag"),           # _route()
            ChatResponse(content=_dag_agent_dsl()),  # DAG agent
        ])
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "echo ok through a DAG", "mode": "auto", "review_level": "careful"},
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    event_types = [event["type"] for event in events]
    assert "dag" in event_types
    assert event_types[-1] == "done"
    assert events[-1]["status"] == "awaiting_review"
    assert events[-1]["pending_review"]["kind"] == "initial_dag"
    assert events[-1]["dag"]["status"] == "review_required"
    assert events[-1]["final_answer"] == ""


def test_api_fast_dag_streams_planning_think_and_live_trace() -> None:
    state.runner = _runner(
        MockProvider([
            ChatResponse(content="dag"),                                          # _route()
            ChatResponse(content="<think>planning dag</think>\n" + _dag_agent_dsl()),  # DAG agent
            ChatResponse(content="Final answer: echo:ok"),                        # execute observation
        ])
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "echo ok through a DAG", "mode": "auto", "review_level": "fast"},
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    done_index = next(index for index, event in enumerate(events) if event["type"] == "done")
    token_text = "".join(event.get("content", "") for event in events if event["type"] == "token")
    assert "<think>planning dag</think>" in token_text
    assert any(event.get("type") == "trace" for event in events[:done_index])
    assert events[-1]["status"] == "completed"
    assert events[-1]["dag"]["status"] == "completed"


def test_api_fast_dag_streams_failed_and_replanned_dag_versions() -> None:
    state.runner = _runner(
        MockProvider([
            ChatResponse(content="dag"),                                              # _route()
            ChatResponse(content='task: fail first\nbad = fail_tool(text="boom")\n'),  # DAG agent initial
            ChatResponse(content='task: repaired\nanswer = echo(text="ok")\n'),        # replan
            ChatResponse(content="Recovered after replanning."),                       # execute observation
        ])
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "repair through DAG", "mode": "auto", "review_level": "fast"},
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    dag_events = [event["dag"] for event in events if event["type"] == "dag"]
    assert any(dag["version"] == 1 and dag["status"] == "failed" for dag in dag_events)
    assert any(dag["version"] == 2 and dag["status"] == "completed" for dag in dag_events)
    assert events[-1]["status"] == "completed"


def test_api_dag_mode_returns_failed_fast_dag_answer() -> None:
    state.runner = _runner(
        MockProvider([
            ChatResponse(content='task: fail\nbad = fail_tool(text="boom")\n'),
            ChatResponse(content="NO_CHANGE"),
            ChatResponse(content="The DAG failed after retrying the failing node."),
        ])
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json={"message": "run a failing DAG", "mode": "dag", "review_level": "fast"},
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    assert events[-1]["status"] == "failed"
    assert events[-1]["dag"]["status"] == "failed"


def test_api_resume_executes_reviewed_dag_and_trace_endpoint_reads_run_trace() -> None:
    state.runner = _runner(
        MockProvider([
            ChatResponse(content="dag"),            # _route()
            ChatResponse(content=_dag_agent_dsl()),  # DAG agent
            ChatResponse(content="Final answer: echo:ok"),  # execute observation
        ])
    )
    client = TestClient(app)

    stream_response = client.post(
        "/messages/stream",
        json={"message": "echo ok through a DAG", "mode": "auto", "review_level": "careful"},
    )
    stream_events = _sse_events(stream_response.text)
    task_id = stream_events[-1]["task_id"]
    review_id = stream_events[-1]["pending_review"]["review_id"]
    dag = stream_events[-1]["dag"]
    dag["nodes"][0]["payload"]["invocation"]["arguments"] = {"text": "reviewed"}

    resume_response = client.post(
        "/messages/resume",
        json={"review_id": review_id, "dag": dag},
    )

    assert resume_response.status_code == 200
    resume_events = _sse_events(resume_response.text)
    assert resume_events[-1]["status"] == "completed"
    assert resume_events[-1]["dag"]["status"] == "completed"
    assert any(event.get("type") == "trace" for event in resume_events)

    trace_response = client.get(f"/tasks/{task_id}/trace")
    assert trace_response.status_code == 200
    trace = trace_response.json()["trace"]
    answer = _dag_node_trace(trace, "answer")
    capability = _capability_trace(answer, "tool.echo")
    assert capability["input"] == {"text": "reviewed"}
    assert capability["output"] == "echo:reviewed"
    assert capability["status"] == "completed"


def test_api_resume_review_returns_final_answer_without_streaming_answer_text() -> None:
    state.runner = _runner(
        MockProvider([
            ChatResponse(content="dag"),            # _route()
            ChatResponse(content=_dag_agent_dsl()),  # DAG agent
            ChatResponse(content="<think>observe</think>DAG agent final answer"),
        ])
    )
    client = TestClient(app)

    stream_response = client.post(
        "/messages/stream",
        json={"message": "echo ok through a DAG", "mode": "auto", "review_level": "careful"},
    )
    stream_events = _sse_events(stream_response.text)
    review_id = stream_events[-1]["pending_review"]["review_id"]
    dag = stream_events[-1]["dag"]

    resume_response = client.post(
        "/messages/resume",
        json={"review_id": review_id, "dag": dag},
    )

    assert resume_response.status_code == 200
    events = _sse_events(resume_response.text)
    token_text = "".join(event.get("content", "") for event in events if event["type"] == "token")
    assert "<think>observe</think>" in token_text
    assert "DAG agent final answer" not in token_text
    assert events[-1]["final_answer"] == "DAG agent final answer"


def test_api_old_dag_lifecycle_routes_are_removed() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    assert client.post("/tasks", json={"message": "old path"}).status_code == 404
    assert client.post("/dags/task_api/approve").status_code == 404
    assert client.post("/dags/task_api/execute").status_code == 404
    assert client.put("/dags/task_api", json={"dag": {}}).status_code == 404


def test_api_capability_list_create_and_test() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    list_response = client.get("/capabilities")

    assert list_response.status_code == 200
    capability_ids = {item["id"] for item in list_response.json()["capabilities"]}
    assert {"tool.echo", "memory.write", "file.write"}.issubset(capability_ids)

    create_response = client.post(
        "/capabilities",
        json={
            "id": "tool.upper",
            "name": "upper",
            "kind": "tool",
            "description": "Uppercase text.",
            "config": {"template": "upper:{text}"},
        },
    )
    assert create_response.status_code == 200

    test_response = client.post(
        "/capabilities/tool.upper/test",
        json={"arguments": {"text": "ok"}},
    )

    assert test_response.status_code == 200
    assert test_response.json()["result"]["content"] == "upper:ok"

    disable_response = client.post("/capabilities/tool.upper/disable")
    assert disable_response.status_code == 200
    assert disable_response.json()["capability"]["enabled"] is False

    enable_response = client.post("/capabilities/tool.upper/enable")
    assert enable_response.status_code == 200
    assert enable_response.json()["capability"]["enabled"] is True

    delete_response = client.delete("/capabilities/tool.upper")
    assert delete_response.status_code == 200
    assert client.post(
        "/capabilities/tool.upper/test",
        json={"arguments": {"text": "ok"}},
    ).status_code == 404


def test_api_capability_test_infers_run_command_boundary() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    command = f'{sys.executable} -c "print(1); print(2)"'

    response = client.post(
        "/capabilities/tool.run_command/test",
        json={"arguments": {"command": command, "cwd": "."}},
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["status"] == "completed"
    assert result["policy_decision"]["boundary_mode"] == "write_limited"
    assert result["policy_decision"]["allowed_commands"] == []
    assert "1" in result["content"]
    assert "2" in result["content"]


def test_api_rejects_removed_custom_tool_capability_kind() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    response = client.post(
        "/capabilities",
        json={
            "id": "custom_tool.upper",
            "name": "upper",
            "kind": "custom_tool",
            "description": "Uppercase text.",
            "config": {"template": "upper:{text}"},
        },
    )

    assert response.status_code == 422


def test_api_session_reset_closes_existing_runner() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    closed: list[str] = []
    state.runner.runtime.capability_catalog.add_shutdown_hook(lambda: closed.append("closed"))
    client = TestClient(app)

    response = client.post("/session/reset")

    assert response.status_code == 200
    assert closed == ["closed"]


def test_api_capability_status_endpoints() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    assert client.get("/mcp/servers").status_code == 200
    assert client.get("/skills").status_code == 200
    sandbox = client.get("/sandbox/status")

    assert sandbox.status_code == 200
    assert sandbox.json()["runner"] in {"local-dev", "container"}


def test_api_profiles_lists_loaded_profile_layers(tmp_path, monkeypatch) -> None:
    profile_dir = tmp_path / "profiles"
    assistant_dir = profile_dir / "assistant"
    assistant_dir.mkdir(parents=True)
    (assistant_dir / "profile.yaml").write_text(
        "name: assistant\n"
        "role: helper\n"
        "description: Helpful profile.\n"
        "layers:\n"
        "  - soul.md\n"
        "memory_file: memory.md\n"
        "output_format: text\n",
        encoding="utf-8",
    )
    (assistant_dir / "soul.md").write_text("You are helpful.", encoding="utf-8")
    (assistant_dir / "memory.md").write_text("Remember scope.", encoding="utf-8")
    monkeypatch.setattr(state, "profile_directory", str(profile_dir))
    client = TestClient(app)

    response = client.get("/profiles")

    assert response.status_code == 200
    payload = response.json()
    assert payload["warnings"] == []
    assert payload["profiles"] == [
        {
            "name": "assistant",
            "role": "helper",
            "description": "Helpful profile.",
            "layers": ["soul.md"],
            "layer_contents": {"soul.md": "You are helpful."},
            "memory_file": "memory.md",
            "memory": "Remember scope.",
            "output_format": "text",
        }
    ]


def test_api_profiles_skips_invalid_profile_with_warning(tmp_path, monkeypatch) -> None:
    profile_dir = tmp_path / "profiles"
    good_dir = profile_dir / "good"
    bad_dir = profile_dir / "bad"
    good_dir.mkdir(parents=True)
    bad_dir.mkdir()
    (good_dir / "profile.yaml").write_text(
        "name: good\nrole: helper\nlayers: []\n",
        encoding="utf-8",
    )
    (bad_dir / "profile.yaml").write_text("[not valid for a profile", encoding="utf-8")
    monkeypatch.setattr(state, "profile_directory", str(profile_dir))
    client = TestClient(app)

    response = client.get("/profiles")

    assert response.status_code == 200
    payload = response.json()
    assert [profile["name"] for profile in payload["profiles"]] == ["good"]
    assert len(payload["warnings"]) == 1
    assert payload["warnings"][0]["name"] == "bad"


def test_api_dag_spec_create_run_and_artifacts() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    create_response = client.post(
        "/dag-specs",
        json={
            "id": "write_note",
            "name": "Write note",
            "artifacts": {
                "note": {
                    "id": "note",
                    "paths": ["notes/output.txt"],
                }
            },
            "nodes": [
                {
                    "id": "write",
                    "title": "Write output note",
                    "payload": {
                        "type": "capability",
                        "invocation": {
                            "capability_id": "tool.write_file",
                            "kind": "tool",
                            "arguments": {
                                "path": "notes/output.txt",
                                "content": "hello",
                            },
                            "boundary": {
                                "mode": "write_limited",
                                "allowed_paths": ["notes/output.txt"],
                            },
                        },
                    },
                    "outputs": ["note"],
                }
            ],
            "edges": [],
        },
    )
    assert create_response.status_code == 200

    get_spec_response = client.get("/dag-specs/write_note")
    assert get_spec_response.status_code == 200
    assert get_spec_response.json()["dag_spec"]["id"] == "write_note"

    list_response = client.get("/dag-specs")
    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()["dag_specs"]] == ["write_note"]

    run_response = client.post("/dag-specs/write_note/run")
    assert run_response.status_code == 200
    run_payload = run_response.json()["dag_run"]
    assert run_payload["spec_id"] == "write_note"
    assert run_payload["status"] == "completed"
    assert run_payload["dag"]["status"] == "completed"
    assert run_payload["dag"]["nodes"][0]["status"] == "completed"
    assert run_payload["trace"]["artifacts"]["note"]["status"] == "created"

    get_run_response = client.get(f"/dag-runs/{run_payload['run_id']}")
    assert get_run_response.status_code == 200
    assert get_run_response.json()["dag_run"]["run_id"] == run_payload["run_id"]

    artifacts_response = client.get(f"/dag-runs/{run_payload['run_id']}/artifacts")
    assert artifacts_response.status_code == 200
    assert artifacts_response.json()["artifacts"]["note"]["paths"] == ["notes/output.txt"]


def test_api_dag_spec_run_uses_requested_workspace_root(tmp_path: Path) -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    workspace_root = tmp_path / "runs"

    create_response = client.post(
        "/dag-specs",
        json={
            "id": "workspace_note",
            "name": "Workspace note",
            "artifacts": {
                "note": {
                    "id": "note",
                    "paths": ["notes/output.txt"],
                }
            },
            "nodes": [
                {
                    "id": "write",
                    "payload": {
                        "type": "capability",
                        "invocation": {
                            "capability_id": "tool.write_file",
                            "kind": "tool",
                            "arguments": {
                                "path": "{{artifact.note.path}}",
                                "content": "hello",
                            },
                            "boundary": {
                                "mode": "write_limited",
                                "allowed_paths": ["{{artifact.note.path}}"],
                            },
                        },
                    },
                    "outputs": ["note"],
                }
            ],
        },
    )
    assert create_response.status_code == 200

    run_response = client.post(
        "/dag-specs/workspace_note/run",
        json={"workspace_root": str(workspace_root)},
    )

    assert run_response.status_code == 200
    run_payload = run_response.json()["dag_run"]
    workspace_path = Path(run_payload["workspace_path"])
    assert workspace_path.parent == workspace_root
    assert (workspace_path / "notes" / "output.txt").read_text(encoding="utf-8") == "hello"


def test_api_dag_spec_artifact_upload_materializes_input_file(tmp_path: Path) -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    workspace_root = tmp_path / "runs"

    create_response = client.post(
        "/dag-specs",
        json={
            "id": "uploaded_source",
            "name": "Uploaded source",
            "artifacts": {
                "source": {
                    "id": "source",
                    "paths": ["inputs/source.txt"],
                }
            },
            "nodes": [
                {
                    "id": "read",
                    "payload": {
                        "type": "capability",
                        "invocation": {
                            "capability_id": "tool.read_file",
                            "kind": "tool",
                            "arguments": {
                                "path": "{{artifact.source.path}}",
                            },
                            "boundary": {
                                "mode": "read_only",
                                "allowed_paths": ["{{artifact.source.path}}"],
                            },
                        },
                    },
                    "inputs": ["source"],
                }
            ],
        },
    )
    assert create_response.status_code == 200

    upload_response = client.post(
        "/dag-specs/uploaded_source/artifacts/source/upload",
        files=[("files", ("source.txt", b"hello from upload", "text/plain"))],
    )
    assert upload_response.status_code == 200
    assert upload_response.json()["files"] == ["source.txt"]

    run_response = client.post(
        "/dag-specs/uploaded_source/run",
        json={"workspace_root": str(workspace_root)},
    )

    assert run_response.status_code == 200
    run_payload = run_response.json()["dag_run"]
    workspace_path = Path(run_payload["workspace_path"])
    assert run_payload["status"] == "completed"
    assert (workspace_path / "inputs" / "source.txt").read_text(encoding="utf-8") == "hello from upload"
    assert run_payload["trace"]["root"]["children"][0]["output"] == "hello from upload"
    assert run_payload["trace"]["artifacts"]["source"]["status"] == "created"


def test_api_created_tool_capability_can_run_in_dag_spec() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    create_capability_response = client.post(
        "/capabilities",
        json={
            "id": "tool.upper",
            "name": "upper",
            "kind": "tool",
            "description": "Uppercase-ish text.",
            "config": {"template": "upper:{text}"},
        },
    )
    assert create_capability_response.status_code == 200

    create_spec_response = client.post(
        "/dag-specs",
        json={
            "id": "tool_dag",
            "name": "Tool DAG",
            "nodes": [
                {
                    "id": "call_custom",
                    "payload": {
                        "type": "capability",
                        "invocation": {
                            "capability_id": "tool.upper",
                            "kind": "tool",
                            "arguments": {"text": "ok"},
                        },
                    },
                }
            ],
            "edges": [],
        },
    )
    assert create_spec_response.status_code == 200

    run_response = client.post("/dag-specs/tool_dag/run")

    assert run_response.status_code == 200
    payload = run_response.json()["dag_run"]
    assert payload["status"] == "completed"
    assert payload["trace"]["root"]["children"][0]["output"] == "upper:ok"


def test_api_dag_spec_run_stream_returns_live_events_and_stores_run() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    create_response = client.post(
        "/dag-specs",
        json={
            "id": "stream_note",
            "name": "Stream note",
            "artifacts": {
                "note": {
                    "id": "note",
                    "paths": ["notes/output.txt"],
                }
            },
            "nodes": [
                {
                    "id": "write",
                    "payload": {
                        "type": "capability",
                        "invocation": {
                            "capability_id": "tool.write_file",
                            "kind": "tool",
                            "arguments": {
                                "path": "notes/output.txt",
                                "content": "hello",
                            },
                            "boundary": {
                                "mode": "write_limited",
                                "allowed_paths": ["notes/output.txt"],
                            },
                        },
                    },
                    "outputs": ["note"],
                }
            ],
        },
    )
    assert create_response.status_code == 200

    response = client.post("/dag-specs/stream_note/run/stream")

    assert response.status_code == 200
    events = _sse_events(response.text)
    assert events[0]["type"] == "status"
    assert any(event["type"] == "trace" for event in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["dag_run"]["status"] == "completed"
    run_id = events[-1]["dag_run"]["run_id"]
    assert client.get(f"/dag-runs/{run_id}").json()["dag_run"]["run_id"] == run_id


def test_api_dag_spec_run_stream_uses_requested_workspace_root(tmp_path: Path) -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    workspace_root = tmp_path / "stream-runs"
    create_response = client.post(
        "/dag-specs",
        json={
            "id": "stream_workspace_note",
            "name": "Stream workspace note",
            "artifacts": {
                "note": {
                    "id": "note",
                    "paths": ["notes/output.txt"],
                }
            },
            "nodes": [
                {
                    "id": "write",
                    "payload": {
                        "type": "capability",
                        "invocation": {
                            "capability_id": "tool.write_file",
                            "kind": "tool",
                            "arguments": {
                                "path": "{{artifact.note.path}}",
                                "content": "hello",
                            },
                            "boundary": {
                                "mode": "write_limited",
                                "allowed_paths": ["{{artifact.note.path}}"],
                            },
                        },
                    },
                    "outputs": ["note"],
                }
            ],
        },
    )
    assert create_response.status_code == 200

    response = client.post(
        "/dag-specs/stream_workspace_note/run/stream",
        json={"workspace_root": str(workspace_root)},
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    dag_run = events[-1]["dag_run"]
    workspace_path = Path(dag_run["workspace_path"])
    assert workspace_path.parent == workspace_root
    assert (workspace_path / "notes" / "output.txt").read_text(encoding="utf-8") == "hello"


def test_api_dag_spec_run_fails_when_required_artifact_is_missing() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    create_response = client.post(
        "/dag-specs",
        json={
            "id": "missing_output",
            "name": "Missing output",
            "artifacts": {
                "note": {
                    "id": "note",
                    "paths": ["notes/missing.txt"],
                }
            },
            "nodes": [
                {
                    "id": "answer",
                    "payload": {
                        "type": "capability",
                        "invocation": {
                            "capability_id": "tool.echo",
                            "kind": "tool",
                            "arguments": {"text": "ok"},
                        },
                    },
                    "outputs": ["note"],
                }
            ],
        },
    )
    assert create_response.status_code == 200

    run_response = client.post("/dag-specs/missing_output/run")

    assert run_response.status_code == 200
    payload = run_response.json()["dag_run"]
    assert payload["status"] == "failed"
    assert payload["trace"]["artifacts"]["note"]["status"] == "missing"
    assert payload["dag"]["status"] == "failed"


def test_api_dag_spec_validation_and_missing_resources() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    invalid_response = client.post(
        "/dag-specs",
        json={
            "id": "invalid",
            "name": "Invalid",
            "artifacts": {},
            "nodes": [
                {
                    "id": "answer",
                    "payload": {
                        "type": "capability",
                        "invocation": {
                            "capability_id": "tool.echo",
                            "kind": "tool",
                            "arguments": {"text": "ok"},
                        },
                    },
                    "inputs": ["missing"],
                }
            ],
        },
    )

    assert invalid_response.status_code == 400
    assert client.get("/dag-specs/missing").status_code == 404
    assert client.post("/dag-specs/missing/run").status_code == 404
    assert client.get("/dag-runs/missing").status_code == 404
    assert client.get("/dag-runs/missing/artifacts").status_code == 404


def _runner(provider: MockProvider) -> Runner:
    runner = Runner(provider=provider)
    registry = ToolRegistry()
    registry.register(
        name="echo",
        handler=lambda text: f"echo:{text}",
        action="read",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    registry.register(
        name="fail_tool",
        handler=lambda text: (_ for _ in ()).throw(RuntimeError(f"failed:{text}")),
        action="read",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    ToolCapabilityProvider(registry).register_into(runner.runtime.capability_catalog)
    runner.runtime.refresh_toolsets()
    return runner


def _dag_agent_dsl() -> str:
    return 'task: mock\nanswer = echo(text="ok")\n'


def _sse_events(text: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in text.splitlines()
        if line.startswith("data: ")
    ]


def _dag_node_trace(trace: dict, node_id: str) -> dict:
    for node in _walk_trace(trace):
        if node.get("kind") == "dag_node" and node.get("ref", {}).get("node_id") == node_id:
            return node
    raise AssertionError(f"Missing dag_node trace for {node_id}")


def _capability_trace(trace_or_node: dict, capability_id: str) -> dict:
    for node in _walk_trace(trace_or_node):
        if node.get("kind") == "capability_call" and node.get("ref", {}).get("capability_id") == capability_id:
            return node
    raise AssertionError(f"Missing capability_call trace for {capability_id}")


def _walk_trace(trace_or_node: dict):
    root = trace_or_node.get("root", trace_or_node)
    stack = [root]
    while stack:
        node = stack.pop(0)
        yield node
        stack[0:0] = node.get("children", [])
