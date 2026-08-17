import asyncio
from pathlib import Path

import pytest

import dagent
from dagent.harness_runtime.dag_builder import DAGValidationError
from dagent.providers import ChatResponse, MockProvider, ToolCall


def run(coro):
    return asyncio.run(coro)


def _agent_dag(agent: dagent.ToolAgent) -> dagent.Dag:
    dag = dagent.Dag("agent_review")
    dag.add_node(dagent.Node("assistant", target=agent, inputs={"prompt": "Do the task."}))
    return dag


def test_careful_static_agent_approval_resumes_and_preserves_graph_input(tmp_path) -> None:
    calls: list[str] = []

    @dagent.tool(risk="high")
    def publish(text: str) -> str:
        calls.append(text)
        return f"published:{text}"

    @dagent.tool
    def after_review(text: str) -> str:
        return f"after:{text}"

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_1", name="tool_publish", arguments={"text": "draft"})]),
        ChatResponse(content="complete"),
    ])
    agent = dagent.ToolAgent(name="helper", profile="conversation", capabilities=[publish])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, skill_roots=[])
    dag = dagent.Dag("agent_review", input=str)
    assistant = dagent.Node("assistant", target=agent, inputs={"prompt": dag.input})
    after = dagent.Node("after", target=after_review, inputs={"text": dag.input})
    dag.add_node(assistant)
    dag.add_node(after)
    dag.add_edge(assistant, after)

    first = run(runner.run(dag, graph_input="resume input", review="careful"))

    assert first.status == "awaiting_review"
    assert first.pending_review is not None
    assert first.pending_review.capability_call.capability_id == "tool.publish"
    assert first.state.static_agent_continuation is not None
    assert calls == []

    resumed = run(runner.resume(first.review.approve(), checkpoint=first.checkpoint))

    assert resumed is not None
    assert resumed.status == "completed"
    assert resumed.node_output("assistant") == "complete"
    assert resumed.node_output("after") == "after:resume input"
    assert calls == ["draft"]


def test_static_agent_rejection_continues_without_executing_tool(tmp_path) -> None:
    calls: list[str] = []

    @dagent.tool(risk="medium")
    def send(text: str) -> str:
        calls.append(text)
        return text

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_1", name="tool_send", arguments={"text": "x"})]),
        ChatResponse(content="continued after denial"),
    ])
    agent = dagent.ToolAgent(name="helper", profile="conversation", capabilities=[send])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, skill_roots=[])

    first = run(runner.run(_agent_dag(agent), review="careful"))
    resumed = run(runner.resume(first.review.reject(), checkpoint=first.checkpoint))

    assert resumed is not None
    assert resumed.status == "completed"
    assert resumed.node_output("assistant") == "continued after denial"
    assert calls == []


def test_static_agent_fast_boundary_review_allows_only_pending_invocation(tmp_path) -> None:
    provider = MockProvider([
        ChatResponse(tool_calls=[
            ToolCall(
                id="call_1",
                name="tool_write_file",
                arguments={"path": "other.txt", "content": "approved"},
            )
        ]),
        ChatResponse(content="written"),
    ])
    agent = dagent.ToolAgent(
        name="helper",
        profile="conversation",
        capabilities=["tool.write_file"],
    )
    dag = dagent.Dag("boundary_review")
    dag.add_node(dagent.Node(
        "assistant",
        target=agent,
        inputs={"prompt": "Write the file."},
        boundary=dagent.Boundary(allowed_paths=["allowed.txt"]),
    ))
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, skill_roots=[])

    first = run(runner.run(dag, review="fast"))

    assert first.status == "awaiting_review"
    assert first.pending_review.payload["reason"] == "boundary_violation"
    assert not (tmp_path / "other.txt").exists()

    resumed = run(runner.resume(first.review.approve(), checkpoint=first.checkpoint))

    assert resumed is not None
    assert resumed.status == "completed"
    assert (Path(resumed.workspace_path) / "other.txt").read_text() == "approved"


def test_static_agent_review_level_change_applies_to_later_tool_calls(tmp_path) -> None:
    calls: list[str] = []

    @dagent.tool(risk="high")
    def publish(text: str) -> str:
        calls.append(text)
        return text

    provider = MockProvider([
        ChatResponse(tool_calls=[
            ToolCall(
                id="call_1",
                name="tool_write_file",
                arguments={"path": "other.txt", "content": "approved"},
            )
        ]),
        ChatResponse(tool_calls=[
            ToolCall(
                id="call_2",
                name="tool_publish",
                arguments={"text": "draft"},
            )
        ]),
        ChatResponse(content="complete"),
    ])
    agent = dagent.ToolAgent(
        name="helper",
        profile="conversation",
        capabilities=["tool.write_file", publish],
    )
    dag = dagent.Dag("review_level_change")
    dag.add_node(dagent.Node(
        "assistant",
        target=agent,
        inputs={"prompt": "Write and publish."},
        boundary=dagent.Boundary(allowed_paths=["allowed.txt"]),
    ))
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, skill_roots=[])

    first = run(runner.run(dag, review="fast"))

    assert first.pending_review is not None
    assert first.pending_review.capability_call.capability_id == "tool.write_file"
    second = run(runner.resume(
        first.review.approve(review_level="careful"),
        checkpoint=first.checkpoint,
    ))

    assert second is not None
    assert second.status == "awaiting_review"
    assert second.pending_review is not None
    assert second.pending_review.capability_call.capability_id == "tool.publish"
    assert calls == []

    final = run(runner.resume(second.review.approve(), checkpoint=second.checkpoint))

    assert final is not None
    assert final.status == "completed"
    assert calls == ["draft"]


