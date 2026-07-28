import asyncio
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

import dagent
import dagent.runner as runner_module
import dagent.capabilities.mcp.server_task as server_task_module
from dagent.capabilities.catalog import CapabilityCatalog
from dagent.config import UserPythonToolConfig
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import (
    CapabilityDefinition,
    CapabilityInvocation,
    CapabilityResult,
    MCPServerSnapshot,
    MCPToolSnapshot,
)


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
    return dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=MockProvider([]))


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
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)
    manager = FakeMCPManager()

    definitions = runner._add_mcp_server("mock-server", {"command": "fake"}, manager=manager)

    assert manager.started is True
    assert [definition.id for definition in definitions] == [capability_id]
    assert any(definition.id == capability_id for definition in runner.capabilities)
    snapshot = runner.mcp_server_snapshot("mock-server")
    assert snapshot is not None
    assert snapshot.name == "mock-server"
    assert snapshot.capability_ids == [capability_id]
    assert len(snapshot.tools) == 1
    assert snapshot.tools[0].capability_id == capability_id
    assert snapshot.tools[0].server == "mock-server"
    assert snapshot.tools[0].tool == "lookup"
    assert snapshot.tools[0].definition.id == capability_id
    assert [server.name for server in runner.list_mcp_server_snapshots()] == ["mock-server"]

    result = run(runner.run(dagent.ToolAgent(profile="conversation"), input="lookup x"))
    assert result.output_text == "done"
    assert any(tool["function"]["name"] == function_name for tool in provider.requests[0]["tools"])

    result = run(runner.runtime.capability_executor.execute(
        CapabilityInvocation(capability_id=capability_id, kind="mcp", arguments={"query": "x"})
    ))
    assert result.status == "completed"
    assert result.content == "found:x"
    runner.close()


def test_runner_passes_stdio_stderr_policy_to_mcp_manager(monkeypatch, tmp_path) -> None:
    observed: list[tuple[dict, str]] = []

    class RecordingManager(FakeMCPManager):
        def __init__(self, servers, *, mcp_stdio_stderr):
            super().__init__()
            observed.append((servers, mcp_stdio_stderr))

    monkeypatch.setattr(runner_module, "MCPServerManager", RecordingManager)
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=MockProvider([]),
        mcp_servers={"mock-server": {"command": "fake"}},
        mcp_stdio_stderr="inherit",
    )

    assert observed == [({"mock-server": {"command": "fake"}}, "inherit")]
    runner.close()


def test_runner_derive_inherits_and_can_override_stdio_stderr_policy(
    monkeypatch,
    tmp_path,
) -> None:
    observed: list[str] = []

    class RecordingManager(FakeMCPManager):
        def __init__(self, servers, *, mcp_stdio_stderr):
            super().__init__()
            observed.append(mcp_stdio_stderr)

    monkeypatch.setattr(runner_module, "MCPServerManager", RecordingManager)
    base = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path / "base",
        provider=MockProvider([]),
        mcp_stdio_stderr="inherit",
    )
    inherited = base.derive(
        workspace=tmp_path / "inherited",
        mcp_servers={"mock-server": {"command": "fake"}},
    )
    overridden = base.derive(
        workspace=tmp_path / "overridden",
        mcp_servers={"mock-server": {"command": "fake"}},
        mcp_stdio_stderr="discard",
    )

    assert observed == ["inherit", "discard"]
    overridden.close()
    inherited.close()
    base.close()


