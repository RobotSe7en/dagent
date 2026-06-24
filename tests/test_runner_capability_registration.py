import asyncio
import json
from types import SimpleNamespace

import pytest

import dagent
import dagent.runner as runner_module
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import CapabilityDefinition, CapabilityInvocation, CapabilityResult


def run(coro):
    return asyncio.run(coro)


def user_messages(content: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": content}]


class FakeMCPManager:
    available = True

    def __init__(self) -> None:
        self.started = False
        self.shutdown_calls = 0
        self.last_errors: dict[str, str] = {}

    def start(self) -> None:
        self.started = True

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def discovered_tools(self):
        return {
            "mock-server": [
                SimpleNamespace(
                    name="lookup",
                    description="Lookup a value.",
                    inputSchema={"properties": {"query": {"type": "string"}}, "required": ["query"]},
                )
            ]
        }

    def call_tool_blocking(self, server_name, tool_name, arguments, timeout):
        return SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(type="text", text="found:x")],
            structuredContent=None,
        )


def _runner(tmp_path) -> dagent.Runner:
    return dagent.Runner(workspace=tmp_path, provider=MockProvider([]))


def test_add_skill_root_makes_skill_discoverable_at_runtime(tmp_path) -> None:
    runner = _runner(tmp_path)
    skill_dir = tmp_path / "extra" / "writing" / "summarize"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: summarize\ndescription: Summarize carefully.\n---\nUse concise summaries.",
        encoding="utf-8",
    )

    runner.add_skill_root(tmp_path / "extra")

    names = {skill.name for skill in runner.skill_store.list()}
    assert "summarize" in names

    result = run(runner.runtime.capability_executor.execute(
        CapabilityInvocation(capability_id="skill.list", kind="skill")
    ))
    payload = json.loads(result.content)
    assert any(skill["name"] == "summarize" for skill in payload["skills"])

    definition = runner.runtime.capability_catalog.get("skill.list")
    assert str(tmp_path / "extra") in definition.config["roots"]
    runner.close()


def test_add_skill_root_is_idempotent(tmp_path) -> None:
    runner = _runner(tmp_path)
    before = len(runner.skill_store.roots)
    runner.add_skill_root(tmp_path / "extra")
    runner.add_skill_root(tmp_path / "extra")
    assert len(runner.skill_store.roots) == before + 1
    runner.close()


def test_add_mcp_server_registers_tools_and_makes_them_visible(tmp_path) -> None:
    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="mcp_mock_server__lookup",
                    arguments={"query": "x"},
                )
            ]
        ),
        ChatResponse(content="done"),
    ])
    runner = dagent.Runner(workspace=tmp_path, provider=provider)
    manager = FakeMCPManager()

    definitions = runner._add_mcp_server("mock-server", {"command": "fake"}, manager=manager)

    assert manager.started is True
    assert [definition.id for definition in definitions] == ["mcp.mock_server.lookup"]
    assert any(definition.id == "mcp.mock_server.lookup" for definition in runner.capabilities)

    result = run(runner.run(dagent.ToolAgent(profile="conversation"), messages=user_messages("lookup x")))
    assert result.output_text == "done"
    assert any(tool["function"]["name"] == "mcp_mock_server__lookup" for tool in provider.requests[0]["tools"])

    result = run(runner.runtime.capability_executor.execute(
        CapabilityInvocation(capability_id="mcp.mock_server.lookup", kind="mcp", arguments={"query": "x"})
    ))
    assert result.status == "completed"
    assert result.content == "found:x"
    runner.close()


def test_add_agent_registers_capability_and_tool_agent_can_delegate(tmp_path) -> None:
    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="helper",
                    arguments={"prompt": "summarize this"},
                )
            ]
        ),
        ChatResponse(content="helper answer"),
        ChatResponse(content="done"),
    ])
    runner = dagent.Runner(workspace=tmp_path, provider=provider)
    helper = dagent.ToolAgent(
        profile="conversation",
        name="helper",
        max_steps=1,
        capabilities=[],
        skills=[],
        description="Summarizes text.",
    )

    definition = runner.add_agent(helper)
    result = run(runner.run(
        dagent.ToolAgent(
            profile="conversation",
            capabilities=[],
            skills=[],
            agents=["agent.helper"],
        ),
        messages=user_messages("delegate"),
    ))

    assert definition.id == "agent.helper"
    assert result.output_text == "done"
    assert {tool["function"]["name"] for tool in provider.requests[0]["tools"]} == {"helper"}
    assert provider.requests[1]["tools"] == []
    runner.close()


