import asyncio

import pytest
from pydantic import ValidationError

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


def run(coro):
    return asyncio.run(coro)


def user_messages(content: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": content}]


def tool_names(request: dict) -> set[str]:
    return {
        tool["function"]["name"]
        for tool in request["tools"]
        if tool.get("type") == "function"
    }


def test_runner_result_exposes_round_trippable_checkpoint(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="done")])
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )

    result = run(runner.run(
        dagent.ToolAgent(profile="conversation", capabilities=[]),
        messages=user_messages("hello"),
    ))

    checkpoint = result.checkpoint
    assert checkpoint is not None
    restored = dagent.RunCheckpoint.model_validate_json(
        checkpoint.model_dump_json()
    )
    assert restored == checkpoint
    assert restored.plan.runtime_kind == "tool"
    assert restored.plan.capability_ids == ()
    assert restored.plan.skill_ids == ()
    assert len(restored.plan.fingerprint) == 64
    assert restored.usage == dagent.ExecutionUsage(
        total_operations=1,
        model_turns=1,
        capability_calls=0,
    )

    with pytest.raises(ValidationError, match="frozen"):
        restored.plan.tool_profile.content = "tampered"

    tampered = checkpoint.model_dump(mode="json")
    tampered["plan"]["tool_profile"]["content"] = "tampered"
    with pytest.raises(ValidationError, match="fingerprint"):
        dagent.RunCheckpoint.model_validate(tampered)

    copied_plan = restored.plan.model_copy(update={"max_tool_steps": 99})
    with pytest.raises(ValidationError, match="fingerprint"):
        dagent.RunCheckpoint(
            state=restored.state,
            plan=copied_plan,
            usage=restored.usage,
        )


def test_checkpoint_rejects_pending_capability_outside_plan_scope(tmp_path) -> None:
    calls: list[str] = []

    @dagent.tool(risk="medium")
    def allowed(text: str) -> str:
        return text

    @dagent.tool(risk="medium")
    def outside(text: str) -> str:
        calls.append(text)
        return text

    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(id="call_1", name="tool_allowed", arguments={"text": "x"})
            ]
        )
    ])
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        capabilities=[outside],
        skill_roots=[],
    )
    first = run(runner.run(
        dagent.ToolAgent(
            profile="conversation",
            capabilities=[allowed],
            review="careful",
        ),
        messages=user_messages("run allowed"),
    ))
    assert first.checkpoint is not None

    payload = first.checkpoint.model_dump(mode="json")
    payload["state"]["pending_invocation"]["capability_id"] = "tool.outside"
    capability_call = payload["state"]["pending_review"]["capability_call"]
    capability_call["capability_id"] = "tool.outside"
    capability_call["tool_name"] = "tool_outside"

    with pytest.raises(ValidationError, match="outside the resolved scope"):
        dagent.RunCheckpoint.model_validate(payload)

    tampered_state = dagent.RunState.model_validate(payload["state"])
    with pytest.raises(KeyError, match="not enabled"):
        run(runner.runtime.resume_review(
            first.review.review_id,  # type: ignore[union-attr]
            run_state=tampered_state,
            approved=True,
        ))
    assert calls == []