@pytest.mark.skipif(
    not server_task_module.MCP_SDK_AVAILABLE,
    reason="MCP SDK is not installed",
)
def test_runner_default_does_not_persist_mcp_child_stderr_credentials(
    monkeypatch,
    tmp_path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    credential = "DAGENT_MCP_STDERR_CREDENTIAL_SENTINEL"
    server_code = """
import os
import sys
from mcp.server.fastmcp import FastMCP

sys.stderr.write(os.environ["MCP_TEST_CREDENTIAL"] + "\\n")
sys.stderr.flush()
server = FastMCP("stderr-fixture")

@server.tool()
def echo(value: str) -> str:
    return value

server.run(transport="stdio")
"""
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path / "workspace",
        provider=MockProvider([]),
        mcp_servers={
            "fixture": {
                "command": sys.executable,
                "args": ["-c", server_code],
                "env": {"MCP_TEST_CREDENTIAL": credential},
                "connect_timeout": 10,
            }
        },
    )
    runner.close()

    log_path = home / ".dagent" / "logs" / "mcp-stderr.log"
    assert not log_path.exists()
    assert all(
        credential not in path.read_text(encoding="utf-8", errors="replace")
        for path in home.rglob("*")
        if path.is_file()
    )


def test_runner_rejects_unknown_stdio_stderr_mode(tmp_path) -> None:
    with pytest.raises(
        ValueError,
        match="mcp_stdio_stderr must be 'discard' or 'inherit'",
    ):
        dagent.Runner(
            runtime_directory=".runtime",
            workspace=tmp_path,
            provider=MockProvider([]),
            mcp_stdio_stderr="persistent",
        )


def test_add_mcp_server_can_lazy_register_snapshot_without_starting_manager(tmp_path) -> None:
    definition = CapabilityDefinition(
        id="mcp.docs.search",
        kind="mcp",
        description="Search docs",
        config={"server": "docs", "tool": "search"},
    )
    snapshot = MCPServerSnapshot(
        name="docs",
        capability_ids=["mcp.docs.search"],
        tools=[MCPToolSnapshot(
            capability_id="mcp.docs.search",
            server="docs",
            tool="search",
            definition=definition,
        )],
    )
    runner = _runner(tmp_path)
    manager = FakeMCPManager()

    definitions = runner._add_mcp_server(
        "docs",
        {"command": "fake"},
        manager=manager,
        snapshot=snapshot,
        lazy_connect=True,
    )

    assert manager.started is False
    assert [definition.id for definition in definitions] == ["mcp.docs.search"]
    registered_snapshot = runner.mcp_server_snapshot("docs")
    assert registered_snapshot is not None
    assert registered_snapshot.name == "docs"
    assert registered_snapshot.capability_ids == ["mcp.docs.search"]
    assert registered_snapshot.tools[0].definition.policy.risk == "medium"
    assert registered_snapshot.tools[0].definition.policy.sandbox_required is True
    runner.close()


def test_runner_catalog_view_is_read_only_and_includes_mcp_snapshots(tmp_path) -> None:
    capability_id = _mock_server_capability_id()
    runner = _runner(tmp_path)
    manager = FakeMCPManager()
    runner._add_mcp_server("mock-server", {"command": "fake"}, manager=manager)

    view = runner.catalog_view()
    dumped = view.model_dump(mode="json")

    assert dumped["workspace_root"] == str(tmp_path.resolve())
    assert capability_id in {capability["id"] for capability in dumped["capabilities"]}
    assert dumped["mcp_servers"][0]["name"] == "mock-server"
    assert dumped["mcp_servers"][0]["tools"][0]["capability_id"] == capability_id
    assert "handler" not in json.dumps(dumped)
    assert "_entries" not in json.dumps(dumped)
    runner.close()


def test_runner_catalog_view_filters_mcp_snapshots_with_capability_filters(tmp_path) -> None:
    capability_id = _mock_server_capability_id()
    runner = _runner(tmp_path)
    manager = FakeMCPManager()
    runner._add_mcp_server("mock-server", {"command": "fake"}, manager=manager)

    runner.set_capability_enabled(capability_id, False)

    assert runner.catalog_view(kind="tool").mcp_servers == []
    assert runner.catalog_view(kind="mcp", enabled_only=True).mcp_servers == []
    assert runner.mcp_server_snapshot("mock-server", enabled_only=True).capability_ids == []
    runner.close()


