import asyncio
import json
from pathlib import Path

from dagent.harness_runtime import ValidatorAgent, FeedbackLearnerAgent
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider
from dagent.schemas import RunTrace, RunTraceNode


def run(coro):
    return asyncio.run(coro)


def profile(role: str) -> AgentProfile:
    return AgentProfile(
        name=role,
        content=f"{role} soul\n\n{role} agent",
    )


def test_validator_agent_parses_validation_json() -> None:
    provider = MockProvider(
        [
            ChatResponse(
                content=json.dumps(
                    {
                        "passed": False,
                        "issues": [
                            {
                                "node_id": "n1",
                                "message": "Boundary too broad.",
                            }
                        ],
                        "summary": "Result incomplete.",
                    }
                )
            )
        ]
    )
    validator = ValidatorAgent(provider=provider, profile=profile("validator_agent"))

    result = run(validator.validate(
        user_request="check",
        final_answer="some answer",
        execution_context="node n1 completed",
    ))

    assert result.passed is False
    assert result.issues[0].node_id == "n1"
    assert result.summary == "Result incomplete."


def test_validator_agent_omits_empty_execution_context_section() -> None:
    provider = MockProvider([
        ChatResponse(content='{"passed": true, "issues": [], "summary": "ok"}')
    ])
    validator = ValidatorAgent(provider=provider, profile=profile("validator_agent"))

    run(validator.validate(user_request="check", final_answer="some answer"))

    prompt = provider.requests[0]["messages"][1]["content"]
    assert "User request:\ncheck" in prompt
    assert "Execution context:" not in prompt
    assert "{%" not in prompt


def test_validator_agent_includes_execution_and_workspace_context(
    tmp_path: Path,
) -> None:
    provider = MockProvider([
        ChatResponse(content='{"passed": true, "issues": [], "summary": "ok"}')
    ])
    validator = ValidatorAgent(provider=provider, profile=profile("validator_agent"))

    run(validator.validate(
        user_request="check",
        final_answer="some answer",
        execution_context="node n1 completed",
        workspace_path=tmp_path,
    ))

    system_prompt = provider.requests[0]["messages"][0]["content"]
    prompt = provider.requests[0]["messages"][1]["content"]
    assert "## Runtime Context" in system_prompt
    assert f"- Workspace root: {tmp_path.resolve()}" in system_prompt
    assert "Execution context:\nnode n1 completed" in prompt
    assert "{%" not in prompt


def test_feedback_learner_agent_returns_notes_with_workspace_context(
    tmp_path: Path,
) -> None:
    provider = MockProvider([ChatResponse(content="Prefer narrow allowed_paths.")])
    learner = FeedbackLearnerAgent(provider=provider, profile=profile("feedback_learner"))
    trace = RunTrace(run_id="run_1", root=RunTraceNode.run(run_id="run_1", status="completed"))

    result = run(
        learner.learn(
            feedback="Too broad",
            trace=trace,
            workspace_path=tmp_path,
        )
    )

    assert result.notes == "Prefer narrow allowed_paths."
    assert f"- Workspace root: {tmp_path.resolve()}" in (
        provider.requests[0]["messages"][0]["content"]
    )
    assert "Too broad" in provider.requests[0]["messages"][1]["content"]
