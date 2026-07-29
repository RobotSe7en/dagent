from __future__ import annotations

import asyncio
import hashlib
import inspect
import json

import pytest
from pydantic import ValidationError

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas.prompt import (
    MAX_PROMPT_EXTENSION_CONTENT_CHARS,
    MAX_PROMPT_EXTENSIONS_TOTAL_CONTENT_CHARS,
)
from tests.planner_helpers import (
    capability_plan_response,
    final_answer_response,
)


def run(coro):
    return asyncio.run(coro)


def _system_prompt(provider: MockProvider, request_index: int) -> str:
    return provider.requests[request_index]["messages"][0]["content"]


def _legacy_v4_checkpoint_payload(
    checkpoint: dagent.RunCheckpoint,
) -> dict:
    payload = checkpoint.model_dump(mode="json")
    payload["schema_version"] = 4
    plan = payload["plan"]
    plan["schema_version"] = 4
    plan.pop("prompt_extensions", None)
    plan["fingerprint"] = hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in plan.items()
                if key != "fingerprint"
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return payload


def test_prompt_extension_normalizes_identity_targets_and_content() -> None:
    extension = dagent.PromptExtension(
        id=" Host.Workspace_Policy ",
        content="\nUse the declared workspace.\n",
        targets=[
            "tool_agent",
            "dag_planner",
            "tool_agent",
        ],
    )

    assert extension.id == "host.workspace_policy"
    assert extension.content == "\nUse the declared workspace.\n"
    assert extension.targets == ("dag_planner", "tool_agent")
    assert dagent.PromptExtension(
        id="host.default",
        content="Default targets.",
    ).targets == (
        "dag_planner",
        "registered_agent",
        "tool_agent",
    )

    with pytest.raises(ValidationError, match="frozen"):
        extension.content = "changed"
    with pytest.raises(ValidationError, match="Extra inputs"):
        dagent.PromptExtension(
            id="host.extra",
            content="content",
            unexpected=True,
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"id": "host.empty", "content": "  "}, "must not be empty"),
        (
            {
                "id": "host.unknown",
                "content": "content",
                "targets": ["validator"],
            },
            "Unknown prompt extension targets",
        ),
        (
            {
                "id": "host.none",
                "content": "content",
                "targets": [],
            },
            "targets must not be empty",
        ),
        (
            {
                "id": "host.too_large",
                "content": "x" * (MAX_PROMPT_EXTENSION_CONTENT_CHARS + 1),
            },
            "exceeds the maximum size",
        ),
    ],
)
def test_prompt_extension_rejects_invalid_payloads(
    payload: dict,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        dagent.PromptExtension.model_validate(payload)


def test_runner_rejects_duplicate_and_oversized_extension_collections(
    tmp_path,
) -> None:
    duplicate_a = dagent.PromptExtension(
        id="HOST.DUPLICATE",
        content="first",
    )
    duplicate_b = dagent.PromptExtension(
        id="host.duplicate",
        content="second",
    )
    with pytest.raises(ValueError, match="ids must be unique"):
        dagent.Runner(
            workspace=tmp_path,
            runtime_directory=".runtime",
            provider=MockProvider([]),
            prompt_extensions=[duplicate_a, duplicate_b],
        )

    full_a = dagent.PromptExtension(
        id="host.full_a",
        content="a" * MAX_PROMPT_EXTENSION_CONTENT_CHARS,
    )
    full_b = dagent.PromptExtension(
        id="host.full_b",
        content="b" * MAX_PROMPT_EXTENSION_CONTENT_CHARS,
    )
    assert (
        len(full_a.content) + len(full_b.content)
        == MAX_PROMPT_EXTENSIONS_TOTAL_CONTENT_CHARS
    )
    overflow = dagent.PromptExtension(
        id="host.overflow",
        content="x",
    )
    with pytest.raises(ValueError, match="total maximum size"):
        dagent.Runner(
            workspace=tmp_path,
            runtime_directory=".runtime",
            provider=MockProvider([]),
            prompt_extensions=[full_a, full_b, overflow],
        )


def test_runner_default_keeps_prompt_and_public_api_behavior_unchanged(
    tmp_path,
) -> None:
    provider = MockProvider([ChatResponse(content="done")])
    runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=provider,
    )

    result = run(
        runner.run(
            dagent.ToolAgent(profile="conversation", capabilities=[]),
            input="hello",
        )
    )

    assert "prompt_extensions" in inspect.signature(dagent.Runner).parameters
    assert "prompt_extensions" in inspect.signature(
        dagent.Runner.from_config
    ).parameters
    assert "prompt_extensions" in inspect.signature(
        dagent.Runner.derive
    ).parameters
    assert runner.prompt_extensions == ()
    assert result.plan is not None
    assert result.plan.schema_version == 4
    assert result.plan.prompt_extensions == ()
    assert "prompt_extensions" not in result.plan.model_dump(mode="json")
    assert "## Host Prompt Extension:" not in _system_prompt(provider, 0)