def test_checkpoint_resume_rebuilds_target_runtime_and_exact_scope(tmp_path) -> None:
    calls: list[str] = []

    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        calls.append(text)
        return f"wrote:{text}"

    @dagent.tool
    def unrelated() -> str:
        return "not visible"

    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="tool_write",
                    arguments={"text": "hello"},
                )
            ]
        ),
        ChatResponse(content="done"),
    ])
    profile = dagent.AgentProfile(
        name="checkpoint_writer",
        content="CHECKPOINT_WRITER_PROFILE",
    )
    agent = dagent.ToolAgent(
        profile=profile,
        capabilities=[write],
        max_steps=3,
        review="careful",
    )
    first_runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )

    first = run(first_runner.run(agent, messages=user_messages("write hello")))
    assert first.checkpoint is not None
    serialized = first.checkpoint.model_dump_json()
    first_runner.close()

    second_runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        capabilities=[write, unrelated],
        skill_roots=[],
    )
    checkpoint = dagent.RunCheckpoint.model_validate_json(serialized)
    resumed = run(second_runner.resume(
        first.review.approve(),  # type: ignore[union-attr]
        checkpoint=checkpoint,
    ))

    assert resumed is not None
    assert resumed.output_text == "done"
    assert calls == ["hello"]
    assert "CHECKPOINT_WRITER_PROFILE" in provider.requests[-1]["messages"][0]["content"]
    assert tool_names(provider.requests[-1]) == {"tool_write"}
    assert resumed.plan is not None
    assert resumed.plan.max_tool_steps == 3
    assert resumed.plan.capability_ids == ("tool.write",)
    assert resumed.usage == dagent.ExecutionUsage(
        total_operations=3,
        model_turns=2,
        capability_calls=1,
    )


def test_checkpoint_resume_rejects_missing_capability(tmp_path) -> None:
    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        return text

    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(id="call_1", name="tool_write", arguments={"text": "x"})
            ]
        )
    ])
    first_runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )
    first = run(first_runner.run(
        dagent.ToolAgent(
            profile="conversation",
            capabilities=[write],
            review="careful",
        ),
        messages=user_messages("write"),
    ))
    assert first.checkpoint is not None

    second_runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )
    with pytest.raises(ValueError, match="not registered: tool.write"):
        run(second_runner.resume(
            first.review.approve(),  # type: ignore[union-attr]
            checkpoint=first.checkpoint,
        ))


def test_model_limit_is_reserved_before_provider_call(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="not reached")])
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )

    with pytest.raises(dagent.ExecutionLimitExceeded) as raised:
        run(runner.run(
            dagent.ToolAgent(profile="conversation", capabilities=[]),
            messages=user_messages("hello"),
            limits=dagent.ExecutionLimits(max_model_turns=0),
        ))

    assert raised.value.limit_name == "max_model_turns"
    assert provider.requests == []


def test_capability_limit_is_reserved_before_handler_call(tmp_path) -> None:
    calls: list[str] = []

    @dagent.tool
    def write(text: str) -> str:
        calls.append(text)
        return text

    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(id="call_1", name="tool_write", arguments={"text": "x"})
            ]
        )
    ])
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )

    with pytest.raises(dagent.ExecutionLimitExceeded) as raised:
        run(runner.run(
            dagent.ToolAgent(profile="conversation", capabilities=[write]),
            messages=user_messages("write"),
            limits=dagent.ExecutionLimits(max_capability_calls=0),
        ))

    assert raised.value.limit_name == "max_capability_calls"
    assert calls == []
    assert len(provider.requests) == 1


def test_checkpoint_resume_restores_usage_without_resetting_limit(tmp_path) -> None:
    calls: list[str] = []

    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        calls.append(text)
        return text

    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(id="call_1", name="tool_write", arguments={"text": "x"})
            ]
        ),
        ChatResponse(content="not reached"),
    ])
    first_runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )
    first = run(first_runner.run(
        dagent.ToolAgent(
            profile="conversation",
            capabilities=[write],
            review="careful",
        ),
        messages=user_messages("write"),
        limits=dagent.ExecutionLimits(max_total_operations=2),
    ))
    assert first.checkpoint is not None
    first_runner.close()

    second_runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        capabilities=[write],
        skill_roots=[],
    )
    with pytest.raises(dagent.ExecutionLimitExceeded) as raised:
        run(second_runner.resume(
            first.review.approve(),  # type: ignore[union-attr]
            checkpoint=first.checkpoint,
        ))

    assert raised.value.limit_name == "max_total_operations"
    assert raised.value.usage == dagent.ExecutionUsage(
        total_operations=2,
        model_turns=1,
        capability_calls=1,
    )
    assert raised.value.checkpoint is not None
    assert raised.value.checkpoint.state.status == "failed"
    assert raised.value.checkpoint.state.pending_review is None
    assert second_runner.run_checkpoint(first.run_id) == raised.value.checkpoint
    assert calls == ["x"]
    assert len(provider.requests) == 1

    with pytest.raises(ValueError, match="already been consumed"):
        run(second_runner.resume(
            first.review.approve(),  # type: ignore[union-attr]
            checkpoint=first.checkpoint,
        ))
    assert calls == ["x"]


