import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import AsyncExitStack, asynccontextmanager
import threading
import time
from types import SimpleNamespace

import pytest

from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.capabilities.mcp import MCPCapabilityProvider
from dagent.capabilities.mcp.config import build_http_headers, build_stdio_env
from dagent.capabilities.mcp.handlers import make_mcp_tool_handler
from dagent.capabilities.mcp.manager import MCPServerManager
import dagent.capabilities.mcp.manager as manager_module
import dagent.capabilities.mcp.server_task as server_task_module
from dagent.capabilities.mcp.server_task import MCPServerTask
from dagent.harness_runtime import CapabilityExecutor
from dagent.schemas import (
    CapabilityDefinition,
    CapabilityInvocation,
    MCPServerSnapshot,
    MCPToolSnapshot,
)


def run(coro):
    return asyncio.run(coro)


class FakeMCPManager:
    available = True

    def __init__(self) -> None:
        self.started = False
        self.shutdown_calls = 0
        self.calls: list[tuple[str, str, dict]] = []
        self.timeouts: list[float] = []

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
                    outputSchema={
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok", "missing"],
                    },
                )
            ]
        }

    def call_tool_blocking(self, server_name: str, tool_name: str, arguments: dict, timeout: float):
        self.calls.append((server_name, tool_name, arguments))
        self.timeouts.append(timeout)
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

    capability_id = f"mcp.mock_server_{_short_hash('mock-server')}.lookup"
    definition = catalog.get(capability_id)
    assert manager.started is True
    assert definition is not None
    assert definition.parameters["required"] == ["query"]
    assert definition.output_schema == {
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok", "missing"],
    }
    result = run(executor.execute(
        CapabilityInvocation(
            capability_id=capability_id,
            kind="mcp",
            arguments={"query": "x"},
        )
    ))
    payload = json.loads(result.content)
    assert payload == {"result": "found:x", "structuredContent": {"ok": True}}
    assert result.value == {"ok": True}
    assert manager.calls == [("mock-server", "lookup", {"query": "x"})]
    assert manager.timeouts == [300]


def test_mcp_provider_accepts_snake_case_output_schema() -> None:
    class SnakeCaseOutputSchemaManager(FakeMCPManager):
        def discovered_tools(self):
            return {
                "mock-server": [
                    SimpleNamespace(
                        name="lookup",
                        description="Lookup a value.",
                        inputSchema={"type": "object", "properties": {}},
                        output_schema={
                            "type": "object",
                            "properties": {"title": {"type": "string"}},
                            "required": ["title"],
                        },
                    )
                ]
            }

    manager = SnakeCaseOutputSchemaManager()
    catalog = CapabilityCatalog()

    MCPCapabilityProvider(
        servers={"mock-server": {"command": "fake"}},
        manager=manager,
    ).register_into(catalog)

    definition = catalog.get(f"mcp.mock_server_{_short_hash('mock-server')}.lookup")
    assert definition is not None
    assert definition.output_schema == {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    }


def test_mcp_provider_disambiguates_normalized_mcp_name_collisions() -> None:
    class CollidingNameManager(FakeMCPManager):
        def discovered_tools(self):
            return {
                "mock-server": [
                    SimpleNamespace(name="lookup-tool", description="", inputSchema={"type": "object"}),
                    SimpleNamespace(name="lookup_tool", description="", inputSchema={"type": "object"}),
                ]
            }

    manager = CollidingNameManager()
    catalog = CapabilityCatalog()
    provider = MCPCapabilityProvider(
        servers={"mock-server": {"command": "fake"}},
        manager=manager,
    )

    provider.register_into(catalog)

    server_key = f"mock_server_{_short_hash('mock-server')}"
    transformed_tool_id = f"mcp.{server_key}.lookup_tool_{_short_hash('lookup-tool')}"
    already_safe_tool_id = f"mcp.{server_key}.lookup_tool"
    transformed = catalog.get(transformed_tool_id)
    already_safe = catalog.get(already_safe_tool_id)

    assert provider.registration_errors == []
    assert transformed is not None
    assert already_safe is not None
    assert transformed.config == {"server": "mock-server", "tool": "lookup-tool"}
    assert already_safe.config == {"server": "mock-server", "tool": "lookup_tool"}


def test_mcp_provider_registers_manager_shutdown_with_catalog() -> None:
    manager = FakeMCPManager()
    catalog = CapabilityCatalog()

    MCPCapabilityProvider(
        servers={"mock-server": {"command": "fake"}},
        manager=manager,
    ).register_into(catalog)

    catalog.shutdown()

    assert manager.shutdown_calls == 1


