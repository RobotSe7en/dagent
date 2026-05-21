import asyncio
from typing import Any

from dagent import DAgent, ReviewDecision, ReviewHandle, RunResult, Runner, capability
from dagent.schemas import DAG, DAGSpec, PendingReview, RuntimeResponse


def run(coro):
    return asyncio.run(coro)


class FakeRuntime:
    def __init__(self) -> None:
        self.run_response = RuntimeResponse(status="completed", final_answer="done", task_id="task_1")
        self.resume_response = RuntimeResponse(status="completed", final_answer="resumed", task_id="task_1")
        self.dag_run = object()
        self.registered: list[Any] = []
        self.replaced: list[Any] = []
        self.last_run: dict[str, Any] = {}
        self.last_resume: dict[str, Any] = {}
        self.last_spec: dict[str, Any] = {}

    async def handle_message(self, message: str, **kwargs: Any) -> RuntimeResponse:
        self.last_run = {"message": message, **kwargs}
        return self.run_response

    async def resume_review(self, review_id: str, **kwargs: Any) -> RuntimeResponse | None:
        self.last_resume = {"review_id": review_id, **kwargs}
        return self.resume_response

    async def run_dag_spec(self, spec: DAGSpec, **kwargs: Any) -> object:
        self.last_spec = {"spec": spec, **kwargs}
        return self.dag_run

    def register_capability(self, capability: Any) -> Any:
        self.registered.append(capability)
        return capability.definition

    def replace_capability(self, capability: Any) -> Any:
        self.replaced.append(capability)
        return capability.definition


def test_run_result_exposes_stable_result_and_review_facade() -> None:
    dag = DAG(dag_id="dag_1", task_id="task_1")
    pending = PendingReview(
        review_id="review_1",
        kind="initial_dag",
        message="Please review",
        proposed_dag=dag,
    )
    result = RunResult(
        RuntimeResponse(
            status="awaiting_review",
            final_answer="",
            dag=dag,
            task_id="task_1",
            pending_review=pending,
        )
    )

    assert result.requires_review is True
    assert result.awaiting_review is True
    assert result.output_text == ""
    assert result.output == ""
    assert isinstance(result.review, ReviewHandle)
    assert result.review.id == "review_1"
    assert result.review.dag is dag
    assert result.review.approve() == ReviewDecision(review_id="review_1", approved=True, dag=dag)
    assert result.review.reject(review_level="careful") == ReviewDecision(
        review_id="review_1",
        approved=False,
        review_level="careful",
    )


def test_agent_registers_capabilities_and_runner_delegates_to_runtime() -> None:
    runtime = FakeRuntime()
    agent = DAgent(runtime=runtime)

    @capability
    def echo(text: str) -> str:
        return text

    returned = agent.with_capabilities([echo])
    result = run(Runner.run(agent, "hello", mode="tool", review="careful"))

    assert returned is agent
    assert runtime.registered == [echo]
    assert isinstance(result, RunResult)
    assert result.output_text == "done"
    assert runtime.last_run["message"] == "hello"
    assert runtime.last_run["mode"] == "tool"
    assert runtime.last_run["review_level"] == "careful"


def test_runner_resumes_reviews_and_runs_dag_specs() -> None:
    runtime = FakeRuntime()
    agent = DAgent(runtime=runtime)
    dag = DAG(dag_id="dag_1", task_id="task_1")
    decision = ReviewDecision(review_id="review_1", approved=True, dag=dag, review_level="fast")
    spec = DAGSpec(id="spec_1", name="Spec")

    resumed = run(Runner.resume(agent, decision))
    dag_run = run(Runner.run_spec(agent, spec, workspace_root=".runs"))

    assert isinstance(resumed, RunResult)
    assert resumed.output_text == "resumed"
    assert runtime.last_resume == {
        "review_id": "review_1",
        "dag": dag,
        "approved": True,
        "review_level": "fast",
        "on_token": None,
        "on_event": None,
    }
    assert dag_run is runtime.dag_run
    assert runtime.last_spec == {
        "spec": spec,
        "workspace_root": ".runs",
        "artifact_uploads": None,
        "on_token": None,
        "on_event": None,
    }