def test_runner_from_config_and_derive_propagate_extensions(tmp_path) -> None:
    extension = dagent.PromptExtension(
        id="host.workspace",
        content="Use the host workspace policy.",
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                "provider:",
                "  base_url: https://example.test/v1",
                "  model: test-model",
                "  api_key: test-key",
            ]
        ),
        encoding="utf-8",
    )
    configured = dagent.Runner.from_config(
        config,
        workspace=tmp_path / "configured",
        runtime_directory=".runtime",
        prompt_extensions=[extension],
    )
    inherited = configured.derive(workspace=tmp_path / "inherited")
    cleared = configured.derive(
        workspace=tmp_path / "cleared",
        prompt_extensions=(),
    )

    assert configured.prompt_extensions == (extension,)
    assert configured.runtime.tool_agent.prompt_extensions == (extension,)
    assert configured.runtime.dag_agent.prompt_extensions == (extension,)
    assert inherited.prompt_extensions == (extension,)
    assert cleared.prompt_extensions == ()

    cleared.close()
    inherited.close()
    configured.close()


def test_tool_and_auto_tool_paths_receive_only_matching_extensions(
    tmp_path,
) -> None:
    extensions = [
        dagent.PromptExtension(
            id="host.z_tool",
            content="TOOL_ONLY_EXTENSION",
            targets=["tool_agent"],
        ),
        dagent.PromptExtension(
            id="host.a_shared",
            content="SHARED_EXTENSION",
        ),
        dagent.PromptExtension(
            id="host.dag",
            content="DAG_ONLY_EXTENSION",
            targets=["dag_planner"],
        ),
    ]
    tool_provider = MockProvider([ChatResponse(content="tool done")])
    tool_runner = dagent.Runner(
        workspace=tmp_path / "tool",
        runtime_directory=".runtime",
        provider=tool_provider,
        prompt_extensions=extensions,
    )

    tool_result = run(
        tool_runner.run(
            dagent.ToolAgent(profile="conversation", capabilities=[]),
            input="tool request",
        )
    )
    tool_system = _system_prompt(tool_provider, 0)

    assert "SHARED_EXTENSION" in tool_system
    assert "TOOL_ONLY_EXTENSION" in tool_system
    assert "DAG_ONLY_EXTENSION" not in tool_system
    assert tool_system.index("host.a_shared") < tool_system.index("host.z_tool")
    assert tool_provider.requests[0]["tools"] == []
    assert tool_result.plan is not None
    assert tool_result.plan.capability_ids == ()

    auto_provider = MockProvider(
        [
            ChatResponse(content="tool"),
            ChatResponse(content="auto done"),
        ]
    )
    auto_runner = dagent.Runner(
        workspace=tmp_path / "auto",
        runtime_directory=".runtime",
        provider=auto_provider,
        prompt_extensions=extensions,
    )
    auto_result = run(
        auto_runner.run(
            dagent.AutoAgent(capabilities=[], skills=[]),
            input="auto request",
        )
    )

    assert "SHARED_EXTENSION" not in _system_prompt(auto_provider, 0)
    assert "TOOL_ONLY_EXTENSION" not in _system_prompt(auto_provider, 0)
    assert "SHARED_EXTENSION" in _system_prompt(auto_provider, 1)
    assert "TOOL_ONLY_EXTENSION" in _system_prompt(auto_provider, 1)
    assert "DAG_ONLY_EXTENSION" not in _system_prompt(auto_provider, 1)
    assert auto_result.plan is not None
    assert auto_result.plan.capability_ids == ()