def test_top_level_agent_can_expose_all_registered_agents(tmp_path) -> None:
    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="helper",
                    arguments={"prompt": "summarize this"},
                )
            ]
        ),
        ChatResponse(content="helper answer"),
        ChatResponse(content="done"),
    ])
    runner = dagent.Runner(workspace=tmp_path, provider=provider)
    runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", max_steps=1, capabilities=[], skills=[]))

    result = run(runner.run(
        dagent.ToolAgent(
            profile="conversation",
            capabilities=[],
            skills=[],
            agents="registered",
        ),
        messages=user_messages("delegate"),
    ))

    assert result.output_text == "done"
    assert {tool["function"]["name"] for tool in provider.requests[0]["tools"]} == {"helper"}
    runner.close()


def test_add_agent_is_idempotent_for_same_config_and_rejects_conflict(tmp_path) -> None:
    runner = _runner(tmp_path)
    helper = dagent.ToolAgent(profile="conversation", name="helper", max_steps=1, capabilities=[], skills=[])

    first = runner.add_agent(helper)
    second = runner.add_agent(helper)

    assert first.id == "agent.helper"
    assert second.id == "agent.helper"
    with pytest.raises(ValueError, match="already registered with different config"):
        runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", max_steps=2, capabilities=[], skills=[]))
    runner.close()


def test_add_agent_rejects_non_fast_review(tmp_path) -> None:
    runner = _runner(tmp_path)

    with pytest.raises(ValueError, match='review="fast"'):
        runner.add_agent(
            dagent.ToolAgent(
                profile="conversation",
                name="helper",
                capabilities=[],
                skills=[],
                review="careful",
            )
        )
    runner.close()


def test_add_agent_rejects_invalid_name(tmp_path) -> None:
    runner = _runner(tmp_path)

    with pytest.raises(ValueError, match="Agent names"):
        runner.add_agent(dagent.ToolAgent(profile="conversation", name="../bad", capabilities=[], skills=[]))
    runner.close()


def test_add_agent_rejects_llm_function_name_collision(tmp_path) -> None:
    runner = _runner(tmp_path)

    @dagent.tool(name="helper")
    def helper_tool() -> str:
        return "tool"

    runner.add_tool(helper_tool)

    with pytest.raises(ValueError, match="LLM tool name collision"):
        runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=[], skills=[]))
    runner.close()


def test_add_tool_rejects_registered_agent_llm_function_name_collision(tmp_path) -> None:
    runner = _runner(tmp_path)
    runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=[], skills=[]))

    @dagent.tool(name="helper")
    def helper_tool() -> str:
        return "tool"

    with pytest.raises(ValueError, match="LLM tool name collision"):
        runner.add_tool(helper_tool)

    assert runner.get_capability("tool.helper") is None
    assert runner.get_capability("agent.helper") is not None
    runner.close()


def test_register_capability_rejects_registered_agent_llm_function_name_collision(tmp_path) -> None:
    runner = _runner(tmp_path)
    runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=[], skills=[]))
    definition = CapabilityDefinition(id="tool.raw_helper", name="helper", kind="tool")

    with pytest.raises(ValueError, match="LLM tool name collision"):
        runner.register_capability(
            definition,
            lambda invocation: CapabilityResult.completed(invocation, "raw"),
        )

    assert runner.get_capability("tool.raw_helper") is None
    assert runner.get_capability("agent.helper") is not None
    runner.close()


