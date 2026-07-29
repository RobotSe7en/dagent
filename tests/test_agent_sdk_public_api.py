import asyncio
import inspect

import pytest
from pydantic import ValidationError

import dagent
from dagent.providers import ChatResponse, ChatStreamEvent, MockProvider, ToolCall
from tests.planner_helpers import capability_plan_response, final_answer_response


def run(coro):
    return asyncio.run(coro)


def test_package_exposes_tool_and_separate_agent_entrypoints() -> None:
    assert hasattr(dagent, "tool")
    assert hasattr(dagent.capabilities, "tool")
    assert not hasattr(dagent, "capability")
    assert not hasattr(dagent.capabilities, "capability")
    assert hasattr(dagent, "Runner")
    assert hasattr(dagent, "AutoAgent")
    assert hasattr(dagent, "ToolAgent")
    assert hasattr(dagent, "DagAgent")
    assert hasattr(dagent, "ArtifactUpload")
    assert hasattr(dagent, "CapabilityScope")
    assert hasattr(dagent, "ProfileStore")
    assert hasattr(dagent, "Provider")
    assert hasattr(dagent, "ReviewLevel")
    assert hasattr(dagent, "RunStreamEvent")
    assert hasattr(dagent, "RunCheckpoint")
    assert hasattr(dagent, "ResolvedRunPlan")
    assert hasattr(dagent, "ExecutionLimits")
    assert hasattr(dagent, "ExecutionUsage")
    assert hasattr(dagent, "ExecutionLimitExceeded")
    assert hasattr(dagent, "SkillStore")
    assert hasattr(dagent, "load_builtin_profile")
    assert hasattr(dagent, "list_builtin_profiles")
    assert hasattr(dagent, "validate_dag_spec")
    assert hasattr(dagent, "Node")
    assert hasattr(dagent.Runner, "stream")
    assert hasattr(dagent.Runner, "resume_stream")
    assert hasattr(dagent.Runner, "add_agent")
    assert not hasattr(dagent, "RunStreamChunk")
    assert not hasattr(dagent.Runner, "stream_events")
    assert not hasattr(dagent.Runner, "resume_stream_events")
    assert not hasattr(dagent, "NodeRef")
    assert not hasattr(dagent.Dag, "capability_node")
    assert not hasattr(dagent.Dag, "agent_node")
    assert not hasattr(dagent, "DAgent")
    assert not hasattr(dagent, "OpenAICompatibleProvider")
    assert not hasattr(dagent, "ProviderConfig")
    assert not hasattr(dagent, "RuntimeMode")
    assert not hasattr(dagent, "run_dag")
    assert not hasattr(dagent, "MCPServerRegistrationResult")
    assert not hasattr(dagent, "MCPServerSnapshot")
    assert not hasattr(dagent, "MCPToolSnapshot")
    assert not hasattr(dagent, "RunnerCatalogView")
    assert not hasattr(dagent.schemas, "RunMessage")


def test_auto_agent_is_public_target_without_mode_field() -> None:
    assert "mode" not in inspect.signature(dagent.AutoAgent).parameters


def test_runner_accepts_exact_workspace_path_parameter() -> None:
    assert "workspace_path" in inspect.signature(dagent.Runner.run).parameters
    assert "workspace_path" in inspect.signature(dagent.Runner.stream).parameters


def test_builtin_profiles_are_available_from_package_root() -> None:
    profile = dagent.load_builtin_profile("conversation")

    assert profile.name == "conversation"
    assert "General-Purpose Agent" in profile.content
    assert "conversation" in {item.name for item in dagent.list_builtin_profiles()}


def test_provider_is_public_from_package_root() -> None:
    provider = dagent.Provider(
        base_url="https://example.test/v1",
        model="test-model",
        api_key="test-key",
        reasoning={"enabled": True, "effort": "medium"},
        extra_request_args={"temperature": 0},
        extra_body={"chat_template_kwargs": {"enable_thinking": True}},
    )

    assert provider.config.base_url == "https://example.test/v1"
    assert provider.config.model == "test-model"
    assert provider.config.api_key == "test-key"
    assert provider.config.reasoning is not None
    assert provider.config.reasoning.enabled is True
    assert provider.config.reasoning.effort == "medium"
    assert provider.config.extra_request_args == {"temperature": 0}
    assert provider.config.extra_body == {
        "chat_template_kwargs": {"enable_thinking": True},
    }
    assert "config" not in inspect.signature(dagent.Provider).parameters


def test_review_handle_decisions_accept_reviewer_feedback() -> None:
    handle = dagent.ReviewHandle(
        dagent.PendingReview(
            review_id="review_1",
            kind="capability_review",
            message="Review capability call.",
            capability_call={
                "invocation_id": "call_1",
                "capability_id": "tool.search",
                "tool_name": "tool_search",
                "arguments": {},
            },
        )
    )

    approved = handle.approve(feedback="Continue, then summarize the result.")
    rejected = handle.reject(feedback="Use the allowed notes file instead.")

    assert approved.feedback == "Continue, then summarize the result."
    assert rejected.feedback == "Use the allowed notes file instead."


