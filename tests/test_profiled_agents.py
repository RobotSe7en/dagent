import asyncio
import json

from dagent.harness.dag_review import DAGReviewerAgent
from dagent.harness.feedback_learner import FeedbackLearnerAgent
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider
from dagent.schemas import DAG, DAGNode, TraceEvent


def run(coro):
    return asyncio.run(coro)


def profile(role: str) -> AgentProfile:
    return AgentProfile(
        name=role,
        role=role,
        system_prompt=f"{role} system",
        user_prompt="Request: {{ user_request }} DAG: {{ dag_json }} Feedback: {{ feedback }} Trace: {{ trace_json }}",
    )


def test_dag_reviewer_agent_parses_review_json() -> None:
    provider = MockProvider(
        [
            ChatResponse(
                content=json.dumps(
                    {
                        "approved": False,
                        "issues": [
                            {
                                "node_id": "n1",
                                "severity": "high",
                                "message": "Boundary too broad.",
                            }
                        ],
                        "suggested_changes": [{"op": "replace"}],
                    }
                )
            )
        ]
    )
    reviewer = DAGReviewerAgent(provider=provider, profile=profile("dag_reviewer"))
    dag = DAG(dag_id="dag_1", task_id="task_1", nodes=[DAGNode(id="n1", title="N1", goal="G")])

    result = run(reviewer.review(user_request="check", dag=dag))

    assert result.approved is False
    assert result.issues[0].node_id == "n1"
    assert result.issues[0].severity == "high"
    assert result.suggested_changes == [{"op": "replace"}]


def test_feedback_learner_agent_returns_notes() -> None:
    provider = MockProvider([ChatResponse(content="Prefer narrow allowed_paths.")])
    learner = FeedbackLearnerAgent(provider=provider, profile=profile("feedback_learner"))
    trace = TraceEvent(event_id="e1", event_type="dag_started", dag_id="dag_1")

    result = run(learner.learn(feedback="Too broad", trace_events=[trace]))

    assert result.notes == "Prefer narrow allowed_paths."
    assert "Too broad" in provider.requests[0]["messages"][1]["content"]

