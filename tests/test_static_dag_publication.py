import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

import dagent
from dagent.harness_runtime.dag_builder import DAGValidationError
from dagent.providers import ChatResponse, MockProvider
from dagent.result import (
    CapabilityCallCompletedData,
    CapabilityCallFailedData,
    ResponseFinishedData,
    ResponseStartedData,
    RunFailedData,
    ValidationPassedData,
    ValidationRetryData,
)


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


def test_pydantic_graph_input_validation_uses_generated_schema_aliases(
    tmp_path: Path,
) -> None:
    class AliasedInput(BaseModel):
        value: str = Field(alias="externalValue")

    @dagent.tool
    def echo_alias(value: str) -> str:
        return f"echo:{value}"

    dag = dagent.Dag("aliased_input", input=AliasedInput)
    node = dagent.Node(
        "echo",
        target=echo_alias,
        inputs={"value": dag.input.value},
    )
    dag.add_node(node)
    dag.output = node.output
    spec = dag.to_dag_spec()
    graph_input = AliasedInput(externalValue="dagent")

    assert "externalValue" in spec.input_schema["properties"]
    dagent.validate_dag_input(spec, graph_input)

    runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=MockProvider([]),
    )
    result = run(runner.run(dag, graph_input=graph_input))

    assert result.status == "completed"
    assert result.output_value == "echo:dagent"


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


@pytest.mark.parametrize(
    ("event_type", "data", "expected_type"),
    [
        ("response.started", ResponseStartedData(response_id="response_1"), ResponseStartedData),
        ("response.finished", ResponseFinishedData(response_id="response_1"), ResponseFinishedData),
        (
            "capability.call.completed",
            CapabilityCallCompletedData(
                invocation_id="call_1",
                capability_id="tool.echo",
            ),
            CapabilityCallCompletedData,
        ),
        (
            "capability.call.failed",
            CapabilityCallFailedData(
                invocation_id="call_1",
                capability_id="tool.echo",
                content="failed",
            ),
            CapabilityCallFailedData,
        ),
        ("validation.passed", ValidationPassedData(summary="ok"), ValidationPassedData),
        (
            "validation.retry",
            ValidationRetryData(summary="retry", reason="fix it"),
            ValidationRetryData,
        ),
        (
            "run.failed",
            RunFailedData(message="failed", error_type="RuntimeError"),
            RunFailedData,
        ),
    ],
)
def test_stream_event_model_validate_uses_envelope_type_for_payload(
    event_type: str,
    data,
    expected_type: type,
) -> None:
    event = dagent.RunStreamEvent(type=event_type, data=data)
    payload = event.model_dump(mode="json")

    restored = dagent.RunStreamEvent.model_validate(payload)

    assert type(restored.data) is expected_type
    assert restored.model_dump(mode="json") == payload