def test_tool_decorator_has_tool_only_signature() -> None:
    assert "id" not in inspect.signature(dagent.tool).parameters
    assert "name" in inspect.signature(dagent.tool).parameters
    assert "display_name" in inspect.signature(dagent.tool).parameters
    assert "kind" not in inspect.signature(dagent.tool).parameters
    assert "manager" not in inspect.signature(dagent.Runner.add_mcp_server).parameters
    assert hasattr(dagent.Runner, "remove_mcp_server")
    assert hasattr(dagent.Runner, "replace_mcp_server")
    assert hasattr(dagent.Runner, "reload_tools")
    assert hasattr(dagent.Runner, "reload_python_tool_sources")
    assert hasattr(dagent.Runner, "derive")
    assert hasattr(dagent.Runner, "catalog_view")
    assert hasattr(dagent.Runner, "mcp_server_snapshot")
    assert hasattr(dagent.Runner, "list_mcp_server_snapshots")
    assert hasattr(dagent.Runner, "reload_mcp_servers_with_snapshots")
    assert "manager" not in inspect.signature(dagent.Runner.replace_mcp_server).parameters


def test_tool_decorator_matches_tool_default_kind() -> None:
    @dagent.tool
    def search(q: str) -> str:
        """Search text."""
        return f"found:{q}"

    @dagent.tool(risk="medium")
    def write_file(path: str, content: str) -> str:
        return f"wrote:{path}"

    assert search.definition.id == "tool.search"
    assert search.definition.name == "tool_search"
    assert search.definition.display_name == "tool_search"
    assert search.definition.kind == "tool"
    assert search.definition.policy.risk == "low"
    assert write_file.definition.id == "tool.write_file"
    assert write_file.definition.policy.risk == "medium"


def test_tool_decorator_registers_tool_capability() -> None:
    @dagent.tool
    def search(q: str) -> str:
        """Search text."""
        return f"found:{q}"

    assert search.definition.id == "tool.search"
    assert search.definition.name == "tool_search"
    assert search.definition.display_name == "tool_search"
    assert search.definition.kind == "tool"


def test_runner_runs_profile_backed_tool_agent_cycle(tmp_path) -> None:
    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    _profile_root(tmp_path)
    provider = MockProvider([ChatResponse(content="hello")])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=["tool.search"],
    )
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        capabilities=[search],
        profile_root=tmp_path / "profiles",
        skill_roots=[],
    )

    result = run(runner.run(agent, input="hi"))

    assert result.output_text == "hello"
    system_message = provider.requests[0]["messages"][0]["content"]
    assert "You are a conversation profile." in system_message
    assert "search" not in system_message
    assert _tool_names(provider.requests[0]) == {"tool_search"}


def test_runner_loads_builtin_profile_without_cwd_profiles(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="hello")])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    result = run(runner.run(dagent.ToolAgent(profile="conversation"), input="hi"))

    assert result.output_text == "hello"
    system_message = provider.requests[0]["messages"][0]["content"]
    assert "General-Purpose Agent" in system_message


def test_runner_stream_yields_unified_event_protocol(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="<think>checking</think>hello")])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    async def collect() -> list[dagent.RunStreamEvent]:
        return [
            event
            async for event in runner.stream(
                dagent.ToolAgent(profile="conversation"),
                input="hi",
            )
        ]

    events = run(collect())

    assert [event.type for event in events] == [
        "run.started",
        "response.started",
        "response.reasoning.delta",
        "response.content.delta",
        "response.finished",
        "run.finished",
    ]
    assert events[0].data.kind == "tool"
    assert events[0].run_id is not None
    result = events[-1].data.result
    assert isinstance(result, dagent.RunResult)
    assert result.output_text == "hello"
    assert events[0].run_id == result.run_id
    assert all(event.run_id == result.run_id for event in events)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))


def test_runner_stream_brackets_each_model_call_with_response_boundaries(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="<think>checking</think>hello")])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    async def collect() -> list[dagent.RunStreamEvent]:
        return [
            event
            async for event in runner.stream(
                dagent.ToolAgent(profile="conversation"),
                input="hi",
            )
        ]

    events = run(collect())

    started = next(event for event in events if event.type == "response.started")
    finished = next(event for event in events if event.type == "response.finished")
    deltas = [event for event in events if event.type.endswith(".delta")]
    assert started.data.response_id
    assert started.data.response_id == finished.data.response_id
    assert started.data.model_step == 1
    assert started.data.run_id == events[0].run_id
    assert all(event.data.response_id == started.data.response_id for event in deltas)
    reasoning = "".join(
        event.data.delta for event in events if event.type == "response.reasoning.delta"
    )
    content = "".join(
        event.data.delta for event in events if event.type == "response.content.delta"
    )
    assert reasoning == "checking"
    assert content == "hello"