def test_dag_initial_plan_and_replan_use_extensions_in_fixed_order(
    tmp_path,
) -> None:
    @dagent.tool
    def fail_tool(text: str) -> str:
        raise RuntimeError(f"failed:{text}")

    @dagent.tool
    def echo(text: str) -> str:
        return f"echo:{text}"

    provider = MockProvider(
        [
            ChatResponse(
                content=capability_plan_response(
                    "tool.fail_tool",
                    {"text": "boom"},
                    node_id="bad",
                )
            ),
            ChatResponse(
                content=capability_plan_response(
                    "tool.echo",
                    {"text": "recovered"},
                    node_id="answer",
                )
            ),
            ChatResponse(
                content=final_answer_response("Recovered after replanning.")
            ),
        ]
    )
    runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=provider,
        prompt_extensions=[
            dagent.PromptExtension(
                id="host.dag_b",
                content="DAG_EXTENSION_B",
                targets=["dag_planner"],
            ),
            dagent.PromptExtension(
                id="host.dag_a",
                content="DAG_EXTENSION_A",
                targets=["dag_planner"],
            ),
            dagent.PromptExtension(
                id="host.tool",
                content="TOOL_EXTENSION",
                targets=["tool_agent"],
            ),
        ],
    )

    result = run(
        runner.run(
            dagent.DagAgent(capabilities=[fail_tool, echo]),
            input="repair through a DAG",
        )
    )

    assert result.output_text == "Recovered after replanning."
    for request_index in (0, 1):
        system = _system_prompt(provider, request_index)
        assert "DAG_EXTENSION_A" in system
        assert "DAG_EXTENSION_B" in system
        assert "TOOL_EXTENSION" not in system
        assert (
            system.index("## Runtime Context")
            < system.index("Host Prompt Extension: host.dag_a")
            < system.index("Host Prompt Extension: host.dag_b")
            < system.index("## Required Planner Response JSON Schema")
            < system.index("## Capability Catalog")
        )


def test_auto_dag_route_uses_dag_extensions_for_initial_plan_and_replan(
    tmp_path,
) -> None:
    @dagent.tool
    def fail_tool() -> str:
        raise RuntimeError("failed")

    @dagent.tool
    def recover() -> str:
        return "recovered"

    provider = MockProvider(
        [
            ChatResponse(content="dag"),
            ChatResponse(
                content=capability_plan_response(
                    "tool.fail_tool",
                    {},
                    node_id="bad",
                )
            ),
            ChatResponse(
                content=capability_plan_response(
                    "tool.recover",
                    {},
                    node_id="answer",
                )
            ),
            ChatResponse(content=final_answer_response("auto recovered")),
        ]
    )
    runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=provider,
        prompt_extensions=[
            dagent.PromptExtension(
                id="host.dag",
                content="AUTO_DAG_EXTENSION",
                targets=["dag_planner"],
            ),
            dagent.PromptExtension(
                id="host.tool",
                content="AUTO_TOOL_EXTENSION",
                targets=["tool_agent"],
            ),
        ],
    )

    result = run(
        runner.run(
            dagent.AutoAgent(capabilities=[fail_tool, recover]),
            input="route and recover",
        )
    )

    assert result.output_text == "auto recovered"
    assert "AUTO_DAG_EXTENSION" not in _system_prompt(provider, 0)
    for request_index in (1, 2):
        system = _system_prompt(provider, request_index)
        assert "AUTO_DAG_EXTENSION" in system
        assert "AUTO_TOOL_EXTENSION" not in system


