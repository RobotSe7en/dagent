import asyncio
import json

from dagent.harness_runtime import ValidatorAgent, FeedbackLearnerAgent
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider
from dagent.schemas import DAG, DAGNode, TraceEvent


def run(coro):
    return asyncio.run(coro)


def profile(role: str) -> AgentProfile:
    return AgentProfile(
        name=role,
        role=role,
        layers=["soul.md", "agent.md"],
        layer_contents={
            "soul.md": f"{role} soul",
            "agent.md": f"{role} agent",
        },
        memory="profile memory",
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


def test_feedback_learner_agent_returns_notes() -> None:
    provider = MockProvider([ChatResponse(content="Prefer narrow allowed_paths.")])
    learner = FeedbackLearnerAgent(provider=provider, profile=profile("feedback_learner"))
    trace = TraceEvent(event_id="e1", event_type="dag_started", dag_id="dag_1")

    result = run(learner.learn(feedback="Too broad", trace_events=[trace]))

    assert result.notes == "Prefer narrow allowed_paths."
    assert "Too broad" in provider.requests[0]["messages"][1]["content"]
