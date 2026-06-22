import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from fastapi.testclient import TestClient

import api.app as app_module
import dagent.runner as runner_module
from api.app import app, state
from dagent.runner import Runner
from dagent.capabilities.providers import ToolCapabilityProvider
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.capabilities.tools.registry import ToolRegistry
from dagent.schemas import CapabilityDefinition, CapabilityPolicy, CapabilityResult


def test_api_state_owns_runner_without_runtime_shim() -> None:
    assert not hasattr(state, "harness_runtime")
    assert not hasattr(state, "dag_runs")
    assert not hasattr(state, "custom_mcp_capability_ids")
    assert not hasattr(state, "mcp_provider_factory")
    assert not hasattr(app_module, "MCPCapabilityProvider")


def test_api_does_not_keep_stream_payload_shims() -> None:
    assert not hasattr(app_module, "_sdk_event_payloads")
    assert not hasattr(app_module, "_runtime_event_payload")
    assert not hasattr(app_module, "_runtime_response_payloads")
    assert not hasattr(app_module, "_dag_node_from_user_node")
    assert not hasattr(app_module, "_target_kind_and_risk")


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
            "managed": False,
        }
    ]
    assert viewed.status_code == 200
    assert viewed.json()["content"] == "Body."
    assert viewed.json()["skill_dir"] == str(skill_dir.resolve())


def test_api_installs_markdown_skill_and_serves_through_capability(monkeypatch, tmp_path) -> None:
    state.close_runner()
    managed_root = tmp_path / "managed-skills"
    monkeypatch.setattr(state, "get_managed_skill_root", lambda: managed_root)
    monkeypatch.setattr(state, "get_skill_roots", lambda: [managed_root])
    state.runner = Runner(
        provider=MockProvider([ChatResponse(content="unused")]),
        skill_roots=[managed_root],
    )
    client = TestClient(app)

    install_response = client.post(
        "/skills/install",
        data={
            "content": "---\nname: summarize\ndescription: Summarize carefully.\n---\nUse concise summaries.",
            "category": "writing",
        }
    )
    list_response = client.get("/skills")
    view_response = client.get("/skills/writing/summarize")
    capability_response = client.post(
        "/capabilities/skill.view/test",
        json={"arguments": {"name": "writing/summarize"}},
    )

    assert install_response.status_code == 200
    assert list_response.status_code == 200
    assert {
        "name": "summarize",
        "description": "Summarize carefully.",
        "category": "writing",
        "path": str(managed_root / "writing" / "summarize" / "SKILL.md"),
        "managed": True,
    } in list_response.json()["skills"]
    assert view_response.status_code == 200
    assert view_response.json()["content"] == "Use concise summaries."
    assert view_response.json()["skill_dir"] == str((managed_root / "writing" / "summarize").resolve())
    payload = json.loads(capability_response.json()["result"]["content"])
    assert payload["content"] == "Use concise summaries."


def test_api_installs_plain_markdown_skill_and_rejects_name_collision(monkeypatch, tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "plain"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: existing\ndescription: Existing skill.\n---\nBody.",
        encoding="utf-8",
    )
    state.close_runner()
    managed_root = tmp_path / "managed-skills"
    monkeypatch.setattr(state, "get_managed_skill_root", lambda: managed_root)
    monkeypatch.setattr(state, "get_skill_roots", lambda: [tmp_path / "skills", managed_root])
    client = TestClient(app)

    installed = client.post(
        "/skills/install",
        data={
            "name": "draft",
            "description": "Draft skill.",
            "content": "# Draft Skill\n\nUse this temporary instruction.",
        },
    )
    collision = client.post(
        "/skills/install",
        data={
            "name": "existing",
            "content": "Body.",
        },
    )

    assert installed.status_code == 200
    assert installed.json()["skill"]["content"].startswith("# Draft Skill")
    assert installed.json()["skill"]["skill_dir"] == str((managed_root / "draft").resolve())
    assert collision.status_code == 400
    assert "collides" in collision.json()["detail"]


def test_api_text_skill_install_requires_explicit_or_frontmatter_name(monkeypatch, tmp_path) -> None:
    state.close_runner()
    managed_root = tmp_path / "managed-skills"
    monkeypatch.setattr(state, "get_managed_skill_root", lambda: managed_root)
    monkeypatch.setattr(state, "get_skill_roots", lambda: [managed_root])
    client = TestClient(app)

    response = client.post("/skills/install", data={"content": "Use terse prose."})

    assert response.status_code == 400
    assert "Skill name is required" in response.json()["detail"]


def test_api_markdown_file_install_can_use_filename_stem(monkeypatch, tmp_path) -> None:
    state.close_runner()
    managed_root = tmp_path / "managed-skills"
    monkeypatch.setattr(state, "get_managed_skill_root", lambda: managed_root)
    monkeypatch.setattr(state, "get_skill_roots", lambda: [managed_root])
    client = TestClient(app)

    response = client.post(
        "/skills/install",
        files={"file": ("brief.md", b"Use brief responses.", "text/markdown")},
    )

    assert response.status_code == 200
    assert response.json()["skill"]["name"] == "brief"
    assert response.json()["skill"]["skill_dir"] == str((managed_root / "brief").resolve())