def test_default_extensions_do_not_reach_validator(tmp_path) -> None:
    provider = MockProvider(
        [
            ChatResponse(content="answer"),
            ChatResponse(
                content=json.dumps(
                    {
                        "passed": True,
                        "issues": [],
                        "summary": "ok",
                    }
                )
            ),
        ]
    )
    runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=provider,
        validator="validator_agent",
        prompt_extensions=[
            dagent.PromptExtension(
                id="host.default",
                content="DEFAULT_EXTENSION",
            )
        ],
    )

    result = run(
        runner.run(
            dagent.ToolAgent(profile="conversation", capabilities=[]),
            input="answer",
        )
    )

    assert result.output_text == "answer"
    assert "DEFAULT_EXTENSION" in _system_prompt(provider, 0)
    assert "DEFAULT_EXTENSION" not in _system_prompt(provider, 1)


def test_registered_agent_receives_only_registered_agent_extensions(
    tmp_path,
) -> None:
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="agent_helper",
                        arguments={"prompt": "help"},
                    )
                ]
            ),
            ChatResponse(content="child answer"),
            ChatResponse(content="outer answer"),
        ]
    )
    helper = dagent.ToolAgent(
        profile=dagent.AgentProfile(
            name="helper",
            content="REGISTERED_AGENT_PROFILE",
        ),
        name="helper",
        max_steps=1,
        capabilities=[],
        skills=[],
    )
    runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=provider,
        prompt_extensions=[
            dagent.PromptExtension(
                id="host.tool",
                content="OUTER_TOOL_EXTENSION",
                targets=["tool_agent"],
            ),
            dagent.PromptExtension(
                id="host.registered",
                content="REGISTERED_AGENT_EXTENSION",
                targets=["registered_agent"],
            ),
            dagent.PromptExtension(
                id="host.dag",
                content="DAG_EXTENSION",
                targets=["dag_planner"],
            ),
        ],
    )

    result = run(
        runner.run(
            dagent.ToolAgent(
                profile="conversation",
                capabilities=[],
                agents=[helper],
            ),
            input="delegate",
        )
    )

    outer_system = _system_prompt(provider, 0)
    child_system = _system_prompt(provider, 1)
    assert result.output_text == "outer answer"
    assert "OUTER_TOOL_EXTENSION" in outer_system
    assert "REGISTERED_AGENT_EXTENSION" not in outer_system
    assert "REGISTERED_AGENT_EXTENSION" in child_system
    assert "OUTER_TOOL_EXTENSION" not in child_system
    assert "DAG_EXTENSION" not in child_system
    assert (
        child_system.index("REGISTERED_AGENT_PROFILE")
        < child_system.index("## Runtime Context")
        < child_system.index(
            "Host Prompt Extension: host.registered"
        )
        < child_system.index("## DAG Runtime Context")
    )


def test_review_checkpoint_freezes_extensions_and_plan_fingerprint(
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
                        arguments={"text": "hello"},
                    )
                ]
            ),
            ChatResponse(content="done"),
        ]
    )
    frozen_extension = dagent.PromptExtension(
        id="host.workspace",
        content="FROZEN_WORKSPACE_EXTENSION",
        targets=["tool_agent"],
    )
    first_runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=provider,
        prompt_extensions=[frozen_extension],
    )
    first = run(
        first_runner.run(
            dagent.ToolAgent(
                profile="conversation",
                capabilities=[write],
                review="careful",
            ),
            input="write",
        )
    )

    assert first.requires_review
    assert first.checkpoint is not None
    assert first.checkpoint.schema_version == 5
    assert first.checkpoint.plan.schema_version == 5
    assert first.checkpoint.plan.prompt_extensions == (frozen_extension,)
    serialized = first.checkpoint.model_dump_json()
    restored = dagent.RunCheckpoint.model_validate_json(serialized)
    assert restored.plan.fingerprint == first.checkpoint.plan.fingerprint
    plan_payload = restored.plan.model_dump(mode="json")
    changed_extension_plan = dagent.ResolvedRunPlan.model_validate(
        {
            **plan_payload,
            "prompt_extensions": [
                {
                    **plan_payload["prompt_extensions"][0],
                    "content": "CHANGED_FROZEN_WORKSPACE_EXTENSION",
                }
            ],
            "fingerprint": "",
        }
    )
    assert changed_extension_plan.fingerprint != restored.plan.fingerprint
    first_runner.close()

    second_runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=provider,
        capabilities=[write],
        prompt_extensions=[
            dagent.PromptExtension(
                id="host.workspace",
                content="CURRENT_RUNNER_EXTENSION",
                targets=["tool_agent"],
            )
        ],
    )
    resumed = run(
        second_runner.resume(
            first.review.approve(),  # type: ignore[union-attr]
            checkpoint=restored,
        )
    )

    assert resumed is not None
    assert resumed.plan is not None
    assert resumed.plan.prompt_extensions == (frozen_extension,)
    assert resumed.plan.fingerprint == restored.plan.fingerprint
    resumed_system = _system_prompt(provider, 1)
    assert "FROZEN_WORKSPACE_EXTENSION" in resumed_system
    assert "CURRENT_RUNNER_EXTENSION" not in resumed_system


