import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

import dagent
from dagent.harness_runtime.dag_builder import DAGValidationError
from dagent.providers import ChatResponse, MockProvider


def run(coro):
    return asyncio.run(coro)


def test_dag_spec_rejects_invalid_or_non_self_contained_input_schema() -> None:
    @dagent.tool
    def marker() -> str:
        return "done"

    dag = dagent.Dag("schema_validation")
    dag.add_node(dagent.Node("marker", target=marker))
    spec = dag.to_dag_spec()

    with pytest.raises(DAGValidationError, match="Draft 2020-12"):
        dagent.validate_dag_spec(
            spec.model_copy(update={"input_schema": {"type": "not-a-json-schema-type"}})
        )

    with pytest.raises(DAGValidationError, match="not self-contained"):
        dagent.validate_dag_spec(
            spec.model_copy(
                update={"input_schema": {"$ref": "https://example.test/external-schema"}}
            )
        )


def test_validate_dag_input_accepts_general_sdk_schema_without_applying_defaults() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.test/dag-input",
        "$defs": {
            "payload": {
                "$id": "payload",
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer", "default": 3},
                },
                "required": ["name"],
            }
        },
        "$ref": "payload",
    }
    graph_input = {"name": "dagent"}
    original = deepcopy(graph_input)

    dagent.validate_dag_input(schema, graph_input)

    assert graph_input == original
    assert "count" not in graph_input
    dagent.validate_dag_input({"type": "string"}, "scalar inputs remain valid")


def test_validate_dag_input_reports_typed_error_and_instance_path() -> None:
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    }

    with pytest.raises(dagent.DAGInputValidationError) as raised:
        dagent.validate_dag_input(schema, {"count": "three"})

    assert raised.value.path == ("count",)
    assert raised.value.schema_path[-1] == "type"
    assert "$.count" in str(raised.value)


def test_runner_rejects_invalid_graph_input_before_workspace_events_or_capabilities(
    tmp_path: Path,
) -> None:
    calls: list[int] = []
    events: list[dict] = []

    @dagent.tool
    def record(count: int) -> int:
        calls.append(count)
        return count

    dag = dagent.Dag(
        "validated_input",
        input_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
    )
    node = dagent.Node("record", target=record, inputs={"count": dag.input.count})
    dag.add_node(node)
    dag.output = node.output
    workspace_path = tmp_path / "invalid-run-workspace"
    runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=MockProvider([]),
    )

    with pytest.raises(dagent.DAGInputValidationError):
        run(
            runner.run(
                dag,
                graph_input={"count": "three"},
                workspace_path=workspace_path,
                run_id="invalid-input",
                on_event=events.append,
            )
        )

    assert calls == []
    assert events == []
    assert not workspace_path.exists()
    assert runner.run_checkpoint("invalid-input") is None


@pytest.mark.parametrize(
    ("output_kind", "expected_value", "expected_text"),
    [
        ("scalar", "done", "done"),
        ("list", ["done", 2], '["done", 2]'),
        ("object", {"answer": "done", "count": 2}, '{"answer": "done", "count": 2}'),
    ],
)
def test_static_dag_structured_output_round_trips_through_result_event_and_checkpoint(
    tmp_path: Path,
    output_kind: str,
    expected_value,
    expected_text: str,
) -> None:
    @dagent.tool
    def marker() -> str:
        return "done"

    dag = dagent.Dag(f"structured_{output_kind}")
    node = dagent.Node("marker", target=marker)
    dag.add_node(node)
    if output_kind == "scalar":
        dag.output = node.output
    elif output_kind == "list":
        dag.output = [node.output, 2]
    else:
        dag.output = {"answer": node.output, "count": 2}
    runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=MockProvider([]),
    )

    async def collect() -> list[dagent.RunStreamEvent]:
        return [event async for event in runner.stream(dag)]

    events = run(collect())
    finished = events[-1]
    result = finished.data.result

    assert finished.type == "run.finished"
    assert result.output_value == expected_value
    assert result.output_text == expected_text
    assert result.checkpoint is not None
    assert result.checkpoint.state.trace is not None
    assert result.checkpoint.state.trace.root.value == expected_value

    result_payload = result.model_dump(mode="json")
    assert result_payload["output_value"] == expected_value
    assert dagent.RunResult.model_validate(result_payload).output_value == expected_value

    event_payload = finished.model_dump(mode="json")
    assert event_payload["data"]["result"]["output_value"] == expected_value
    restored_event = dagent.RunStreamEvent.model_validate(event_payload)
    assert restored_event.data.result.output_value == expected_value
    assert restored_event.data.result.output_text == expected_text


def test_non_static_result_keeps_output_value_none_and_output_text_unchanged(
    tmp_path: Path,
) -> None:
    runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=MockProvider([ChatResponse(content="hello")]),
    )

    result = run(
        runner.run(dagent.ToolAgent(profile="conversation"), input="say hello")
    )

    assert result.output_text == "hello"
    assert result.output_value is None
    assert result.model_dump(mode="json")["output_value"] is None
