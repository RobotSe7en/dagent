"""Internal reads borrow definitions; public values and model schemas are isolated."""

from contextlib import closing

import pytest

import dagent
from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import CapabilityDefinition, CapabilityPolicy


def _adapter():
    catalog = CapabilityCatalog()
    definition = CapabilityDefinition(
        id="tool.echo", kind="tool",
        parameters={"type": "object", "properties": {"values": {"type": "array", "items": {"type": "string"}}}},
        output_schema={"type": "array", "items": {"type": "string"}},
        policy=CapabilityPolicy(secrets=["original"]),
        config={"nested": {"values": ["original"]}},
    )
    catalog.register(definition, lambda invocation: dagent.CapabilityResult.completed(invocation, "ok"))
    return catalog, CapabilityToolAdapter(catalog, toolsets=[CapabilityToolset("test", (definition.id,))]), definition


@pytest.mark.parametrize("source", ["get", "entry", "capabilities", "allowed", "call"])
def test_returned_definitions_do_not_expose_catalog_state(source):
    catalog, adapter, original = _adapter()
    if source == "get":
        definition = catalog.get(original.id)
    elif source == "entry":
        definition = catalog.get_entry(original.id).definition
    elif source == "capabilities":
        definition = adapter.capabilities(("test",))[0]
    elif source == "allowed":
        definition = adapter.ensure_allowed(original.id, enabled_toolsets=("test",))
    else:
        definition = adapter.definition_from_tool_call(ToolCall(id="call", name="tool_echo", arguments={}),
                                                      enabled_toolsets=("test",))
    definition.name = "changed"
    definition.parameters["properties"]["values"]["items"]["type"] = "integer"
    definition.output_schema["items"]["type"] = "integer"
    definition.policy.secrets.append("changed")
    definition.config["nested"]["values"].append("changed")
    assert catalog.get(original.id) == original
    assert adapter.capabilities(("test",)) == [original]


def test_tool_schemas_are_fresh_and_deeply_isolated():
    catalog, adapter, original = _adapter()
    first = adapter.definitions(("test",))
    first[0]["function"]["parameters"]["properties"]["values"]["items"]["type"] = "integer"
    first[0]["function"]["name"] = "changed"
    second = adapter.definitions(("test",))
    assert second[0]["function"]["name"] == "tool_echo"
    assert second[0]["function"]["parameters"] == original.parameters
    assert catalog.get(original.id) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_provider_schema_mutation_does_not_reach_catalog_or_next_step(tmp_path, stream):
    @dagent.tool
    def echo(values: list[str]) -> str:
        return ",".join(values)

    class MutatingProvider(MockProvider):
        async def chat(self, messages, tools=None, *, response_format=None):
            parameters = tools[0]["function"]["parameters"]
            assert parameters["properties"]["values"]["items"]["type"] == "string"
            parameters["properties"]["values"]["items"]["type"] = "integer"
            return await super().chat(messages, tools, response_format=response_format)

    provider = MutatingProvider([
        ChatResponse(tool_calls=[ToolCall(id="call", name="tool_echo", arguments={"values": ["original"]})]),
        ChatResponse(content="done"),
    ])
    with closing(dagent.Runner(workspace=tmp_path, provider=provider, skill_roots=[])) as runner:
        agent = dagent.ToolAgent(profile="conversation", capabilities=[echo])
        if stream:
            events = [event async for event in runner.stream(agent, input="echo")]
            result = events[-1].data.result
        else:
            result = await runner.run(agent, input="echo")
        assert runner.get_capability("tool.echo").parameters == echo.definition.parameters
    assert result.status == "completed" and len(provider.requests) == 2


@pytest.mark.asyncio
async def test_loop_definition_copy_counts_do_not_grow_with_tool_count(tmp_path, monkeypatch):
    copies = []
    original_copy = CapabilityDefinition.model_copy

    def counted(self, *args, **kwargs):
        if kwargs.get("deep"):
            copies.append(self.id)
        return original_copy(self, *args, **kwargs)

    monkeypatch.setattr(CapabilityDefinition, "model_copy", counted)
    per_size = []
    for tool_count in (3, 40):
        bindings = []
        for index in range(tool_count):
            def echo() -> str:
                return "ok"

            echo.__name__ = f"echo_{index}"
            bindings.append(dagent.tool(name=f"echo_{index}")(echo))
        per_step = []

        class CountingProvider(MockProvider):
            async def chat(self, messages, tools=None, *, response_format=None):
                per_step.append(len(copies))
                copies.clear()
                return await super().chat(messages, tools, response_format=response_format)

        provider = CountingProvider([
            *(ChatResponse(tool_calls=[ToolCall(id=f"call_{index}", name="echo_0", arguments={})]) for index in range(3)),
            ChatResponse(content="done"),
        ])
        with closing(dagent.Runner(workspace=tmp_path, provider=provider, skill_roots=[])) as runner:
            result = await runner.run(dagent.ToolAgent(profile="conversation", capabilities=bindings), input="repeat")
        assert result.status == "completed"
        # The first request includes registration/configuration. The following
        # intervals each include one execution and preparation of the next step.
        assert len(per_step) == 4
        assert len(set(per_step[1:])) == 1
        assert 0 < per_step[1] < 10
        per_size.append(per_step[1:])
    assert per_size[0] == per_size[1]


def test_borrowed_reads_observe_catalog_updates_and_preserve_selection_errors():
    catalog, adapter, definition = _adapter()
    catalog.set_enabled(definition.id, False)
    assert adapter.definitions(("test",)) == []
    with pytest.raises(KeyError, match="Capability 'tool.echo' is not enabled"):
        adapter.ensure_allowed(definition.id, enabled_toolsets=("test",))
    renamed = definition.model_copy(update={"name": "renamed"})
    catalog.replace(renamed, lambda invocation: dagent.CapabilityResult.completed(invocation, "ok"))
    call = ToolCall(id="call", name="renamed", arguments={})
    assert adapter.definition_from_tool_call(call, enabled_toolsets=("test",)) == renamed
    with pytest.raises(KeyError, match="Capability 'tool.missing' is not registered in enabled toolsets"):
        adapter.definitions(("test",), capability_ids=["tool.missing"])
    catalog.delete(definition.id)
    with pytest.raises(KeyError, match="Capability 'tool.echo' is not registered"):
        adapter.definitions(("test",))
