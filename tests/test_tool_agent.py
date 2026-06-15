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


class StrictToolMessageProvider(MockProvider):
    async def chat(self, messages, tools=None):
        _assert_complete_tool_call_messages(messages)
        return await super().chat(messages, tools=tools)


def _assert_complete_tool_call_messages(messages: list[dict]) -> None:
    index = 0
    while index < len(messages):
        message = messages[index]
        tool_calls = message.get("tool_calls") or []
        if message.get("role") != "assistant" or not tool_calls:
            index += 1
            continue

        expected_ids = [tool_call["id"] for tool_call in tool_calls]
        seen_ids: list[str] = []
        cursor = index + 1
        while cursor < len(messages) and messages[cursor].get("role") == "tool":
            seen_ids.append(messages[cursor].get("tool_call_id"))
            cursor += 1
        missing = [tool_call_id for tool_call_id in expected_ids if tool_call_id not in seen_ids]
        assert not missing, f"missing tool messages for {missing}"
        index = cursor


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


def test_tool_agent_boundary_violation_requires_review_even_for_low_risk_tool(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "secret.txt").write_text("secret", encoding="utf-8")
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "blocked/secret.txt"},
                    )
                ]
            ),
        ]
    )
    agent = ToolAgent(loop=make_loop(tmp_path, provider), profile=_profile())

    result = run(
        agent.run_messages(
            [{"role": "user", "content": "Read the blocked file"}],
            boundary=Boundary(mode="read_only", allowed_paths=["allowed"]),
            review_level="fast",
        )
    )

    assert result.state.status == "awaiting_review"
    assert result.state.pending_review is not None
    assert result.state.pending_review.kind == "capability_review"
    assert result.state.pending_review.message == "Review boundary override: tool.read_file"
    assert result.state.pending_review.capability_call == {
        "invocation_id": "call_1",
        "capability_id": "tool.read_file",
        "arguments": {"path": "blocked/secret.txt"},
    }
    assert result.state.pending_review.payload["reason"] == "boundary_violation"
    assert "outside allowed paths" in result.state.pending_review.payload["error"]


def test_tool_agent_approves_boundary_review_for_one_tool_call(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "secret.txt").write_text("secret", encoding="utf-8")
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "blocked/secret.txt"},
                    )
                ]
            ),
            ChatResponse(content="I read the approved file."),
        ]
    )
    agent = ToolAgent(loop=make_loop(tmp_path, provider), profile=_profile())
    first = run(
        agent.run_messages(
            [{"role": "user", "content": "Read the blocked file"}],
            boundary=Boundary(mode="read_only", allowed_paths=["allowed"]),
            review_level="fast",
        )
    )

    resumed = run(agent.resume_review(first.state, approved=True))

    assert resumed.state.status == "completed"
    assert resumed.output_text == "I read the approved file."
    assert agent.messages[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "read_file",
        "content": "secret",
    }


def test_tool_agent_rejects_boundary_review_without_executing_tool(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    target = blocked / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="write_file",
                        arguments={"path": "blocked/secret.txt", "content": "changed"},
                    )
                ]
            ),
            ChatResponse(content="I will continue without writing."),
        ]
    )
    agent = ToolAgent(loop=make_loop(tmp_path, provider), profile=_profile())
    first = run(
        agent.run_messages(
            [{"role": "user", "content": "Write the blocked file"}],
            boundary=Boundary(mode="write_limited", allowed_paths=["allowed"]),
            review_level="fast",
        )
    )

    resumed = run(agent.resume_review(first.state, approved=False))

    assert resumed.state.status == "completed"
    assert resumed.output_text == "I will continue without writing."
    assert target.read_text(encoding="utf-8") == "secret"
    assert agent.messages[2]["content"] == (
        "[DENIED] Human reviewer denied this tool call. Continue without executing it."
    )


def test_tool_agent_rejects_review_with_sibling_tool_call_keeps_provider_history_valid(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()
    (allowed / "notes.txt").write_text("allowed", encoding="utf-8")
    target = blocked / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    provider = StrictToolMessageProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="write_file",
                        arguments={"path": "blocked/secret.txt", "content": "changed"},
                    ),
                    ToolCall(
                        id="call_2",
                        name="read_file",
                        arguments={"path": "allowed/notes.txt"},
                    ),
                ]
            ),
            ChatResponse(content="I will continue without writing."),
        ]
    )
    agent = ToolAgent(loop=make_loop(tmp_path, provider), profile=_profile())
    first = run(
        agent.run_messages(
            [{"role": "user", "content": "Write blocked and read allowed"}],
            boundary=Boundary(mode="write_limited", allowed_paths=["allowed"]),
            review_level="fast",
        )
    )

    resumed = run(agent.resume_review(first.state, approved=False))

    assert resumed.state.status == "completed"
    assert resumed.output_text == "I will continue without writing."
    assert target.read_text(encoding="utf-8") == "secret"
    assert agent.messages[2]["tool_call_id"] == "call_1"
    assert agent.messages[3]["tool_call_id"] == "call_2"
    assert "[TOOL_SKIPPED]" in agent.messages[3]["content"]