def test_mcp_provider_skips_capability_id_collisions() -> None:
    manager = FakeMCPManager()
    catalog = CapabilityCatalog()
    capability_id = f"mcp.mock_server_{_short_hash('mock-server')}.lookup"
    catalog.register(
        CapabilityDefinition(
            id=capability_id,
            kind="mcp",
        ),
        handler=lambda invocation: None,
    )
    provider = MCPCapabilityProvider(
        servers={"mock-server": {"command": "fake"}},
        manager=manager,
    )

    provider.register_into(catalog)

    assert catalog.get(capability_id) is not None
    assert provider.registration_errors
    assert "already registered" in provider.registration_errors[0]


def test_mcp_provider_does_not_downgrade_invalid_tool_timeout_to_registration_error() -> None:
    manager = FakeMCPManager()
    catalog = CapabilityCatalog()
    provider = MCPCapabilityProvider(
        servers={"mock-server": {"command": "fake", "tool_timeout": "not-a-number"}},
        manager=manager,
    )

    with pytest.raises(ValueError, match="could not convert string to float"):
        provider.register_into(catalog)

    assert provider.registration_errors == []
    assert catalog.list(kind="mcp") == []


def test_mcp_provider_registers_snapshot_without_starting_manager() -> None:
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
    manager = FakeMCPManager()
    provider = MCPCapabilityProvider(
        {"docs": {"command": "fake"}},
        manager=manager,
        snapshots={"docs": snapshot},
        lazy_connect=True,
    )
    catalog = CapabilityCatalog(workspace_root=".")

    provider.register_into(catalog)

    assert manager.started is False
    assert catalog.get("mcp.docs.search") is not None


def test_mcp_provider_lazy_connect_requires_snapshots() -> None:
    manager = FakeMCPManager()
    provider = MCPCapabilityProvider(
        {"docs": {"command": "fake"}},
        manager=manager,
        lazy_connect=True,
    )

    with pytest.raises(ValueError, match="snapshot"):
        provider.register_into(CapabilityCatalog(workspace_root="."))

    assert manager.started is False


def test_mcp_provider_lazy_snapshot_applies_config_filters_and_policy() -> None:
    search = CapabilityDefinition(
        id="mcp.docs.search",
        kind="mcp",
        description="Search docs",
        policy={"risk": "low", "network": False},
        config={"server": "docs", "tool": "search"},
    )
    write = CapabilityDefinition(
        id="mcp.docs.write",
        kind="mcp",
        description="Write docs",
        policy={"risk": "low", "network": False},
        config={"server": "docs", "tool": "write"},
    )
    snapshot = MCPServerSnapshot(
        name="docs",
        capability_ids=["mcp.docs.search", "mcp.docs.write"],
        tools=[
            MCPToolSnapshot(
                capability_id="mcp.docs.search",
                server="docs",
                tool="search",
                definition=search,
            ),
            MCPToolSnapshot(
                capability_id="mcp.docs.write",
                server="docs",
                tool="write",
                definition=write,
            ),
        ],
    )
    manager = FakeMCPManager()
    catalog = CapabilityCatalog(workspace_root=".")

    MCPCapabilityProvider(
        {
            "docs": {
                "command": "fake",
                "url": "https://mcp.example.test",
                "risk": "high",
                "include_tools": ["search"],
                "exclude_tools": ["write"],
            }
        },
        manager=manager,
        snapshots={"docs": snapshot},
        lazy_connect=True,
    ).register_into(catalog)

    assert catalog.get("mcp.docs.write") is None
    registered = catalog.get("mcp.docs.search")
    assert registered is not None
    assert registered.policy.risk == "high"
    assert registered.policy.network is True


