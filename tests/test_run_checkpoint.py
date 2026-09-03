import asyncio
import inspect
import warnings

import pytest
from pydantic import ValidationError

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


def run(coro):
    return asyncio.run(coro)


def tool_names(request: dict) -> set[str]:
    return {
        tool["function"]["name"]
        for tool in request["tools"]
        if tool.get("type") == "function"
    }


def legacy_checkpoint(
    checkpoint: dagent.RunCheckpoint,
    *,
    schema_version: int,
    capability_fingerprints: dict[str, str] | None = None,
) -> dagent.RunCheckpoint:
    state_payload = checkpoint.state.model_dump(mode="json")
    if schema_version == 4:
        state_payload["schema_version"] = 3
        state_payload.pop("input_artifact_files", None)
    state = dagent.RunState.model_validate(state_payload)

    plan_payload = checkpoint.plan.model_dump(mode="json")
    plan_payload["schema_version"] = schema_version
    plan_payload["max_tool_steps"] = checkpoint.plan.max_steps or 888
    plan_payload["max_dag_cycles"] = checkpoint.plan.max_steps or 888
    plan_payload["limits"] = {
        "max_total_operations": 1,
        "max_model_turns": 1,
        "max_capability_calls": 1,
    }
    if capability_fingerprints is not None:
        plan_payload["capability_fingerprints"].update(capability_fingerprints)
    plan_payload.pop("max_steps", None)
    plan_payload["fingerprint"] = ""
    plan = dagent.ResolvedRunPlan.model_validate(plan_payload)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return dagent.RunCheckpoint(
            schema_version=schema_version,
            state=state,
            plan=plan,
            usage=checkpoint.usage,
        )