def test_add_mcp_server_rolls_back_registered_agent_llm_function_name_collision(monkeypatch, tmp_path) -> None:
    class CollidingMCPProvider:
        def __init__(self, servers, *, manager=None):
            self.servers = servers
            self.manager = SimpleNamespace(last_errors={}, shutdown=lambda: None)
            self.registration_errors: list[str] = []

        def register_into(self, catalog):
            catalog.register(
                CapabilityDefinition(
                    id="mcp.mock.helper",
                    name="helper",
                    kind="mcp",
                    config={"server": "mock", "tool": "helper"},
                ),
                lambda invocation: CapabilityResult.completed(invocation, "mcp"),
            )

    monkeypatch.setattr(runner_module.MCPServerManager, "available", True)
    monkeypatch.setattr(runner_module, "MCPCapabilityProvider", CollidingMCPProvider)
    runner = _runner(tmp_path)
    runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=[], skills=[]))

    with pytest.raises(ValueError, match="LLM tool name collision"):
        runner.add_mcp_server("mock", {"command": "fake"})

    assert runner.get_capability("mcp.mock.helper") is None
    assert runner.get_capability("agent.helper") is not None
    runner.close()


def test_remove_agent_capability_clears_registered_agent_config(tmp_path) -> None:
    runner = _runner(tmp_path)
    helper = dagent.ToolAgent(profile="conversation", name="helper", max_steps=1, capabilities=[], skills=[])
    runner.add_agent(helper)

    runner.remove_capability("agent.helper")
    replacement = runner.add_agent(
        dagent.ToolAgent(profile="conversation", name="helper", max_steps=2, capabilities=[], skills=[])
    )

    assert replacement.id == "agent.helper"
    runner.close()


def test_remove_capability_rejects_registered_agent_dependency_without_mutation(tmp_path) -> None:
    runner = _runner(tmp_path)

    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    runner.add_tool(search)
    runner.add_agent(
        dagent.ToolAgent(
            profile="conversation",
            name="helper",
            capabilities=["tool.search"],
            skills=[],
        )
    )

    with pytest.raises(ValueError, match="agent.helper"):
        runner.remove_capability("tool.search")

    assert runner.get_capability("tool.search") is not None
    assert runner.get_capability("agent.helper") is not None
    runner.close()


def test_remove_mcp_server_rejects_registered_agent_dependency_without_mutation(tmp_path) -> None:
    runner = _runner(tmp_path)
    manager = FakeMCPManager()
    runner._add_mcp_server("mock-server", {"command": "fake"}, manager=manager)
    runner.add_agent(
        dagent.ToolAgent(
            profile="conversation",
            name="helper",
            capabilities=["mcp.mock_server.lookup"],
            skills=[],
        )
    )

    with pytest.raises(ValueError, match="agent.helper"):
        runner.remove_mcp_server("mock-server")

    assert runner.get_capability("mcp.mock_server.lookup") is not None
    assert runner.get_capability("agent.helper") is not None
    assert manager.shutdown_calls == 0
    runner.close()


def test_registered_subagent_cannot_expose_agents(tmp_path) -> None:
    runner = _runner(tmp_path)

    with pytest.raises(ValueError, match="cannot expose subagents"):
        runner.add_agent(
            dagent.ToolAgent(
                profile="conversation",
                name="outer",
                capabilities=[],
                skills=[],
                agents=[dagent.ToolAgent(profile="conversation", name="inner", capabilities=[], skills=[])],
            )
        )
    runner.close()


def test_registered_subagent_cannot_bind_agent_capabilities(tmp_path) -> None:
    runner = _runner(tmp_path)
    runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=[], skills=[]))

    with pytest.raises(ValueError, match="cannot expose subagents"):
        runner.add_agent(
            dagent.ToolAgent(
                profile="conversation",
                name="outer",
                capabilities=["agent.helper"],
                skills=[],
            )
        )
    runner.close()


def test_remove_mcp_server_unregisters_tools_and_shutdowns_manager(tmp_path) -> None:
    runner = _runner(tmp_path)
    manager = FakeMCPManager()
    runner._add_mcp_server("mock-server", {"command": "fake"}, manager=manager)

    runner.remove_mcp_server("mock-server")

    assert runner.runtime.capability_catalog.get("mcp.mock_server.lookup") is None
    assert manager.shutdown_calls == 1
    runner.close()
    assert manager.shutdown_calls == 1


