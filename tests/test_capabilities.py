import asyncio
import time

import pytest
from pydantic import ValidationError

from dagent.capabilities import CapabilityCatalog, create_default_capability_catalog
from dagent.capabilities.toolsets import CapabilityToolAdapter, CapabilityToolset
from dagent.harness_runtime import CapabilityExecutor
from dagent.schemas import CapabilityDefinition, CapabilityInvocation, CapabilityResult
from dagent.providers import ToolCall
from dagent.schemas import Boundary


def run(coro):
    return asyncio.run(coro)


def _result(invocation: CapabilityInvocation, content: str) -> CapabilityResult:
    return CapabilityResult(
        invocation_id=invocation.invocation_id,
        capability_id=invocation.capability_id,
        kind=invocation.kind,
        status="completed",
        content=content,
    )


def test_capability_kind_rejects_removed_file_and_shell_kinds() -> None:
    with pytest.raises(ValidationError):
        CapabilityDefinition(id="file.read", name="file_read", kind="file")

    with pytest.raises(ValidationError):
        CapabilityInvocation(capability_id="shell.say_hello", kind="shell")


def test_boundary_rejects_removed_mode_and_allowed_commands_fields() -> None:
    with pytest.raises(ValidationError):
        Boundary(mode="read_only", allowed_paths=["."])

    with pytest.raises(ValidationError):
        Boundary(allowed_paths=["."], allowed_commands=["ls"])


def test_capability_invocation_rejects_removed_boundary_peer_fields() -> None:
    stale_payloads = [
        {"boundary_mode": "read_only"},
        {"mode": "read_only"},
        {"allowed_commands": ["ls"]},
    ]

    for payload in stale_payloads:
        with pytest.raises(ValidationError):
            CapabilityInvocation(
                capability_id="tool.read_file",
                kind="tool",
                **payload,
            )


def test_capability_catalog_replaces_definition_and_handler_atomically() -> None:
    catalog = CapabilityCatalog()
    executor = CapabilityExecutor(catalog)
    first = CapabilityDefinition(id="tool.echo", name="echo", kind="tool")
    second = first.model_copy(update={"description": "second"})

    catalog.register(first, lambda invocation: _result(invocation, "first"))
    catalog.replace(second, lambda invocation: _result(invocation, "second"))

    result = run(executor.execute(
        CapabilityInvocation(capability_id="tool.echo", kind="tool")
    ))

    assert catalog.get("tool.echo") == second
    assert result.content == "second"


def test_capability_catalog_delete_removes_definition_and_handler() -> None:
    catalog = CapabilityCatalog()
    executor = CapabilityExecutor(catalog)
    definition = CapabilityDefinition(id="tool.echo", name="echo", kind="tool")

    catalog.register(definition, lambda invocation: _result(invocation, "old"))
    catalog.delete("tool.echo")
    catalog.register(definition, lambda invocation: _result(invocation, "new"))

    result = run(executor.execute(
        CapabilityInvocation(capability_id="tool.echo", kind="tool")
    ))

    assert result.content == "new"


def test_capability_executor_runs_sync_handlers_off_event_loop() -> None:
    catalog = CapabilityCatalog()
    executor = CapabilityExecutor(catalog)

    def handler(invocation: CapabilityInvocation) -> CapabilityResult:
        time.sleep(0.2)
        return _result(invocation, "done")

    catalog.register(CapabilityDefinition(id="tool.slow", name="slow", kind="tool"), handler)

    async def run_with_tick() -> float:
        start = time.perf_counter()

        async def tick() -> float:
            await asyncio.sleep(0.02)
            return time.perf_counter() - start

        invocation = CapabilityInvocation(capability_id="tool.slow", kind="tool")
        _, tick_delay = await asyncio.gather(executor.execute(invocation), tick())
        return tick_delay

    assert run(run_with_tick()) < 0.15


def test_capability_tool_adapter_exposes_enabled_toolset_as_openai_tools() -> None:
    catalog = CapabilityCatalog()
    catalog.register(
        CapabilityDefinition(
            id="tool.read_file",
            name="read_file",
            kind="tool",
            description="Read a file.",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        ),
        lambda invocation: _result(invocation, "read"),
    )
    catalog.register(
        CapabilityDefinition(id="tool.write_file", name="write_file", kind="tool", enabled=False),
        lambda invocation: _result(invocation, "write"),
    )
    adapter = CapabilityToolAdapter(
        catalog,
        toolsets=[CapabilityToolset(name="builtin", capability_ids=("tool.read_file", "tool.write_file"))],
    )

    definitions = adapter.definitions(("builtin",))

    assert definitions == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]


def test_capability_tool_adapter_maps_tool_call_to_invocation() -> None:
    catalog = CapabilityCatalog()
    catalog.register(
        CapabilityDefinition(id="tool.read_file", name="read_file", kind="tool"),
        lambda invocation: _result(invocation, "read"),
    )
    adapter = CapabilityToolAdapter(
        catalog,
        toolsets=[CapabilityToolset(name="builtin", capability_ids=("tool.read_file",))],
    )

    invocation = adapter.invocation_from_tool_call(
        ToolCall(id="call_1", name="read_file", arguments={"path": "README.md"}),
        Boundary(allowed_paths=["."]),
        enabled_toolsets=("builtin",),
    )

    assert invocation.invocation_id == "call_1"
    assert invocation.capability_id == "tool.read_file"
    assert invocation.kind == "tool"
    assert invocation.arguments == {"path": "README.md"}
    assert invocation.boundary.allowed_paths == ["."]


def test_capability_tool_adapter_rejects_name_collisions() -> None:
    catalog = CapabilityCatalog()
    catalog.register(
        CapabilityDefinition(id="tool.read", name="read", kind="tool"),
        lambda invocation: _result(invocation, "tool"),
    )
    catalog.register(
        CapabilityDefinition(id="memory.read", name="read", kind="memory"),
        lambda invocation: _result(invocation, "memory"),
    )
    adapter = CapabilityToolAdapter(
        catalog,
        toolsets=[CapabilityToolset(name="builtin", capability_ids=("tool.read", "memory.read"))],
    )

    with pytest.raises(ValueError, match="LLM tool name collision"):
        adapter.definitions(("builtin",))


def test_capability_tool_adapter_rejects_unknown_toolset() -> None:
    adapter = CapabilityToolAdapter(CapabilityCatalog(), toolsets=[])

    with pytest.raises(KeyError, match="Capability toolset 'missing' is not registered"):
        adapter.definitions(("missing",))


def test_capability_tool_adapter_keeps_explicit_empty_toolsets_empty() -> None:
    adapter = CapabilityToolAdapter(create_default_capability_catalog(), toolsets=[])

    with pytest.raises(KeyError, match="Capability toolset 'builtin' is not registered"):
        adapter.definitions(("builtin",))


def test_default_builtin_toolset_exposes_read_write_search_and_shell() -> None:
    adapter = CapabilityToolAdapter(create_default_capability_catalog())

    names = [
        definition["function"]["name"]
        for definition in adapter.definitions(("builtin",))
    ]

    assert names == ["read_file", "write_file", "edit_file", "list_files", "grep", "shell"]