def test_runner_result_exposes_round_trippable_checkpoint(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="done")])
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )

    result = run(runner.run(
        dagent.ToolAgent(profile="conversation", capabilities=[]),
        input="hello",
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
    assert restored.schema_version == 6
    assert restored.plan.schema_version == 6
    assert restored.plan.max_steps == 888
    assert "max_tool_steps" not in restored.plan.model_dump(mode="json")
    assert "max_dag_cycles" not in restored.plan.model_dump(mode="json")
    assert "limits" not in restored.plan.model_dump(mode="json")
    assert restored.plan.runtime_directory == ".runtime"
    assert restored.plan.extra_system_prompt is None
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

    legacy = checkpoint.model_dump(mode="json")
    legacy["schema_version"] = 3
    legacy["plan"]["schema_version"] = 3
    with pytest.raises(ValidationError, match="Input should be 4, 5 or 6"):
        dagent.RunCheckpoint.model_validate(legacy)

    copied_plan = restored.plan.model_copy(update={"max_steps": 99})
    with pytest.raises(ValidationError, match="fingerprint"):
        dagent.RunCheckpoint(
            state=restored.state,
            plan=copied_plan,
            usage=restored.usage,
        )


@pytest.mark.parametrize("schema_version", [4, 5])
def test_runner_resumes_legacy_checkpoint_and_emits_v6_checkpoint(
    tmp_path,
    schema_version: int,
) -> None:
    calls: list[str] = []

    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        calls.append(text)
        return f"wrote:{text}"

    provider = MockProvider([
        ChatResponse(tool_calls=[
            ToolCall(id="call_1", name="tool_write", arguments={"text": "hello"})
        ]),
        ChatResponse(content="done"),
    ])
    first_runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )
    first = run(first_runner.run(
        dagent.ToolAgent(profile="conversation", capabilities=[write], review="careful"),
        input="write hello",
    ))
    assert first.checkpoint is not None

    legacy = legacy_checkpoint(first.checkpoint, schema_version=schema_version)
    serialized = legacy.model_dump_json()
    with pytest.warns(DeprecationWarning, match="execution limits.*ignored"):
        restored_legacy = dagent.RunCheckpoint.model_validate_json(serialized)

    assert restored_legacy.schema_version == schema_version
    assert restored_legacy.plan.schema_version == schema_version
    assert restored_legacy.state.schema_version == (3 if schema_version == 4 else 4)
    if schema_version == 4:
        assert "input_artifact_files" not in restored_legacy.model_dump(mode="json")["state"]

    second_runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        capabilities=[write],
        skill_roots=[],
    )
    with pytest.warns(DeprecationWarning, match="execution limits.*ignored"):
        resumed = run(second_runner.resume(
            first.review.approve(),  # type: ignore[union-attr]
            checkpoint=restored_legacy,
        ))

    assert resumed is not None
    assert resumed.output_text == "done"
    assert calls == ["hello"]
    assert resumed.state.schema_version == 4
    assert resumed.checkpoint is not None
    assert resumed.checkpoint.schema_version == 6
    assert resumed.checkpoint.plan.max_steps == 888


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
        runtime_directory=".runtime",
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
        input="run allowed",
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
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )

    first = run(first_runner.run(agent, input="write hello"))
    assert first.checkpoint is not None
    serialized = first.checkpoint.model_dump_json()
    first_runner.close()

    second_runner = dagent.Runner(
        runtime_directory=".runtime",
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
    assert resumed.plan.max_steps == 3
    assert resumed.plan.capability_ids == ("tool.write",)
    assert resumed.usage == dagent.ExecutionUsage(
        total_operations=3,
        model_turns=2,
        capability_calls=1,
    )


def test_checkpoint_resume_keeps_context_limits_across_multiple_review_gates(
    tmp_path,
) -> None:
    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        return f"wrote:{text}"

    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="tool_write",
                        arguments={"text": "first"},
                    )
                ]
            ),
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_2",
                        name="tool_write",
                        arguments={"text": "second"},
                    )
                ]
            ),
        ]
    )
    provider.context_window_tokens = 16384
    provider.output_reserve_tokens = 2048
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=[write],
        review="careful",
    )

    first = run(runner.run(agent, input="write twice"))
    assert first.requires_review
    assert first.checkpoint is not None
    assert first.checkpoint.plan.context_window_tokens == 16384
    assert first.checkpoint.plan.output_reserve_tokens == 2048
    assert first.checkpoint.plan.runtime_directory == ".runtime"

    provider.context_window_tokens = 4096
    provider.output_reserve_tokens = 512
    second = run(
        runner.resume(
            first.review.approve(),  # type: ignore[union-attr]
            checkpoint=first.checkpoint,
        )
    )

    assert second.requires_review
    assert second.checkpoint is not None
    assert second.checkpoint.plan.context_window_tokens == 16384
    assert second.checkpoint.plan.output_reserve_tokens == 2048
    assert second.checkpoint.plan.runtime_directory == ".runtime"
    runner.close()


def test_checkpoint_resume_rejects_missing_capability(tmp_path) -> None:
    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        return text

    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(id="call_1", name="tool_write", arguments={"text": "x"})
            ]
        ),
        ChatResponse(content="done"),
    ])
    first_runner = dagent.Runner(
        runtime_directory=".runtime",
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
        input="write",
    ))
    assert first.checkpoint is not None

    second_runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )
    with pytest.raises(ValueError, match="not registered: tool.write"):
        run(second_runner.resume(
            first.review.approve(),  # type: ignore[union-attr]
            checkpoint=first.checkpoint,
        ))


def test_checkpoint_resume_rejects_changed_capability_definition(tmp_path) -> None:
    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        return text

    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(id="call_1", name="tool_write", arguments={"text": "x"})
            ]
        ),
        ChatResponse(content="done"),
    ])
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        capabilities=[write],
        skill_roots=[],
    )
    first = run(
        runner.run(
            dagent.ToolAgent(
                profile="conversation",
                capabilities=[write],
                review="careful",
            ),
            input="write",
        )
    )
    assert first.checkpoint is not None
    runner.replace_capability(
        write.definition.model_copy(
            update={"description": "changed after checkpoint"}
        ),
        write.handler,
    )

    with pytest.raises(ValueError, match="definition changed"):
        run(
            runner.resume(
                first.review.approve(),  # type: ignore[union-attr]
                checkpoint=first.checkpoint,
            )
        )