def test_runner_stream_content_deltas_match_output_text(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="<think>checking</think>\n\nhello world")])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    async def collect() -> list[dagent.RunStreamEvent]:
        return [
            event
            async for event in runner.stream(
                dagent.ToolAgent(profile="conversation"),
                input="hi",
            )
        ]

    events = run(collect())

    content = "".join(
        event.data.delta for event in events if event.type == "response.content.delta"
    )
    result = events[-1].data.result
    assert content == "hello world"
    assert content == result.output_text
    assert result.state.trace.root.output is None
    model_call = next(
        node
        for node in result.state.trace.root.children
        if node.kind == "model_call"
    )
    assert model_call.output == {
        "content": "hello world",
        "reasoning": "checking",
        "refusal": "",
    }


def test_runner_stream_maps_provider_reasoning_channel(tmp_path) -> None:
    class ReasoningStreamProvider:
        async def chat(self, messages, tools=None, *, response_format=None):
            return ChatResponse(content="hello", reasoning_content="checking")

        async def stream_chat(self, messages, tools=None, *, response_format=None):
            yield ChatStreamEvent(
                type="token",
                channel="reasoning",
                content="checking",
            )
            yield ChatStreamEvent(
                type="token",
                channel="content",
                content="hello",
            )
            yield ChatStreamEvent(
                type="done",
                response=ChatResponse(
                    content="hello",
                    reasoning_content="checking",
                ),
            )

    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=ReasoningStreamProvider())

    async def collect() -> list[dagent.RunStreamEvent]:
        return [
            event
            async for event in runner.stream(
                dagent.ToolAgent(profile="conversation"),
                input="hi",
            )
        ]

    events = run(collect())

    reasoning = "".join(
        event.data.delta for event in events if event.type == "response.reasoning.delta"
    )
    content = "".join(
        event.data.delta for event in events if event.type == "response.content.delta"
    )
    result = events[-1].data.result
    assert reasoning == "checking"
    assert content == "hello"
    assert result.output_text == "hello"


def test_runner_stream_brackets_chat_only_provider_response(tmp_path) -> None:
    class ChatOnlyProvider:
        async def chat(self, messages, tools=None, *, response_format=None):
            return ChatResponse(content="hello")

    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=ChatOnlyProvider())

    async def collect() -> list[dagent.RunStreamEvent]:
        return [
            event
            async for event in runner.stream(
                dagent.ToolAgent(profile="conversation"),
                input="hi",
            )
        ]

    events = run(collect())

    assert [event.type for event in events] == [
        "run.started",
        "response.started",
        "response.finished",
        "run.finished",
    ]
    assert events[1].data.run_id == events[0].run_id
    assert events[1].data.response_id == events[2].data.response_id


def test_run_result_and_stream_event_model_dump_are_json_ready(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="<think>checking</think>hello")])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    async def collect() -> list[dagent.RunStreamEvent]:
        return [
            event
            async for event in runner.stream(
                dagent.ToolAgent(profile="conversation"),
                input="hi",
            )
        ]

    events = run(collect())
    result = events[-1].data.result

    assert result is not None
    assert result.model_dump(mode="json")["output_text"] == "hello"
    run_id = events[0].run_id
    response_id = next(
        event.data.response_id for event in events if event.type == "response.started"
    )
    delta_events = [
        event.model_dump(mode="json")
        for event in events
        if event.type.endswith(".delta")
    ]
    assert delta_events == [
        {
            "type": "response.reasoning.delta",
            "data": {
                "delta": "checking",
                "response_id": response_id,
                "model_step": 1,
                "run_id": run_id,
                "dag_id": None,
                "node_id": None,
                "parent_capability_id": None,
            },
            "sequence": 3,
            "run_id": run_id,
        },
        {
            "type": "response.content.delta",
            "data": {
                "delta": "hello",
                "response_id": response_id,
                "model_step": 1,
                "run_id": run_id,
                "dag_id": None,
                "node_id": None,
                "parent_capability_id": None,
            },
            "sequence": 4,
            "run_id": run_id,
        },
    ]
    assert events[-1].model_dump(mode="json")["data"]["result"]["output_text"] == "hello"


def test_run_result_model_validate_round_trips_current_payload_and_rejects_legacy_fields(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="hello")])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    result = run(runner.run(dagent.ToolAgent(profile="conversation"), input="hi"))
    restored = dagent.RunResult.model_validate(result.model_dump(mode="json"))

    assert restored.output_text == "hello"
    assert restored.run_id == result.run_id
    assert restored.state == result.state
    with pytest.raises(ValidationError):
        dagent.RunResult.model_validate({
            "output": "hello",
            "output_text": "hello",
            "state": result.state.model_dump(mode="json"),
        })
    with pytest.raises(ValidationError):
        dagent.RunResult.model_validate({
            "output_text": None,
            "state": result.state.model_dump(mode="json"),
        })


def test_runner_stream_does_not_poll_queue_with_timeout(tmp_path, monkeypatch) -> None:
    provider = MockProvider([ChatResponse(content="hello")])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    async def fail_wait_for(*args, **kwargs):
        raise AssertionError("streaming should not use timeout polling")

    monkeypatch.setattr(asyncio, "wait_for", fail_wait_for)

    async def collect() -> list[dagent.RunStreamEvent]:
        return [
            event
            async for event in runner.stream(
                dagent.ToolAgent(profile="conversation"),
                input="hi",
            )
        ]

    events = run(collect())

    assert events[-1].type == "run.finished"
    assert events[-1].data.result.output_text == "hello"


