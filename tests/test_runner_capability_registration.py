import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

import dagent
import dagent.runner as runner_module
from dagent.capabilities.catalog import CapabilityCatalog
from dagent.capabilities.toolsets import CapabilityToolAdapter, CapabilityToolset
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import CapabilityDefinition, CapabilityInvocation, CapabilityResult


def run(coro):
    return asyncio.run(coro)


def user_messages(content: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": content}]


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


def _mock_server_capability_id(tool_name: str = "lookup") -> str:
    return f"mcp.mock_server_{_short_hash('mock-server')}.{tool_name}"


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
    capability_id = _mock_server_capability_id()
    function_name = capability_id.replace(".", "_")
    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name=function_name,
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
    assert [definition.id for definition in definitions] == [capability_id]
    assert any(definition.id == capability_id for definition in runner.capabilities)

    result = run(runner.run(dagent.ToolAgent(profile="conversation"), messages=user_messages("lookup x")))
    assert result.output_text == "done"
    assert any(tool["function"]["name"] == function_name for tool in provider.requests[0]["tools"])

    result = run(runner.runtime.capability_executor.execute(
        CapabilityInvocation(capability_id=capability_id, kind="mcp", arguments={"query": "x"})
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
                    name="agent_helper",
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
    assert {tool["function"]["name"] for tool in provider.requests[0]["tools"]} == {"agent_helper"}
    assert provider.requests[1]["tools"] == []
    runner.close()


def test_top_level_agent_can_expose_all_registered_agents(tmp_path) -> None:
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
    assert {tool["function"]["name"] for tool in provider.requests[0]["tools"]} == {"agent_helper"}
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
    with pytest.raises(ValueError, match="Agent names"):
        runner.add_agent(dagent.ToolAgent(profile="conversation", name="bad-name", capabilities=[], skills=[]))
    runner.close()


def test_add_agent_uses_namespaced_function_name_and_allows_tool_same_short_name(tmp_path) -> None:
    runner = _runner(tmp_path)

    @dagent.tool
    def helper() -> str:
        return "tool"

    runner.add_tool(helper)
    runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=[], skills=[]))

    assert runner.get_capability("tool.helper") is not None
    assert runner.get_capability("agent.helper") is not None
    assert (
        runner.runtime.tool_agent.loop.tool_adapter.function_name_for_capability(
            "tool.helper",
            enabled_toolsets=("builtin",),
        )
        == "tool_helper"
    )
    assert (
        runner.runtime.tool_agent.loop.tool_adapter.function_name_for_capability(
            "agent.helper",
            enabled_toolsets=("builtin",),
        )
        == "agent_helper"
    )
    runner.close()


def test_add_tool_allows_registered_agent_same_short_name(tmp_path) -> None:
    runner = _runner(tmp_path)
    runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=[], skills=[]))

    @dagent.tool
    def helper() -> str:
        return "tool"

    runner.add_tool(helper)

    assert runner.get_capability("tool.helper") is not None
    assert runner.get_capability("agent.helper") is not None
    runner.close()


def test_add_mcp_server_allows_registered_agent_same_short_name(monkeypatch, tmp_path) -> None:
    class SameShortNameMCPProvider:
        def __init__(self, servers, *, manager=None):
            self.servers = servers
            self.manager = SimpleNamespace(last_errors={}, shutdown=lambda: None)
            self.registration_errors: list[str] = []

        def register_into(self, catalog):
            catalog.register(
                CapabilityDefinition(
                    id="mcp.mock.helper",
                    kind="mcp",
                    config={"server": "mock", "tool": "helper"},
                ),
                lambda invocation: CapabilityResult.completed(invocation, "mcp"),
            )

    monkeypatch.setattr(runner_module.MCPServerManager, "available", True)
    monkeypatch.setattr(runner_module, "MCPCapabilityProvider", SameShortNameMCPProvider)
    runner = _runner(tmp_path)
    runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=[], skills=[]))

    runner.add_mcp_server("mock", {"command": "fake"})

    assert runner.get_capability("mcp.mock.helper") is not None
    assert runner.get_capability("agent.helper") is not None
    runner.close()


