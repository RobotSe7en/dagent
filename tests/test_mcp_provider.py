import asyncio
import json
from concurrent.futures import TimeoutError
from types import SimpleNamespace

from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.capabilities.mcp import MCPCapabilityProvider
from dagent.capabilities.mcp.config import build_stdio_env
from dagent.capabilities.mcp.manager import MCPServerManager
import dagent.capabilities.mcp.manager as manager_module
from dagent.harness_runtime import CapabilityExecutor
from dagent.schemas import CapabilityDefinition, CapabilityInvocation


def run(coro):
    return asyncio.run(coro)


class FakeMCPManager:
    available = True

    def __init__(self) -> None:
        self.started = False
        self.shutdown_calls = 0
        self.calls: list[tuple[str, str, dict]] = []

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
                    inputSchema={
                        "properties": {"query": {"type": "string"}},
                        "required": ["query", "missing"],
                    },
                )
            ]
        }

    def call_tool_blocking(self, server_name: str, tool_name: str, arguments: dict, timeout: float):
        self.calls.append((server_name, tool_name, arguments))
        return SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(type="text", text="found:x")],
            structuredContent={"ok": True},
        )


def test_mcp_provider_registers_discovered_tools_with_safe_names() -> None:
    manager = FakeMCPManager()
    catalog = CapabilityCatalog()
    executor = CapabilityExecutor(catalog)

    MCPCapabilityProvider(
        servers={"mock-server": {"command": "fake"}},
        manager=manager,
    ).register_into(catalog)

    definition = catalog.get("mcp.mock_server.lookup")
    assert manager.started is True
    assert definition is not None
    assert definition.name == "mcp_mock_server__lookup"
    assert definition.parameters["required"] == ["query"]
    result = run(executor.execute(
        CapabilityInvocation(
            capability_id="mcp.mock_server.lookup",
            kind="mcp",
            arguments={"query": "x"},
        )
    ))
    payload = json.loads(result.content)
    assert payload == {"result": "found:x", "structuredContent": {"ok": True}}
    assert result.value == {"ok": True}
    assert manager.calls == [("mock-server", "lookup", {"query": "x"})]


def test_mcp_provider_registers_manager_shutdown_with_catalog() -> None:
    manager = FakeMCPManager()
    catalog = CapabilityCatalog()

    MCPCapabilityProvider(
        servers={"mock-server": {"command": "fake"}},
        manager=manager,
    ).register_into(catalog)

    catalog.shutdown()

    assert manager.shutdown_calls == 1


def test_mcp_provider_skips_function_name_collisions() -> None:
    manager = FakeMCPManager()
    catalog = CapabilityCatalog()
    catalog.register(
        CapabilityDefinition(
            id="tool.collision",
            name="mcp_mock_server__lookup",
            kind="tool",
        ),
        handler=lambda invocation: None,
    )
    provider = MCPCapabilityProvider(
        servers={"mock-server": {"command": "fake"}},
        manager=manager,
    )

    provider.register_into(catalog)

    assert catalog.get("mcp.mock_server.lookup") is None
    assert provider.registration_errors
    assert "collides" in provider.registration_errors[0]


def test_mcp_provider_detects_name_collision_through_catalog_public_api() -> None:
    class PublicCatalog:
        def __init__(self) -> None:
            self.registered = False

        def get(self, capability_id: str):
            return None

        def get_by_name(self, name: str):
            return CapabilityDefinition(id="tool.collision", name=name, kind="tool")

        def register(self, definition, handler):
            self.registered = True

    provider = MCPCapabilityProvider(servers={"mock-server": {"command": "fake"}})

    provider._register_tool(
        PublicCatalog(),
        "mock-server",
        "lookup",
        SimpleNamespace(name="lookup", description="", inputSchema={"type": "object"}),
    )

    assert provider.registration_errors
    assert "collides" in provider.registration_errors[0]


def test_mcp_manager_cancels_tool_call_future_on_timeout(monkeypatch) -> None:
    class FakeFuture:
        def __init__(self) -> None:
            self.cancelled = False

        def result(self, timeout: float):
            raise TimeoutError()

        def cancel(self) -> None:
            self.cancelled = True

    class FakeTask:
        async def call_tool(self, tool_name: str, arguments: dict):
            return None

    future = FakeFuture()

    def fake_run_coroutine_threadsafe(coro, loop):
        coro.close()
        return future

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", fake_run_coroutine_threadsafe)
    manager = MCPServerManager({"mock": {"command": "fake"}})
    manager._loop = object()
    manager.tasks["mock"] = FakeTask()

    try:
        manager.call_tool_blocking("mock", "lookup", {}, timeout=0.01)
    except TimeoutError:
        pass
    else:
        raise AssertionError("tool call timeout should be raised")

    assert future.cancelled is True


def test_mcp_manager_shuts_down_server_task_after_connect_timeout(monkeypatch) -> None:
    created_tasks = []

    class HangingTask:
        def __init__(self, name: str, config: dict) -> None:
            self.name = name
            self.config = config
            self.tools = []
            self.last_error = None
            self.shutdown_called = False
            created_tasks.append(self)

        async def start(self) -> None:
            await asyncio.sleep(3600)

        async def shutdown(self) -> None:
            self.shutdown_called = True

    monkeypatch.setattr(manager_module, "MCPServerTask", HangingTask)
    manager = MCPServerManager({"slow": {"command": "fake", "connect_timeout": 0.01}})
    manager.available = True

    try:
        manager.start()
        assert created_tasks[0].shutdown_called is True
        assert "slow" not in manager.discovered_tools()
    finally:
        manager.shutdown()


def test_stdio_env_filters_host_secrets(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "safe-path")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("DAGENT_VISIBLE", "visible")

    env = build_stdio_env({"EXPLICIT_TOKEN": "${DAGENT_VISIBLE}"})

    assert env["PATH"] == "safe-path"
    assert env["EXPLICIT_TOKEN"] == "visible"
    assert "OPENAI_API_KEY" not in env