def test_reload_mcp_servers_records_dangling_subagent_dependency_without_raising(monkeypatch, tmp_path) -> None:
    class FlakyMCPProvider:
        offline = False

        def __init__(self, servers, *, manager=None):
            self.servers = servers
            self.manager = SimpleNamespace(last_errors={}, shutdown=lambda: None)
            self.registration_errors: list[str] = []

        def register_into(self, catalog):
            if FlakyMCPProvider.offline:
                raise RuntimeError("search backend is offline")
            for name in self.servers:
                catalog.register(
                    CapabilityDefinition(
                        id=f"mcp.{name}.lookup",
                        name=f"mcp_{name}__lookup",
                        kind="mcp",
                        config={"server": name, "tool": "lookup"},
                    ),
                    lambda invocation: CapabilityResult.completed(invocation, "found"),
                )

    monkeypatch.setattr(runner_module.MCPServerManager, "available", True)
    monkeypatch.setattr(runner_module, "MCPCapabilityProvider", FlakyMCPProvider)
    runner = _runner(tmp_path)
    runner.add_mcp_server("search", {"command": "fake"})
    runner.add_agent(
        dagent.ToolAgent(
            profile="conversation",
            name="helper",
            capabilities=["mcp.search.lookup"],
            skills=[],
        )
    )

    # The depended-on server fails to come back during the rebuild.
    FlakyMCPProvider.offline = True
    registered, errors = runner.reload_mcp_servers(
        {"search": {"command": "fake"}},
        replace_names={"search"},
    )

    assert registered == set()
    assert "search" in errors
    assert "agent.helper" in errors
    assert "mcp.search.lookup" in errors["agent.helper"]
    assert runner.get_capability("mcp.search.lookup") is None
    assert runner.get_capability("agent.helper") is not None
    runner.close()


def test_replace_mcp_server_removes_previous_tools_before_registering_new_ones(monkeypatch, tmp_path) -> None:
    class FakeMCPProvider:
        def __init__(self, servers, *, manager=None):
            self.servers = servers
            self.manager = SimpleNamespace(last_errors={}, shutdown=lambda: None)
            self.registration_errors: list[str] = []

        def register_into(self, catalog):
            for name, config in self.servers.items():
                for tool_name in config.get("tools", []):
                    safe_name = tool_name.replace("-", "_")
                    catalog.register(
                        CapabilityDefinition(
                            id=f"mcp.{name}.{safe_name}",
                            name=f"mcp_{name}__{safe_name}",
                            kind="mcp",
                            config={"server": name, "tool": tool_name},
                        ),
                        lambda invocation: CapabilityResult(
                            invocation_id=invocation.invocation_id,
                            capability_id=invocation.capability_id,
                            kind=invocation.kind,
                            status="completed",
                            content="ok",
                        ),
                    )

    monkeypatch.setattr(runner_module.MCPServerManager, "available", True)
    monkeypatch.setattr(runner_module, "MCPCapabilityProvider", FakeMCPProvider)
    runner = _runner(tmp_path)
    runner.add_mcp_server("mock", {"command": "fake", "tools": ["old"]})

    definitions = runner.replace_mcp_server("mock", {"command": "fake", "tools": ["new"]})

    assert [definition.id for definition in definitions] == ["mcp.mock.new"]
    assert runner.runtime.capability_catalog.get("mcp.mock.old") is None
    assert runner.runtime.capability_catalog.get("mcp.mock.new") is not None
    runner.close()


def test_replace_mcp_server_removes_constructor_registered_tools(monkeypatch, tmp_path) -> None:
    class FakeMCPProvider:
        def __init__(self, servers, *, manager=None):
            self.servers = servers
            self.manager = SimpleNamespace(last_errors={}, shutdown=lambda: None)
            self.registration_errors: list[str] = []

        def register_into(self, catalog):
            for name, config in self.servers.items():
                for tool_name in config.get("tools", []):
                    safe_name = tool_name.replace("-", "_")
                    catalog.register(
                        CapabilityDefinition(
                            id=f"mcp.{name}.{safe_name}",
                            name=f"mcp_{name}__{safe_name}",
                            kind="mcp",
                            config={"server": name, "tool": tool_name},
                        ),
                        lambda invocation: CapabilityResult(
                            invocation_id=invocation.invocation_id,
                            capability_id=invocation.capability_id,
                            kind=invocation.kind,
                            status="completed",
                            content="ok",
                        ),
                    )

    monkeypatch.setattr(runner_module.MCPServerManager, "available", True)
    monkeypatch.setattr(runner_module, "MCPCapabilityProvider", FakeMCPProvider)
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([]),
        mcp_servers={"mock": {"command": "fake", "tools": ["old"]}},
    )

    definitions = runner.replace_mcp_server("mock", {"command": "fake", "tools": ["new"]})

    assert [definition.id for definition in definitions] == ["mcp.mock.new"]
    assert runner.runtime.capability_catalog.get("mcp.mock.old") is None
    assert runner.runtime.capability_catalog.get("mcp.mock.new") is not None
    runner.close()