def test_runner_stream_failed_event_is_terminal(tmp_path) -> None:
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=MockProvider([]))
    events: list[dagent.RunStreamEvent] = []

    async def collect() -> None:
        async for event in runner.stream(dagent.ToolAgent(profile="conversation")):
            events.append(event)

    run(collect())

    assert events[-1].type == "run.failed"
    assert events[-1].data.message == "input is required for ToolAgent targets."
    assert events[-1].data.error_type == "TypeError"


def test_runner_auto_agent_routes_to_tool_result(tmp_path) -> None:
    provider = MockProvider([
        ChatResponse(content="tool"),
        ChatResponse(content="hello from tool"),
    ])
    agent = dagent.AutoAgent(capabilities=[], skills=[])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    result = run(runner.run(agent, input="hi"))

    assert result.kind == "tool"
    assert result.output_text == "hello from tool"


def test_runner_auto_agent_tool_mode_can_delegate_to_registered_agent(tmp_path) -> None:
    provider = MockProvider([
        ChatResponse(content="tool"),
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="agent_helper",
                    arguments={"prompt": "summarize this"},
                )
            ]
        ),
        ChatResponse(content="helper answer"),
        ChatResponse(content="done"),
    ])
    helper = dagent.ToolAgent(profile="conversation", name="helper", max_steps=1, capabilities=[], skills=[])
    agent = dagent.AutoAgent(capabilities=[], skills=[], agents=[helper])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    result = run(runner.run(agent, input="delegate"))

    assert result.kind == "tool"
    assert result.output_text == "done"
    assert {tool["function"]["name"] for tool in provider.requests[1]["tools"]} == {"agent_helper"}
    assert provider.requests[2]["tools"] == []


def test_runner_auto_agent_routes_to_dynamic_dag_result(tmp_path) -> None:
    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    provider = MockProvider([
        ChatResponse(content="dag"),
        ChatResponse(content=capability_plan_response(
            "tool.search", {"q": "X"}, node_id="lookup"
        )),
        ChatResponse(content=final_answer_response("Report: found:X")),
    ])
    agent = dagent.AutoAgent(capabilities=[search], skills=[])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    result = run(runner.run(agent, input="research X"))

    assert result.kind == "dynamic_dag"
    assert result.output_text == "Report: found:X"
    assert result.dag is not None
    assert result.dag.nodes[-1].payload.invocation.capability_id == "tool.search"


def test_runner_dynamic_dag_follow_up_review_advances_conversation_revision(
    tmp_path,
) -> None:
    @dagent.tool
    def fail_tool(text: str) -> str:
        raise RuntimeError(f"failed:{text}")

    @dagent.tool
    def echo(text: str) -> str:
        return f"echo:{text}"

    provider = MockProvider([
        ChatResponse(content="dag"),
        ChatResponse(content=capability_plan_response(
            "tool.fail_tool", {"text": "boom"}, node_id="bad"
        )),
        ChatResponse(content=capability_plan_response(
            "tool.echo", {"text": "recovered"}, node_id="answer"
        )),
    ])
    agent = dagent.AutoAgent(
        capabilities=[fail_tool, echo],
        skills=[],
        review="careful",
    )
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
    )

    initial = run(runner.run(agent, input="repair through a reviewed DAG"))

    assert initial.requires_review
    assert initial.review is not None
    assert initial.pending_review is not None
    assert initial.pending_review.kind == "initial_dag"
    assert initial.conversation is not None
    assert initial.checkpoint is not None

    follow_up = run(
        runner.resume(
            initial.review.approve(),
            checkpoint=initial.checkpoint,
        )
    )

    assert follow_up is not None
    assert follow_up.requires_review
    assert follow_up.pending_review is not None
    assert follow_up.pending_review.kind == "dag_replan"
    assert follow_up.conversation is not None
    assert follow_up.checkpoint is not None
    assert follow_up.conversation.revision > initial.conversation.revision
    assert follow_up.conversation.items == initial.conversation.items
    assert (
        follow_up.conversation
        == follow_up.state.conversation
        == follow_up.checkpoint.state.conversation
    )


def test_runner_dag_agent_can_plan_registered_agent_node(tmp_path) -> None:
    provider = MockProvider([
        ChatResponse(content=capability_plan_response(
            "agent.helper", {"prompt": "summarize this"}, node_id="ask_helper"
        )),
        ChatResponse(content="helper answer"),
        ChatResponse(content=final_answer_response("final answer")),
    ])
    helper = dagent.ToolAgent(profile="conversation", name="helper", max_steps=1, capabilities=[], skills=[])
    agent = dagent.DagAgent(capabilities=[], skills=[], agents=[helper])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    result = run(runner.run(agent, input="delegate"))

    assert result.kind == "dynamic_dag"
    assert result.output_text == "final answer"
    assert result.dag is not None
    assert result.dag.nodes[-1].payload.invocation.capability_id == "agent.helper"


