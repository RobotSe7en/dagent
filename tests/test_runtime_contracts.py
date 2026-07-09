from __future__ import annotations

import pytest
from pydantic import ValidationError

from dagent.result import RunStartedData, RunStreamEvent
from dagent.schemas import RuntimeFrame, RuntimeRunSpec


def test_runtime_run_spec_rejects_unknown_host_fields() -> None:
    with pytest.raises(ValidationError):
        RuntimeRunSpec.model_validate({
            "schema_version": 1,
            "run_id": "run_1",
            "action": "run",
            "target": {"type": "tool_agent", "messages": [{"role": "user", "content": "hi"}]},
            "provider": {"base_url": "http://llm.test/v1", "model": "test"},
            "org_id": "org_1",
        })


def test_runtime_run_spec_includes_host_run_id_and_validation() -> None:
    spec = RuntimeRunSpec.model_validate({
        "schema_version": 1,
        "run_id": "run_1",
        "action": "run",
        "target": {"type": "tool_agent", "messages": [{"role": "user", "content": "hi"}]},
        "provider": {"base_url": "http://llm.test/v1", "model": "test"},
        "validation": {"enabled": True, "validator": "validator_agent", "max_retries": 2},
    })

    assert spec.run_id == "run_1"
    assert spec.validation.enabled is True
    assert spec.validation.max_retries == 2


def test_runtime_run_spec_rejects_unsafe_run_id_at_schema_boundary() -> None:
    with pytest.raises(ValidationError, match="run_id"):
        RuntimeRunSpec.model_validate({
            "schema_version": 1,
            "run_id": "../escape",
            "action": "run",
            "target": {"type": "tool_agent", "messages": [{"role": "user", "content": "hi"}]},
            "provider": {"base_url": "http://llm.test/v1", "model": "test"},
        })


def test_runtime_run_spec_rejects_unsafe_state_run_id_at_schema_boundary() -> None:
    with pytest.raises(ValidationError, match="run_id"):
        RuntimeRunSpec.model_validate({
            "schema_version": 1,
            "action": "resume",
            "review_decision": {"review_id": "review_1", "approved": True},
            "provider": {"base_url": "http://llm.test/v1", "model": "test"},
            "state": {
                "schema_version": 1,
                "run_id": "../escape",
                "kind": "tool",
                "status": "awaiting_review",
            },
        })


def test_runtime_run_spec_rejects_unknown_provider_fields() -> None:
    with pytest.raises(ValidationError, match="provider"):
        RuntimeRunSpec.model_validate({
            "schema_version": 1,
            "action": "run",
            "target": {"type": "tool_agent", "messages": [{"role": "user", "content": "hi"}]},
            "provider": {
                "base_url": "http://llm.test/v1",
                "model": "test",
                "unexpected_provider_field": "unsupported",
            },
        })


def test_runtime_run_spec_rejects_unknown_python_tool_fields() -> None:
    with pytest.raises(ValidationError, match="python_tools"):
        RuntimeRunSpec.model_validate({
            "schema_version": 1,
            "action": "run",
            "target": {"type": "tool_agent", "messages": [{"role": "user", "content": "hi"}]},
            "provider": {"base_url": "http://llm.test/v1", "model": "test"},
            "python_tool_user_config_dir": ".",
            "python_tools": [
                {
                    "id": "sdk_source",
                    "path": "tool.py",
                    "unexpected_python_tool_field": "unsupported",
                }
            ],
        })


def test_runtime_frame_validates_event_payload() -> None:
    event = RunStreamEvent(
        type="run.started",
        data=RunStartedData(kind="tool"),
        sequence=1,
        run_id="run_1",
    )

    frame = RuntimeFrame(type="event", payload=event.model_dump(mode="json"))

    assert frame.event_payload().type == "run.started"


def test_runtime_bye_distinguishes_process_and_run_status() -> None:
    frame = RuntimeFrame(
        type="bye",
        payload={"process_status": "completed", "run_status": "awaiting_review", "exit_code": 0},
    )

    assert frame.bye_payload().process_status == "completed"
    assert frame.bye_payload().run_status == "awaiting_review"


def test_runtime_run_spec_resume_requires_state() -> None:
    with pytest.raises(ValidationError, match="state"):
        RuntimeRunSpec.model_validate({
            "schema_version": 1,
            "run_id": "run_1",
            "action": "resume",
            "review_decision": {"review_id": "review_1", "approved": True},
            "provider": {"base_url": "http://llm.test/v1", "model": "test"},
        })


def test_runtime_bye_rejects_contradictory_process_status() -> None:
    with pytest.raises(ValidationError, match="exit_code"):
        RuntimeFrame(
            type="bye",
            payload={"process_status": "failed", "run_status": None, "exit_code": 0},
        )