def test_api_skill_list_reports_malformed_frontmatter(monkeypatch, tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "bad"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: [unterminated\n---\nBody.", encoding="utf-8")
    monkeypatch.setattr(state, "get_skill_roots", lambda: [tmp_path / "skills"])
    client = TestClient(app)

    response = client.get("/skills")

    assert response.status_code == 400
    assert "invalid YAML" in response.json()["detail"]


def test_api_installs_zip_skill_package(monkeypatch, tmp_path) -> None:
    state.close_runner()
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

    installed = client.post(
        "/skills/install",
        files={"file": ("bundle.zip", buffer.getvalue(), "application/zip")},
    )
    viewed = client.get("/skills/writing/summarize")
    script = client.get("/skills/writing/summarize", params={"file_path": "scripts/run.py"})

    assert installed.status_code == 200
    assert installed.json()["skill"]["skill_dir"] == str((managed_root / "writing" / "summarize").resolve())
    assert installed.json()["skill"]["linked_files"] == {
        "references": ["references/style.md"],
        "scripts": ["scripts/run.py"],
    }
    assert viewed.status_code == 200
    assert script.status_code == 200
    assert script.json()["content"] == "print('ok')\n"


def test_api_memory_mcp_server_reload_updates_runtime_catalog(monkeypatch) -> None:
    class FakeMCPProvider:
        def __init__(self, servers, *, manager=None):
            self.servers = servers
            self.manager = SimpleNamespace(last_errors={})
            self.registration_errors: list[str] = []

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
    state.custom_mcp_errors.clear()
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    monkeypatch.setattr(runner_module.MCPServerManager, "available", True)
    monkeypatch.setattr(runner_module, "MCPCapabilityProvider", FakeMCPProvider)
    client = TestClient(app)

    create_response = client.post(
        "/mcp/servers",
        json={"name": "mock", "command": "fake", "risk": "low"},
    )
    capability_ids = {item["id"] for item in client.get("/capabilities").json()["capabilities"]}
    delete_response = client.delete("/mcp/servers/mock")
    capability_ids_after_delete = {item["id"] for item in client.get("/capabilities").json()["capabilities"]}

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
    state.custom_mcp_servers["temp"] = {"command": "fake"}

    response = client.post("/session/reset")

    assert response.status_code == 200
    assert state.custom_capabilities == {}
    assert state.custom_mcp_servers == {}


def test_api_message_stream_can_return_tool_answer_without_dag() -> None:
    state.runner = _runner(MockProvider([
        ChatResponse(content="tool"),            # _route()
        ChatResponse(content="hello there"),     # ToolAgentLoop
    ]))
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json=_message_request("hello", target="auto"),
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    result = _stream_result(events[-1])
    _assert_result_shape(result)
    assert events[-1]["type"] == "run.finished"
    assert _result_dag(result) is None
    assert result["output_text"] == "hello there"


def test_api_message_stream_scopes_capabilities_and_skills(monkeypatch, tmp_path) -> None:
    state.close_runner()
    managed_root = tmp_path / "managed-skills"
    monkeypatch.setattr(state, "get_managed_skill_root", lambda: managed_root)
    monkeypatch.setattr(state, "get_skill_roots", lambda: [managed_root])
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_1", name="skills_list", arguments={})]),
        ChatResponse(content="scoped answer"),
    ])
    state.runner = _runner(provider, skill_roots=[managed_root])
    client = TestClient(app)

    install_response = client.post(
        "/skills/install",
        data={"name": "brief", "content": "Use brief responses."},
    )
    response = client.post(
        "/messages/stream",
        json=_message_request(
            "hello",
            target="tool",
            capability_ids=["tool.echo"],
            skills=["brief"],
        ),
    )

    assert install_response.status_code == 200
    assert response.status_code == 200
    assert [tool["function"]["name"] for tool in provider.requests[0]["tools"]] == [
        "echo",
        "skills_list",
        "skill_view",
    ]
    system_content = provider.requests[0]["messages"][0]["content"]
    assert "Use brief responses." not in system_content
    assert "fail_tool" not in system_content
    tool_content = provider.requests[1]["messages"][-1]["content"]
    assert "brief" in tool_content


def test_api_message_stream_rejects_unknown_capability_scope() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json=_message_request("hello", target="tool", capability_ids=["tool.missing"]),
    )

    assert response.status_code == 400
    assert "tool.missing" in response.json()["detail"]


def test_api_message_stream_rejects_runtime_mode_field() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json=_message_request("hello", mode="auto"),
    )

    assert response.status_code == 422


def test_api_message_stream_rejects_dag_spec_as_public_target() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json=_message_request("hello", target="dag_spec"),
    )

    assert response.status_code == 422