def test_runner_dag_agent_dynamic_adjust_false_keeps_initial_dag_fixed(tmp_path) -> None:
    @dagent.tool
    def fail_tool(text: str) -> str:
        raise RuntimeError(f"failed:{text}")

    @dagent.tool
    def echo(text: str) -> str:
        return f"echo:{text}"

    _profile_root(tmp_path, "planner")
    provider = MockProvider([
        ChatResponse(content=capability_plan_response(
            "tool.fail_tool", {"text": "boom"}, node_id="bad"
        )),
        ChatResponse(content=capability_plan_response(
            "tool.echo", {"text": "ok"}, node_id="answer"
        )),
        ChatResponse(content=final_answer_response("Recovered after replanning.")),
    ])
    agent = dagent.DagAgent(
        planner_profile="planner",
        capabilities=[fail_tool, echo],
        dynamic_adjust=False,
    )
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        profile_root=tmp_path / "profiles",
    )

    result = run(runner.run(agent, input="repair through DAG"))

    assert result.kind == "dynamic_dag"
    assert result.state.dynamic_adjust is False
    assert result.state.status == "failed"
    assert result.dag is not None
    assert result.dag.version == 1
    assert result.dag.status == "failed"
    assert len(provider.requests) == 1


def test_runner_resume_stream_continues_pending_review(tmp_path) -> None:
    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        return f"wrote:{text}"

    _profile_root(tmp_path)
    provider = MockProvider([
        ChatResponse(
            content="",
            tool_calls=[ToolCall(id="call_1", name="tool_write", arguments={"text": "hello"})],
        ),
        ChatResponse(content="<think>checking</think>done"),
    ])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[write], review="careful")
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    first = run(runner.run(agent, input="write hello"))
    assert first.requires_review
    assert first.review is not None
    assert first.checkpoint is not None

    async def collect() -> list[dagent.RunStreamEvent]:
        return [
            event
            async for event in runner.resume_stream(
                first.review.approve(),
                checkpoint=first.checkpoint,
            )
        ]

    events = run(collect())

    assert events[0].type == "run.started"
    assert events[0].run_id == first.run_id
    completed = [
        event for event in events
        if event.type == "capability.call.completed"
        and event.data.invocation_id == "call_1"
    ]
    assert len(completed) == 1
    assert completed[0].data.content == "wrote:hello"
    assert completed[0].data.run_id == first.run_id
    assert [
        event.data.delta for event in events if event.type == "response.content.delta"
    ] == ["done"]
    assert events[-1].type == "run.finished"
    assert events[-1].data.result.output_text == "done"
    assert events[-1].run_id == first.run_id


def test_runner_stream_yields_typed_review_event(tmp_path) -> None:
    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        return f"wrote:{text}"

    _profile_root(tmp_path)
    provider = MockProvider([
        ChatResponse(
            content="",
            tool_calls=[ToolCall(id="call_1", name="tool_write", arguments={"text": "hello"})],
        ),
    ])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[write], review="careful")
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    async def collect() -> list[dagent.RunStreamEvent]:
        return [event async for event in runner.stream(agent, input="write hello")]

    events = run(collect())

    review_events = [event for event in events if event.type == "review.required"]
    assert review_events
    assert review_events[-1].data.kind == "capability_review"
    assert review_events[-1].data.message == "Review capability call: tool.write"
    assert events[-1].type == "run.finished"
    result = events[-1].data.result
    assert result.requires_review
    assert result.review is not None
    assert result.review.kind == "capability_review"
    dumped = result.model_dump(mode="json")
    pending_review = dumped["state"]["pending_review"]
    assert pending_review["kind"] == "capability_review"
    assert pending_review["review_id"] == review_events[-1].data.review_id
    assert pending_review["capability_call"] == {
        "invocation_id": "call_1",
        "capability_id": "tool.write",
        "tool_name": "tool_write",
        "arguments": {"text": "hello"},
    }
    assert pending_review["payload"] == {"capability_id": "tool.write", "risk": "medium"}


def test_run_result_public_surface_uses_single_names(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="hello")])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    result = run(runner.run(dagent.ToolAgent(profile="conversation"), input="hi"))

    assert result.output_text == "hello"
    assert result.run_id is not None
    assert result.state is not None
    assert result.requires_review is False
    for legacy_name in ("final_answer", "output", "task_id", "awaiting_review", "raw"):
        assert not hasattr(result, legacy_name)
    assert not hasattr(dagent.RunResult, "from_dag_run")
    assert hasattr(dagent.Runner, "run_trace")
    assert not hasattr(dagent.Runner, "task_trace")


def test_runtime_stream_adapter_does_not_convert_task_id_to_run_id(tmp_path) -> None:
    from dagent.runner import _stream_event_from_runtime

    started = _stream_event_from_runtime({
        "type": "capability_call",
        "invocation_id": "call_1",
        "capability_id": "tool.echo",
        "arguments": {"text": "ok"},
        "task_id": "run_1",
        "dag_id": "dag_1",
        "node_id": "answer",
    })

    assert started.data.run_id is None
    assert not hasattr(started.data, "task_id")


