import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient

import api.app as app_module
import dagent.runner as runner_module
from api.agent_presets import AgentPreset
from api.app import app, state
from dagent import ToolAgent
from dagent.capabilities.providers import ToolCapabilityProvider
from dagent.capabilities.tools.registry import ToolRegistry
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.runner import Runner
from dagent.schemas import CapabilityDefinition, CapabilityPolicy, CapabilityResult


@pytest.fixture(autouse=True)
def isolate_user_config(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(state, "get_user_config_path", lambda: tmp_path / ".dagent" / "config.yaml")


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


def test_api_mcp_server_name_is_strict_local_key() -> None:
    state.close_runner()
    state.custom_mcp_servers.clear()
    state.custom_mcp_errors.clear()
    client = TestClient(app)

    response = client.post(
        "/mcp/servers",
        json={"name": "github-search", "command": "fake"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "MCP server name is a local workspace key and may contain only letters, numbers, and underscores."
    )
    assert "github-search" not in state.custom_mcp_servers


def test_api_mcp_update_rejects_registered_agent_dependency_without_mutation(monkeypatch) -> None:
    class FakeMCPProvider:
        def __init__(self, servers, *, manager=None):
            self.servers = servers
            self.manager = SimpleNamespace(last_errors={})
            self.registration_errors: list[str] = []

        def register_into(self, catalog):
            for name, config in self.servers.items():
                catalog.register(
                    CapabilityDefinition(
                        id=f"mcp.{name}.lookup",
                        kind="mcp",
                        policy=CapabilityPolicy(risk=config.get("risk", "medium")),
                        config={"server": name, "tool": "lookup"},
                    ),
                    lambda invocation: CapabilityResult.completed(invocation, "found"),
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
    state.runner.add_agent(ToolAgent(
        profile=AgentProfile(name="helper_profile", content="Registered helper profile."),
        name="helper",
        capabilities=["mcp.mock.lookup"],
        skills=[],
    ))
    update_response = client.put(
        "/mcp/servers/mock",
        json={"name": "mock", "command": "fake", "risk": "high"},
    )

    assert create_response.status_code == 200
    assert update_response.status_code == 400
    assert "agent.helper" in update_response.json()["detail"]
    assert state.custom_mcp_servers["mock"]["risk"] == "low"
    assert state.runner.get_capability("mcp.mock.lookup") is not None


def test_api_mcp_delete_rejects_registered_agent_dependency_without_mutation(monkeypatch) -> None:
    class FakeMCPProvider:
        def __init__(self, servers, *, manager=None):
            self.servers = servers
            self.manager = SimpleNamespace(last_errors={})
            self.registration_errors: list[str] = []

        def register_into(self, catalog):
            for name in self.servers:
                catalog.register(
                    CapabilityDefinition(
                        id=f"mcp.{name}.lookup",
                        kind="mcp",
                        config={"server": name, "tool": "lookup"},
                    ),
                    lambda invocation: CapabilityResult.completed(invocation, "found"),
                )

    state.close_runner()
    state.custom_mcp_servers.clear()
    state.custom_mcp_errors.clear()
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    monkeypatch.setattr(runner_module.MCPServerManager, "available", True)
    monkeypatch.setattr(runner_module, "MCPCapabilityProvider", FakeMCPProvider)
    client = TestClient(app, raise_server_exceptions=False)

    create_response = client.post(
        "/mcp/servers",
        json={"name": "mock", "command": "fake"},
    )
    state.runner.add_agent(ToolAgent(
        profile=AgentProfile(name="helper_profile", content="Registered helper profile."),
        name="helper",
        capabilities=["mcp.mock.lookup"],
        skills=[],
    ))
    delete_response = client.delete("/mcp/servers/mock")

    assert create_response.status_code == 200
    assert delete_response.status_code == 400
    assert "agent.helper" in delete_response.json()["detail"]
    assert "mock" in state.custom_mcp_servers
    assert state.runner.get_capability("mcp.mock.lookup") is not None


def test_api_mcp_create_allows_unrelated_server_when_agent_depends_on_existing_mcp(monkeypatch) -> None:
    class FakeMCPProvider:
        def __init__(self, servers, *, manager=None):
            self.servers = servers
            self.manager = SimpleNamespace(last_errors={})
            self.registration_errors: list[str] = []

        def register_into(self, catalog):
            for name in self.servers:
                catalog.register(
                    CapabilityDefinition(
                        id=f"mcp.{name}.lookup",
                        kind="mcp",
                        config={"server": name, "tool": "lookup"},
                    ),
                    lambda invocation: CapabilityResult.completed(invocation, "found"),
                )

    state.close_runner()
    state.custom_mcp_servers.clear()
    state.custom_mcp_errors.clear()
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    monkeypatch.setattr(runner_module.MCPServerManager, "available", True)
    monkeypatch.setattr(runner_module, "MCPCapabilityProvider", FakeMCPProvider)
    client = TestClient(app)

    search_response = client.post(
        "/mcp/servers",
        json={"name": "search", "command": "fake"},
    )
    state.runner.add_agent(ToolAgent(
        profile=AgentProfile(name="helper_profile", content="Registered helper profile."),
        name="helper",
        capabilities=["mcp.search.lookup"],
        skills=[],
    ))
    weather_response = client.post(
        "/mcp/servers",
        json={"name": "weather", "command": "fake"},
    )

    assert search_response.status_code == 200
    assert weather_response.status_code == 200
    assert state.runner.get_capability("mcp.search.lookup") is not None
    assert state.runner.get_capability("mcp.weather.lookup") is not None
    assert set(state.custom_mcp_servers) == {"search", "weather"}


def test_api_memory_http_mcp_server_uses_url_config(monkeypatch) -> None:
    class FakeMCPProvider:
        def __init__(self, servers, *, manager=None):
            self.servers = servers
            self.manager = SimpleNamespace(last_errors={})
            self.registration_errors: list[str] = []

        def register_into(self, catalog):
            for name, config in self.servers.items():
                catalog.register(
                    CapabilityDefinition(
                        id=f"mcp.{name}.lookup",
                        kind="mcp",
                        description="Lookup.",
                        policy=CapabilityPolicy(
                            risk=config.get("risk", "medium"),
                            network=bool(config.get("url")),
                        ),
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

    response = client.post(
        "/mcp/servers",
        json={
            "name": "remote",
            "transport": "http",
            "url": "https://mcp.example.test/mcp",
            "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
            "risk": "high",
        },
    )

    assert response.status_code == 200
    server = response.json()["server"]
    assert server["status"] == "connected"
    assert server["config"] == {
        "transport": "http",
        "url": "https://mcp.example.test/mcp",
        "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
        "enabled": True,
        "risk": "high",
        "connect_timeout": 30,
        "tool_timeout": 60,
    }
    assert server["tools"][0]["policy"]["network"] is True


def test_api_rejects_enabled_http_mcp_server_without_url() -> None:
    state.close_runner()
    state.custom_mcp_servers.clear()
    state.custom_mcp_errors.clear()
    client = TestClient(app)

    response = client.post(
        "/mcp/servers",
        json={"name": "remote", "transport": "http"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Enabled HTTP MCP servers require a url."


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


def test_api_message_stream_rejects_relative_workspace_root_escape() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json=_message_request("hello", target="tool", workspace_root="../outside"),
    )

    assert response.status_code == 400
    assert "workspace_root" in response.json()["detail"]


def test_api_message_stream_scopes_capabilities_and_skills(monkeypatch, tmp_path) -> None:
    state.close_runner()
    managed_root = tmp_path / "managed-skills"
    monkeypatch.setattr(state, "get_managed_skill_root", lambda: managed_root)
    monkeypatch.setattr(state, "get_skill_roots", lambda: [managed_root])
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_1", name="skill_list", arguments={})]),
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
        "tool_echo",
        "skill_list",
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
                        name="tool_read_file",
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
                    name="tool_read_file",
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
            tool_calls=[ToolCall(id="call_1", name="tool_echo", arguments={"text": "hello"})],
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
            ChatResponse(content='task: fail first\nbad = tool_fail_tool(text="boom")\n'),  # DAG agent initial
            ChatResponse(content='task: repaired\nanswer = tool_echo(text="ok")\n'),        # replan
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
            ChatResponse(content='task: fail first\nbad = tool_fail_tool(text="boom")\n'),  # DAG agent initial
            ChatResponse(content='task: repaired\nanswer = tool_echo(text="ok")\n'),        # would replan if enabled
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
            ChatResponse(content='task: fail\nbad = tool_fail_tool(text="boom")\n'),
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
                tool_calls=[ToolCall(id="call_1", name="tool_echo", arguments={"text": "hello"})],
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


def test_api_capability_test_rejects_removed_top_level_boundary_fields() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    command = f'{sys.executable} -c "print(1)"'

    for stale_payload in (
        {"boundary_mode": "read_only"},
        {"mode": "read_only"},
        {"allowed_commands": ["ls"]},
    ):
        response = client.post(
            "/capabilities/tool.shell/test",
            json={
                "arguments": {"command": command, "cwd": "."},
                **stale_payload,
            },
        )

        assert response.status_code == 422


def test_api_capability_test_rejects_removed_nested_boundary_fields() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app, raise_server_exceptions=False)
    command = f'{sys.executable} -c "print(1)"'

    response = client.post(
        "/capabilities/tool.shell/test",
        json={
            "arguments": {"command": command, "cwd": "."},
            "boundary": {"allowed_paths": ["."], "mode": "read_only"},
        },
    )

    assert response.status_code == 422


def test_api_rejects_removed_custom_tool_capability_kind() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    response = client.post(
        "/capabilities",
        json={
            "id": "custom_tool.upper",
            "kind": "custom_tool",
            "description": "Uppercase text.",
            "config": {"template": "upper:{text}"},
        },
    )

    assert response.status_code == 422


def test_api_rejects_whitespace_padded_capability_id_without_persisting() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    response = client.post(
        "/capabilities",
        json={
            "id": "tool.upper ",
            "kind": "tool",
            "description": "Uppercase text.",
            "config": {"template": "upper:{text}"},
        },
    )

    assert response.status_code == 400
    assert "Capability ids" in response.json()["detail"]
    assert state.runner.get_capability("tool.upper ") is None
    assert state.runner.get_capability("tool.upper") is None
    assert "tool.upper " not in state.custom_capabilities
    state.close_runner()


def test_api_delete_capability_rejects_registered_agent_dependency_without_mutation() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app, raise_server_exceptions=False)
    create_response = client.post(
        "/capabilities",
        json={
            "id": "tool.search",
            "kind": "tool",
            "config": {"template": "found:{q}"},
        },
    )
    state.runner.add_agent(ToolAgent(
        profile=AgentProfile(name="helper_profile", content="Registered helper profile."),
        name="helper",
        capabilities=["tool.search"],
        skills=[],
    ))

    delete_response = client.delete("/capabilities/tool.search")

    assert create_response.status_code == 200
    assert delete_response.status_code == 400
    assert "agent.helper" in delete_response.json()["detail"]
    assert "tool.search" in state.custom_capabilities
    assert state.runner.get_capability("tool.search") is not None


def test_api_disable_capability_rejects_registered_agent_dependency_without_mutation() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app, raise_server_exceptions=False)
    create_response = client.post(
        "/capabilities",
        json={
            "id": "tool.search",
            "kind": "tool",
            "config": {"template": "found:{q}"},
        },
    )
    state.runner.add_agent(ToolAgent(
        profile=AgentProfile(name="helper_profile", content="Registered helper profile."),
        name="helper",
        capabilities=["tool.search"],
        skills=[],
    ))

    disable_response = client.post("/capabilities/tool.search/disable")

    assert create_response.status_code == 200
    assert disable_response.status_code == 400
    assert "agent.helper" in disable_response.json()["detail"]
    assert state.runner.get_capability("tool.search").enabled is True
    assert state.runner.get_capability("agent.helper") is not None


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
    managed_dir = tmp_path / "managed-profiles"
    profile_dir.mkdir()
    managed_dir.mkdir()
    (profile_dir / "assistant.md").write_text("# Assistant\n\nYou are helpful.", encoding="utf-8")
    (profile_dir / "conversation.md").write_text("# Custom Conversation\n\nUse project style.", encoding="utf-8")
    (managed_dir / "analyst.md").write_text("# Analyst\n\nRead carefully.", encoding="utf-8")
    monkeypatch.setattr(state, "profile_directory", str(profile_dir))
    monkeypatch.setattr(state, "get_managed_profile_root", lambda: managed_dir)
    client = TestClient(app)

    response = client.get("/profiles")

    assert response.status_code == 200
    payload = response.json()
    assert payload["warnings"] == []
    profiles = {(profile["source"], profile["name"]): profile for profile in payload["profiles"]}
    assert ("builtin", "conversation") in profiles
    assert profiles[("builtin", "conversation")]["id"] == "builtin:conversation"
    assert profiles[("config", "conversation")]["id"] == "config:conversation"
    assert profiles[("managed", "analyst")]["editable"] is True
    assert profiles[("config", "assistant")] == {
        "id": "config:assistant",
        "name": "assistant",
        "description": "Assistant",
        "content": "# Assistant\n\nYou are helpful.",
        "source": "config",
        "editable": False,
        "deletable": False,
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
    config_profiles = [profile for profile in payload["profiles"] if profile["source"] == "config"]
    assert [profile["name"] for profile in config_profiles] == ["good"]
    assert len(payload["warnings"]) == 1
    assert payload["warnings"][0]["name"] == "bad"


def test_api_managed_profile_crud_and_validation(monkeypatch, tmp_path) -> None:
    state.close_runner()
    managed_dir = tmp_path / "managed-profiles"
    monkeypatch.setattr(state, "get_managed_profile_root", lambda: managed_dir)
    client = TestClient(app)

    created = client.post("/profiles", json={"name": "analyst", "content": "# Analyst\n\nRead carefully."})
    listed = client.get("/profiles")

    assert created.status_code == 200
    assert created.json()["profile"]["source"] == "managed"
    assert created.json()["profile"]["editable"] is True
    assert (managed_dir / "analyst.md").read_text(encoding="utf-8") == "# Analyst\n\nRead carefully."
    assert ("managed", "analyst") in {
        (profile["source"], profile["name"])
        for profile in listed.json()["profiles"]
    }
    updated = client.put("/profiles/analyst", json={"content": "# Analyst\n\nUse terse answers."})

    assert updated.status_code == 200
    assert updated.json()["profile"]["content"] == "# Analyst\n\nUse terse answers."

    deleted = client.delete("/profiles/analyst")
    invalid = client.post("/profiles", json={"name": "../bad", "content": "Nope."})

    assert deleted.status_code == 200
    assert not (managed_dir / "analyst.md").exists()
    assert invalid.status_code == 400


def test_api_managed_profiles_surface_agent_capabilities(monkeypatch, tmp_path) -> None:
    state.close_runner()
    managed_dir = tmp_path / "managed-profiles"
    monkeypatch.setattr(state, "get_managed_profile_root", lambda: managed_dir)
    managed_dir.mkdir()
    (managed_dir / "analyst.md").write_text("# Analyst\n\nRead carefully.", encoding="utf-8")
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    response = client.get("/capabilities", params={"kind": "agent"})

    assert response.status_code == 200
    capabilities = {capability["id"]: capability for capability in response.json()["capabilities"]}
    assert capabilities["agent.analyst"]["kind"] == "agent"
    assert capabilities["agent.analyst"]["config"] == {"profile": "analyst", "source": "managed"}
    assert capabilities["agent.analyst"]["parameters"]["properties"]["prompt"]["default"] == ""
    assert capabilities["agent.analyst"]["parameters"]["properties"]["max_steps"]["default"] == 8


def test_api_profile_agent_capability_ids_use_clean_names(monkeypatch) -> None:
    monkeypatch.setattr(
        app_module,
        "_agent_profile_candidates",
        lambda: [("managed", AgentProfile(name=" analyst ", description="Analyst", content="Read carefully."))],
    )

    definitions = app_module._profile_agent_capabilities()

    assert [definition.id for definition in definitions] == ["agent.analyst"]


def test_api_agent_preset_crud_registers_agent_capability(monkeypatch, tmp_path) -> None:
    state.close_runner()
    agent_root = tmp_path / "agents"
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    created = client.post(
        "/agents",
        json={
            "name": "helper",
            "profile": "conversation",
            "description": "Summarizes text.",
            "max_steps": 1,
            "capabilities": [],
            "skills": [],
            "agents": [],
            "review": "fast",
        },
    )
    listed = client.get("/agents")
    capabilities = client.get("/capabilities", params={"kind": "agent"})
    deleted = client.delete("/agents/helper")

    assert created.status_code == 200
    assert created.json()["agent"]["id"] == "agent.helper"
    assert created.json()["agent"]["profile"] == "conversation"
    assert created.json()["agent"]["capabilities"] == []
    assert listed.status_code == 200
    assert [agent["id"] for agent in listed.json()["agents"]] == ["agent.helper"]
    capability_ids = {capability["id"] for capability in capabilities.json()["capabilities"]}
    assert "agent.helper" in capability_ids
    assert deleted.status_code == 200
    assert not (agent_root / "helper.json").exists()
    state.close_runner()


def test_api_capability_endpoints_reject_agent_preset_capabilities(monkeypatch, tmp_path) -> None:
    state.close_runner()
    agent_root = tmp_path / "agents"
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    created = client.post(
        "/agents",
        json={
            "name": "helper",
            "profile": "conversation",
            "capabilities": [],
            "skills": [],
        },
    )

    disabled = client.post("/capabilities/agent.helper/disable")
    deleted = client.delete("/capabilities/agent.helper")

    assert created.status_code == 200
    assert disabled.status_code == 400
    assert "agents" in disabled.json()["detail"]
    assert deleted.status_code == 400
    assert (agent_root / "helper.json").exists()
    assert state.get_runner().get_capability("agent.helper") is not None
    state.close_runner()


def test_api_agent_preset_rejects_legacy_capability_ids_request(monkeypatch, tmp_path) -> None:
    state.close_runner()
    agent_root = tmp_path / "agents"
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    response = client.post(
        "/agents",
        json={
            "name": "helper",
            "profile": "conversation",
            "capability_ids": [],
            "skills": [],
        },
    )

    assert response.status_code == 422
    assert not (agent_root / "helper.json").exists()
    assert state.get_runner().get_capability("agent.helper") is None
    state.close_runner()


def test_api_agent_preset_rejects_missing_capability_without_persisting(monkeypatch, tmp_path) -> None:
    state.close_runner()
    agent_root = tmp_path / "agents"
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    response = client.post(
        "/agents",
        json={
            "name": "helper",
            "profile": "conversation",
            "capabilities": ["tool.missing"],
            "skills": [],
        },
    )

    assert response.status_code == 400
    assert "tool.missing" in response.json()["detail"]
    assert not (agent_root / "helper.json").exists()
    assert state.get_runner().get_capability("agent.helper") is None
    state.close_runner()


def test_api_agent_preset_rejects_nested_agents_and_non_fast_review(monkeypatch, tmp_path) -> None:
    state.close_runner()
    agent_root = tmp_path / "agents"
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    nested = client.post(
        "/agents",
        json={
            "name": "helper",
            "profile": "conversation",
            "capabilities": [],
            "skills": [],
            "agents": ["agent.other"],
        },
    )
    careful = client.post(
        "/agents",
        json={
            "name": "reviewed",
            "profile": "conversation",
            "capabilities": [],
            "skills": [],
            "review": "careful",
        },
    )

    assert nested.status_code == 400
    assert "cannot expose subagents" in nested.json()["detail"]
    assert careful.status_code == 400
    assert "review=\"fast\"" in careful.json()["detail"]
    assert not (agent_root / "helper.json").exists()
    assert not (agent_root / "reviewed.json").exists()
    state.close_runner()


def test_api_retries_agent_preset_registration_after_capability_dependency_is_created(monkeypatch, tmp_path) -> None:
    state.close_runner()
    agent_root = tmp_path / "agents"
    agent_root.mkdir()
    (agent_root / "helper.json").write_text(
        json.dumps({
            "name": "helper",
            "profile": "conversation",
            "description": "",
            "max_steps": 1,
            "capabilities": ["tool.late"],
            "skills": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    monkeypatch.setattr(state, "_create_runner", lambda: Runner(provider=MockProvider([])))
    client = TestClient(app)

    response = client.post(
        "/capabilities",
        json=CapabilityDefinition(id="tool.late", kind="tool").model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert state.get_runner().get_capability("agent.helper") is not None
    assert "helper" not in state.agent_preset_errors
    list_response = client.get("/agents")
    assert "helper" not in list_response.json()["errors"]
    state.close_runner()


def test_api_agent_preset_rejects_profile_name_collision(monkeypatch, tmp_path) -> None:
    state.close_runner()
    agent_root = tmp_path / "agents"
    managed_dir = tmp_path / "managed-profiles"
    managed_dir.mkdir()
    (managed_dir / "helper.md").write_text("# Helper\n\nProfile helper.", encoding="utf-8")
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    monkeypatch.setattr(state, "get_managed_profile_root", lambda: managed_dir)
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    response = client.post(
        "/agents",
        json={
            "name": "helper",
            "profile": "conversation",
            "capabilities": [],
            "skills": [],
        },
    )

    assert response.status_code == 400
    assert "profile" in response.json()["detail"]
    assert not (agent_root / "helper.json").exists()
    state.close_runner()


def test_api_agents_skips_invalid_preset_files(monkeypatch, tmp_path) -> None:
    state.close_runner()
    agent_root = tmp_path / "agents"
    agent_root.mkdir()
    (agent_root / "broken.json").write_text("{not json", encoding="utf-8")
    (agent_root / "helper.json").write_text(
        json.dumps({
            "name": "helper",
            "profile": "conversation",
            "description": "",
            "max_steps": 1,
            "capabilities": [],
            "skills": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    monkeypatch.setattr(state, "_create_runner", lambda: _runner(MockProvider([ChatResponse(content="unused")])))
    client = TestClient(app)

    list_response = client.get("/agents")
    runner = state.get_runner()

    assert list_response.status_code == 200
    payload = list_response.json()
    assert [agent["id"] for agent in payload["agents"]] == ["agent.helper"]
    assert payload["errors"]["broken"]
    assert runner.get_capability("agent.helper") is not None
    assert state.agent_preset_errors["broken"]
    state.close_runner()


def test_api_agents_reports_persisted_preset_profile_name_collision(monkeypatch, tmp_path) -> None:
    state.close_runner()
    agent_root = tmp_path / "agents"
    managed_dir = tmp_path / "managed-profiles"
    agent_root.mkdir()
    managed_dir.mkdir()
    (managed_dir / "helper.md").write_text("# Helper\n\nProfile helper.", encoding="utf-8")
    (agent_root / "helper.json").write_text(
        json.dumps({
            "name": "helper",
            "profile": "conversation",
            "description": "",
            "max_steps": 1,
            "capabilities": [],
            "skills": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    monkeypatch.setattr(state, "get_managed_profile_root", lambda: managed_dir)
    monkeypatch.setattr(state, "_create_runner", lambda: _runner(MockProvider([ChatResponse(content="unused")])))
    client = TestClient(app)

    list_response = client.get("/agents")
    runner = state.get_runner()

    assert list_response.status_code == 200
    payload = list_response.json()
    assert payload["agents"] == []
    assert "conflicts with an agent profile" in payload["errors"]["helper"]
    assert runner.get_capability("agent.helper") is None
    state.close_runner()


def test_api_agents_reports_legacy_persisted_preset_without_migration(monkeypatch, tmp_path) -> None:
    state.close_runner()
    agent_root = tmp_path / "agents"
    agent_root.mkdir()
    (agent_root / "legacy.json").write_text(
        json.dumps({
            "name": "legacy",
            "profile": "conversation",
            "description": "",
            "max_steps": 1,
            "capability_ids": [],
            "skills": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    monkeypatch.setattr(state, "_create_runner", lambda: _runner(MockProvider([ChatResponse(content="unused")])))
    client = TestClient(app)

    list_response = client.get("/agents")
    runner = state.get_runner()

    assert list_response.status_code == 200
    payload = list_response.json()
    assert payload["agents"] == []
    assert "capability_ids" in payload["errors"]["legacy"]
    assert runner.get_capability("agent.legacy") is None
    state.close_runner()


def test_api_agents_reports_persisted_presets_that_fail_registration(monkeypatch, tmp_path) -> None:
    state.close_runner()
    agent_root = tmp_path / "agents"
    agent_root.mkdir()
    presets = {
        "helper": {
            "name": "helper",
            "profile": "conversation",
            "description": "",
            "max_steps": 1,
            "capabilities": [],
            "skills": [],
        },
        "missing_cap": {
            "name": "missing_cap",
            "profile": "conversation",
            "description": "",
            "max_steps": 1,
            "capabilities": ["tool.missing"],
            "skills": [],
        },
        "nested": {
            "name": "nested",
            "profile": "conversation",
            "description": "",
            "max_steps": 1,
            "capabilities": [],
            "skills": [],
            "agents": ["agent.helper"],
        },
        "reviewed": {
            "name": "reviewed",
            "profile": "conversation",
            "description": "",
            "max_steps": 1,
            "capabilities": [],
            "skills": [],
            "review": "careful",
        },
        "unknown_profile": {
            "name": "unknown_profile",
            "profile": "missing_profile",
            "description": "",
            "max_steps": 1,
            "capabilities": [],
            "skills": [],
        },
    }
    for name, payload in presets.items():
        (agent_root / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    monkeypatch.setattr(state, "_create_runner", lambda: _runner(MockProvider([ChatResponse(content="unused")])))
    client = TestClient(app)

    list_response = client.get("/agents")
    runner = state.get_runner()

    assert list_response.status_code == 200
    payload = list_response.json()
    assert [agent["id"] for agent in payload["agents"]] == ["agent.helper"]
    assert "tool.missing" in payload["errors"]["missing_cap"]
    assert "cannot expose subagents" in payload["errors"]["nested"]
    assert "review=\"fast\"" in payload["errors"]["reviewed"]
    assert "missing_profile" in payload["errors"]["unknown_profile"]
    for name in ("missing_cap", "nested", "reviewed", "unknown_profile"):
        assert runner.get_capability(f"agent.{name}") is None
    state.close_runner()


def test_api_agent_update_validates_registration_before_overwriting(monkeypatch, tmp_path) -> None:
    state.close_runner()
    agent_root = tmp_path / "agents"
    store = app_module.AgentPresetStore(agent_root)
    store.save(AgentPreset(
        name="helper",
        profile="conversation",
        max_steps=1,
        capabilities=[],
        skills=[],
    ))
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)

    def conflicting_runner() -> Runner:
        runner = _runner(MockProvider([ChatResponse(content="unused")]))
        runner.register_capability(
            CapabilityDefinition(id="agent.helper", kind="agent"),
            lambda invocation: CapabilityResult.completed(invocation, "raw"),
        )
        return runner

    monkeypatch.setattr(state, "_create_runner", conflicting_runner)
    client = TestClient(app)

    response = client.put(
        "/agents/helper",
        json={
            "profile": "conversation",
            "description": "Updated.",
            "max_steps": 2,
            "capabilities": [],
            "skills": [],
        },
    )

    assert response.status_code == 400
    assert "already registered" in response.json()["detail"]
    saved = store.load("helper")
    assert saved.max_steps == 1
    assert saved.description == ""
    state.close_runner()


def test_api_message_stream_can_delegate_to_selected_agent_preset(monkeypatch, tmp_path) -> None:
    state.close_runner()
    agent_root = tmp_path / "agents"
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="agent_helper",
                        arguments={"prompt": "summarize this"},
                    )
            ]
        ),
        ChatResponse(content="helper answer"),
        ChatResponse(content="done"),
    ])
    state.runner = _runner(provider)
    client = TestClient(app)

    create_response = client.post(
        "/agents",
        json={
            "name": "helper",
            "profile": "conversation",
            "max_steps": 1,
            "capabilities": [],
            "skills": [],
        },
    )
    stream_response = client.post(
        "/messages/stream",
        json=_message_request(
            "delegate",
            target="tool",
            capability_ids=[],
            skills=[],
            agent_scope="selected",
            agent_ids=["agent.helper"],
        ),
    )

    assert create_response.status_code == 200
    assert stream_response.status_code == 200
    events = _sse_events(stream_response.text)
    result = _stream_result(events[-1])
    assert result["output_text"] == "done"
    assert {tool["function"]["name"] for tool in provider.requests[0]["tools"]} == {"agent_helper"}
    assert provider.requests[1]["tools"] == []
    state.close_runner()


def test_api_message_stream_rejects_agent_capability_ids_when_agent_scope_is_none(monkeypatch, tmp_path) -> None:
    state.close_runner()
    agent_root = tmp_path / "agents"
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    create_response = client.post(
        "/agents",
        json={
            "name": "helper",
            "profile": "conversation",
            "max_steps": 1,
            "capabilities": [],
            "skills": [],
        },
    )
    stream_response = client.post(
        "/messages/stream",
        json=_message_request(
            "delegate",
            target="tool",
            capability_ids=["agent.helper"],
            skills=[],
            agent_scope="none",
        ),
    )

    assert create_response.status_code == 200
    assert stream_response.status_code == 400
    assert "agent_scope" in stream_response.json()["detail"]
    state.close_runner()


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
    workspace_path = Path(run_payload["workspace_path"])
    assert workspace_path.parent == state.runner.runtime.capability_catalog.workspace_root / "runs"
    assert (workspace_path / "notes" / "output.txt").read_text(encoding="utf-8") == "hello"

    get_run_response = client.get(f"/dag-runs/{run_payload['run_id']}")
    assert get_run_response.status_code == 200
    assert get_run_response.json()["dag_run"]["run_id"] == run_payload["run_id"]

    artifacts_response = client.get(f"/dag-runs/{run_payload['run_id']}/artifacts")
    assert artifacts_response.status_code == 200
    assert artifacts_response.json()["artifacts"]["note"]["paths"] == ["notes/output.txt"]
    artifact_files = artifacts_response.json()["files"]
    assert artifact_files == [
        {
            "id": "dag:note:notes/output.txt",
            "artifact_id": "note",
            "source": "dag_artifact",
            "path": "notes/output.txt",
            "name": "output.txt",
            "media_type": "text/plain",
            "preview_kind": "text",
            "previewable": True,
            "size": 5,
            "status": "created",
            "error": None,
            "preview_url": f"/runs/{run_payload['run_id']}/artifacts/preview?path=notes%2Foutput.txt",
        }
    ]

    preview_response = client.get(
        f"/runs/{run_payload['run_id']}/artifacts/preview",
        params={"path": "notes/output.txt"},
    )
    assert preview_response.status_code == 200
    assert preview_response.json() == {
        "run_id": run_payload["run_id"],
        "path": "notes/output.txt",
        "name": "output.txt",
        "media_type": "text/plain",
        "preview_kind": "text",
        "content": "hello",
        "size": 5,
        "truncated": False,
        "truncated_at": 200000,
    }


def test_validate_static_dag_accepts_node_output_reference_with_dependency() -> None:
    state.dags.clear()
    client = TestClient(app)
    spec = {
        "id": "valid_binding",
        "name": "Valid binding",
        "nodes": [
            {
                "id": "search",
                "target": "tool.echo",
                "inputs": {"text": "dagent"},
            },
            {
                "id": "render",
                "target": "tool.echo",
                "inputs": {
                    "text": {
                        "$expr": {
                            "type": "node_output",
                            "node_id": "search",
                            "field": "value",
                        }
                    }
                },
            },
        ],
        "edges": [
            {"source": "search", "target": "render", "reason": "Parameter binding."}
        ],
    }

    response = client.post("/dags/validate", json=spec)

    assert response.status_code == 200
    assert response.json() == {"valid": True, "issues": []}
    assert "valid_binding" not in state.dags


def test_validate_static_dag_reports_node_output_dependency_errors() -> None:
    state.dags.clear()
    client = TestClient(app)
    spec = {
        "id": "missing_binding_edge",
        "name": "Missing binding edge",
        "nodes": [
            {
                "id": "search",
                "target": "tool.echo",
                "inputs": {"text": "dagent"},
            },
            {
                "id": "render",
                "target": "tool.echo",
                "inputs": {
                    "text": {
                        "$expr": {
                            "type": "node_output",
                            "node_id": "search",
                            "field": "value",
                        }
                    }
                },
            },
        ],
        "edges": [],
    }

    response = client.post("/dags/validate", json=spec)

    assert response.status_code == 200
    payload = response.json()
    assert payload["valid"] is False
    assert payload["issues"][0]["severity"] == "error"
    assert payload["issues"][0]["code"] == "dag_validation_error"
    assert "must depend on it" in payload["issues"][0]["message"]
    assert "missing_binding_edge" not in state.dags


def test_static_dag_runs_managed_profile_agent_node(monkeypatch, tmp_path) -> None:
    state.close_runner()
    managed_dir = tmp_path / "managed-profiles"
    managed_dir.mkdir()
    (managed_dir / "analyst.md").write_text("# Analyst\n\nRead carefully.", encoding="utf-8")
    monkeypatch.setattr(state, "get_managed_profile_root", lambda: managed_dir)
    state.runner = _runner(MockProvider([ChatResponse(content="agent answer")]))
    client = TestClient(app)
    spec = {
        "id": "agent_dag",
        "name": "Agent DAG",
        "nodes": [
            {
                "id": "ask",
                "title": "Ask analyst",
                "target": "agent.analyst",
                "inputs": {"prompt": "Summarize the run."},
            }
        ],
        "edges": [],
    }

    create_response = client.post("/dags", json=spec)
    run_response = client.post("/dags/agent_dag/run")

    assert create_response.status_code == 200
    assert run_response.status_code == 200
    run_payload = _result_dag_run(run_response.json()["result"])
    assert run_payload["dag"]["nodes"][0]["status"] == "completed"
    node_trace = run_payload["trace"]["root"]["children"][0]
    assert node_trace["ref"] == {"node_id": "ask"}
    assert node_trace["output"] == "agent answer"
    assert "Read carefully." in state.runner.runtime.provider.requests[0]["messages"][0]["content"]


def test_static_dag_agent_node_uses_public_tool_agent_capability_scope(monkeypatch, tmp_path) -> None:
    state.close_runner()
    managed_dir = tmp_path / "managed-profiles"
    managed_dir.mkdir()
    (managed_dir / "analyst.md").write_text("# Analyst\n\nRead carefully.", encoding="utf-8")
    monkeypatch.setattr(state, "get_managed_profile_root", lambda: managed_dir)
    provider = MockProvider([ChatResponse(content="scoped answer")])
    state.runner = _runner(provider)
    client = TestClient(app)
    spec = {
        "id": "scoped_agent_dag",
        "name": "Scoped Agent DAG",
        "nodes": [
            {
                "id": "ask",
                "target": "agent.analyst",
                "inputs": {"prompt": "Use the allowed tools."},
                "agent": {
                    "capabilities": ["tool.echo"],
                    "skills": [],
                },
            }
        ],
        "edges": [],
    }

    create_response = client.post("/dags", json=spec)
    run_response = client.post("/dags/scoped_agent_dag/run")

    assert create_response.status_code == 200
    assert run_response.status_code == 200
    assert [
        tool["function"]["name"]
        for tool in provider.requests[0]["tools"]
    ] == ["tool_echo"]


def test_static_dag_runs_registered_agent_capability_without_profile(tmp_path) -> None:
    state.close_runner()
    provider = MockProvider([ChatResponse(content="registered answer")])
    state.runner = _runner(provider)
    state.runner.add_agent(ToolAgent(
        profile=AgentProfile(name="helper_profile", content="Registered helper profile."),
        name="helper",
        capabilities=[],
        skills=[],
    ))
    client = TestClient(app)
    spec = {
        "id": "registered_agent_dag",
        "name": "Registered Agent DAG",
        "nodes": [
            {
                "id": "ask",
                "target": "agent.helper",
                "inputs": {"prompt": "Summarize the run."},
            }
        ],
        "edges": [],
    }

    create_response = client.post("/dags", json=spec)
    run_response = client.post("/dags/registered_agent_dag/run")

    assert create_response.status_code == 200
    assert run_response.status_code == 200
    run_payload = _result_dag_run(run_response.json()["result"])
    assert run_payload["dag"]["nodes"][0]["status"] == "completed"
    assert run_payload["trace"]["root"]["children"][0]["output"] == "registered answer"
    assert "Registered helper profile." in provider.requests[0]["messages"][0]["content"]
    state.close_runner()


def test_static_dag_rejects_node_config_for_registered_agent_capability(tmp_path) -> None:
    state.close_runner()
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    state.runner.add_agent(ToolAgent(
        profile=AgentProfile(name="helper_profile", content="Registered helper profile."),
        name="helper",
        capabilities=[],
        skills=[],
    ))
    client = TestClient(app)

    response = client.post(
        "/dags",
        json={
            "id": "registered_agent_with_config",
            "name": "Registered Agent With Config",
            "nodes": [
                {
                    "id": "ask",
                    "target": "agent.helper",
                    "agent": {"capabilities": ["tool.echo"]},
                }
            ],
            "edges": [],
        },
    )

    assert response.status_code == 400
    assert "cannot include node-level agent config" in response.json()["detail"]
    state.close_runner()


def test_static_dag_agent_node_rejects_invalid_agent_scope(monkeypatch, tmp_path) -> None:
    state.close_runner()
    managed_dir = tmp_path / "managed-profiles"
    managed_dir.mkdir()
    (managed_dir / "analyst.md").write_text("# Analyst\n\nRead carefully.", encoding="utf-8")
    monkeypatch.setattr(state, "get_managed_profile_root", lambda: managed_dir)
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    skill_scope_response = client.post(
        "/dags/validate",
        json={
            "id": "bad_agent_scope",
            "name": "Bad Agent Scope",
            "nodes": [
                {
                    "id": "ask",
                    "target": "agent.analyst",
                    "agent": {"capabilities": ["skill.list"]},
                }
            ],
            "edges": [],
        },
    )
    non_agent_response = client.post(
        "/dags",
        json={
            "id": "bad_non_agent_scope",
            "name": "Bad Non-Agent Scope",
            "nodes": [
                {
                    "id": "echo",
                    "target": "tool.echo",
                    "agent": {"capabilities": ["tool.echo"]},
                }
            ],
            "edges": [],
        },
    )

    assert skill_scope_response.status_code == 200
    assert skill_scope_response.json()["valid"] is False
    assert "cannot be used in an agent node capability scope" in skill_scope_response.json()["issues"][0]["message"]
    assert non_agent_response.status_code == 400
    assert "does not target an agent capability" in non_agent_response.json()["detail"]


def test_api_run_artifacts_preview_tool_workspace_markdown_file() -> None:
    state.runner = _runner(
        MockProvider([
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="tool_write_file",
                        arguments={"path": "notes/output.md", "content": "# Hello\n\nBody"},
                    )
                ],
            ),
            ChatResponse(content="done"),
        ])
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json=_message_request(
            "write markdown",
            target="tool",
            capability_ids=["tool.write_file"],
        ),
    )
    assert response.status_code == 200
    result = _stream_result(_sse_events(response.text)[-1])
    run_id = _result_run_id(result)

    artifacts_response = client.get(f"/runs/{run_id}/artifacts")
    assert artifacts_response.status_code == 200
    payload = artifacts_response.json()
    assert payload["artifacts"] == {}
    assert payload["files"] == [
        {
            "id": "run:notes/output.md",
            "artifact_id": None,
            "source": "run_file",
            "path": "notes/output.md",
            "name": "output.md",
            "media_type": "text/markdown",
            "preview_kind": "markdown",
            "previewable": True,
            "size": 13,
            "status": "created",
            "error": None,
            "preview_url": f"/runs/{run_id}/artifacts/preview?path=notes%2Foutput.md",
        }
    ]

    preview_response = client.get(
        f"/runs/{run_id}/artifacts/preview",
        params={"path": "notes/output.md"},
    )
    assert preview_response.status_code == 200
    assert preview_response.json()["preview_kind"] == "markdown"
    assert preview_response.json()["content"] == "# Hello\n\nBody"


def test_api_run_artifacts_preview_rejects_paths_outside_workspace() -> None:
    state.runner = _runner(
        MockProvider([
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="tool_write_file",
                        arguments={"path": "notes/output.txt", "content": "hello"},
                    )
                ],
            ),
            ChatResponse(content="done"),
        ])
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json=_message_request(
            "write text",
            target="tool",
            capability_ids=["tool.write_file"],
        ),
    )
    assert response.status_code == 200
    run_id = _result_run_id(_stream_result(_sse_events(response.text)[-1]))

    preview_response = client.get(
        f"/runs/{run_id}/artifacts/preview",
        params={"path": "../outside.txt"},
    )

    assert preview_response.status_code == 400
    assert "escapes run workspace" in preview_response.json()["detail"]


def test_api_run_artifacts_manifest_lists_unsupported_workspace_files() -> None:
    state.runner = _runner(
        MockProvider([
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="tool_write_file",
                        arguments={"path": "notes/output.txt", "content": "hello"},
                    )
                ],
            ),
            ChatResponse(content="done"),
        ])
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json=_message_request(
            "write text",
            target="tool",
            capability_ids=["tool.write_file"],
        ),
    )
    assert response.status_code == 200
    result = _stream_result(_sse_events(response.text)[-1])
    run_id = _result_run_id(result)
    workspace = Path(result["state"]["workspace_path"])
    (workspace / "exports").mkdir()
    (workspace / "exports" / "report.pdf").write_bytes(b"%PDF-1.7\x00binary")

    artifacts_response = client.get(f"/runs/{run_id}/artifacts")

    assert artifacts_response.status_code == 200
    files = {item["path"]: item for item in artifacts_response.json()["files"]}
    assert files["exports/report.pdf"] == {
        "id": "run:exports/report.pdf",
        "artifact_id": None,
        "source": "run_file",
        "path": "exports/report.pdf",
        "name": "report.pdf",
        "media_type": "application/pdf",
        "preview_kind": None,
        "previewable": False,
        "size": 15,
        "status": "created",
        "error": None,
        "preview_url": None,
    }

    preview_response = client.get(
        f"/runs/{run_id}/artifacts/preview",
        params={"path": "exports/report.pdf"},
    )
    assert preview_response.status_code == 415


def test_api_run_artifacts_preview_truncates_on_utf8_boundary() -> None:
    state.runner = _runner(
        MockProvider([
            ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="tool_write_file",
                        arguments={"path": "notes/output.md", "content": "seed"},
                    )
                ],
            ),
            ChatResponse(content="done"),
        ])
    )
    client = TestClient(app)

    response = client.post(
        "/messages/stream",
        json=_message_request(
            "write markdown",
            target="tool",
            capability_ids=["tool.write_file"],
        ),
    )
    assert response.status_code == 200
    result = _stream_result(_sse_events(response.text)[-1])
    run_id = _result_run_id(result)
    workspace = Path(result["state"]["workspace_path"])
    content = "a" * (app_module.RUN_ARTIFACT_PREVIEW_BYTES - 1) + "é" + "\nrest"
    (workspace / "notes" / "output.md").write_text(content, encoding="utf-8")

    preview_response = client.get(
        f"/runs/{run_id}/artifacts/preview",
        params={"path": "notes/output.md"},
    )

    assert preview_response.status_code == 200
    payload = preview_response.json()
    assert payload["truncated"] is True
    assert payload["content"] == "a" * (app_module.RUN_ARTIFACT_PREVIEW_BYTES - 1)


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
                        "path": {"$expr": {"type": "artifact", "artifact_id": "note", "field": "absolute_path"}},
                        "content": "hello",
                    },
                    "artifact_outputs": ["note"],
                    "boundary": {
                        "allowed_paths": [
                            {"$expr": {"type": "artifact", "artifact_id": "note", "field": "absolute_path"}}
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


def test_api_dag_run_rejects_relative_workspace_root_escape() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    create_response = client.post(
        "/dags",
        json={
            "id": "workspace_escape",
            "name": "Workspace Escape",
            "nodes": [
                {
                    "id": "echo",
                    "target": "tool.echo",
                    "inputs": {"text": "ok"},
                }
            ],
            "edges": [],
        },
    )
    assert create_response.status_code == 200

    response = client.post(
        "/dags/workspace_escape/run",
        json={"workspace_root": "../outside"},
    )

    assert response.status_code == 400
    assert "workspace_root" in response.json()["detail"]


def test_api_workspace_root_rejects_tilde_expansion() -> None:
    try:
        app_module._clean_workspace_root("~/dagent-runs")
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "workspace_root" in str(exc.detail)
    else:
        raise AssertionError("workspace_root with '~' should be rejected")


def test_api_dag_run_resolves_artifact_path_against_requested_run_workspace(tmp_path: Path) -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    workspace_root = tmp_path / "outside-runs"

    create_response = client.post(
        "/dags",
        json={
            "id": "outside_path_note",
            "name": "Outside path note",
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
        "/dags/outside_path_note/run",
        json={"workspace_root": str(workspace_root)},
    )

    assert run_response.status_code == 200
    run_payload = _result_dag_run(run_response.json()["result"])
    workspace_path = Path(run_payload["workspace_path"])
    assert run_payload["status"] == "completed"
    assert workspace_path.parent == workspace_root
    assert (workspace_path / "notes" / "output.txt").read_text(encoding="utf-8") == "hello"
    invocation = run_payload["trace"]["root"]["children"][0]["children"][0]["capability_execution"]["invocation"]
    assert invocation["arguments"]["path"] == "notes/output.txt"


def test_api_static_dag_write_file_literal_path_uses_run_workspace(tmp_path: Path) -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    workspace_root = tmp_path / "runs"

    create_response = client.post(
        "/dags",
        json={
            "id": "literal_path_note",
            "name": "Literal path note",
            "nodes": [
                {
                    "id": "write",
                    "target": "tool.write_file",
                    "inputs": {
                        "path": "notes/plain.txt",
                        "content": "hello",
                    },
                    "boundary": {"allowed_paths": ["notes/plain.txt"]},
                }
            ],
        },
    )
    assert create_response.status_code == 200

    run_response = client.post(
        "/dags/literal_path_note/run",
        json={"workspace_root": str(workspace_root)},
    )

    assert run_response.status_code == 200
    run_payload = _result_dag_run(run_response.json()["result"])
    workspace_path = Path(run_payload["workspace_path"])
    assert run_payload["status"] == "completed"
    assert workspace_path.parent == workspace_root
    assert (workspace_path / "notes" / "plain.txt").read_text(encoding="utf-8") == "hello"
    assert not (state.runner.runtime.capability_catalog.workspace_root / "notes" / "plain.txt").exists()


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
                        "path": {"$expr": {"type": "artifact", "artifact_id": "source", "field": "absolute_path"}},
                    },
                    "artifact_inputs": ["source"],
                    "boundary": {
                        "allowed_paths": [
                            {"$expr": {"type": "artifact", "artifact_id": "source", "field": "absolute_path"}}
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


def test_api_dag_artifact_upload_materializes_input_file_on_each_run(tmp_path: Path) -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)
    workspace_root = tmp_path / "runs"

    create_response = client.post(
        "/dags",
        json={
            "id": "uploaded_source_repeat",
            "name": "Uploaded source repeat",
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
                        "path": {"$expr": {"type": "artifact", "artifact_id": "source", "field": "absolute_path"}},
                    },
                    "artifact_inputs": ["source"],
                    "boundary": {
                        "allowed_paths": [
                            {"$expr": {"type": "artifact", "artifact_id": "source", "field": "absolute_path"}}
                        ],
                    },
                }
            ],
        },
    )
    assert create_response.status_code == 200

    upload_response = client.post(
        "/dags/uploaded_source_repeat/artifacts/source/upload",
        files=[("files", ("source.txt", b"repeat upload", "text/plain"))],
    )
    assert upload_response.status_code == 200

    first_response = client.post(
        "/dags/uploaded_source_repeat/run",
        json={"workspace_root": str(workspace_root)},
    )
    second_response = client.post(
        "/dags/uploaded_source_repeat/run",
        json={"workspace_root": str(workspace_root)},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_payload = _result_dag_run(first_response.json()["result"])
    second_payload = _result_dag_run(second_response.json()["result"])
    first_workspace = Path(first_payload["workspace_path"])
    second_workspace = Path(second_payload["workspace_path"])
    assert first_workspace != second_workspace
    assert (first_workspace / "inputs" / "source.txt").read_text(encoding="utf-8") == "repeat upload"
    assert (second_workspace / "inputs" / "source.txt").read_text(encoding="utf-8") == "repeat upload"
    assert first_payload["trace"]["root"]["children"][0]["output"] == "repeat upload"
    assert second_payload["trace"]["root"]["children"][0]["output"] == "repeat upload"


def test_api_created_tool_capability_can_run_in_dag() -> None:
    state.runner = _runner(MockProvider([ChatResponse(content="unused")]))
    client = TestClient(app)

    create_capability_response = client.post(
        "/capabilities",
        json={
            "id": "tool.upper",
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
    workspace_path = Path(dag_run["workspace_path"])
    assert workspace_path.parent == state.runner.runtime.capability_catalog.workspace_root / "runs"
    assert (workspace_path / "notes" / "output.txt").read_text(encoding="utf-8") == "hello"
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
                        "path": {"$expr": {"type": "artifact", "artifact_id": "note", "field": "absolute_path"}},
                        "content": "hello",
                    },
                    "artifact_outputs": ["note"],
                    "boundary": {
                        "allowed_paths": [
                            {"$expr": {"type": "artifact", "artifact_id": "note", "field": "absolute_path"}}
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


def test_api_model_management_persists_runtime_models_to_user_config(monkeypatch, tmp_path) -> None:
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
    user_config = tmp_path / ".dagent" / "config.yaml"
    monkeypatch.setenv("DAGENT_CONFIG", str(config))
    monkeypatch.setattr(state, "get_user_config_path", lambda: user_config, raising=False)
    state.close_runner()
    state.custom_model_providers.clear()
    state.active_model_id = None
    client = TestClient(app)

    try:
        created = client.post(
            "/models",
            json={
                "id": "local-qwen",
                "name": "Local Qwen",
                "base_url": "http://localhost:8000/v1",
                "model": "qwen3-coder",
                "api_key_env": "LOCAL_QWEN_API_KEY",
            },
        )
        activated = client.post("/models/local-qwen/activate")
        state.close_runner()
        state.custom_model_providers.clear()
        state.active_model_id = None
        listed = client.get("/models")

        raw = yaml.safe_load(user_config.read_text(encoding="utf-8"))
        assert created.status_code == 200
        assert activated.status_code == 200
        assert raw["active_model"] == "local-qwen"
        assert raw["model_providers"]["local-qwen"] == {
            "name": "Local Qwen",
            "base_url": "http://localhost:8000/v1",
            "model": "qwen3-coder",
            "api_key_env": "LOCAL_QWEN_API_KEY",
            "timeout_seconds": 60,
            "strip_thinking": False,
        }
        assert listed.status_code == 200
        assert listed.json()["active_model_id"] == "local-qwen"
        assert [model["id"] for model in listed.json()["models"]] == ["config", "local-qwen"]
    finally:
        state.close_runner()
        state.custom_model_providers.clear()
        state.active_model_id = None


def test_api_mcp_management_persists_user_servers_to_user_config(monkeypatch, tmp_path) -> None:
    class FakeMCPProvider:
        def __init__(self, servers, *, manager=None):
            self.servers = servers
            self.manager = SimpleNamespace(last_errors={})
            self.registration_errors: list[str] = []

        def register_into(self, catalog):
            for name, config in self.servers.items():
                catalog.register(
                    CapabilityDefinition(
                        id=f"mcp.{name}.lookup",
                        kind="mcp",
                        description="Lookup.",
                        policy=CapabilityPolicy(risk=config.get("risk", "medium")),
                        config={"server": name, "tool": "lookup"},
                    ),
                    lambda invocation: CapabilityResult.completed(invocation, "found"),
                )

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
    user_config = tmp_path / ".dagent" / "config.yaml"
    monkeypatch.setenv("DAGENT_CONFIG", str(config))
    monkeypatch.setattr(state, "get_user_config_path", lambda: user_config, raising=False)
    monkeypatch.setattr(runner_module.MCPServerManager, "available", True)
    monkeypatch.setattr(runner_module, "MCPCapabilityProvider", FakeMCPProvider)
    state.close_runner()
    state.custom_mcp_servers.clear()
    state.custom_mcp_errors.clear()
    client = TestClient(app)

    try:
        created = client.post(
            "/mcp/servers",
            json={"name": "mock", "command": "fake", "risk": "low"},
        )
        state.close_runner()
        state.custom_mcp_servers.clear()
        state.custom_mcp_errors.clear()
        listed = client.get("/mcp/servers")

        raw = yaml.safe_load(user_config.read_text(encoding="utf-8"))
        servers = {server["name"]: server for server in listed.json()["servers"]}
        assert created.status_code == 200
        assert raw["mcp_servers"]["mock"] == {
            "transport": "stdio",
            "command": "fake",
            "enabled": True,
            "risk": "low",
            "connect_timeout": 30,
            "tool_timeout": 60,
        }
        assert servers["mock"]["source"] == "user"
        assert servers["mock"]["status"] == "connected"
    finally:
        state.close_runner()
        state.custom_mcp_servers.clear()
        state.custom_mcp_errors.clear()


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
    return 'task: mock\nanswer = tool_echo(text="ok")\n'


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
