import asyncio
import time

import pytest
from pydantic import ValidationError

from dagent.capabilities import CapabilityCatalog, create_default_capability_catalog
from dagent.capabilities.toolsets import CapabilityToolAdapter, CapabilityToolset
from dagent.harness_runtime import CapabilityExecutor
from dagent.schemas import (
    CapabilityDefinition,
    CapabilityInvocation,
    CapabilityResult,
    validate_capability_name,
)
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
        CapabilityDefinition(id="file.read", kind="file")

    with pytest.raises(ValidationError):
        CapabilityInvocation(capability_id="shell.say_hello", kind="shell")


def test_capability_definition_defaults_name_and_display_name() -> None:
    definition = CapabilityDefinition(id="tool.echo", kind="tool")

    assert definition.name == "tool_echo"
    assert definition.display_name == "tool_echo"


def test_capability_definition_accepts_explicit_name_and_display_name() -> None:
    definition = CapabilityDefinition(
        id="mcp.github.create_issue",
        kind="mcp",
        name="github_create_issue",
        display_name="Create issue",
    )

    assert definition.name == "github_create_issue"
    assert definition.display_name == "Create issue"


def test_capability_definition_rejects_invalid_name() -> None:
    with pytest.raises(ValueError, match="Capability names"):
        validate_capability_name("bad-name")

    with pytest.raises(ValidationError):
        CapabilityDefinition(id="tool.echo", kind="tool", name="bad-name")


def test_capability_catalog_rejects_whitespace_padded_ids() -> None:
    catalog = CapabilityCatalog()

    with pytest.raises(ValueError, match="Capability ids"):
        catalog.register(
            CapabilityDefinition(id=" tool.echo ", kind="tool"),
            lambda invocation: _result(invocation, "echo"),
        )

    assert catalog.get(" tool.echo ") is None
    assert catalog.get("tool.echo") is None


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
    first = CapabilityDefinition(id="tool.echo", kind="tool")
    second = first.model_copy(update={"description": "second"})

    catalog.register(first, lambda invocation: _result(invocation, "first"))
    catalog.replace(second, lambda invocation: _result(invocation, "second"))

    result = run(executor.execute(
        CapabilityInvocation(capability_id="tool.echo", kind="tool")
    ))

    assert catalog.get("tool.echo") == second
    assert result.content == "second"


def test_capability_catalog_rejects_duplicate_names() -> None:
    catalog = CapabilityCatalog()
    first = CapabilityDefinition(id="tool.first", kind="tool", name="shared")
    second = CapabilityDefinition(id="tool.second", kind="tool", name="shared")

    catalog.register(first, lambda invocation: _result(invocation, "first"))

    with pytest.raises(ValueError, match="Capability name 'shared' is already registered"):
        catalog.register(second, lambda invocation: _result(invocation, "second"))

    assert catalog.get("tool.first") == first
    assert catalog.get("tool.second") is None


def test_capability_catalog_replace_maintains_name_index_atomically() -> None:
    catalog = CapabilityCatalog()
    first = CapabilityDefinition(id="tool.first", kind="tool", name="first")
    second = CapabilityDefinition(id="tool.second", kind="tool", name="second")

    catalog.register(first, lambda invocation: _result(invocation, "first"))
    catalog.register(second, lambda invocation: _result(invocation, "second"))

    with pytest.raises(ValueError, match="Capability name 'second' is already registered"):
        catalog.replace(
            first.model_copy(update={"name": "second"}),
            lambda invocation: _result(invocation, "updated"),
        )

    assert catalog.get("tool.first") == first
    assert catalog.get("tool.second") == second

    updated = first.model_copy(update={"name": "updated"})
    catalog.replace(updated, lambda invocation: _result(invocation, "updated"))
    catalog.register(
        CapabilityDefinition(id="tool.third", kind="tool", name="first"),
        lambda invocation: _result(invocation, "third"),
    )

    assert catalog.get("tool.first") == updated


def test_capability_catalog_delete_removes_definition_and_handler() -> None:
    catalog = CapabilityCatalog()
    executor = CapabilityExecutor(catalog)
    definition = CapabilityDefinition(id="tool.echo", kind="tool")

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

    catalog.register(CapabilityDefinition(id="tool.slow", kind="tool"), handler)

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
            kind="tool",
            description="Read a file.",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        ),
        lambda invocation: _result(invocation, "read"),
    )
    catalog.register(
        CapabilityDefinition(id="tool.write_file", kind="tool", enabled=False),
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
                "name": "tool_read_file",
                "description": "Read a file.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]


def test_capability_tool_adapter_maps_tool_call_to_invocation() -> None:
    catalog = CapabilityCatalog()
    catalog.register(
        CapabilityDefinition(id="tool.read_file", kind="tool"),
        lambda invocation: _result(invocation, "read"),
    )
    adapter = CapabilityToolAdapter(
        catalog,
        toolsets=[CapabilityToolset(name="builtin", capability_ids=("tool.read_file",))],
    )

    invocation = adapter.invocation_from_tool_call(
        ToolCall(id="call_1", name="tool_read_file", arguments={"path": "README.md"}),
        Boundary(allowed_paths=["."]),
        enabled_toolsets=("builtin",),
    )

    assert invocation.invocation_id == "call_1"
    assert invocation.capability_id == "tool.read_file"
    assert invocation.kind == "tool"
    assert invocation.arguments == {"path": "README.md"}
    assert invocation.boundary.allowed_paths == ["."]


def test_capability_tool_adapter_uses_definition_names() -> None:
    catalog = CapabilityCatalog()
    catalog.register(
        CapabilityDefinition(id="tool.read", kind="tool", name="read"),
        lambda invocation: _result(invocation, "tool"),
    )
    catalog.register(
        CapabilityDefinition(id="memory.read", kind="memory"),
        lambda invocation: _result(invocation, "memory"),
    )
    adapter = CapabilityToolAdapter(
        catalog,
        toolsets=[CapabilityToolset(name="builtin", capability_ids=("tool.read", "memory.read"))],
    )

    names = [
        definition["function"]["name"]
        for definition in adapter.definitions(("builtin",))
    ]

    assert names == ["read", "memory_read"]

    invocation = adapter.invocation_from_tool_call(
        ToolCall(id="call_1", name="read", arguments={}),
        Boundary(),
        enabled_toolsets=("builtin",),
    )
    assert invocation.capability_id == "tool.read"


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

    assert names == [
        "tool_read_file",
        "tool_write_file",
        "tool_edit_file",
        "tool_list_files",
        "tool_grep",
        "tool_shell",
    ]