def test_api_message_stream_capability_review_event_includes_call_and_payload(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    blocked = tmp_path / "blocked"
    workspace.mkdir()
    blocked.mkdir()
    (blocked / "secret.txt").write_text("private", encoding="utf-8")
    state.runner = Runner(
        workspace=workspace,
        provider=MockProvider([
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "../blocked/secret.txt"},
                    )
                ],
            )
        ]),
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json=_message_request(
            "read outside the workspace",
            target="tool",
            capability_ids=["tool.read_file"],
        ),
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    review_event = next(event for event in events if event["type"] == "review.required")
    review = review_event["data"]
    assert review["kind"] == "capability_review"
    assert review["capability_call"] == {
        "invocation_id": "call_1",
        "capability_id": "tool.read_file",
        "arguments": {"path": "../blocked/secret.txt"},
    }
    assert review["payload"]["capability_id"] == "tool.read_file"
    assert review["payload"]["risk"] == "low"
    assert review["payload"]["reason"] == "boundary_violation"
    assert "outside allowed paths" in review["payload"]["error"]
    assert "secret.txt" in review["payload"]["error"]


def test_api_resume_capability_review_forwards_reviewer_feedback(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    blocked = tmp_path / "blocked"
    workspace.mkdir()
    blocked.mkdir()
    (blocked / "secret.txt").write_text("private", encoding="utf-8")
    provider = MockProvider([
        ChatResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="read_file",
                    arguments={"path": "../blocked/secret.txt"},
                )
            ],
        ),
        ChatResponse(content="I will read the allowed README instead."),
    ])
    state.runner = Runner(workspace=workspace, provider=provider)
    client = TestClient(app)

    stream_response = client.post(
        "/messages/stream",
        json=_message_request(
            "read outside the workspace",
            target="tool",
            capability_ids=["tool.read_file"],
        ),
    )
    stream_events = _sse_events(stream_response.text)
    review_id = next(
        event["data"]["review_id"]
        for event in stream_events
        if event["type"] == "review.required"
    )

    resume_response = client.post(
        "/messages/resume",
        json={
            "review_id": review_id,
            "approved": False,
            "feedback": "Read README.md instead.",
        },
    )

    assert resume_response.status_code == 200
    resume_result = _stream_result(_sse_events(resume_response.text)[-1])
    assert resume_result["output_text"] == "I will read the allowed README instead."
    assert "Reviewer feedback: Read README.md instead." in provider.requests[1]["messages"][-1]["content"]


def test_api_run_trace_endpoint_reads_tool_mode_run_trace() -> None:
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
        json=_message_request("echo hello", target="tool"),
    )
    events = _sse_events(response.text)
    run_id = _result_run_id(_stream_result(events[-1]))

    trace_response = client.get(f"/runs/{run_id}/trace")

    assert trace_response.status_code == 200
    assert trace_response.json()["run_id"] == run_id
    trace = trace_response.json()["trace"]
    capability = _capability_trace(trace, "tool.echo")
    assert capability["capability_execution"]["invocation"]["arguments"] == {"text": "hello"}
    assert capability["capability_execution"]["result"]["content"] == "echo:hello"
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
        json=_message_request("echo ok through a DAG", target="auto", review_level="careful"),
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    event_types = [event["type"] for event in events]
    result = _stream_result(events[-1])
    assert "dag.updated" in event_types
    assert event_types[-1] == "run.finished"
    assert _result_status(result) == "awaiting_review"
    assert _result_review(result)["kind"] == "initial_dag"
    assert _result_dag(result)["status"] == "review_required"
    assert result["output_text"] == ""


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
        json=_message_request("echo ok through a DAG", target="auto", review_level="fast"),
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    done_index = next(index for index, event in enumerate(events) if event["type"] == "run.finished")
    reasoning_text = "".join(
        event["data"]["delta"]
        for event in events
        if event["type"] == "response.reasoning.delta"
    )
    result = _stream_result(events[-1])
    assert not any(event["type"] == "response.raw.delta" for event in events)
    assert reasoning_text == "planning dag"
    # Chat endpoints drop dag content deltas; the answer arrives in run.finished.
    assert not any(event["type"] == "response.content.delta" for event in events)
    assert any(event.get("type") == "trace.updated" for event in events[:done_index])
    assert _result_status(result) == "completed"
    assert _result_dag(result)["status"] == "completed"
    assert result["output_text"] == "Final answer: echo:ok"


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
        json=_message_request("repair through DAG", target="auto", review_level="fast"),
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    dag_events = [event["data"]["dag"] for event in events if event["type"] == "dag.updated"]
    assert any(dag["version"] == 1 and dag["status"] == "failed" for dag in dag_events)
    assert any(dag["version"] == 2 and dag["status"] == "completed" for dag in dag_events)
    assert _result_status(_stream_result(events[-1])) == "completed"