def test_runner_derive_applies_overlays_without_mutating_base(monkeypatch, tmp_path) -> None:
    class FakeMCPProvider:
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

    @dagent.tool
    def derived_lookup(query: str) -> str:
        return f"found:{query}"

    monkeypatch.setattr(runner_module.MCPServerManager, "available", True)
    monkeypatch.setattr(runner_module, "MCPCapabilityProvider", FakeMCPProvider)
    base = _runner(tmp_path / "base")

    derived = base.derive(
        workspace=tmp_path / "derived",
        capabilities=[derived_lookup],
        mcp_servers={"mock": {"command": "fake"}},
        agents=[
            dagent.ToolAgent(
                profile="conversation",
                name="helper",
                capabilities=["tool.derived_lookup", "mcp.mock.lookup"],
                skills=[],
            )
        ],
    )

    assert derived is not base
    assert derived.runtime.provider is base.runtime.provider
    assert str(derived.runtime.capability_catalog.workspace_root) == str((tmp_path / "derived").resolve())
    assert derived.sandbox is not base.sandbox
    assert derived.sandbox.docker is not base.sandbox.docker
    assert derived.get_capability("tool.derived_lookup") is not None
    assert derived.get_capability("mcp.mock.lookup") is not None
    assert derived.get_capability("agent.helper") is not None
    assert base.get_capability("tool.derived_lookup") is None
    assert base.get_capability("mcp.mock.lookup") is None
    assert base.get_capability("agent.helper") is None
    derived.close()
    base.close()


def test_runner_derive_can_inherit_local_tools_with_exclusions(tmp_path) -> None:
    @dagent.tool
    def base_lookup(query: str) -> str:
        return f"base:{query}"

    @dagent.tool
    def managed_lookup(query: str) -> str:
        return f"managed:{query}"

    base = _runner(tmp_path / "base")
    base.add_tools([base_lookup, managed_lookup])

    derived = base.derive(
        workspace=tmp_path / "derived",
        inherit_local_tools=True,
        exclude_local_tool_ids={"tool.managed_lookup"},
    )

    assert derived.get_capability("tool.base_lookup") is not None
    assert derived.get_capability("tool.managed_lookup") is None

    result = run(derived.runtime.capability_executor.execute(
        CapabilityInvocation(
            capability_id="tool.base_lookup",
            kind="tool",
            arguments={"query": "x"},
        )
    ))
    assert result.status == "completed"
    assert result.content == "base:x"
    derived.close()
    base.close()


def test_runner_derive_local_tool_inheritance_skips_raw_capabilities(tmp_path) -> None:
    base = _runner(tmp_path / "base")
    base.register_capability(
        CapabilityDefinition(id="tool.raw_custom", kind="tool"),
        lambda invocation: CapabilityResult.completed(invocation, "raw"),
    )

    derived = base.derive(
        workspace=tmp_path / "derived",
        inherit_local_tools=True,
    )

    assert derived.get_capability("tool.raw_custom") is None
    derived.close()
    base.close()


def test_runner_derive_default_workspace_uses_resolved_base_workspace(monkeypatch, tmp_path) -> None:
    original_cwd = tmp_path / "original"
    later_cwd = tmp_path / "later"
    original_cwd.mkdir()
    later_cwd.mkdir()
    monkeypatch.chdir(original_cwd)
    base = _runner("relative-workspace")
    expected_workspace = original_cwd / "relative-workspace"

    monkeypatch.chdir(later_cwd)
    derived = base.derive()

    assert str(derived.runtime.capability_catalog.workspace_root) == str(expected_workspace.resolve())
    derived.close()
    base.close()


