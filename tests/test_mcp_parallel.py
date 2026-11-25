"""MCP server declarations and shared connection admission."""

import asyncio
from contextlib import closing
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")
from mcp.types import Tool

import dagent
from dagent.capabilities.mcp import MCPCapabilityProvider
from dagent.capabilities.mcp.server_task import MCPServerTask
from dagent.capabilities.catalog import CapabilityCatalog
from dagent.providers import ChatResponse, MockProvider, ToolCall
from tests.test_mcp_provider import FakeMCPManager


def tool(name="query", safe=True, **kwargs):
    return Tool(name=name, inputSchema={"type": "object"},
                _meta={"dagent/parallelSafe": safe}, **kwargs)


async def wait(event):
    await asyncio.wait_for(event.wait(), 5)


@pytest.mark.parametrize("metadata,expected", [(None, False), ({}, False),
                         ({"dagent/parallelSafe": False}, False), ({"dagent/parallelSafe": True}, True)])
def test_server_metadata_is_preserved_by_registration_and_lazy_snapshot(metadata, expected, tmp_path):
    class Manager(FakeMCPManager):
        def discovered_tools(self):
            return {"docs": [Tool(name="query", inputSchema={"type": "object"}, _meta=metadata,
                                  annotations={"readOnlyHint": True})]}

    with closing(dagent.Runner(workspace=tmp_path, provider=MockProvider(), skill_roots=[])) as runner:
        runner._add_mcp_server("docs", {"command": "fake"}, manager=Manager())
        snapshot = runner.mcp_server_snapshot("docs")
        assert snapshot.tools[0].definition.parallel_safe is expected
    catalog = CapabilityCatalog()
    MCPCapabilityProvider({"docs": {"command": "fake"}}, manager=Manager(),
                          snapshots={"docs": snapshot}, lazy_connect=True).register_into(catalog)
    assert catalog.get("mcp.docs.query").parallel_safe is expected


@pytest.mark.parametrize("value", ["true", 1, None, []])
def test_invalid_server_safety_declaration_fails_registration(value):
    class Manager(FakeMCPManager):
        def discovered_tools(self):
            return {"docs": [tool(safe=value)]}

    with pytest.raises(ValueError, match="parallelSafe.*boolean"):
        MCPCapabilityProvider({"docs": {"command": "fake"}}, manager=Manager()).register_into(CapabilityCatalog())


@pytest.mark.parametrize("value", [0, -1, True, "2", 1.5, None])
def test_invalid_connection_capacity_fails_before_connect(value):
    manager = FakeMCPManager()
    with pytest.raises(ValueError, match="max_concurrency.*positive integer"):
        MCPCapabilityProvider({"docs": {"command": "fake", "max_concurrency": value}},
                              manager=manager).register_into(CapabilityCatalog())
    assert not manager.started


@pytest.mark.asyncio
async def test_same_server_parallel_calls_and_exclusive_barrier_share_capacity():
    started = [asyncio.Event() for _ in range(4)]
    release = [asyncio.Event() for _ in range(4)]
    order = []
    active = peak = 0

    async def execute(name, arguments):
        nonlocal active, peak
        index = arguments["index"]
        active += 1
        peak = max(peak, active)
        if name == "mutate":
            assert active == 1
        order.append(f"start{index}")
        started[index].set()
        try:
            await release[index].wait()
            return index
        finally:
            order.append(f"end{index}")
            active -= 1

    server = MCPServerTask("docs", {"max_concurrency": 2})
    server.tools = [tool(), tool("mutate", False)]
    server._session = SimpleNamespace(call_tool=execute)
    tasks = [asyncio.create_task(server.call_tool("query", {"index": i})) for i in range(2)]
    await wait(started[1])
    tasks.append(asyncio.create_task(server.call_tool("mutate", {"index": 2})))
    tasks.append(asyncio.create_task(server.call_tool("query", {"index": 3})))
    release[1].set()
    assert await tasks[1] == 1
    assert not started[2].is_set() and not started[3].is_set()
    release[0].set()
    await wait(started[2])
    assert not started[3].is_set()
    release[2].set()
    await wait(started[3])
    release[3].set()
    assert await asyncio.gather(*tasks) == [0, 1, 2, 3]
    assert peak == 2
    assert order.index("end0") < order.index("start2") < order.index("end2") < order.index("start3")