def test_api_dynamic_adjust_false_keeps_generated_dag_fixed_after_failure() -> None:
    state.runner = _runner(
        MockProvider([
            ChatResponse(content="dag"),                                              # _route()
            ChatResponse(content='task: fail first\nbad = fail_tool(text="boom")\n'),  # DAG agent initial
            ChatResponse(content='task: repaired\nanswer = echo(text="ok")\n'),        # would replan if enabled
            ChatResponse(content="Recovered after replanning."),
        ])
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json=_message_request(
            "repair through DAG",
            target="auto",
            review_level="fast",
            dynamic_adjust=False,
        ),
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    dag_events = [event["data"]["dag"] for event in events if event["type"] == "dag.updated"]
    result = _stream_result(events[-1])
    assert any(dag["version"] == 1 and dag["status"] == "failed" for dag in dag_events)
    assert not any(dag["version"] == 2 for dag in dag_events)
    assert _result_status(result) == "failed"
    assert result["state"]["dynamic_adjust"] is False


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
        json=_message_request("run a failing DAG", target="dag", review_level="fast"),
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    result = _stream_result(events[-1])
    assert _result_status(result) == "failed"
    assert _result_dag(result)["status"] == "failed"


def test_api_resume_executes_reviewed_dag_and_run_trace_endpoint_reads_run_trace() -> None:
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
        json=_message_request("echo ok through a DAG", target="auto", review_level="careful"),
    )
    stream_events = _sse_events(stream_response.text)
    stream_result = _stream_result(stream_events[-1])
    run_id = _result_run_id(stream_result)
    review_id = _result_review(stream_result)["review_id"]
    dag = _result_dag(stream_result)
    dag["nodes"][0]["payload"]["invocation"]["arguments"] = {"text": "reviewed"}

    resume_response = client.post(
        "/messages/resume",
        json={"review_id": review_id, "dag": dag},
    )

    assert resume_response.status_code == 200
    resume_events = _sse_events(resume_response.text)
    resume_result = _stream_result(resume_events[-1])
    assert _result_status(resume_result) == "completed"
    assert _result_dag(resume_result)["status"] == "completed"
    assert any(event.get("type") == "trace.updated" for event in resume_events)

    trace_response = client.get(f"/runs/{run_id}/trace")
    assert trace_response.status_code == 200
    assert trace_response.json()["run_id"] == run_id
    trace = trace_response.json()["trace"]
    answer = _dag_node_trace(trace, "answer")
    capability = _capability_trace(answer, "tool.echo")
    assert capability["capability_execution"]["invocation"]["arguments"] == {"text": "reviewed"}
    assert capability["capability_execution"]["result"]["content"] == "echo:reviewed"
    assert capability["status"] == "completed"


def test_api_legacy_tasks_trace_route_is_not_available() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="done")]))
    client = TestClient(app)

    response = client.get("/tasks/run_1/trace")

    assert response.status_code == 404


def test_api_resume_review_delivers_dag_answer_only_in_run_finished() -> None:
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
        json=_message_request("echo ok through a DAG", target="auto", review_level="careful"),
    )
    stream_events = _sse_events(stream_response.text)
    stream_result = _stream_result(stream_events[-1])
    review_id = _result_review(stream_result)["review_id"]
    dag = _result_dag(stream_result)

    resume_response = client.post(
        "/messages/resume",
        json={"review_id": review_id, "dag": dag},
    )

    assert resume_response.status_code == 200
    events = _sse_events(resume_response.text)
    reasoning_text = "".join(
        event["data"]["delta"]
        for event in events
        if event["type"] == "response.reasoning.delta"
    )
    assert not any(event["type"] == "response.raw.delta" for event in events)
    assert reasoning_text == "observe"
    # Chat endpoints drop dag content deltas; the answer arrives in run.finished.
    assert not any(event["type"] == "response.content.delta" for event in events)
    assert _stream_result(events[-1])["output_text"] == "DAG agent final answer"


def test_api_tool_validation_holds_answer_until_validation_passes() -> None:
    state.runner = _runner(
        MockProvider([
            ChatResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hello"})],
            ),
            ChatResponse(content="Validated final answer."),
            ChatResponse(content='{"passed": true, "issues": [], "summary": "good"}'),
        ])
    )
    state.runner.enable_validation = True
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json=_message_request("echo hello", target="tool"),
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    event_types = [event["type"] for event in events]
    passed_index = event_types.index("validation.passed")
    content_indexes = [
        index for index, event in enumerate(events)
        if event["type"] == "response.content.delta"
    ]
    content_text = "".join(
        event["data"]["delta"]
        for event in events
        if event["type"] == "response.content.delta"
    )
    assert "validation.started" in event_types
    assert content_indexes and all(index > passed_index for index in content_indexes)
    assert content_text == "Validated final answer."
    assert event_types[-1] == "run.finished"
    assert _stream_result(events[-1])["output_text"] == "Validated final answer."


def test_api_tool_validation_retry_never_streams_rejected_answer() -> None:
    state.runner = _runner(
        MockProvider([
            ChatResponse(content="Rejected draft answer."),
            ChatResponse(content='{"passed": false, "issues": [{"message": "incomplete"}], "summary": "needs work"}'),
            ChatResponse(content="Improved final answer."),
            ChatResponse(content='{"passed": true, "issues": [], "summary": "good"}'),
        ])
    )
    state.runner.enable_validation = True
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json=_message_request("answer me", target="tool"),
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    event_types = [event["type"] for event in events]
    passed_index = event_types.index("validation.passed")
    content_indexes = [
        index for index, event in enumerate(events)
        if event["type"] == "response.content.delta"
    ]
    content_text = "".join(
        event["data"]["delta"]
        for event in events
        if event["type"] == "response.content.delta"
    )
    assert event_types.index("validation.retry") < passed_index
    assert "Rejected draft" not in content_text
    assert content_text == "Improved final answer."
    assert all(index > passed_index for index in content_indexes)
    assert _stream_result(events[-1])["output_text"] == "Improved final answer."