def test_mcp_provider_lazy_snapshot_skips_disabled_server() -> None:
    definition = CapabilityDefinition(
        id="mcp.docs.search",
        kind="mcp",
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
    catalog = CapabilityCatalog(workspace_root=".")

    MCPCapabilityProvider(
        {"docs": {"command": "fake", "enabled": False}},
        manager=FakeMCPManager(),
        snapshots={"docs": snapshot},
        lazy_connect=True,
    ).register_into(catalog)

    assert catalog.list(kind="mcp") == []


def test_mcp_provider_lazy_snapshot_rejects_identity_mismatch() -> None:
    snapshot = MCPServerSnapshot(
        name="docs",
        capability_ids=["mcp.docs.search"],
        tools=[MCPToolSnapshot(
            capability_id="mcp.docs.search",
            server="docs",
            tool="search",
            definition=CapabilityDefinition(id="tool.docs_search", kind="tool"),
        )],
    )
    provider = MCPCapabilityProvider(
        {"docs": {"command": "fake"}},
        manager=FakeMCPManager(),
        snapshots={"docs": snapshot},
        lazy_connect=True,
    )

    with pytest.raises(ValueError, match="definition"):
        provider.register_into(CapabilityCatalog(workspace_root="."))


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


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
    except TimeoutError as exc:
        assert str(exc) == "MCP tool 'lookup' on server 'mock' timed out after 0.01 seconds."
    else:
        raise AssertionError("tool call timeout should be raised")

    assert future.cancelled is True


def test_mcp_manager_starts_server_on_first_tool_call(monkeypatch) -> None:
    starts: list[str] = []
    calls: list[tuple[str, dict]] = []

    class LazyFakeTask:
        tools = []
        last_error = None

        def __init__(self, name: str, config: dict) -> None:
            self.name = name
            self.config = config

        async def start(self) -> None:
            starts.append(self.name)

        async def call_tool(self, tool_name: str, arguments: dict):
            calls.append((tool_name, arguments))
            return "ok"

        async def shutdown(self) -> None:
            return None

    monkeypatch.setattr(manager_module, "MCPServerTask", LazyFakeTask)
    manager = MCPServerManager({
        "mock": {"command": "fake"},
        "disabled": {"command": "fake", "enabled": False},
    })

    try:
        result = manager.call_tool_blocking("mock", "lookup", {"query": "x"}, timeout=1)
    finally:
        manager.shutdown()

    assert result == "ok"
    assert starts == ["mock"]
    assert calls == [("lookup", {"query": "x"})]


def test_mcp_manager_waits_for_concurrent_lazy_start_before_tool_call(monkeypatch) -> None:
    start_entered = threading.Event()
    release_start = threading.Event()
    starts: list[str] = []

    class LazyFakeTask:
        tools = []
        last_error = None

        def __init__(self, name: str, config: dict) -> None:
            self.name = name
            self.config = config
            self.started = False

        async def start(self) -> None:
            start_entered.set()
            await asyncio.to_thread(release_start.wait)
            self.started = True
            starts.append(self.name)

        async def call_tool(self, tool_name: str, arguments: dict):
            return "ok" if self.started else "called-before-start"

        async def shutdown(self) -> None:
            return None

    monkeypatch.setattr(manager_module, "MCPServerTask", LazyFakeTask)
    manager = MCPServerManager({"mock": {"command": "fake"}})

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(manager.call_tool_blocking, "mock", "lookup", {}, 1)
            assert start_entered.wait(timeout=1)
            second = executor.submit(manager.call_tool_blocking, "mock", "lookup", {}, 1)
            time.sleep(0.05)
            release_start.set()

            assert first.result(timeout=1) == "ok"
            assert second.result(timeout=1) == "ok"
    finally:
        release_start.set()
        manager.shutdown()

    assert starts == ["mock"]


def test_mcp_tool_handler_returns_timeout_error_text() -> None:
    class TimeoutManager:
        def call_tool_blocking(self, server_name: str, tool_name: str, arguments: dict, timeout: float):
            raise TimeoutError("MCP tool 'lookup' on server 'mock' timed out after 1 seconds.")

    handler = make_mcp_tool_handler(
        TimeoutManager(),
        server_name="mock",
        tool_name="lookup",
        timeout_seconds=1,
    )
    invocation = CapabilityInvocation(capability_id="mcp.mock.lookup", kind="mcp")

    result = run(handler(invocation))

    assert result.status == "failed"
    assert result.stop_reason == "mcp_call_error"
    assert result.error == "MCP tool 'lookup' on server 'mock' timed out after 1 seconds."


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
        assert manager.last_errors["slow"] == "timed out after 0.01 seconds."
        assert "slow" not in manager.discovered_tools()
    finally:
        manager.shutdown()


def test_mcp_manager_uses_default_connect_timeout(monkeypatch) -> None:
    captured: dict[str, float] = {}

    class FakeFuture:
        def result(self, timeout: float):
            captured["timeout"] = timeout
            return None

        def cancel(self) -> None:
            return None

    class FakeTask:
        def __init__(self, name: str, config: dict) -> None:
            self.tools = []
            self.last_error = None

        async def start(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

    def fake_run_coroutine_threadsafe(coro, loop):
        coro.close()
        return FakeFuture()

    monkeypatch.setattr(manager_module, "MCPServerTask", FakeTask)
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", fake_run_coroutine_threadsafe)
    manager = MCPServerManager({"mock": {"command": "fake"}})
    manager.available = True

    try:
        manager.start()
        assert captured["timeout"] == 60
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


def test_http_headers_expand_environment_references(monkeypatch) -> None:
    monkeypatch.setenv("DAGENT_MCP_TOKEN", "visible-token")

    headers = build_http_headers({
        "Authorization": "Bearer ${DAGENT_MCP_TOKEN}",
        "X-Static": "value",
    })

    assert headers == {
        "Authorization": "Bearer visible-token",
        "X-Static": "value",
    }


def test_mcp_server_task_uses_streamable_http_transport(monkeypatch) -> None:
    events: list[tuple] = []

    class FakeHTTPClient:
        def __init__(self, *, headers, timeout):
            self.headers = headers
            self.timeout = timeout
            events.append(("http_client", headers))

        async def __aenter__(self):
            events.append(("http_client_enter",))
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            events.append(("http_client_exit",))

    @asynccontextmanager
    async def fake_streamable_http_client(url, *, http_client=None, terminate_on_close=True):
        events.append(("streamable_http", url, http_client.headers, terminate_on_close))
        yield "read", "write", lambda: "session-1"

    monkeypatch.setenv("DAGENT_MCP_TOKEN", "token")
    monkeypatch.setattr(
        server_task_module,
        "create_mcp_http_client",
        lambda headers=None, timeout=None: FakeHTTPClient(headers=headers, timeout=timeout),
    )
    monkeypatch.setattr(server_task_module, "streamable_http_client", fake_streamable_http_client)

    task = MCPServerTask(
        "remote",
        {
            "transport": "http",
            "url": "https://mcp.example.test/mcp",
            "headers": {"Authorization": "Bearer ${DAGENT_MCP_TOKEN}"},
            "connect_timeout": 12,
            "tool_timeout": 90,
        },
    )

    async def open_streams():
        async with AsyncExitStack() as stack:
            return await task._open_streams(stack)

    read_stream, write_stream = run(open_streams())

    assert (read_stream, write_stream) == ("read", "write")
    assert events == [
        ("http_client", {"Authorization": "Bearer token"}),
        ("http_client_enter",),
        (
            "streamable_http",
            "https://mcp.example.test/mcp",
            {"Authorization": "Bearer token"},
            True,
        ),
        ("http_client_exit",),
    ]


def test_mcp_server_task_uses_separate_http_timeouts(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeHTTPClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    @asynccontextmanager
    async def fake_streamable_http_client(url, *, http_client=None):
        yield "read", "write", lambda: None

    def fake_create_mcp_http_client(headers=None, timeout=None):
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeHTTPClient()

    monkeypatch.setattr(server_task_module, "create_mcp_http_client", fake_create_mcp_http_client)
    monkeypatch.setattr(server_task_module, "streamable_http_client", fake_streamable_http_client)

    task = MCPServerTask(
        "remote",
        {
            "transport": "http",
            "url": "https://mcp.example.test/mcp",
            "connect_timeout": 7,
            "tool_timeout": 55,
        },
    )

    async def open_streams():
        async with AsyncExitStack() as stack:
            return await task._open_streams(stack)

    run(open_streams())

    timeout = captured["timeout"]
    assert timeout.connect == 7
    assert timeout.write == 7
    assert timeout.pool == 7
    assert timeout.read == 55


def test_mcp_server_task_uses_default_http_timeouts(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeHTTPClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    @asynccontextmanager
    async def fake_streamable_http_client(url, *, http_client=None):
        yield "read", "write", lambda: None

    def fake_create_mcp_http_client(headers=None, timeout=None):
        captured["timeout"] = timeout
        return FakeHTTPClient()

    monkeypatch.setattr(server_task_module, "create_mcp_http_client", fake_create_mcp_http_client)
    monkeypatch.setattr(server_task_module, "streamable_http_client", fake_streamable_http_client)

    task = MCPServerTask(
        "remote",
        {
            "transport": "http",
            "url": "https://mcp.example.test/mcp",
        },
    )

    async def open_streams():
        async with AsyncExitStack() as stack:
            return await task._open_streams(stack)

    run(open_streams())

    timeout = captured["timeout"]
    assert timeout.connect == 60
    assert timeout.write == 60
    assert timeout.pool == 60
    assert timeout.read == 300


def test_mcp_server_task_requires_explicit_http_transport_for_url() -> None:
    task = MCPServerTask("remote", {"url": "https://mcp.example.test/mcp"})

    try:
        task._transport()
    except ValueError as exc:
        assert "transport: http" in str(exc)
    else:
        raise AssertionError("url-based MCP configs should require explicit HTTP transport")


def test_mcp_server_task_rejects_transport_aliases() -> None:
    for transport in ("HTTP", " http ", "streamable_http", "sse"):
        task = MCPServerTask(
            "remote",
            {
                "transport": transport,
                "url": "https://mcp.example.test/mcp",
            },
        )

        try:
            task._transport()
        except ValueError:
            pass
        else:
            raise AssertionError(f"transport {transport!r} should be rejected")
