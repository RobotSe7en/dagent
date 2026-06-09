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
