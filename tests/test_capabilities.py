from dagent.capabilities import CapabilityCatalog
from dagent.harness_runtime import CapabilityExecutor
from dagent.schemas import CapabilityDefinition, CapabilityInvocation, CapabilityResult


def _result(invocation: CapabilityInvocation, content: str) -> CapabilityResult:
    return CapabilityResult(
        invocation_id=invocation.invocation_id,
        capability_id=invocation.capability_id,
        kind=invocation.kind,
        status="completed",
        content=content,
    )


def test_capability_catalog_replaces_definition_and_handler_atomically() -> None:
    catalog = CapabilityCatalog()
    executor = CapabilityExecutor(catalog)
    first = CapabilityDefinition(id="custom_tool.echo", name="echo", kind="custom_tool")
    second = first.model_copy(update={"description": "second"})

    catalog.register(first, lambda invocation: _result(invocation, "first"))
    catalog.replace(second, lambda invocation: _result(invocation, "second"))

    result = executor.execute(
        CapabilityInvocation(capability_id="custom_tool.echo", kind="custom_tool")
    )

    assert catalog.get("custom_tool.echo") == second
    assert result.content == "second"


def test_capability_catalog_delete_removes_definition_and_handler() -> None:
    catalog = CapabilityCatalog()
    executor = CapabilityExecutor(catalog)
    definition = CapabilityDefinition(id="custom_tool.echo", name="echo", kind="custom_tool")

    catalog.register(definition, lambda invocation: _result(invocation, "old"))
    catalog.delete("custom_tool.echo")
    catalog.register(definition, lambda invocation: _result(invocation, "new"))

    result = executor.execute(
        CapabilityInvocation(capability_id="custom_tool.echo", kind="custom_tool")
    )

    assert result.content == "new"