def test_static_agent_supports_multiple_review_cycles_and_new_runner_checkpoint(tmp_path) -> None:
    calls: list[str] = []

    @dagent.tool(risk="high")
    def publish(text: str) -> str:
        calls.append(text)
        return text

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_1", name="tool_publish", arguments={"text": "one"})]),
        ChatResponse(tool_calls=[ToolCall(id="call_2", name="tool_publish", arguments={"text": "two"})]),
        ChatResponse(content="all done"),
    ])
    agent = dagent.ToolAgent(name="helper", profile="conversation", capabilities=[publish])
    first_runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, skill_roots=[])
    first = run(first_runner.run(_agent_dag(agent), review="careful"))
    checkpoint = dagent.RunCheckpoint.model_validate_json(first.checkpoint.model_dump_json())

    second_runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, skill_roots=[])
    second_runner.add_agent(agent)
    second = run(second_runner.resume(first.review.approve(), checkpoint=checkpoint))

    assert second is not None
    assert second.status == "awaiting_review"
    assert calls == ["one"]
    final = run(second_runner.resume(second.review.approve(), checkpoint=second.checkpoint))
    assert final is not None
    assert final.status == "completed"
    assert calls == ["one", "two"]


def test_static_agent_checkpoint_rejects_changed_execution_configuration(tmp_path) -> None:
    @dagent.tool(risk="high")
    def publish(text: str) -> str:
        return text

    profile = dagent.AgentProfile(name="helper_profile", content="Original profile.")
    agent = dagent.ToolAgent(name="helper", profile=profile, capabilities=[publish])
    provider = MockProvider([
        ChatResponse(tool_calls=[
            ToolCall(
                id="call_1",
                name="tool_publish",
                arguments={"text": "draft"},
            )
        ]),
    ])
    first_runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, skill_roots=[])
    first = run(first_runner.run(_agent_dag(agent), review="careful"))
    checkpoint = dagent.RunCheckpoint.model_validate_json(first.checkpoint.model_dump_json())

    changed_agent = dagent.ToolAgent(
        name="helper",
        profile=dagent.AgentProfile(
            name="helper_profile",
            content="Changed profile.",
        ),
        capabilities=[publish],
    )
    second_runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, skill_roots=[])
    second_runner.add_agent(changed_agent)

    with pytest.raises(ValueError, match="Checkpoint capability definition changed: agent.helper"):
        run(second_runner.resume(first.review.approve(), checkpoint=checkpoint))


def test_static_agent_resume_limit_failure_clears_continuation(tmp_path) -> None:
    calls: list[str] = []

    @dagent.tool(risk="high")
    def publish(text: str) -> str:
        calls.append(text)
        return text

    provider = MockProvider([
        ChatResponse(tool_calls=[
            ToolCall(
                id="call_1",
                name="tool_publish",
                arguments={"text": "draft"},
            )
        ]),
        ChatResponse(content="not reached"),
    ])
    agent = dagent.ToolAgent(name="helper", profile="conversation", capabilities=[publish])
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=provider, skill_roots=[])
    first = run(runner.run(
        _agent_dag(agent),
        review="careful",
        limits=dagent.ExecutionLimits(max_total_operations=2),
    ))

    with pytest.raises(dagent.ExecutionLimitExceeded) as raised:
        run(runner.resume(first.review.approve(), checkpoint=first.checkpoint))

    assert raised.value.checkpoint is not None
    assert raised.value.checkpoint.state.status == "failed"
    assert raised.value.checkpoint.state.pending_review is None
    assert raised.value.checkpoint.state.static_agent_continuation is None
    assert runner.run_checkpoint(first.run_id) == raised.value.checkpoint
    assert calls == []


def test_static_high_risk_capability_stays_direct_and_nested_agent_shapes_are_rejected(tmp_path) -> None:
    calls: list[str] = []

    @dagent.tool(risk="high")
    def publish(text: str) -> str:
        calls.append(text)
        return text

    direct = dagent.Dag("direct")
    direct.add_node(dagent.Node("publish", target=publish, inputs={"text": "now"}))
    runner = dagent.Runner(runtime_directory=".runtime", workspace=tmp_path, provider=MockProvider([]), skill_roots=[])

    result = run(runner.run(direct, review="careful"))

    assert result.status == "completed"
    assert calls == ["now"]

    agent = dagent.ToolAgent(name="helper", profile="conversation", capabilities=[publish])
    unsupported = dagent.Dag("unsupported")
    unsupported.add_node(dagent.MapNode("fanout", target=agent, over=["x"], inputs={"prompt": "x"}))

    with pytest.raises(DAGValidationError, match="cannot target an agent capability"):
        run(runner.run(unsupported, review="careful"))