def test_api_old_task_and_dag_lifecycle_routes_are_removed() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    assert client.post("/tasks", json={"message": "old path"}).status_code == 404
    assert client.get("/dag-specs").status_code == 404
    assert client.post("/dag-specs", json={}).status_code == 404
    assert client.get("/dag-specs/task_api").status_code == 404
    assert client.post("/dag-specs/task_api/run").status_code == 404
    assert client.post("/dag-specs/task_api/run/stream").status_code == 404
    assert client.post("/dag-specs/task_api/artifacts/source/upload").status_code == 404
    assert client.post("/dags/task_api/approve").status_code == 404
    assert client.post("/dags/task_api/execute").status_code == 404
    assert client.put("/dags/task_api", json={"dag": {}}).status_code == 405


def test_api_capability_list_create_and_test() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    list_response = client.get("/capabilities")

    assert list_response.status_code == 200
    capability_ids = {item["id"] for item in list_response.json()["capabilities"]}
    assert {"tool.echo", "memory.write", "tool.write_file"}.issubset(capability_ids)

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


def test_api_capability_test_infers_shell_boundary() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    command = f'{sys.executable} -c "print(1); print(2)"'

    response = client.post(
        "/capabilities/tool.shell/test",
        json={"arguments": {"command": command, "cwd": "."}},
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["status"] == "completed"
    assert result["policy_decision"] == {"allowed_paths": ["."]}
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


def test_api_profiles_lists_markdown_profiles(tmp_path, monkeypatch) -> None:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "assistant.md").write_text("# Assistant\n\nYou are helpful.", encoding="utf-8")
    (profile_dir / "conversation.md").write_text("# Custom Conversation\n\nUse project style.", encoding="utf-8")
    monkeypatch.setattr(state, "profile_directory", str(profile_dir))
    client = TestClient(app)

    response = client.get("/profiles")

    assert response.status_code == 200
    payload = response.json()
    assert payload["warnings"] == []
    profiles = {(profile["source"], profile["name"]): profile for profile in payload["profiles"]}
    assert ("builtin", "conversation") in profiles
    assert profiles[("builtin", "conversation")]["id"] == "builtin:conversation"
    assert profiles[("user", "conversation")]["id"] == "user:conversation"
    assert profiles[("user", "assistant")] == {
        "id": "user:assistant",
        "name": "assistant",
        "description": "Assistant",
        "content": "# Assistant\n\nYou are helpful.",
        "source": "user",
    }


def test_api_profiles_warns_when_markdown_profile_cannot_be_loaded(tmp_path, monkeypatch) -> None:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "good.md").write_text("# Good\n\nUse helpful answers.", encoding="utf-8")
    (profile_dir / "bad.md").mkdir()
    monkeypatch.setattr(state, "profile_directory", str(profile_dir))
    client = TestClient(app)

    response = client.get("/profiles")

    assert response.status_code == 200
    payload = response.json()
    user_profiles = [profile for profile in payload["profiles"] if profile["source"] == "user"]
    assert [profile["name"] for profile in user_profiles] == ["good"]
    assert len(payload["warnings"]) == 1
    assert payload["warnings"][0]["name"] == "bad"


def test_api_dag_create_run_and_artifacts() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    create_response = client.post(
        "/dags",
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
                    "target": "tool.write_file",
                    "inputs": {
                        "path": "notes/output.txt",
                        "content": "hello",
                    },
                    "artifact_outputs": ["note"],
                    "boundary": {
                        "allowed_paths": ["notes/output.txt"],
                    },
                }
            ],
            "edges": [],
        },
    )
    assert create_response.status_code == 200
    assert create_response.json()["dag"]["nodes"][0]["target"] == "tool.write_file"

    get_spec_response = client.get("/dags/write_note")
    assert get_spec_response.status_code == 200
    assert get_spec_response.json()["dag"]["id"] == "write_note"

    list_response = client.get("/dags")
    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()["dags"]] == ["write_note"]

    run_response = client.post("/dags/write_note/run")
    assert run_response.status_code == 200
    result = run_response.json()["result"]
    _assert_result_shape(result)
    run_payload = _result_dag_run(result)
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


def test_api_dag_run_uses_requested_workspace_root(tmp_path: Path) -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    workspace_root = tmp_path / "runs"

    create_response = client.post(
        "/dags",
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
                    "target": "tool.write_file",
                    "inputs": {
                        "path": {"$expr": {"type": "artifact", "artifact_id": "note", "field": "path"}},
                        "content": "hello",
                    },
                    "artifact_outputs": ["note"],
                    "boundary": {
                        "allowed_paths": [
                            {"$expr": {"type": "artifact", "artifact_id": "note", "field": "path"}}
                        ],
                    },
                }
            ],
        },
    )
    assert create_response.status_code == 200

    run_response = client.post(
        "/dags/workspace_note/run",
        json={"workspace_root": str(workspace_root)},
    )

    assert run_response.status_code == 200
    run_payload = _result_dag_run(run_response.json()["result"])
    workspace_path = Path(run_payload["workspace_path"])
    assert workspace_path.parent == workspace_root
    assert (workspace_path / "notes" / "output.txt").read_text(encoding="utf-8") == "hello"