def test_state_continuation_restores_cached_usage_and_limits(tmp_path) -> None:
    provider = MockProvider([
        ChatResponse(content="first"),
        ChatResponse(content="second"),
        ChatResponse(content="not reached"),
    ])
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )
    agent = dagent.ToolAgent(profile="conversation", capabilities=[])
    limits = dagent.ExecutionLimits(max_model_turns=2)

    first = run(runner.run(
        agent,
        messages=user_messages("first"),
        limits=limits,
    ))
    second = run(runner.run(
        agent,
        messages=user_messages("second"),
        state=first.state,
    ))

    assert second.usage == dagent.ExecutionUsage(
        total_operations=2,
        model_turns=2,
        capability_calls=0,
    )
    assert second.plan is not None
    assert second.plan.limits == limits

    with pytest.raises(dagent.ExecutionLimitExceeded):
        run(runner.run(
            agent,
            messages=user_messages("third"),
            state=second.state,
        ))
    assert len(provider.requests) == 2

    with pytest.raises(ValueError, match="state is stale"):
        run(runner.run(
            agent,
            messages=user_messages("stale"),
            state=first.state,
        ))

    with pytest.raises(ValueError, match="cannot replace or expand"):
        run(runner.run(
            agent,
            messages=user_messages("expanded"),
            state=second.state,
            limits=dagent.ExecutionLimits(max_model_turns=3),
        ))


def test_cross_process_run_continuation_restores_checkpoint_usage(tmp_path) -> None:
    first_runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([ChatResponse(content="first")]),
        skill_roots=[],
    )
    agent = dagent.ToolAgent(profile="conversation", capabilities=[])
    first = run(first_runner.run(
        agent,
        messages=user_messages("first"),
        limits=dagent.ExecutionLimits(max_model_turns=2),
    ))
    assert first.checkpoint is not None
    checkpoint = dagent.RunCheckpoint.model_validate_json(
        first.checkpoint.model_dump_json()
    )
    first_runner.close()

    second_runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([ChatResponse(content="second")]),
        skill_roots=[],
    )
    second = run(second_runner.run(
        agent,
        messages=user_messages("second"),
        checkpoint=checkpoint,
    ))

    assert second.usage.model_turns == 2
    assert second.plan is not None
    assert second.plan.limits.max_model_turns == 2


def test_parallel_dag_capability_reservations_are_atomic(tmp_path) -> None:
    calls: list[str] = []

    @dagent.tool
    def record(value: str) -> str:
        calls.append(value)
        return value

    @dagent.tool
    def seed() -> str:
        return "ready"

    dag = dagent.Dag("parallel_budget")
    start = dag.add_node(dagent.Node("seed", target=seed))
    first = dag.add_node(dagent.Node("first", target=record, inputs={"value": "first"}))
    second = dag.add_node(dagent.Node("second", target=record, inputs={"value": "second"}))
    dag.add_edge(start, first)
    dag.add_edge(start, second)
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([]),
        skill_roots=[],
    )

    with pytest.raises(dagent.ExecutionLimitExceeded):
        run(runner.run(
            dag,
            workspace_root=tmp_path / "runs",
            limits=dagent.ExecutionLimits(max_capability_calls=2),
        ))

    assert len(calls) == 1