def test_add_mcp_server_requires_mcp_sdk_when_unavailable(monkeypatch, tmp_path) -> None:
    runner = _runner(tmp_path)
    monkeypatch.setattr(runner_module.MCPServerManager, "available", False)

    with pytest.raises(RuntimeError, match="MCP SDK is not installed"):
        runner.add_mcp_server("mock-server", {"command": "fake"})
    runner.close()


def test_add_mcp_server_raises_on_connect_failure(tmp_path) -> None:
    runner = _runner(tmp_path)
    manager = FakeMCPManager()
    manager.last_errors = {"mock-server": "boom"}
    manager.discovered_tools = lambda: {}

    with pytest.raises(RuntimeError, match="failed to connect: boom"):
        runner._add_mcp_server("mock-server", {"command": "fake"}, manager=manager)
    assert manager.shutdown_calls == 1
    runner.close()


def test_add_mcp_server_rolls_back_partial_registration_on_error(tmp_path) -> None:
    runner = _runner(tmp_path)

    # A tool whose LLM function name collides with one of the MCP server's tools.
    @dagent.tool(id="tool.collide", name="mcp_mock_server__dup")
    def collide() -> str:
        return "x"

    runner.add_tool(collide)

    class TwoToolManager(FakeMCPManager):
        def discovered_tools(self):
            return {
                "mock-server": [
                    SimpleNamespace(name="good", description="", inputSchema={"properties": {}}),
                    SimpleNamespace(name="dup", description="", inputSchema={"properties": {}}),
                ]
            }

    manager = TwoToolManager()

    with pytest.raises(RuntimeError, match="collides"):
        runner._add_mcp_server("mock-server", {"command": "fake"}, manager=manager)

    catalog = runner.runtime.capability_catalog
    # The successfully-registered "good" tool must be rolled back...
    assert catalog.get("mcp.mock_server.good") is None
    # ...the pre-existing colliding tool must survive...
    assert catalog.get("tool.collide") is not None
    # ...and the manager must be shut down.
    assert manager.shutdown_calls == 1
    runner.close()


def test_add_mcp_server_rolls_back_if_discovery_raises(tmp_path) -> None:
    runner = _runner(tmp_path)

    class BrokenDiscoveryManager(FakeMCPManager):
        def discovered_tools(self):
            raise RuntimeError("discovery failed")

    manager = BrokenDiscoveryManager()

    with pytest.raises(RuntimeError, match="discovery failed"):
        runner._add_mcp_server("mock-server", {"command": "fake"}, manager=manager)

    assert manager.started is True
    assert manager.shutdown_calls == 1
    assert not any(capability_id.startswith("mcp.mock_server.") for capability_id in runner.runtime.capability_catalog.ids())
    runner.close()


def test_runner_rejects_runtime_registration_after_close(tmp_path) -> None:
    runner = _runner(tmp_path)

    @dagent.tool
    def search(q: str) -> str:
        return q

    runner.close()

    with pytest.raises(RuntimeError, match="Runner is closed"):
        runner.add_tool(search)
    with pytest.raises(RuntimeError, match="Runner is closed"):
        runner.add_skill_root(tmp_path / "extra")
    with pytest.raises(RuntimeError, match="Runner is closed"):
        runner.add_mcp_server("mock-server", {"command": "fake"})
    with pytest.raises(RuntimeError, match="Runner is closed"):
        runner.remove_mcp_server("mock-server")
    with pytest.raises(RuntimeError, match="Runner is closed"):
        runner.replace_mcp_server("mock-server", {"command": "fake"})