def test_api_dag_artifact_upload_materializes_input_file(tmp_path: Path) -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    workspace_root = tmp_path / "runs"

    create_response = client.post(
        "/dags",
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
                    "target": "tool.read_file",
                    "inputs": {
                        "path": {"$expr": {"type": "artifact", "artifact_id": "source", "field": "path"}},
                    },
                    "artifact_inputs": ["source"],
                    "boundary": {
                        "allowed_paths": [
                            {"$expr": {"type": "artifact", "artifact_id": "source", "field": "path"}}
                        ],
                    },
                }
            ],
        },
    )
    assert create_response.status_code == 200

    upload_response = client.post(
        "/dags/uploaded_source/artifacts/source/upload",
        files=[("files", ("source.txt", b"hello from upload", "text/plain"))],
    )
    assert upload_response.status_code == 200
    assert upload_response.json()["files"] == ["source.txt"]

    run_response = client.post(
        "/dags/uploaded_source/run",
        json={"workspace_root": str(workspace_root)},
    )

    assert run_response.status_code == 200
    run_payload = _result_dag_run(run_response.json()["result"])
    workspace_path = Path(run_payload["workspace_path"])
    assert run_payload["status"] == "completed"
    assert (workspace_path / "inputs" / "source.txt").read_text(encoding="utf-8") == "hello from upload"
    assert run_payload["trace"]["root"]["children"][0]["output"] == "hello from upload"
    assert run_payload["trace"]["artifacts"]["source"]["status"] == "created"


def test_api_created_tool_capability_can_run_in_dag() -> None:
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

    create_dag_response = client.post(
        "/dags",
        json={
            "id": "tool_dag",
            "name": "Tool DAG",
            "nodes": [
                {
                    "id": "call_custom",
                    "target": "tool.upper",
                    "inputs": {"text": "ok"},
                }
            ],
            "edges": [],
        },
    )
    assert create_dag_response.status_code == 200

    run_response = client.post("/dags/tool_dag/run")

    assert run_response.status_code == 200
    payload = _result_dag_run(run_response.json()["result"])
    assert payload["status"] == "completed"
    assert payload["trace"]["root"]["children"][0]["output"] == "upper:ok"


def test_api_dag_run_stream_returns_live_events_and_stores_run() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    create_response = client.post(
        "/dags",
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
                    "target": "tool.write_file",
                    "inputs": {
                        "path": "notes/output.txt",
                        "content": "hello",
                    },
                    "artifact_outputs": ["note"],
                    "boundary": {
                        "allowed_paths": ["notes/output.txt"],
                    },
                }
            ],
        },
    )
    assert create_response.status_code == 200

    response = client.post("/dags/stream_note/run/stream")

    assert response.status_code == 200
    events = _sse_events(response.text)
    result = _stream_result(events[-1])
    assert events[0]["type"] == "run.started"
    assert events[0]["run_id"] == result["state"]["run_id"]
    assert any(event["type"] == "trace.updated" for event in events)
    assert events[-1]["type"] == "run.finished"
    dag_run = _result_dag_run(result)
    assert dag_run["status"] == "completed"
    run_id = dag_run["run_id"]
    assert client.get(f"/dag-runs/{run_id}").json()["dag_run"]["run_id"] == run_id


def test_api_dag_run_stream_uses_requested_workspace_root(tmp_path: Path) -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    workspace_root = tmp_path / "stream-runs"
    create_response = client.post(
        "/dags",
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
                    "target": "tool.write_file",
                    "inputs": {
                        "path": {"$expr": {"type": "artifact", "artifact_id": "note", "field": "path"}},
                        "content": "hello",
                    },
                    "artifact_outputs": ["note"],
                    "boundary": {
                        "allowed_paths": [
                            {"$expr": {"type": "artifact", "artifact_id": "note", "field": "path"}}
                        ],
                    },
                }
            ],
        },
    )
    assert create_response.status_code == 200

    response = client.post(
        "/dags/stream_workspace_note/run/stream",
        json={"workspace_root": str(workspace_root)},
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    dag_run = _result_dag_run(_stream_result(events[-1]))
    workspace_path = Path(dag_run["workspace_path"])
    assert workspace_path.parent == workspace_root
    assert (workspace_path / "notes" / "output.txt").read_text(encoding="utf-8") == "hello"


def test_api_dag_run_fails_when_required_artifact_is_missing() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    create_response = client.post(
        "/dags",
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
                    "target": "tool.echo",
                    "inputs": {"text": "ok"},
                    "artifact_outputs": ["note"],
                }
            ],
        },
    )
    assert create_response.status_code == 200

    run_response = client.post("/dags/missing_output/run")

    assert run_response.status_code == 200
    payload = _result_dag_run(run_response.json()["result"])
    assert payload["status"] == "failed"
    assert payload["trace"]["artifacts"]["note"]["status"] == "missing"
    assert payload["dag"]["status"] == "failed"