def test_dag_replan_review_uses_checkpoint_extension_snapshot(
    tmp_path,
) -> None:
    @dagent.tool
    def fail_tool() -> str:
        raise RuntimeError("failed")

    @dagent.tool
    def recover() -> str:
        return "recovered"

    frozen_extension = dagent.PromptExtension(
        id="host.dag",
        content="FROZEN_DAG_EXTENSION",
        targets=["dag_planner"],
    )
    provider = MockProvider(
        [
            ChatResponse(
                content=capability_plan_response(
                    "tool.fail_tool",
                    {},
                    node_id="bad",
                )
            ),
            ChatResponse(
                content=capability_plan_response(
                    "tool.recover",
                    {},
                    node_id="answer",
                )
            ),
        ]
    )
    first_runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=provider,
        prompt_extensions=[frozen_extension],
    )
    first = run(
        first_runner.run(
            dagent.DagAgent(
                capabilities=[fail_tool, recover],
                review="careful",
            ),
            input="review then recover",
        )
    )
    assert first.requires_review
    assert first.conversation is not None
    assert first.checkpoint is not None
    checkpoint = dagent.RunCheckpoint.model_validate_json(
        first.checkpoint.model_dump_json()
    )
    first_revision = first.conversation.revision
    first_runner.close()

    second_runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=provider,
        capabilities=[fail_tool, recover],
        prompt_extensions=[
            dagent.PromptExtension(
                id="host.dag",
                content="CURRENT_DAG_EXTENSION",
                targets=["dag_planner"],
            )
        ],
    )
    replanned = run(
        second_runner.resume(
            first.review.approve(),  # type: ignore[union-attr]
            checkpoint=checkpoint,
        )
    )

    assert replanned is not None
    assert replanned.requires_review
    assert replanned.pending_review is not None
    assert replanned.pending_review.kind == "dag_replan"
    assert replanned.conversation is not None
    assert replanned.conversation.revision > first_revision
    assert replanned.checkpoint is not None
    assert replanned.plan is not None
    assert replanned.plan.prompt_extensions == (frozen_extension,)
    assert replanned.checkpoint.plan.fingerprint == checkpoint.plan.fingerprint
    assert replanned.checkpoint.state.conversation == replanned.conversation
    replan_system = _system_prompt(provider, 1)
    assert "FROZEN_DAG_EXTENSION" in replan_system
    assert "CURRENT_DAG_EXTENSION" not in replan_system