def test_tool_agent_capability_stream_events_include_run_id(tmp_path) -> None:
    @dagent.tool
    def echo(text: str) -> str:
        return f"echo:{text}"

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_1", name="tool_echo", arguments={"text": "ok"})]),
        ChatResponse(content="done"),
    ])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    async def collect() -> list[dagent.RunStreamEvent]:
        return [
            event
            async for event in runner.stream(
                dagent.ToolAgent(profile="conversation", capabilities=[echo]),
                input="echo ok",
            )
        ]

    events = run(collect())
    result = events[-1].data.result
    capability_events = [event for event in events if event.type.startswith("capability.call.")]

    assert capability_events
    assert all(event.data.run_id == result.run_id for event in capability_events)
    assert all(event.run_id == result.run_id for event in capability_events)


def test_unknown_runtime_stream_event_fails_fast() -> None:
    from dagent.runner import _stream_event_from_runtime

    with pytest.raises(ValueError, match="unsupported stream event type"):
        _stream_event_from_runtime({"type": "legacy.status", "message": "old"})


def test_capability_error_stream_event_does_not_accept_message_alias() -> None:
    from dagent.runner import _stream_event_from_runtime

    event = _stream_event_from_runtime({
        "type": "capability_error",
        "invocation_id": "call_1",
        "capability_id": "tool.echo",
        "message": "old fallback",
    })

    assert event.data.content == ""


def test_dag_agent_does_not_accept_profile_and_runner_runs_dag_loop(tmp_path) -> None:
    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    with pytest.raises(TypeError):
        dagent.DagAgent(profile="conversation")

    provider = MockProvider([
        ChatResponse(content=capability_plan_response(
            "tool.search", {"q": "X"}, node_id="lookup"
        )),
        ChatResponse(content=final_answer_response("Report: found:X")),
    ])
    agent = dagent.DagAgent(
        capabilities=["tool.search"],
    )
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        capabilities=[search],
    )

    result = run(runner.run(agent, input="research X"))

    assert result.output_text == "Report: found:X"
    assert result.dag is not None
    assert result.dag.nodes[-1].payload.invocation.capability_id == "tool.search"


def test_runner_auto_registers_agent_capability_bindings(tmp_path) -> None:
    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    _profile_root(tmp_path)
    provider = MockProvider([ChatResponse(content="hello")])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=[search],
    )
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    result = run(runner.run(agent, input="hi"))

    assert result.output_text == "hello"
    assert runner.runtime.capability_catalog.get("tool.search") is not None


def test_runner_rejects_unknown_agent_capability_id(tmp_path) -> None:
    _profile_root(tmp_path)
    provider = MockProvider([ChatResponse(content="unused")])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=["tool.missing"],
    )
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    with pytest.raises(KeyError, match="tool.missing"):
        run(runner.run(agent, input="hi"))


def test_runner_limits_agent_visible_capabilities(tmp_path) -> None:
    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    @dagent.tool
    def write(text: str) -> str:
        return f"wrote:{text}"

    _profile_root(tmp_path)
    provider = MockProvider([ChatResponse(content="hello")])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=["tool.search"],
    )
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        capabilities=[search, write],
        profile_root=tmp_path / "profiles",
        skill_roots=[],
    )

    run(runner.run(agent, input="hi"))

    system_message = provider.requests[0]["messages"][0]["content"]
    assert "search" not in system_message
    assert "write" not in system_message
    assert _tool_names(provider.requests[0]) == {"tool_search"}


def test_runner_agent_skills_filter_skill_tools_without_prompt_injection(tmp_path) -> None:
    skill_root = tmp_path / "skills"
    for category, name in (("writing", "brief"), ("research", "market")):
        skill_dir = skill_root / category / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} skill.\n---\nUse {name}.",
            encoding="utf-8",
        )
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_1", name="skill_list", arguments={})]),
        ChatResponse(content="done"),
    ])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[], skills=["writing/brief"])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, skill_roots=[skill_root])

    result = run(runner.run(agent, input="list skills"))

    assert result.output_text == "done"
    assert [tool["function"]["name"] for tool in provider.requests[0]["tools"]] == [
        "skill_list",
        "skill_view",
    ]
    system_message = provider.requests[0]["messages"][0]["content"]
    assert "Use brief." not in system_message
    tool_content = provider.requests[1]["messages"][-1]["content"]
    assert "brief" in tool_content
    assert "market" not in tool_content


def test_runner_agent_empty_skills_disables_skill_tools(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="done")])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[], skills=[])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    run(runner.run(agent, input="no tools"))

    assert provider.requests[0]["tools"] == []


def test_runner_default_agent_capabilities_exclude_registered_agent_capabilities(tmp_path) -> None:
    _profile_root(tmp_path, "writer")
    _profile_root(tmp_path, "conversation")
    provider = MockProvider([
        ChatResponse(content="drafted"),
        ChatResponse(content="hello"),
    ])
    writer = dagent.ToolAgent(profile="writer")
    dag = dagent.Dag("agent_flow")
    dag.add_node(dagent.Node("draft", target=writer, inputs={"prompt": "Draft the report."}))
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    run(runner.run(dag, workspace_root=tmp_path / "runs"))
    result = run(runner.run(dagent.ToolAgent(profile="conversation"), input="hi"))

    assert result.output_text == "hello"
    assert "writer" not in _tool_names(provider.requests[-1])