def test_api_dag_run_returns_400_for_unknown_target() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    create_response = client.post(
        "/dags",
        json={
            "id": "unknown_target",
            "name": "Unknown target",
            "nodes": [
                {
                    "id": "answer",
                    "target": "tool.missing",
                    "inputs": {"text": "ok"},
                }
            ],
        },
    )
    assert create_response.status_code == 200

    run_response = client.post("/dags/unknown_target/run")

    assert run_response.status_code == 400
    assert "tool.missing" in run_response.json()["detail"]


def test_api_dag_validation_and_missing_resources() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    invalid_response = client.post(
        "/dags",
        json={
            "id": "invalid",
            "name": "Invalid",
            "artifacts": {},
            "nodes": [
                {
                    "id": "answer",
                    "target": "tool.echo",
                    "inputs": {"text": "ok"},
                    "artifact_inputs": ["missing"],
                }
            ],
        },
    )

    assert invalid_response.status_code == 400
    assert client.get("/dags/missing").status_code == 404
    assert client.post("/dags/missing/run").status_code == 404
    assert client.get("/dag-runs/missing").status_code == 404
    assert client.get("/dag-runs/missing/artifacts").status_code == 404


def test_api_validation_toggle_overrides_config_default_and_survives_reset(monkeypatch, tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join([
            "provider:",
            "  base_url: https://example.test/v1",
            "  model: cfg-model",
            "  api_key: test-key",
            "enable_result_validation: false",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("DAGENT_CONFIG", str(config))
    original_override = getattr(state, "validation_override", None)
    state.close_runner()
    setattr(state, "validation_override", None)
    client = TestClient(app)

    try:
        assert client.get("/settings/validation").json()["enabled"] is False

        enabled_response = client.post("/settings/validation", json={"enabled": True})

        assert enabled_response.status_code == 200
        assert enabled_response.json()["enabled"] is True
        assert state.get_runner().enable_validation is True
        assert state.get_runner().runtime.validator is not None

        reset_response = client.post("/session/reset")
        status_response = client.get("/settings/validation")

        assert reset_response.status_code == 200
        assert status_response.json()["enabled"] is True
        assert state.get_runner().enable_validation is True
        assert state.get_runner().runtime.validator is not None
    finally:
        state.close_runner()
        setattr(state, "validation_override", original_override)


def test_api_model_management_adds_runtime_model_and_activates_with_redacted_secret(monkeypatch, tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join([
            "provider:",
            "  base_url: https://config.example/v1",
            "  model: config-model",
            "  api_key: config-secret",
            "  extra_request_args:",
            "    headers:",
            "      Authorization: Bearer config-nested-secret",
            "  extra_body:",
            "    vendor_secret: config-body-secret",
            "enable_result_validation: false",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("DAGENT_CONFIG", str(config))
    state.close_runner()
    state.custom_model_providers.clear()
    state.active_model_id = None
    client = TestClient(app)

    try:
        listed = client.get("/models")
        created = client.post(
            "/models",
            json={
                "id": "local-qwen",
                "name": "Local Qwen",
                "base_url": "http://localhost:8000/v1",
                "model": "qwen3-coder",
                "api_key": "session-secret",
                "timeout_seconds": 42,
                "strip_thinking": True,
                "extra_request_args": {"headers": {"Authorization": "Bearer nested-secret"}, "timeout": 7},
                "extra_body": {"temperature": 0.2, "metadata": {"api_key": "nested-body-secret"}},
            },
        )
        activated = client.post("/models/local-qwen/activate")

        assert listed.status_code == 200
        assert listed.json()["active_model_id"] == "config"
        assert listed.json()["models"][0]["id"] == "config"
        assert listed.json()["models"][0]["source"] == "config"
        assert listed.json()["models"][0]["api_key_configured"] is True
        assert "api_key" not in listed.json()["models"][0]
        assert listed.json()["models"][0]["extra_request_args"]["headers"]["Authorization"] == "[redacted]"
        assert listed.json()["models"][0]["extra_body"]["vendor_secret"] == "[redacted]"

        assert created.status_code == 200
        created_model = created.json()["model"]
        assert created_model["id"] == "local-qwen"
        assert created_model["source"] == "runtime"
        assert created_model["api_key_configured"] is True
        assert "api_key" not in created_model
        assert created_model["extra_request_args"]["headers"]["Authorization"] == "[redacted]"
        assert created_model["extra_body"]["metadata"]["api_key"] == "[redacted]"

        assert activated.status_code == 200
        assert activated.json()["active_model_id"] == "local-qwen"
        assert activated.json()["model"]["active"] is True
        assert state.get_runner().runtime.provider.config.base_url == "http://localhost:8000/v1"
        assert state.get_runner().runtime.provider.config.model == "qwen3-coder"
        assert state.get_runner().runtime.provider.config.timeout_seconds == 42
        assert state.get_runner().runtime.provider.config.strip_thinking is True
        assert state.get_runner().runtime.provider.config.extra_request_args == {
            "headers": {"Authorization": "Bearer nested-secret"},
            "timeout": 7,
        }
        assert state.get_runner().runtime.provider.config.extra_body == {
            "temperature": 0.2,
            "metadata": {"api_key": "nested-body-secret"},
        }

        redacted_round_trip = client.put(
            "/models/local-qwen",
            json={
                "id": "local-qwen",
                "name": "Local Qwen",
                "base_url": "http://localhost:8000/v1",
                "model": "qwen3-coder",
                "api_key_action": "preserve",
                "timeout_seconds": 42,
                "strip_thinking": True,
                "extra_body": {"temperature": 0.2, "metadata": {"api_key": "[redacted]"}},
            },
        )

        assert redacted_round_trip.status_code == 200
        assert state.get_runner().runtime.provider.config.extra_body == {
            "temperature": 0.2,
            "metadata": {"api_key": "nested-body-secret"},
        }

        updated = client.put(
            "/models/local-qwen",
            json={
                "id": "local-qwen",
                "name": "Local Qwen",
                "base_url": "http://localhost:8000/v1",
                "model": "qwen3-coder",
                "api_key_action": "preserve",
                "timeout_seconds": 30,
                "strip_thinking": True,
                "extra_body": {"temperature": 0.4},
            },
        )

        assert updated.status_code == 200
        assert state.get_runner().runtime.provider.config.api_key == "session-secret"
        assert state.get_runner().runtime.provider.config.timeout_seconds == 30
        assert state.get_runner().runtime.provider.config.extra_body == {"temperature": 0.4}

        cleared = client.put(
            "/models/local-qwen",
            json={
                "id": "local-qwen",
                "name": "Local Qwen",
                "base_url": "http://localhost:8000/v1",
                "model": "qwen3-coder",
                "api_key_action": "clear",
                "timeout_seconds": 30,
                "strip_thinking": True,
            },
        )

        assert cleared.status_code == 200
        assert cleared.json()["model"]["api_key_configured"] is False
        assert state.custom_model_providers["local-qwen"].api_key is None
    finally:
        state.close_runner()
        state.custom_model_providers.clear()
        state.active_model_id = None


def test_api_model_management_deletes_active_runtime_model_and_returns_to_config(monkeypatch, tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join([
            "provider:",
            "  base_url: https://config.example/v1",
            "  model: config-model",
            "  api_key: config-secret",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("DAGENT_CONFIG", str(config))
    state.close_runner()
    state.custom_model_providers.clear()
    state.active_model_id = None
    client = TestClient(app)

    try:
        assert client.post(
            "/models",
            json={
                "id": "runtime-model",
                "name": "Runtime Model",
                "base_url": "https://runtime.example/v1",
                "model": "runtime-model",
                "api_key_env": "RUNTIME_API_KEY",
            },
        ).status_code == 200
        assert client.post("/models/runtime-model/activate").status_code == 200

        deleted = client.delete("/models/runtime-model")
        listed = client.get("/models")

        assert deleted.status_code == 200
        assert deleted.json()["active_model_id"] == "config"
        assert listed.json()["active_model_id"] == "config"
        assert [model["id"] for model in listed.json()["models"]] == ["config"]
        assert state.get_runner().runtime.provider.config.base_url == "https://config.example/v1"
        assert state.get_runner().runtime.provider.config.model == "config-model"
    finally:
        state.close_runner()
        state.custom_model_providers.clear()
        state.active_model_id = None


def _runner(provider: MockProvider, *, skill_roots: list[Path] | None = None) -> Runner:
    runner = Runner(provider=provider, skill_roots=skill_roots)
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


def _message_request(message: str, **fields) -> dict:
    return {
        "messages": [{"role": "user", "content": message}],
        **fields,
    }


def _sse_events(text: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in text.splitlines()
        if line.startswith("data: ")
    ]


def _stream_result(event: dict) -> dict:
    assert event["type"] == "run.finished"
    return event["data"]["result"]


def _assert_result_shape(result: dict) -> None:
    assert set(result) == {"output_text", "state"}


def _result_review(result: dict) -> dict | None:
    state_payload = result.get("state") or {}
    return state_payload.get("pending_review")


def _result_run_id(result: dict) -> str | None:
    state_payload = result.get("state") or {}
    return state_payload.get("run_id")


def _result_status(result: dict) -> str | None:
    state_payload = result.get("state") or {}
    return state_payload.get("status")


def _result_dag(result: dict) -> dict | None:
    state_payload = result.get("state") or {}
    return state_payload.get("dag")


def _result_trace(result: dict) -> dict | None:
    state_payload = result.get("state") or {}
    return state_payload.get("trace")


def _result_dag_run(result: dict) -> dict:
    state_payload = result["state"]
    return {
        "run_id": state_payload["run_id"],
        "spec_id": state_payload.get("spec_id"),
        "workspace_path": state_payload.get("workspace_path") or "",
        "dag": state_payload["dag"],
        "trace": state_payload["trace"],
        "status": state_payload["status"],
    }


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
