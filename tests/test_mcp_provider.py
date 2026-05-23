import asyncio
import json
from types import SimpleNamespace

from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.capabilities.mcp import MCPCapabilityProvider
from dagent.capabilities.mcp.config import build_stdio_env
from dagent.harness_runtime import CapabilityExecutor
from dagent.schemas import CapabilityDefinition, CapabilityInvocation


def run(coro):
    return asyncio.run(coro)


class FakeMCPManager:
    available = True

    def __init__(self) -> None:
        self.started = False
        self.calls: list[tuple[str, str, dict]] = []

    def start(self) -> None:
        self.started = True

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
    assert manager.calls == [("mock-server", "lookup", {"query": "x"})]


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


def test_stdio_env_filters_host_secrets(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "safe-path")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("DAGENT_VISIBLE", "visible")

    env = build_stdio_env({"EXPLICIT_TOKEN": "${DAGENT_VISIBLE}"})

    assert env["PATH"] == "safe-path"
    assert env["EXPLICIT_TOKEN"] == "visible"
    assert "OPENAI_API_KEY" not in env
