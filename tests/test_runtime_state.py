import pytest
from pydantic import ValidationError

from dagent import RunResult
from dagent.harness_runtime.runtime_session import HarnessRuntimeSession
from dagent.schemas import DAG, PendingReview, RunState, RunTrace, RunTraceNode


def test_session_saves_run_state_directly() -> None:
    session = HarnessRuntimeSession()
    dag = DAG(dag_id="dag_1", task_id="task_1")
    trace = RunTrace(
        run_id="task_1",
        root=RunTraceNode.run(run_id="task_1", status="completed"),
    )
    trace.root.output = "done"
    state = RunState(
        run_id="task_1",
        kind="dynamic_dag",
        status="completed",
        dag=dag,
        trace=trace,
        user_request="do it",
        runtime_mode="dag",
    )

    saved = session.save_run_state(state)

    assert saved == state
    assert session.runs["task_1"] == state
    assert session.runs["task_1"].trace.root.output == "done"


def test_session_indexes_pending_review_from_run_state() -> None:
    session = HarnessRuntimeSession()
    review = PendingReview(
        review_id="review_1",
        kind="initial_dag",
        message="review",
    )
    state = RunState(
        run_id="task_1",
        kind="dynamic_dag",
        status="awaiting_review",
        pending_review=review,
    )

    session.save_run_state(state)

    assert session.get_review_state("review_1") == state
    cleared = state.model_copy(update={"status": "completed", "pending_review": None})
    session.save_run_state(cleared)
    assert session.get_review_state("review_1") is None


def test_session_replaces_trace_without_implicit_merge() -> None:
    session = HarnessRuntimeSession()
    old_trace = RunTrace(
        run_id="task_1",
        root=RunTraceNode.run(run_id="task_1", status="completed"),
    )
    old_trace.root.children.append(RunTraceNode.dag_node(parent_id=old_trace.root.id, node_id="old"))
    new_trace = RunTrace(
        run_id="task_1",
        root=RunTraceNode.run(run_id="task_1", status="completed"),
    )
    session.save_run_state(RunState(
        run_id="task_1",
        kind="dynamic_dag",
        status="completed",
        trace=old_trace,
    ))

    saved = session.save_run_state(RunState(
        run_id="task_1",
        kind="dynamic_dag",
        status="completed",
        trace=new_trace,
    ))

    assert saved.trace is not None
    assert saved.trace.root.children == []


def test_pending_capability_review_requires_tool_name() -> None:
    payload = {
        "run_id": "run_1",
        "kind": "tool",
        "status": "awaiting_review",
        "pending_review": {
            "review_id": "review_1",
            "kind": "capability_review",
            "message": "Review capability call.",
            "capability_call": {
                "invocation_id": "call_1",
                "capability_id": "tool.shell",
                "arguments": {"command": "pwd"},
            },
        },
    }

    with pytest.raises(ValidationError):
        RunState.model_validate(payload)
    with pytest.raises(ValidationError):
        RunResult.model_validate({"state": payload, "output_text": ""})
