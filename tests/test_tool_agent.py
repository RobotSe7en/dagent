import asyncio
import inspect
from pathlib import Path

import pytest

from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.capabilities.decorator import tool
from dagent.capabilities.providers import ToolCapabilityProvider
from dagent.harness_runtime import ToolAgent, ToolAgentLoop
from dagent.harness_runtime import CapabilityExecutor
from dagent.harness_runtime.capability_scope import CapabilityScope
from dagent.profiles import AgentProfile
from dagent.schemas import Boundary, CapabilityDefinition, CapabilityPolicy, CapabilityInvocation, CapabilityResult
from dagent.capabilities.tools.file_tools import create_file_tool_registry


def make_loop(tmp_path: Path, provider: MockProvider) -> ToolAgentLoop:
    capability_catalog = CapabilityCatalog(workspace_root=tmp_path)
    capability_executor = CapabilityExecutor(capability_catalog)
    ToolCapabilityProvider(create_file_tool_registry()).register_into(capability_catalog)
    return ToolAgentLoop(
        provider=provider,
        capability_executor=capability_executor,
        tool_adapter=_tool_adapter(capability_catalog),
    )


def run(coro):
    return asyncio.run(coro)


def _tool_adapter(catalog: CapabilityCatalog) -> CapabilityToolAdapter:
    return CapabilityToolAdapter(
        catalog,
        toolsets=[CapabilityToolset("builtin", tuple(sorted(catalog.ids())))],
    )


def test_tool_agent_loop_run_does_not_accept_control_tool_handler() -> None:
    assert "control_tool_handler" not in inspect.signature(ToolAgentLoop.run).parameters


def test_tool_agent_loop_returns_plain_text_response(tmp_path: Path) -> None:
    provider = MockProvider([ChatResponse(content="Done.")])
    loop = make_loop(tmp_path, provider)

    result = run(
        loop.run(
            "Say done",
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
        )
    )

    assert result.state.status == "completed"
    assert result.output_text == "Done."
    assert result.state.internal_messages[-1] == {"role": "assistant", "content": "Done."}


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

    assert result.state.status == "completed"
    assert tokens == ["<think>checking</think>\nDone."]
    assert result.output_text == "Done."


def test_tool_agent_scope_can_disable_all_tools(tmp_path: Path) -> None:
    provider = MockProvider([ChatResponse(content="Plain answer.")])
    agent = ToolAgent(loop=make_loop(tmp_path, provider), profile=_profile())

    result = run(
        agent.run(
            "Answer without tools",
            capability_scope=CapabilityScope(capability_ids=()),
        )
    )

    assert result.state.status == "completed"
    assert provider.requests[0]["tools"] == []
    assert "## Available Tools" not in provider.requests[0]["messages"][0]["content"]


def test_tool_agent_scope_filters_tools_without_injecting_skill_prompt(tmp_path: Path) -> None:
    provider = MockProvider([ChatResponse(content="Done.")])
    agent = ToolAgent(loop=make_loop(tmp_path, provider), profile=_profile())

    result = run(
        agent.run(
            "Use scoped context",
            capability_scope=CapabilityScope(
                capability_ids=("tool.read_file",),
                skills=("writing/summarize",),
            ),
        )
    )

    assert result.state.status == "completed"
    assert result.state.capability_scope.capability_ids == ("tool.read_file",)
    assert result.state.capability_scope.skills == ("writing/summarize",)
    assert [tool["function"]["name"] for tool in provider.requests[0]["tools"]] == ["read_file"]
    system_content = provider.requests[0]["messages"][0]["content"]
    assert "writing/summarize" not in system_content
    assert "write_file" not in system_content


def test_tool_agent_fast_review_guard_preserves_execution_context(tmp_path: Path) -> None:
    seen_task_ids: list[str | None] = []

    @tool(risk="medium", supports_context=True)
    def needs_context(text: str, *, context=None, callbacks=None) -> str:
        seen_task_ids.append(context.task_id if context is not None else None)
        return f"ok:{text}"

    catalog = CapabilityCatalog(workspace_root=tmp_path)
    catalog.register(needs_context.definition, needs_context.handler, supports_context=True)
    provider = MockProvider(
        [
            ChatResponse(
                reasoning_content="need the context-aware tool",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="needs_context",
                        arguments={"text": "hello"},
                    )
                ]
            ),
            ChatResponse(content="Done."),
        ]
    )
    agent = ToolAgent(
        loop=ToolAgentLoop(
            provider=provider,
            capability_executor=CapabilityExecutor(catalog),
            tool_adapter=_tool_adapter(catalog),
        ),
        profile=_profile(),
    )

    result = run(agent.run("Use context", review_level="fast"))

    assert result.state.status == "completed"
    assert seen_task_ids
    assert seen_task_ids[0] is not None
    assistant_message = provider.requests[1]["messages"][-2]
    assert assistant_message["role"] == "assistant"
    assert assistant_message["reasoning_content"] == "need the context-aware tool"