def test_catalog_rejects_mcp_function_name_collisions() -> None:
    catalog = CapabilityCatalog()
    catalog.register(
        CapabilityDefinition(id="mcp.foo.bar_baz", kind="mcp"),
        lambda invocation: CapabilityResult.completed(invocation, "ok"),
    )

    with pytest.raises(ValueError, match="Capability name 'mcp_foo_bar_baz' is already registered"):
        catalog.register(
            CapabilityDefinition(id="mcp.foo_bar.baz", kind="mcp"),
            lambda invocation: CapabilityResult.completed(invocation, "ok"),
        )


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


def test_disable_capability_rejects_registered_agent_dependency_without_mutation(tmp_path) -> None:
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
        runner.set_capability_enabled("tool.search", False)

    assert runner.get_capability("tool.search").enabled is True
    assert runner.get_capability("agent.helper") is not None
    runner.close()


def test_remove_mcp_server_rejects_registered_agent_dependency_without_mutation(tmp_path) -> None:
    capability_id = _mock_server_capability_id()
    runner = _runner(tmp_path)
    manager = FakeMCPManager()
    runner._add_mcp_server("mock-server", {"command": "fake"}, manager=manager)
    runner.add_agent(
        dagent.ToolAgent(
            profile="conversation",
            name="helper",
            capabilities=[capability_id],
            skills=[],
        )
    )

    with pytest.raises(ValueError, match="agent.helper"):
        runner.remove_mcp_server("mock-server")

    assert runner.get_capability(capability_id) is not None
    assert runner.get_capability("agent.helper") is not None
    assert manager.shutdown_calls == 0
    runner.close()


def test_replace_mcp_server_allows_registered_agent_dependency_when_ids_stay_stable(monkeypatch, tmp_path) -> None:
    class StableMCPProvider:
        def __init__(self, servers, *, manager=None):
            self.servers = servers
            self.manager = SimpleNamespace(last_errors={}, shutdown=lambda: None)
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

    monkeypatch.setattr(runner_module.MCPServerManager, "available", True)
    monkeypatch.setattr(runner_module, "MCPCapabilityProvider", StableMCPProvider)
    runner = _runner(tmp_path)
    runner.add_mcp_server("search", {"command": "old"})
    runner.add_agent(
        dagent.ToolAgent(
            profile="conversation",
            name="helper",
            capabilities=["mcp.search.lookup"],
            skills=[],
        )
    )

    definitions = runner.replace_mcp_server("search", {"command": "new"})

    assert [definition.id for definition in definitions] == ["mcp.search.lookup"]
    assert runner.get_capability("agent.helper") is not None
    assert runner.get_capability("mcp.search.lookup") is not None
    runner.close()


def test_replace_mcp_server_rejects_registered_agent_dependency_when_ids_change(monkeypatch, tmp_path) -> None:
    class ConfiguredMCPProvider:
        def __init__(self, servers, *, manager=None):
            self.servers = servers
            self.manager = SimpleNamespace(last_errors={}, shutdown=lambda: None)
            self.registration_errors: list[str] = []

        def register_into(self, catalog):
            for name, config in self.servers.items():
                for tool_name in config["tools"]:
                    catalog.register(
                        CapabilityDefinition(
                            id=f"mcp.{name}.{tool_name}",
                            kind="mcp",
                            config={"server": name, "tool": tool_name},
                        ),
                        lambda invocation: CapabilityResult.completed(invocation, "found"),
                    )

    monkeypatch.setattr(runner_module.MCPServerManager, "available", True)
    monkeypatch.setattr(runner_module, "MCPCapabilityProvider", ConfiguredMCPProvider)
    runner = _runner(tmp_path)
    runner.add_mcp_server("search", {"command": "old", "tools": ["lookup"]})
    runner.add_agent(
        dagent.ToolAgent(
            profile="conversation",
            name="helper",
            capabilities=["mcp.search.lookup"],
            skills=[],
        )
    )

    with pytest.raises(ValueError, match="agent.helper"):
        runner.replace_mcp_server("search", {"command": "new", "tools": ["query"]})

    assert runner.get_capability("mcp.search.lookup") is not None
    assert runner.get_capability("mcp.search.query") is None
    assert runner.get_capability("agent.helper") is not None
    runner.close()