def test_legacy_checkpoint_accepts_only_the_known_agent_schema_change(
    tmp_path,
) -> None:
    calls: list[str] = []

    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        calls.append(text)
        return text

    provider = MockProvider([
        ChatResponse(tool_calls=[
            ToolCall(id="call_1", name="tool_write", arguments={"text": "x"})
        ]),
        ChatResponse(content="done"),
    ])
    agent = dagent.ToolAgent(
        name="helper",
        profile="conversation",
        capabilities=[write],
        max_steps=2,
    )
    dag = dagent.Dag("legacy_agent_checkpoint")
    dag.add_node(dagent.Node("helper", target=agent, inputs={"prompt": "write"}))
    first_runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )
    first = run(first_runner.run(dag, review="careful"))
    assert first.checkpoint is not None
    legacy_agent_fingerprint = first_runner._legacy_agent_capability_fingerprint(
        "agent.helper"
    )
    legacy = legacy_checkpoint(
        first.checkpoint,
        schema_version=5,
        capability_fingerprints={"agent.helper": legacy_agent_fingerprint},
    )
    first_runner.close()

    second_runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )
    second_runner.add_agent(agent)
    with pytest.warns(DeprecationWarning, match="execution limits.*ignored"):
        resumed = run(second_runner.resume(
            first.review.approve(),  # type: ignore[union-attr]
            checkpoint=legacy,
        ))

    assert resumed is not None
    assert resumed.node_output("helper") == "done"
    assert resumed.checkpoint is not None
    assert resumed.checkpoint.schema_version == 6
    assert calls == ["x"]


def test_legacy_checkpoint_requires_the_old_child_agent_default_to_be_pinned(
    tmp_path,
) -> None:
    calls: list[str] = []

    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        calls.append(text)
        return text

    provider = MockProvider([
        ChatResponse(tool_calls=[
            ToolCall(id="call_1", name="tool_write", arguments={"text": "x"})
        ]),
        ChatResponse(content="done"),
    ])
    old_agent = dagent.ToolAgent(
        name="helper",
        profile="conversation",
        capabilities=[write],
        max_steps=8,
    )
    dag = dagent.Dag("legacy_default_agent_checkpoint")
    dag.add_node(dagent.Node("helper", target=old_agent, inputs={"prompt": "write"}))
    first_runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )
    first = run(first_runner.run(dag, review="careful"))
    assert first.checkpoint is not None
    legacy = legacy_checkpoint(
        first.checkpoint,
        schema_version=5,
        capability_fingerprints={
            "agent.helper": first_runner._legacy_agent_capability_fingerprint(
                "agent.helper"
            )
        },
    )
    first_runner.close()

    default_runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )
    default_runner.add_agent(dagent.ToolAgent(
        name="helper",
        profile="conversation",
        capabilities=[write],
    ))
    with pytest.warns(DeprecationWarning, match="execution limits.*ignored"):
        with pytest.raises(ValueError, match="Register.*max_steps=8"):
            run(default_runner.resume(
                first.review.approve(),  # type: ignore[union-attr]
                checkpoint=legacy,
            ))
    default_runner.close()

    pinned_runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )
    pinned_runner.add_agent(old_agent)
    with pytest.warns(DeprecationWarning, match="execution limits.*ignored"):
        resumed = run(pinned_runner.resume(
            first.review.approve(),  # type: ignore[union-attr]
            checkpoint=legacy,
        ))

    assert resumed is not None
    assert resumed.node_output("helper") == "done"
    assert resumed.checkpoint is not None
    assert resumed.checkpoint.schema_version == 6
    assert calls == ["x"]


def test_v6_static_plan_rejects_root_max_steps(tmp_path) -> None:
    @dagent.tool
    def echo(text: str) -> str:
        return text

    dag = dagent.Dag("static_checkpoint")
    dag.add_node(dagent.Node("echo", target=echo, inputs={"text": "ok"}))
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=MockProvider([]),
        skill_roots=[],
    )
    result = run(runner.run(dag))
    assert result.plan is not None
    payload = result.plan.model_dump(mode="json")
    assert "max_steps" not in payload

    payload["max_steps"] = 1
    payload["fingerprint"] = ""
    with pytest.raises(
        ValidationError,
        match="V6 static DAG plans cannot contain max_steps",
    ):
        dagent.ResolvedRunPlan.model_validate(payload)


