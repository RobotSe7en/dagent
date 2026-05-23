import asyncio
import json

from dagent import CapabilityBinding, capability
from dagent.capabilities import capability as capability_from_subsystem
from dagent.schemas import CapabilityInvocation, CapabilityResult


def run(coro):
    return asyncio.run(coro)


def test_capability_decorator_builds_definition_from_function_signature() -> None:
    @capability(risk="low")
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
    assert capability_from_subsystem is capability


def test_capability_decorator_serializes_structured_results_and_failures() -> None:
    @capability(id="tool.lookup")
    async def lookup(query: str) -> dict[str, object]:
        return {"query": query, "matches": [1, 2]}

    @capability(id="tool.boom")
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

    assert json.loads(lookup_result.content) == {"query": "sdk", "matches": [1, 2]}
    assert boom_result == CapabilityResult(
        invocation_id=boom_result.invocation_id,
        capability_id="tool.boom",
        kind="tool",
        status="failed",
        error="bad input",
        stop_reason="ValueError",
    )