def test_tool_agent_scope_rejects_model_call_to_excluded_tool(tmp_path: Path) -> None:
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="write_file",
                        arguments={"path": "notes.txt", "content": "hi"},
                    )
                ]
            ),
            ChatResponse(content="Recovered without writing."),
        ]
    )
    agent = ToolAgent(loop=make_loop(tmp_path, provider), profile=_profile())

    result = run(
        agent.run(
            "Only read files",
            capability_scope=CapabilityScope(capability_ids=("tool.read_file",)),
        )
    )

    assert result.state.status == "completed"
    assert not [
        node
        for node in result.state.trace.root.children
        if node.kind == "capability_call"
    ]
    assert result.output_text == "Recovered without writing."
    assert not (tmp_path / "notes.txt").exists()
    tool_message = next(message for message in result.state.internal_messages if message["role"] == "tool")
    assert tool_message["role"] == "tool"
    assert tool_message["name"] == "write_file"
    assert "[TOOL_ERROR]" in tool_message["content"]
    assert "write_file" in tool_message["content"]


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

    assert result.state.status == "completed"
    assert result.output_text == "I read it."
    assert result.state.internal_messages[1]["role"] == "assistant"
    assert result.state.internal_messages[1]["tool_calls"][0]["function"]["name"] == "read_file"
    assert result.state.internal_messages[1]["tool_calls"][0]["function"]["arguments"] == (
        '{"path": "notes.txt"}'
    )
    assert result.state.internal_messages[2] == {
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

    assert result.state.status == "completed"
    run_id = result.state.run_id
    assert [event["type"] for event in events] == [
        "response_started",
        "response_finished",
        "capability_call",
        "capability_result",
        "response_started",
        "response_token",
        "response_finished",
    ]
    capability_events = [event for event in events if event["type"].startswith("capability_")]
    assert capability_events[0] == {
        "type": "capability_call",
        "invocation_id": "call_1",
        "capability_id": "tool.read_file",
        "arguments": {"path": "notes.txt"},
        "run_id": run_id,
    }
    assert capability_events[1] == {
        "type": "capability_result",
        "invocation_id": "call_1",
        "capability_id": "tool.read_file",
        "arguments": {"path": "notes.txt"},
        "run_id": run_id,
        "content": "hello from file",
    }
    assert events[0]["model_step"] == 1
    assert events[4]["model_step"] == 2
    assert events[0]["response_id"] != events[4]["response_id"]


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

    assert result.state.status == "failed"
    assert result.output_text == ""
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

    assert result.state.status == "completed"
    tool_msg = result.state.internal_messages[2]
    assert tool_msg["role"] == "tool"
    assert "[BOUNDARY_VIOLATION]" in tool_msg["content"]


def test_tool_agent_resume_review_uses_adapter_function_name_for_capability(tmp_path: Path) -> None:
    catalog = CapabilityCatalog(workspace_root=tmp_path)
    definition = CapabilityDefinition(
        id="memory.read",
        name="memory.read",
        kind="memory",
        policy=CapabilityPolicy(risk="medium"),
    )
    catalog.register(definition, _capability_result("file content"))
    adapter = CapabilityToolAdapter(
        catalog,
        toolsets=[CapabilityToolset("builtin", ("memory.read",))],
    )
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="memory_read",
                        arguments={"key": "notes"},
                    )
                ]
            ),
            ChatResponse(content="Read it."),
        ]
    )
    agent = ToolAgent(
        loop=ToolAgentLoop(
            provider=provider,
            capability_executor=CapabilityExecutor(catalog),
            tool_adapter=adapter,
        ),
        profile=_profile(),
    )

    first = run(agent.run("Read notes", review_level="careful"))
    state = first.state.model_copy(update={"user_request": "Read notes", "review_level": "careful"})

    resumed = run(agent.resume_review(state, approved=True))

    assert resumed.state.status == "completed"
    assert agent.messages[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "memory_read",
        "content": "file content",
    }


def _capability_result(content: str):
    def handler(invocation: CapabilityInvocation) -> CapabilityResult:
        return CapabilityResult(
            invocation_id=invocation.invocation_id,
            capability_id=invocation.capability_id,
            kind=invocation.kind,
            status="completed",
            content=content,
        )

    return handler


def _profile() -> AgentProfile:
    return AgentProfile(
        name="conversation",
        content="You are a conversation agent.",
    )
