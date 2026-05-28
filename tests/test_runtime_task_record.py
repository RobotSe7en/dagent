from dataclasses import fields

from dagent.harness_runtime import RuntimeTaskRecord
from dagent.harness_runtime.runtime_session import HarnessRuntimeSession
from dagent.schemas import DAG, LoopOutcome, RunTrace, RunTraceNode


def test_runtime_task_record_fields_are_only_resume_state() -> None:
    field_names = {field.name for field in fields(RuntimeTaskRecord)}

    assert field_names == {
        "task_id",
        "mode",
        "user_request",
        "review_level",
        "pending_review",
        "dag",
        "trace",
        "runtime_mode",
        "spec_id",
        "workspace_path",
        "capability_scope",
    }


def test_session_save_loop_outcome_projects_single_trace_without_result_fields() -> None:
    session = HarnessRuntimeSession()
    dag = DAG(dag_id="dag_1", task_id="task_1")
    trace = RunTrace(
        run_id="task_1",
        root=RunTraceNode.run(run_id="task_1", status="completed"),
    )
    trace.root.output = "done"
    outcome = LoopOutcome(
        status="completed",
        final_answer="done",
        dag=dag,
        trace=trace,
        task_id="task_1",
    )

    record = session.save_loop_outcome(
        task_id=None,
        mode="dag",
        user_request="do it",
        review_level="fast",
        loop_outcome=outcome,
        runtime_mode="dag",
    )

    assert record.task_id == "task_1"
    assert record.dag is dag
    assert record.trace is trace
    assert record.trace.root.output == "done"
    assert not hasattr(record, "status")
    assert not hasattr(record, "final_response")
    assert not hasattr(record, "invocations")
    assert not hasattr(record, "traces")