def test_runner_resume_continues_pending_tool_agent_runtime(tmp_path) -> None:
    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        return f"wrote:{text}"

    _profile_root(tmp_path)
    provider = MockProvider([
        ChatResponse(
            content="",
            tool_calls=[ToolCall(id="call_1", name="tool_write", arguments={"text": "hello"})],
        ),
        ChatResponse(content="done"),
    ])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[write], review="careful")
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    first = run(runner.run(agent, input="write hello"))
    assert first.requires_review
    assert first.review is not None

    assert first.checkpoint is not None
    resumed = run(
        runner.resume(first.review.approve(), checkpoint=first.checkpoint)
    )

    assert resumed is not None
    assert resumed.output_text == "done"


def test_runner_resume_can_restore_pending_capability_gate_from_checkpoint(tmp_path) -> None:
    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        return f"wrote:{text}"

    provider = MockProvider([
        ChatResponse(
            content="",
            tool_calls=[ToolCall(id="call_1", name="tool_write", arguments={"text": "hello"})],
        ),
        ChatResponse(content="done"),
    ])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[write], review="careful")
    first_runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, capabilities=[write])

    first = run(first_runner.run(agent, input="write hello"))
    assert first.checkpoint is not None
    saved_checkpoint = dagent.RunCheckpoint.model_validate(
        first.checkpoint.model_dump(mode="json")
    )
    first_runner.close()

    second_runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, capabilities=[write])
    resumed = run(
        second_runner.resume(
            first.review.approve(),
            checkpoint=saved_checkpoint,
        )
    )

    assert resumed is not None
    assert resumed.output_text == "done"
    assert resumed.conversation is not None
    assert [item.type for item in resumed.conversation.items] == [
        "user",
        "assistant",
        "tool_result",
        "assistant",
    ]
    assert resumed.conversation.items[1].tool_calls[0].id == "call_1"
    assert resumed.conversation.items[2].call_id == "call_1"
    assert resumed.conversation.items[2].content.text == "wrote:hello"
    assert resumed.conversation.items[-1].content == "done"


def test_runner_run_does_not_accept_runtime_state(tmp_path) -> None:
    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        return f"wrote:{text}"

    provider = MockProvider([
        ChatResponse(
            content="",
            tool_calls=[ToolCall(id="call_1", name="tool_write", arguments={"text": "hello"})],
        ),
    ])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[write], review="careful")
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, capabilities=[write])

    assert "state" not in inspect.signature(runner.run).parameters
    with pytest.raises(TypeError, match="unexpected keyword argument 'state'"):
        run(runner.run(agent, input="continue", state=None))


def test_runner_resume_can_restore_pending_dag_review_from_checkpoint(tmp_path) -> None:
    _profile_root(tmp_path, "planner")
    provider = MockProvider([
        ChatResponse(content=capability_plan_response(
            "tool.search", {"q": "X"}, node_id="lookup"
        )),
        ChatResponse(content=final_answer_response("Report: found:X")),
    ])

    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    agent = dagent.DagAgent(planner_profile="planner", capabilities=[search], review="careful")
    first_runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    first = run(first_runner.run(agent, input="research X"))
    assert first.requires_review
    assert first.review is not None
    assert first.checkpoint is not None
    saved_checkpoint = dagent.RunCheckpoint.model_validate(
        first.checkpoint.model_dump(mode="json")
    )
    first_runner.close()

    second_runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, capabilities=[search])
    decision = dagent.ReviewDecision(review_id=first.review.review_id, approved=True)
    resumed = run(second_runner.resume(decision, checkpoint=saved_checkpoint))

    assert resumed is not None
    assert resumed.output_text == "Report: found:X"


def test_runner_resume_rejects_mismatched_serialized_review_state(tmp_path) -> None:
    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        return f"wrote:{text}"

    provider = MockProvider([
        ChatResponse(
            content="",
            tool_calls=[ToolCall(id="call_1", name="tool_write", arguments={"text": "hello"})],
        ),
    ])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[write], review="careful")
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, capabilities=[write])

    first = run(runner.run(agent, input="write hello"))
    decision = dagent.ReviewDecision(review_id="review_other", approved=True)

    with pytest.raises(ValueError, match="does not match decision"):
        assert first.checkpoint is not None
        run(runner.resume(decision, checkpoint=first.checkpoint))