@pytest.mark.asyncio
async def test_cancelled_queued_and_active_rpc_release_capacity():
    started = asyncio.Event()

    async def execute(name, arguments):
        if arguments.get("block"):
            started.set()
            await asyncio.Event().wait()
        return "done"

    server = MCPServerTask("docs", {"max_concurrency": 2})
    server.tools = [tool(), tool("mutate", False)]
    server._session = SimpleNamespace(call_tool=execute)
    active = asyncio.create_task(server.call_tool("query", {"block": True}))
    await wait(started)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(server.call_tool("mutate", {}), .01)
    # Cancelling the exclusive waiter must unblock later readers.
    assert await asyncio.wait_for(server.call_tool("query", {}), 1) == "done"
    active.cancel()
    await asyncio.gather(active, return_exceptions=True)
    assert await asyncio.wait_for(server.call_tool("mutate", {}), 1) == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [1, 2])
async def test_connection_capacity_defaults_and_request_response_pairing(capacity):
    entered = asyncio.Event()
    release = asyncio.Event()
    active = peak = 0

    async def execute(name, arguments):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered.set()
        try:
            await release.wait()
            return arguments["index"]
        finally:
            active -= 1

    server = MCPServerTask("docs", {} if capacity == 1 else {"max_concurrency": capacity})
    server.tools = [tool()]
    server._session = SimpleNamespace(call_tool=execute)
    tasks = [asyncio.create_task(server.call_tool("query", {"index": i})) for i in range(3)]
    await wait(entered)
    release.set()
    assert await asyncio.gather(*tasks) == [0, 1, 2]
    assert peak == capacity


@pytest.mark.asyncio
async def test_toolloop_and_mcp_connection_both_allow_overlapping_calls(tmp_path):
    both = asyncio.Event()
    calls = []
    server = MCPServerTask("docs", {"max_concurrency": 2})
    server.tools = [tool()]

    async def execute(name, arguments):
        calls.append(arguments["index"])
        if len(calls) == 2:
            both.set()
        await both.wait()
        return SimpleNamespace(isError=False, content=[], structuredContent=arguments)

    server._session = SimpleNamespace(call_tool=execute)

    class Manager(FakeMCPManager):
        def discovered_tools(self):
            return {"docs": server.tools}

        async def call_tool(self, server_name, tool_name, arguments, timeout):
            return await server.call_tool(tool_name, arguments)

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id=str(i), name="mcp_docs_query", arguments={"index": i}) for i in range(2)]),
        ChatResponse(content="done"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        runner._add_mcp_server("docs", {"command": "fake", "max_concurrency": 2}, manager=Manager())
        result = await asyncio.wait_for(runner.run(dagent.ToolAgent(profile="conversation",
                        capabilities=["mcp.docs.query"], max_tool_concurrency=2), input="query"), 5)
    values = [item.value for item in result.conversation.items if isinstance(item, dagent.ToolResultMessage)]
    assert values == [{"index": 0}, {"index": 1}]


@pytest.mark.asyncio
async def test_real_stdio_requests_overlap_and_responses_keep_their_identity(tmp_path):
    # Both server handlers must arrive before either can return. A serialized
    # client fails the rendezvous, rather than merely taking longer to finish.
    program = '''import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
server = Server("parallel-test")
arrived = 0
both = asyncio.Event()
@server.list_tools()
async def tools():
    return [Tool(name="query", inputSchema={"type":"object"}, _meta={"dagent/parallelSafe": True})]
@server.call_tool()
async def call(name, arguments):
    global arrived
    arrived += 1
    if arrived == 2:
        both.set()
    await asyncio.wait_for(both.wait(), 5)
    return [TextContent(type="text", text=arguments["label"])]
async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
asyncio.run(main())
'''
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id=label, name="mcp_docs_query", arguments={"label": label}) for label in ("a", "b")]),
        ChatResponse(content="done"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        runner.add_mcp_server("docs", {"command": sys.executable, "args": ["-c", program],
                        "connect_timeout": 10, "tool_timeout": 10, "max_concurrency": 2})
        result = await asyncio.wait_for(runner.run(dagent.ToolAgent(profile="conversation",
                        capabilities=["mcp.docs.query"], max_tool_concurrency=2), input="query"), 15)
    messages = [item for item in result.conversation.items if isinstance(item, dagent.ToolResultMessage)]
    assert [item.status for item in messages] == ["completed", "completed"]
    assert [item.value for item in messages] == ["a", "b"]