def test_runner_execution_methods_do_not_expose_global_limits() -> None:
    assert "limits" not in inspect.signature(dagent.Runner.run).parameters
    assert "limits" not in inspect.signature(dagent.Runner.stream).parameters
    assert not hasattr(dagent, "ExecutionLimits")
    assert not hasattr(dagent, "ExecutionLimitExceeded")


def test_run_usage_counts_model_and_capability_calls(tmp_path) -> None:
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
        ),
        ChatResponse(content="done"),
    ])
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )

    result = run(runner.run(
        dagent.ToolAgent(profile="conversation", capabilities=[write]),
        input="write",
    ))

    assert calls == ["x"]
    assert len(provider.requests) == 2
    assert result.usage == dagent.ExecutionUsage(
        total_operations=3,
        model_turns=2,
        capability_calls=1,
    )


def test_checkpoint_resume_restores_and_accumulates_usage(tmp_path) -> None:
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
        ChatResponse(content="done"),
    ])
    first_runner = dagent.Runner(
        runtime_directory=".runtime",
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
        input="write",
    ))
    assert first.checkpoint is not None
    first_runner.close()

    second_runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        capabilities=[write],
        skill_roots=[],
    )
    resumed = run(second_runner.resume(
        first.review.approve(),  # type: ignore[union-attr]
        checkpoint=first.checkpoint,
    ))

    assert resumed is not None
    assert resumed.output_text == "done"
    assert resumed.usage == dagent.ExecutionUsage(
        total_operations=3,
        model_turns=2,
        capability_calls=1,
    )
    assert resumed.checkpoint is not None
    assert second_runner.run_checkpoint(first.run_id) == resumed.checkpoint
    assert calls == ["x"]
    assert len(provider.requests) == 2

    with pytest.raises(ValueError, match="already been consumed"):
        run(second_runner.resume(
            first.review.approve(),  # type: ignore[union-attr]
            checkpoint=first.checkpoint,
        ))
    assert calls == ["x"]


def test_conversation_continuation_starts_new_usage_counters(tmp_path) -> None:
    provider = MockProvider([
        ChatResponse(content="first"),
        ChatResponse(content="second"),
        ChatResponse(content="third"),
    ])
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        skill_roots=[],
    )
    agent = dagent.ToolAgent(profile="conversation", capabilities=[])
    first = run(runner.run(
        agent,
        input="first",
    ))
    second = run(runner.run(
        agent,
        input="second",
        conversation=first.conversation,
    ))

    assert second.usage == dagent.ExecutionUsage(
        total_operations=1,
        model_turns=1,
        capability_calls=0,
    )
    assert second.plan is not None
    assert second.plan.max_steps == 888
    assert first.run_id != second.run_id
    assert provider.requests[1]["messages"][-3:] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first"},
        {"role": "user", "content": "second"},
    ]


def test_cross_process_conversation_continuation_has_independent_usage(tmp_path) -> None:
    first_runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=MockProvider([ChatResponse(content="first")]),
        skill_roots=[],
    )
    agent = dagent.ToolAgent(profile="conversation", capabilities=[])
    first = run(first_runner.run(
        agent,
        input="first",
    ))
    assert first.conversation is not None
    conversation = dagent.ConversationState.model_validate_json(
        first.conversation.model_dump_json()
    )
    first_runner.close()

    second_runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=MockProvider([ChatResponse(content="second")]),
        skill_roots=[],
    )
    second = run(second_runner.run(
        agent,
        input="second",
        conversation=conversation,
    ))

    assert second.usage.model_turns == 1
    assert second.plan is not None
    assert second.plan.max_steps == 888


def test_parallel_dag_capability_usage_is_recorded_atomically(tmp_path) -> None:
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
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=MockProvider([]),
        skill_roots=[],
    )

    result = run(runner.run(
        dag,
        workspace_root=tmp_path / "runs",
    ))

    assert sorted(calls) == ["first", "second"]
    assert result.usage == dagent.ExecutionUsage(
        total_operations=3,
        model_turns=0,
        capability_calls=3,
    )
