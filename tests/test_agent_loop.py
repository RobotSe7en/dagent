import asyncio
from pathlib import Path

import pytest

from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.runtime import AgentLoop
from dagent.schemas import Boundary
from dagent.tools.boundary import BoundaryViolation
from dagent.tools.executor import ToolExecutor
from dagent.tools.file_tools import create_file_tool_registry


def make_loop(tmp_path: Path, provider: MockProvider) -> AgentLoop:
    executor = ToolExecutor(create_file_tool_registry(), workspace_root=tmp_path)
    return AgentLoop(provider=provider, tool_executor=executor)


def run(coro):
    return asyncio.run(coro)


def test_agent_loop_returns_plain_text_response(tmp_path: Path) -> None:
    provider = MockProvider([ChatResponse(content="Done.")])
    loop = make_loop(tmp_path, provider)

    result = run(
        loop.run(
            "Say done",
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
        )
    )

    assert result.completed is True
    assert result.final_response == "Done."
    assert result.steps == 1
    assert result.messages[-1] == {"role": "assistant", "content": "Done."}


def test_agent_loop_executes_tool_call_and_writes_result_to_messages(
    tmp_path: Path,
) -> None:
    (tmp_path / "notes.txt").write_text("hello from file", encoding="utf-8")
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "notes.txt"},
                    )
                ]
            ),
            ChatResponse(content="I read it."),
        ]
    )
    loop = make_loop(tmp_path, provider)

    result = run(
        loop.run(
            "Read notes",
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
        )
    )

    assert result.completed is True
    assert result.final_response == "I read it."
    assert result.steps == 2
    assert result.messages[1]["role"] == "assistant"
    assert result.messages[1]["tool_calls"][0]["function"]["name"] == "read_file"
    assert result.messages[1]["tool_calls"][0]["function"]["arguments"] == (
        '{"path": "notes.txt"}'
    )
    assert result.messages[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "read_file",
        "content": "hello from file",
    }
    assert provider.requests[1]["messages"][-1]["role"] == "tool"


def test_agent_loop_stops_at_max_steps(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "notes.txt"},
                    )
                ]
            ),
            ChatResponse(content="This response should not be used."),
        ]
    )
    loop = make_loop(tmp_path, provider)

    result = run(
        loop.run(
            "Read notes",
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
            max_steps=1,
        )
    )

    assert result.completed is False
    assert result.stop_reason == "max_steps"
    assert result.steps == 1
    assert result.final_response == ""
    assert len(provider.requests) == 1


def test_agent_loop_enforces_boundary_for_tool_calls(tmp_path: Path) -> None:
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="write_file",
                        arguments={"path": "notes.txt", "content": "nope"},
                    )
                ]
            )
        ]
    )
    loop = make_loop(tmp_path, provider)

    with pytest.raises(BoundaryViolation, match="read_only"):
        run(
            loop.run(
                "Write notes",
                boundary=Boundary(mode="read_only", allowed_paths=["."]),
            )
        )


def test_agent_loop_hides_forbidden_tools_from_provider(tmp_path: Path) -> None:
    provider = MockProvider([ChatResponse(content="No tools needed.")])
    loop = make_loop(tmp_path, provider)

    run(
        loop.run(
            "Answer directly",
            boundary=Boundary(mode="read_only", forbidden_tools=["write_file"]),
        )
    )

    exposed_names = {
        tool_definition["function"]["name"]
        for tool_definition in provider.requests[0]["tools"]
    }
    assert "read_file" in exposed_names
    assert "grep" in exposed_names
    assert "write_file" not in exposed_names
