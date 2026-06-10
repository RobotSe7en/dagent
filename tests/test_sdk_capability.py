import asyncio
import json

from pydantic import BaseModel

from dagent import CapabilityBinding, tool
from dagent.capabilities import tool as tool_from_subsystem
from dagent.schemas import CapabilityInvocation, CapabilityResult


def run(coro):
    return asyncio.run(coro)


def test_tool_decorator_builds_definition_from_function_signature() -> None:
    @tool(risk="low")
    def echo(text: str, count: int = 1, loud: bool = False) -> str:
        """Echo text for SDK callers."""
        return f"{text}:{count}:{loud}"

    assert isinstance(echo, CapabilityBinding)
    assert echo.definition.id == "tool.echo"
    assert echo.definition.name == "echo"
    assert echo.definition.kind == "tool"
    assert echo.definition.description == "Echo text for SDK callers."
    assert echo.definition.parameters == {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "count": {"type": "integer", "default": 1},
            "loud": {"type": "boolean", "default": False},
        },
        "required": ["text"],
    }

    result = run(echo.handler(
        CapabilityInvocation(
            capability_id=echo.definition.id,
            kind=echo.definition.kind,
            arguments={"text": "hi"},
        )
    ))

    assert result.status == "completed"
    assert result.content == "hi:1:False"
    assert tool_from_subsystem is tool


def test_tool_decorator_serializes_structured_results_and_failures() -> None:
    class LookupResult(BaseModel):
        query: str
        matches: list[int]

    @tool(id="tool.lookup")
    async def lookup(query: str) -> LookupResult:
        return LookupResult(query=query, matches=[1, 2])

    @tool(id="tool.boom")
    def boom() -> str:
        raise ValueError("bad input")

    lookup_result = run(lookup.handler(
        CapabilityInvocation(
            capability_id=lookup.definition.id,
            kind=lookup.definition.kind,
            arguments={"query": "sdk"},
        )
    ))
    boom_result = run(boom.handler(
        CapabilityInvocation(
            capability_id=boom.definition.id,
            kind=boom.definition.kind,
        )
    ))

    assert lookup.definition.output_schema["properties"] == {
        "query": {"title": "Query", "type": "string"},
        "matches": {
            "items": {"type": "integer"},
            "title": "Matches",
            "type": "array",
        },
    }
    assert json.loads(lookup_result.content) == {"query": "sdk", "matches": [1, 2]}
    assert lookup_result.value == {"query": "sdk", "matches": [1, 2]}
    assert boom_result == CapabilityResult(
        invocation_id=boom_result.invocation_id,
        capability_id="tool.boom",
        kind="tool",
        status="failed",
        error="bad input",
        stop_reason="ValueError",
    )


def test_tool_decorator_resolves_postponed_annotations() -> None:
    namespace: dict[str, object] = {}
    exec(
        "\n".join([
            "from __future__ import annotations",
            "from pydantic import BaseModel",
            "from dagent import tool",
            "",
            "class LookupResult(BaseModel):",
            "    title: str",
            "",
            "@tool",
            "def lookup(query: str) -> LookupResult:",
            "    return LookupResult(title=query)",
        ]),
        namespace,
    )

    lookup = namespace["lookup"]

    assert lookup.definition.parameters["properties"]["query"]["type"] == "string"
    assert lookup.definition.output_schema["properties"]["title"]["type"] == "string"


def test_tool_decorator_keeps_explicit_result_value_unset_for_plain_text() -> None:
    @tool
    def explicit() -> CapabilityResult:
        return CapabilityResult(
            invocation_id="custom",
            capability_id="tool.explicit",
            kind="tool",
            status="completed",
            content="hello",
        )

    result = run(explicit.handler(
        CapabilityInvocation(
            capability_id=explicit.definition.id,
            kind=explicit.definition.kind,
        )
    ))

    assert result.content == "hello"
    assert result.value is None