def test_runner_derive_overlays_validation_without_mutating_base(tmp_path) -> None:
    base = _runner(tmp_path / "base")
    base.enable_validation = True
    base.runtime.max_validation_retries = 3

    inherited = base.derive(workspace=tmp_path / "inherited")
    overridden = base.derive(
        workspace=tmp_path / "overridden",
        runtime_directory="private-runtime",
        enable_validation=False,
        max_validation_retries=5,
    )

    assert base.enable_validation is True
    assert base.runtime.max_validation_retries == 3
    assert inherited.enable_validation is True
    assert inherited.runtime.validator is base.runtime.validator
    assert inherited.runtime.max_validation_retries == 3
    assert inherited.runtime_directory == base.runtime_directory
    assert overridden.enable_validation is False
    assert overridden.runtime.max_validation_retries == 5
    assert overridden.runtime_directory == "private-runtime"
    inherited.close()
    overridden.close()
    base.close()


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
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)
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
        input="delegate",
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
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)
    runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", max_steps=1, capabilities=[], skills=[]))

    result = run(runner.run(
        dagent.ToolAgent(
            profile="conversation",
            capabilities=[],
            skills=[],
            agents="registered",
        ),
        input="delegate",
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


def test_validate_agent_registration_rejects_capability_name_collision(tmp_path) -> None:
    runner = _runner(tmp_path)
    runner.register_capability(
        CapabilityDefinition(id="tool.raw_helper", kind="tool", name="agent_helper"),
        lambda invocation: CapabilityResult.completed(invocation, "raw"),
    )

    with pytest.raises(ValueError, match="Capability name 'agent_helper' is already registered"):
        runner.validate_agent_registration(
            dagent.ToolAgent(profile="conversation", name="helper", capabilities=[], skills=[])
        )
    assert runner.get_capability("agent.helper") is None
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


def test_add_tools_is_atomic_when_later_binding_conflicts(tmp_path) -> None:
    runner = _runner(tmp_path)

    @dagent.tool
    def batch_ok() -> str:
        return "ok"

    @dagent.tool(name="tool_read_file")
    def batch_conflict() -> str:
        return "conflict"

    with pytest.raises(ValueError, match="Capability name 'tool_read_file' is already registered"):
        runner.add_tools([batch_ok, batch_conflict])

    assert runner.get_capability("tool.batch_ok") is None
    assert runner.get_capability("tool.batch_conflict") is None
    runner.close()


def test_add_tools_keeps_existing_idempotent_bindings_valid(tmp_path) -> None:
    runner = _runner(tmp_path)

    @dagent.tool
    def batch_idempotent() -> str:
        return "ok"

    first = runner.add_tool(batch_idempotent)
    runner.validate_tools_registerable([batch_idempotent])
    second = runner.add_tools([batch_idempotent])

    assert first == second[0]
    assert runner.get_capability("tool.batch_idempotent") == first
    runner.close()


def test_validate_tools_registerable_rejects_repeated_idempotent_binding(tmp_path) -> None:
    runner = _runner(tmp_path)

    @dagent.tool
    def batch_repeated() -> str:
        return "ok"

    runner.add_tool(batch_repeated)

    with pytest.raises(ValueError, match="Capability 'tool.batch_repeated' is already registered"):
        runner.validate_tools_registerable([batch_repeated, batch_repeated])
    with pytest.raises(ValueError, match="Capability 'tool.batch_repeated' is already registered"):
        runner.add_tools([batch_repeated, batch_repeated])

    runner.close()


def test_validate_tools_registerable_rejects_batch_name_collisions_without_mutation(tmp_path) -> None:
    runner = _runner(tmp_path)

    @dagent.tool(name="shared_batch_name")
    def batch_first() -> str:
        return "first"

    @dagent.tool(name="shared_batch_name")
    def batch_second() -> str:
        return "second"

    with pytest.raises(ValueError, match="Capability name 'shared_batch_name' is already registered"):
        runner.validate_tools_registerable([batch_first, batch_second])

    assert runner.get_capability("tool.batch_first") is None
    assert runner.get_capability("tool.batch_second") is None
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
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, capabilities=[echo])
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
        input="delegate",
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


def test_reload_mcp_servers_with_snapshots_returns_typed_registration_result(monkeypatch, tmp_path) -> None:
    class FakeMCPProvider:
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
    monkeypatch.setattr(runner_module, "MCPCapabilityProvider", FakeMCPProvider)
    runner = _runner(tmp_path)

    result = runner.reload_mcp_servers_with_snapshots(
        {"search": {"command": "fake"}},
        replace_names=set(),
    )

    assert result.registered_names == ["search"]
    assert result.errors == {}
    assert len(result.snapshots) == 1
    assert result.snapshots[0].name == "search"
    assert result.snapshots[0].capability_ids == ["mcp.search.lookup"]
    assert result.snapshots[0].tools[0].tool == "lookup"
    runner.close()


def test_reload_tools_replaces_managed_group_without_removing_unrelated_tools(tmp_path) -> None:
    runner = _runner(tmp_path)

    @dagent.tool
    def old_python_tool() -> str:
        return "old"

    @dagent.tool
    def new_python_tool() -> str:
        return "new"

    @dagent.tool
    def unrelated_tool() -> str:
        return "unrelated"

    runner.add_tool(old_python_tool)
    runner.add_tool(unrelated_tool)

    registered, errors = runner.reload_tools(
        {"source": [new_python_tool]},
        replace_ids={"tool.old_python_tool"},
    )

    assert errors == {}
    assert [definition.id for definition in registered["source"]] == ["tool.new_python_tool"]
    assert runner.get_capability("tool.old_python_tool") is None
    assert runner.get_capability("tool.new_python_tool") is not None
    assert runner.get_capability("tool.unrelated_tool") is not None
    runner.close()


def test_reload_tools_isolates_group_registration_errors(tmp_path) -> None:
    runner = _runner(tmp_path)

    @dagent.tool
    def good_python_tool() -> str:
        return "good"

    @dagent.tool(name="tool_read_file")
    def conflicting_python_tool() -> str:
        return "bad"

    registered, errors = runner.reload_tools(
        {
            "good": [good_python_tool],
            "bad": [conflicting_python_tool],
        },
        replace_ids=set(),
    )

    assert [definition.id for definition in registered["good"]] == ["tool.good_python_tool"]
    assert "bad" in errors
    assert "tool_read_file" in errors["bad"]
    assert runner.get_capability("tool.good_python_tool") is not None
    assert runner.get_capability("tool.conflicting_python_tool") is None
    runner.close()


def test_reload_tools_records_dangling_subagent_dependency_without_raising(tmp_path) -> None:
    runner = _runner(tmp_path)

    @dagent.tool
    def old_python_tool() -> str:
        return "old"

    runner.add_tool(old_python_tool)
    runner.add_agent(
        dagent.ToolAgent(
            profile="conversation",
            name="helper",
            capabilities=["tool.old_python_tool"],
            skills=[],
        )
    )

    registered, errors = runner.reload_tools({}, replace_ids={"tool.old_python_tool"})

    assert registered == {}
    assert "agent.helper" in errors
    assert "tool.old_python_tool" in errors["agent.helper"]
    assert runner.get_capability("tool.old_python_tool") is None
    assert runner.get_capability("agent.helper") is not None
    runner.close()


def test_reload_tools_rejects_non_tool_replace_ids(tmp_path) -> None:
    runner = _runner(tmp_path)
    runner.add_agent(
        dagent.ToolAgent(
            profile="conversation",
            name="helper",
            capabilities=[],
            skills=[],
        )
    )
    runner.register_capability(
        CapabilityDefinition(id="mcp.search.lookup", kind="mcp"),
        lambda invocation: CapabilityResult.completed(invocation, "found"),
    )

    with pytest.raises(ValueError, match="only replace tool capabilities"):
        runner.reload_tools({}, replace_ids={"agent.helper"})
    with pytest.raises(ValueError, match="only replace tool capabilities"):
        runner.reload_tools({}, replace_ids={"mcp.search.lookup"})

    assert runner.get_capability("agent.helper") is not None
    assert runner.get_capability("mcp.search.lookup") is not None
    runner.close()


def test_reload_tools_rejects_builtin_replace_ids(tmp_path) -> None:
    runner = _runner(tmp_path)

    with pytest.raises(ValueError, match="cannot replace built-in capability"):
        runner.reload_tools({}, replace_ids={"tool.read_file"})

    assert runner.get_capability("tool.read_file") is not None
    runner.close()


def test_reload_python_tool_sources_loads_configs_and_registers_groups(tmp_path) -> None:
    script = tmp_path / "sdk_tools.py"
    script.write_text(
        "from dagent import tool\n"
        "@tool(description='Echo through SDK loader')\n"
        "def sdk_echo(text: str) -> str:\n"
        "    return text\n",
        encoding="utf-8",
    )
    runner = _runner(tmp_path)

    result = runner.reload_python_tool_sources(
        [UserPythonToolConfig(id="sdk_source", source="path", path=str(script), names=["sdk_echo"])],
        user_config_dir=tmp_path,
        replace_ids=set(),
    )

    assert result.errors == {}
    assert result.capability_ids_by_source == {"sdk_source": ["tool.sdk_echo"]}
    assert [definition.id for definition in result.registered["sdk_source"]] == ["tool.sdk_echo"]
    assert not hasattr(result, "load_result")
    assert "bindings" not in result.model_dump_json()
    assert runner.get_capability("tool.sdk_echo") is not None
    runner.close()


def test_reload_python_tool_sources_checks_runner_before_importing_sources(tmp_path) -> None:
    marker = tmp_path / "imported.txt"
    script = tmp_path / "sdk_tools.py"
    script.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n"
        "from dagent import tool\n"
        "@tool\n"
        "def sdk_echo(text: str) -> str:\n"
        "    return text\n",
        encoding="utf-8",
    )
    runner = _runner(tmp_path)
    runner.close()

    with pytest.raises(RuntimeError, match="Runner is closed"):
        runner.reload_python_tool_sources(
            [UserPythonToolConfig(id="sdk_source", source="path", path=str(script), names=["sdk_echo"])],
            user_config_dir=tmp_path,
            replace_ids=set(),
        )

    assert not marker.exists()


