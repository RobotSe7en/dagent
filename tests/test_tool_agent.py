import asyncio
from pathlib import Path

import pytest

from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.harness_runtime import ToolAgentLoop
from dagent.schemas import Boundary
from dagent.tools.boundary import BoundaryViolation
from dagent.tools.executor import ToolExecutor
from dagent.tools.file_tools import create_file_tool_registry


def make_loop(tmp_path: Path, provider: MockProvider) -> ToolAgentLoop:
    executor = ToolExecutor(create_file_tool_registry(), workspace_root=tmp_path)
    return ToolAgentLoop(provider=provider, tool_executor=executor)


def run(coro):
    return asyncio.run(coro)


def test_tool_agent_loop_returns_plain_text_response(tmp_path: Path) -> None:
    provider = MockProvider([ChatResponse(content="Done.")])
    loop = make_loop(tmp_path, provider)

    result = run(
        loop.run(
            "Say done",
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
        )
    )

    assert result.status == "completed"
    assert result.final_answer == "Done."
    assert result.messages[-1] == {"role": "assistant", "content": "Done."}


def test_tool_agent_loop_streams_response_tokens(tmp_path: Path) -> None:
    provider = MockProvider([ChatResponse(content="<think>checking</think>\nDone.")])
    loop = make_loop(tmp_path, provider)
    tokens: list[str] = []

    result = run(
        loop.run(
            "Say done",
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
            on_token=tokens.append,
        )
    )

    assert result.status == "completed"
    assert tokens == ["<think>checking</think>\nDone."]
    assert result.final_answer == "Done."


def test_tool_agent_loop_executes_tool_call_and_writes_result_to_messages(
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

    assert result.status == "completed"
    assert result.final_answer == "I read it."
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


def test_tool_agent_loop_execution_context_keeps_evidence_after_500_chars(
    tmp_path: Path,
) -> None:
    evidence = "x" * 650 + " recent commit: 2026-05-14 00:17:20 +0800"
    (tmp_path / "notes.txt").write_text(evidence, encoding="utf-8")
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
            ChatResponse(content="The recent commit was at 2026-05-14 00:17:20 +0800."),
        ]
    )
    loop = make_loop(tmp_path, provider)

    result = run(
        loop.run(
            "Read notes",
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
        )
    )

    assert "2026-05-14 00:17:20 +0800" in result.execution_context
    assert "[TRUNCATED" not in result.execution_context


def test_tool_agent_loop_marks_truncated_execution_context(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("x" * 5000, encoding="utf-8")
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
            ChatResponse(content="Read it."),
        ]
    )
    loop = make_loop(tmp_path, provider)

    result = run(
        loop.run(
            "Read notes",
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
        )
    )

    assert "[TRUNCATED after 4000 chars]" in result.execution_context


def test_tool_agent_loop_emits_tool_events_in_execution_order(tmp_path: Path) -> None:
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
    events: list[dict] = []

    result = run(
        loop.run(
            "Read notes",
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
            on_event=events.append,
        )
    )

    assert result.status == "completed"
    assert [event["type"] for event in events] == ["tool_call", "tool_result"]
    assert events[0] == {
        "type": "tool_call",
        "tool_call_id": "call_1",
        "name": "read_file",
        "arguments": {"path": "notes.txt"},
    }
    assert events[1] == {
        "type": "tool_result",
        "tool_call_id": "call_1",
        "name": "read_file",
        "arguments": {"path": "notes.txt"},
        "content": "hello from file",
    }


def test_tool_agent_loop_stops_at_max_steps(tmp_path: Path) -> None:
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

    assert result.status == "failed"
    assert result.final_answer == ""
    assert len(provider.requests) == 1


def test_tool_agent_loop_feeds_boundary_violation_back_as_tool_message(tmp_path: Path) -> None:
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
            ),
            ChatResponse(content="Sorry, I can't write files."),
        ]
    )
    loop = make_loop(tmp_path, provider)

    result = run(
        loop.run(
            "Write notes",
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
        )
    )

    assert result.status == "completed"
    tool_msg = result.messages[2]
    assert tool_msg["role"] == "tool"
    assert "[BOUNDARY_VIOLATION]" in tool_msg["content"]