def test_runner_run_continues_from_serialized_conversation(tmp_path) -> None:
    provider = MockProvider([
        ChatResponse(content="The project color is blue."),
        ChatResponse(content="It is blue."),
    ])
    agent = dagent.ToolAgent(profile="conversation")
    first_runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    first = run(
        first_runner.run(
            agent,
            input="Remember that the project color is blue.",
        )
    )
    assert first.conversation is not None
    saved_conversation = dagent.ConversationState.model_validate(
        first.conversation.model_dump(mode="json")
    )
    first_runner.close()

    assert [item.type for item in first.new_items] == ["user", "assistant"]
    assert first.new_items[-1].content == "The project color is blue."

    second_runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)
    second = run(
        second_runner.run(
            agent,
            input="What color did I mention?",
            conversation=saved_conversation,
        )
    )
    second_runner.close()

    assert second.output_text == "It is blue."
    assert [item.type for item in second.new_items] == ["user", "assistant"]
    assert second.new_items[-1].content == "It is blue."
    assert second.conversation is not None
    assert [item.type for item in second.conversation.items] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_runner_invalid_dag_resume_does_not_consume_review_state(tmp_path) -> None:
    _profile_root(tmp_path, "planner")
    provider = MockProvider([
        ChatResponse(content=capability_plan_response(
            "tool.search", {"q": "X"}, node_id="lookup"
        )),
        ChatResponse(content=final_answer_response("Report: found:X")),
    ])

    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    agent = dagent.DagAgent(planner_profile="planner", capabilities=[search], review="careful")
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    first = run(runner.run(agent, input="research X"))
    assert first.requires_review
    assert first.review is not None

    decision = dagent.ReviewDecision(review_id=first.review.review_id, approved=True)
    assert first.checkpoint is not None
    resumed = run(runner.resume(decision, checkpoint=first.checkpoint))

    assert resumed is not None
    assert resumed.output_text == "Report: found:X"


def test_runner_rejects_conflicting_capability_registration(tmp_path) -> None:
    def make_same_tool(output: str) -> dagent.CapabilityBinding:
        def same() -> str:
            return output

        return dagent.tool(same)

    first = make_same_tool("first")
    second = make_same_tool("second")

    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=MockProvider([]), capabilities=[first])

    with pytest.raises(ValueError, match="tool.same"):
        runner.add_tool(second)


def test_runner_close_shuts_down_capability_resources(tmp_path) -> None:
    closed: list[str] = []
    provider = MockProvider([])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)
    runner.runtime.capability_catalog.add_shutdown_hook(lambda: closed.append("closed"))

    runner.close()
    runner.close()

    assert closed == ["closed"]


def test_runner_with_injected_provider_allows_missing_config(tmp_path, monkeypatch) -> None:
    provider = MockProvider([])
    monkeypatch.setenv("DAGENT_CONFIG", str(tmp_path / "missing.yaml"))

    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    assert runner.runtime.provider is provider


def test_runner_with_injected_provider_ignores_invalid_config(tmp_path, monkeypatch) -> None:
    bad_config = tmp_path / "config.yaml"
    bad_config.write_text("provider: [", encoding="utf-8")
    monkeypatch.setenv("DAGENT_CONFIG", str(bad_config))

    provider = MockProvider([])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider)

    assert runner.runtime.provider is provider


def test_runner_requires_explicit_provider(tmp_path) -> None:
    with pytest.raises(ValueError, match="No provider configured"):
        dagent.Runner(runtime_directory=".runtime", workspace=tmp_path)


def test_runner_from_config_loads_provider_and_profile_root(tmp_path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "conversation.md").write_text("# Config Conversation\n\nFrom config.", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join([
            "provider:",
            "  base_url: https://example.test/v1",
            "  model: cfg-model",
            "  api_key: test-key",
            "profiles:",
            "  directory: profiles",
        ]),
        encoding="utf-8",
    )

    runner = dagent.Runner.from_config(config, runtime_directory=".runtime", workspace=tmp_path)

    assert runner.profile_root == profiles
    assert runner.runtime.provider.config.model == "cfg-model"
    assert runner.runtime.tool_agent.profile.content.startswith("# Config Conversation")


def test_runner_from_config_uses_builtin_profiles_without_profile_directory(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join([
            "provider:",
            "  base_url: https://example.test/v1",
            "  model: cfg-model",
            "  api_key: test-key",
        ]),
        encoding="utf-8",
    )

    runner = dagent.Runner.from_config(config, runtime_directory=".runtime", workspace=tmp_path)

    assert runner.runtime.tool_agent.profile.name == "conversation"


def test_runner_enable_validation_prepares_default_validator(tmp_path) -> None:
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=MockProvider([]))

    assert runner.enable_validation is False
    assert runner.runtime.validator is None

    runner.enable_validation = True

    assert runner.enable_validation is True
    assert runner.runtime.validator is not None


def _profile_root(tmp_path, name: str = "conversation"):
    profiles = tmp_path / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    profile = profiles / f"{name}.md"
    profile.write_text(f"# {name}\n\nYou are a {name} profile.", encoding="utf-8")
    return str(profile)


def _tool_names(request: dict) -> set[str]:
    return {
        tool["function"]["name"]
        for tool in request["tools"]
        if tool.get("type") == "function"
    }


def test_validation_retry_stream_event_preserves_machine_readable_issue_fields() -> None:
    from dagent.runner import _stream_event_from_runtime

    event = _stream_event_from_runtime({
        "type": "retry",
        "summary": "needs work",
        "reason": "fix tool",
        "issues": [
            {
                "message": "Capability is not registered.",
                "node_id": "search",
                "capability_id": "tool.missing",
                "code": "unknown_capability",
            }
        ],
    })

    issue = event.data.issues[0]
    assert issue.node_id == "search"
    assert issue.capability_id == "tool.missing"
    assert issue.code == "unknown_capability"