def test_replace_mcp_server_rolls_back_registered_agent_dependency_on_error(monkeypatch, tmp_path) -> None:
    class FlakyMCPProvider:
        fail = False

        def __init__(self, servers, *, manager=None):
            self.servers = servers
            self.manager = SimpleNamespace(last_errors={}, shutdown=lambda: None)
            self.registration_errors: list[str] = []

        def register_into(self, catalog):
            if FlakyMCPProvider.fail:
                raise RuntimeError("search backend is offline")
            for name in self.servers:
                catalog.register(
                    CapabilityDefinition(
                        id=f"mcp.{name}.lookup",
                        kind="mcp",
                        config={"server": name, "tool": "lookup"},
                    ),
                    lambda invocation: CapabilityResult.completed(invocation, "found"),
                )

    monkeypatch.setattr(runner_module.MCPServerManager, "available", True)
    monkeypatch.setattr(runner_module, "MCPCapabilityProvider", FlakyMCPProvider)
    runner = _runner(tmp_path)
    runner.add_mcp_server("search", {"command": "old"})
    runner.add_agent(
        dagent.ToolAgent(
            profile="conversation",
            name="helper",
            capabilities=["mcp.search.lookup"],
            skills=[],
        )
    )

    FlakyMCPProvider.fail = True
    with pytest.raises(RuntimeError, match="offline"):
        runner.replace_mcp_server("search", {"command": "new"})

    assert runner.get_capability("mcp.search.lookup") is not None
    assert runner.get_capability("agent.helper") is not None
    with pytest.raises(ValueError, match="Capability name 'mcp_search_lookup' is already registered"):
        runner.register_capability(
            CapabilityDefinition(id="tool.duplicate", kind="tool", name="mcp_search_lookup"),
            lambda invocation: CapabilityResult.completed(invocation, "duplicate"),
        )
    runner.close()


def test_tool_agent_delegation_events_include_parent_capability_id(tmp_path) -> None:
    @dagent.tool
    def echo(text: str) -> str:
        return f"echo:{text}"

    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="call_agent",
                    name="agent_helper",
                    arguments={"prompt": "Use echo."},
                )
            ]
        ),
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="call_echo",
                    name="tool_echo",
                    arguments={"text": "hi"},
                )
            ]
        ),
        ChatResponse(content="helper done"),
        ChatResponse(content="done"),
    ])
    runner = dagent.Runner(workspace=tmp_path, provider=provider, capabilities=[echo])
    runner.add_agent(
        dagent.ToolAgent(
            profile="conversation",
            name="helper",
            max_steps=2,
            capabilities=["tool.echo"],
            skills=[],
        )
    )
    events: list[dict] = []

    result = run(runner.run(
        dagent.ToolAgent(profile="conversation", capabilities=[], skills=[], agents=["agent.helper"]),
        messages=user_messages("delegate"),
        on_event=events.append,
    ))

    assert result.output_text == "done"
    child_events = [
        event for event in events
        if event.get("type", "").startswith("capability_")
        and event.get("capability_id") == "tool.echo"
    ]
    assert child_events
    assert {event.get("parent_capability_id") for event in child_events} == {"agent.helper"}
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
    capability_id = _mock_server_capability_id()
    runner = _runner(tmp_path)
    manager = FakeMCPManager()
    runner._add_mcp_server("mock-server", {"command": "fake"}, manager=manager)

    runner.remove_mcp_server("mock-server")

    assert runner.runtime.capability_catalog.get(capability_id) is None
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

    class TwoToolManager(FakeMCPManager):
        def discovered_tools(self):
            return {
                "mock-server": [
                    SimpleNamespace(name="good", description="", inputSchema={"properties": {}}),
                    SimpleNamespace(name="good", description="", inputSchema={"properties": {}}),
                ]
            }

    manager = TwoToolManager()

    with pytest.raises(RuntimeError, match="already registered"):
        runner._add_mcp_server("mock-server", {"command": "fake"}, manager=manager)

    catalog = runner.runtime.capability_catalog
    # The successfully-registered "good" tool must be rolled back...
    assert catalog.get(_mock_server_capability_id("good")) is None
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
    assert not any(
        capability_id.startswith(f"mcp.mock_server_{_short_hash('mock-server')}.")
        for capability_id in runner.runtime.capability_catalog.ids()
    )
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