def test_registered_agent_resume_uses_checkpoint_extension_snapshot(
    tmp_path,
) -> None:
    frozen_extension = dagent.PromptExtension(
        id="host.registered",
        content="FROZEN_REGISTERED_EXTENSION",
        targets=["registered_agent"],
    )
    helper = dagent.ToolAgent(
        profile=dagent.AgentProfile(
            name="helper",
            content="HELPER_PROFILE",
        ),
        name="helper",
        max_steps=1,
        capabilities=[],
        skills=[],
    )
    provider = MockProvider(
        [
            ChatResponse(
                content=capability_plan_response(
                    "agent.helper",
                    {"prompt": "help"},
                    node_id="delegate",
                )
            ),
            ChatResponse(content="child answer"),
            ChatResponse(content=final_answer_response("done")),
        ]
    )
    first_runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=provider,
        prompt_extensions=[frozen_extension],
    )
    first = run(
        first_runner.run(
            dagent.DagAgent(
                capabilities=[],
                agents=[helper],
                review="careful",
            ),
            input="delegate through a reviewed DAG",
        )
    )
    assert first.requires_review
    assert first.checkpoint is not None
    checkpoint = dagent.RunCheckpoint.model_validate_json(
        first.checkpoint.model_dump_json()
    )
    first_runner.close()

    second_runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=provider,
        prompt_extensions=[
            dagent.PromptExtension(
                id="host.registered",
                content="CURRENT_REGISTERED_EXTENSION",
                targets=["registered_agent"],
            )
        ],
    )
    second_runner.add_agent(helper)
    resumed = run(
        second_runner.resume(
            first.review.approve(),  # type: ignore[union-attr]
            checkpoint=checkpoint,
        )
    )

    assert resumed is not None
    assert resumed.output_text == "done"
    assert resumed.plan is not None
    assert resumed.plan.prompt_extensions == (frozen_extension,)
    child_system = _system_prompt(provider, 1)
    assert "FROZEN_REGISTERED_EXTENSION" in child_system
    assert "CURRENT_REGISTERED_EXTENSION" not in child_system


def test_legacy_v4_checkpoint_resumes_with_empty_extension_semantics(
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
                        arguments={"text": "hello"},
                    )
                ]
            ),
            ChatResponse(content="done"),
        ]
    )
    first_runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=provider,
    )
    first = run(
        first_runner.run(
            dagent.ToolAgent(
                profile="conversation",
                capabilities=[write],
                review="careful",
            ),
            input="write",
        )
    )
    assert first.checkpoint is not None
    legacy_payload = _legacy_v4_checkpoint_payload(first.checkpoint)
    first_runner.close()

    legacy = dagent.RunCheckpoint.model_validate(legacy_payload)
    assert legacy.schema_version == 4
    assert legacy.plan.schema_version == 4
    assert legacy.plan.prompt_extensions == ()
    second_runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=provider,
        capabilities=[write],
        prompt_extensions=[
            dagent.PromptExtension(
                id="host.current",
                content="MUST_NOT_APPEAR",
                targets=["tool_agent"],
            )
        ],
    )
    resumed = run(
        second_runner.resume(
            first.review.approve(),  # type: ignore[union-attr]
            checkpoint=legacy,
        )
    )

    assert resumed is not None
    assert resumed.output_text == "done"
    assert resumed.plan is not None
    assert resumed.plan.schema_version == 4
    assert resumed.plan.prompt_extensions == ()
    assert "MUST_NOT_APPEAR" not in _system_prompt(provider, 1)

    legacy_payload["plan"]["prompt_extensions"] = [
        {
            "id": "host.invalid_v4",
            "content": "not allowed",
            "targets": ["tool_agent"],
        }
    ]
    legacy_payload["plan"]["fingerprint"] = ""
    with pytest.raises(ValidationError, match="V4 does not support"):
        dagent.RunCheckpoint.model_validate(legacy_payload)


def test_v3_conversation_continuation_keeps_default_empty_semantics(
    tmp_path,
) -> None:
    first_provider = MockProvider([ChatResponse(content="first")])
    first_runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=first_provider,
    )
    agent = dagent.ToolAgent(profile="conversation", capabilities=[])
    first = run(first_runner.run(agent, input="first"))
    assert first.conversation is not None
    conversation = dagent.ConversationState.model_validate_json(
        first.conversation.model_dump_json()
    )
    assert conversation.schema_version == 3
    first_runner.close()

    second_provider = MockProvider([ChatResponse(content="second")])
    second_runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=second_provider,
    )
    second = run(
        second_runner.run(
            agent,
            input="second",
            conversation=conversation,
        )
    )

    assert second.output_text == "second"
    assert second.plan is not None
    assert second.plan.prompt_extensions == ()
    assert "## Host Prompt Extension:" not in _system_prompt(
        second_provider,
        0,
    )