def test_tool_agent_rejected_review_includes_reviewer_feedback(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    target = blocked / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="write_file",
                        arguments={"path": "blocked/secret.txt", "content": "changed"},
                    )
                ]
            ),
            ChatResponse(content="I will use an allowed file instead."),
        ]
    )
    agent = ToolAgent(loop=make_loop(tmp_path, provider), profile=_profile())
    first = run(
        agent.run_messages(
            [{"role": "user", "content": "Write the blocked file"}],
            boundary=Boundary(mode="write_limited", allowed_paths=["allowed"]),
            review_level="fast",
        )
    )

    resumed = run(
        agent.resume_review(
            first.state,
            approved=False,
            feedback="Use allowed/notes.txt instead.",
        )
    )

    assert resumed.output_text == "I will use an allowed file instead."
    assert target.read_text(encoding="utf-8") == "secret"
    assert "Reviewer feedback: Use allowed/notes.txt instead." in agent.messages[2]["content"]
    assert "Reviewer feedback: Use allowed/notes.txt instead." in provider.requests[1]["messages"][-1]["content"]


def test_tool_agent_boundary_review_takes_precedence_over_careful_risk_review(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="write_file",
                        arguments={"path": "notes.txt", "content": "approved"},
                    )
                ]
            ),
            ChatResponse(content="Wrote after boundary approval."),
        ]
    )
    agent = ToolAgent(loop=make_loop(tmp_path, provider), profile=_profile())
    first = run(
        agent.run_messages(
            [{"role": "user", "content": "Write a file"}],
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
            review_level="careful",
        )
    )

    assert first.state.status == "awaiting_review"
    assert first.state.pending_review is not None
    assert first.state.pending_review.message == "Review boundary override: tool.write_file"
    assert first.state.pending_review.payload == {
        "capability_id": "tool.write_file",
        "risk": "medium",
        "reason": "boundary_violation",
        "error": "read_only boundary cannot perform write operations.",
    }

    resumed = run(agent.resume_review(first.state, approved=True))

    assert resumed.state.status == "completed"
    assert resumed.output_text == "Wrote after boundary approval."
    assert target.read_text(encoding="utf-8") == "approved"


def test_tool_agent_boundary_review_approval_does_not_expand_later_calls(tmp_path: Path) -> None:
    first_file = tmp_path / "first.txt"
    second_file = tmp_path / "second.txt"
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="write_file",
                        arguments={"path": "first.txt", "content": "one"},
                    )
                ]
            ),
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_2",
                        name="write_file",
                        arguments={"path": "second.txt", "content": "two"},
                    )
                ]
            ),
        ]
    )
    agent = ToolAgent(loop=make_loop(tmp_path, provider), profile=_profile())
    first = run(
        agent.run_messages(
            [{"role": "user", "content": "Write two files"}],
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
            review_level="fast",
        )
    )

    resumed = run(agent.resume_review(first.state, approved=True))

    assert first_file.read_text(encoding="utf-8") == "one"
    assert not second_file.exists()
    assert resumed.state.status == "awaiting_review"
    assert resumed.state.pending_review is not None
    assert resumed.state.pending_review.capability_call == {
        "invocation_id": "call_2",
        "capability_id": "tool.write_file",
        "arguments": {"path": "second.txt", "content": "two"},
    }
    assert resumed.state.pending_review.payload["reason"] == "boundary_violation"


def test_tool_agent_shell_cross_boundary_path_requires_review(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()
    (blocked / "secret.txt").write_text("secret", encoding="utf-8")
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="shell",
                        arguments={
                            "cwd": "allowed",
                            "command": "cat ../blocked/secret.txt",
                        },
                    )
                ]
            ),
        ]
    )
    agent = ToolAgent(loop=make_loop(tmp_path, provider), profile=_profile())

    result = run(
        agent.run_messages(
            [{"role": "user", "content": "Read through shell"}],
            boundary=Boundary(mode="write_limited", allowed_paths=["allowed"]),
            review_level="fast",
        )
    )

    assert result.state.status == "awaiting_review"
    assert result.state.pending_review is not None
    assert result.state.pending_review.message == "Review boundary override: tool.shell"
    assert result.state.pending_review.payload["reason"] == "boundary_violation"
    assert "../blocked/secret.txt" in result.state.pending_review.payload["error"]


def test_tool_agent_hard_blocked_command_is_not_reviewable(tmp_path: Path) -> None:
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="shell",
                        arguments={"cwd": ".", "command": "rm -rf /"},
                    )
                ]
            ),
            ChatResponse(content="I will not run that."),
        ]
    )
    agent = ToolAgent(loop=make_loop(tmp_path, provider), profile=_profile())

    result = run(
        agent.run_messages(
            [{"role": "user", "content": "Run a dangerous command"}],
            boundary=Boundary(mode="write_limited", allowed_paths=["."]),
            review_level="fast",
        )
    )

    assert result.state.status == "completed"
    assert result.state.pending_review is None
    tool_message = next(message for message in result.state.internal_messages if message["role"] == "tool")
    assert "[TOOL_ERROR]" in tool_message["content"]
    assert "blocked by shell safety policy" in tool_message["content"]


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
    (tmp_path / "notes.txt").write_text(("x" * 50 + "\n") * 100, encoding="utf-8")
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