def test_reload_python_tool_sources_validates_replace_ids_before_importing_sources(tmp_path) -> None:
    marker = tmp_path / "imported.txt"
    script = tmp_path / "sdk_tools.py"
    script.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n"
        "from dagent import tool\n"
        "@tool\n"
        "def sdk_echo(text: str) -> str:\n"
        "    return text\n",
        encoding="utf-8",
    )
    runner = _runner(tmp_path)

    with pytest.raises(ValueError, match="cannot replace built-in capability"):
        runner.reload_python_tool_sources(
            [UserPythonToolConfig(id="sdk_source", source="path", path=str(script), names=["sdk_echo"])],
            user_config_dir=tmp_path,
            replace_ids={"tool.read_file"},
        )

    assert not marker.exists()
    runner.close()


def test_reload_python_tool_sources_returns_registered_agent_errors(tmp_path) -> None:
    runner = _runner(tmp_path)

    @dagent.tool
    def old_python_tool() -> str:
        return "old"

    runner.add_tool(old_python_tool)
    runner.add_agent(
        dagent.ToolAgent(
            profile="conversation",
            name="helper",
            capabilities=["tool.old_python_tool"],
            skills=[],
        )
    )

    result = runner.reload_python_tool_sources(
        [],
        user_config_dir=tmp_path,
        replace_ids={"tool.old_python_tool"},
    )

    assert "agent.helper" in result.errors
    assert "tool.old_python_tool" in result.errors["agent.helper"]
    assert runner.get_capability("tool.old_python_tool") is None
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
                    catalog.register(
                        CapabilityDefinition(
                            id=f"mcp.{name}.{tool_name}",
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
                    catalog.register(
                        CapabilityDefinition(
                            id=f"mcp.{name}.{tool_name}",
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
        runtime_directory=".runtime",
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
