from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from dagent.result import RunFailedData, RunStartedData, RunStreamEvent
from dagent.schemas import RuntimeFrame, RuntimeRunSpec


class RecordingTransport:
    def __init__(self, frame: RuntimeFrame | None = None) -> None:
        self._frame = frame
        self.frames: list[RuntimeFrame] = []
        self.events: list[RunStreamEvent] = []
        self.closed = False

    def read_frame(self) -> RuntimeFrame:
        if self._frame is None:
            raise EOFError
        frame = self._frame
        self._frame = None
        return frame

    def write_frame(self, frame: RuntimeFrame) -> None:
        self.frames.append(frame)

    def write_event(self, event: RunStreamEvent) -> None:
        self.events.append(event)

    def close(self) -> None:
        self.closed = True


def _runtime_spec(**overrides) -> RuntimeRunSpec:
    payload = {
        "schema_version": 1,
        "run_id": "run_1",
        "action": "run",
        "target": {"type": "tool_agent", "messages": [{"role": "user", "content": "hi"}]},
        "provider": {"base_url": "http://llm.test/v1", "model": "test"},
    }
    payload.update(overrides)
    return RuntimeRunSpec.model_validate(payload)


def test_worker_entrypoint_rejects_non_spec_first_frame() -> None:
    process = subprocess.run(
        [sys.executable, "-m", "dagent.worker", "--transport", "stdio"],
        input=json.dumps({"type": "hello", "payload": {}}) + "\n",
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert process.returncode == 2
    assert "first frame must be spec" in process.stderr


def test_worker_entrypoint_rejects_empty_stdin() -> None:
    process = subprocess.run(
        [sys.executable, "-m", "dagent.worker", "--transport", "stdio"],
        input="",
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert process.returncode == 2
    assert "first frame must be spec" in process.stderr


def test_worker_target_builds_tool_agent() -> None:
    from dagent.worker import _target_from_spec
    from dagent.schemas import RuntimeRunTarget

    target = _target_from_spec(RuntimeRunTarget(
        type="tool_agent",
        messages=[{"role": "user", "content": "hi"}],
        capabilities=["tool.read_file"],
        skills=["docs/readme"],
    ))

    assert target.profile == "conversation"
    assert target.capabilities == ("tool.read_file",)
    assert target.skills == ("docs/readme",)


def test_worker_run_spec_reports_failed_run_in_bye(monkeypatch) -> None:
    import dagent.worker as worker_module

    class FailedRunner:
        def __init__(self, **kwargs) -> None:
            pass

        async def stream(self, *args, **kwargs):
            yield RunStreamEvent(
                type="run.failed",
                data=RunFailedData(message="boom", error_type="RuntimeError"),
                run_id="run_1",
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(worker_module, "Runner", FailedRunner)
    transport = RecordingTransport()

    asyncio.run(worker_module._run_spec(_runtime_spec(), transport))

    bye = transport.frames[-1].bye_payload()
    assert bye.process_status == "completed"
    assert bye.run_status == "failed"
    assert bye.exit_code == 0


def test_worker_run_spec_writes_typed_events_through_transport_fast_path(monkeypatch) -> None:
    import dagent.schemas.runtime as runtime_schema
    import dagent.worker as worker_module

    class EventRunner:
        def __init__(self, **kwargs) -> None:
            pass

        async def stream(self, *args, **kwargs):
            yield RunStreamEvent(
                type="run.started",
                data=RunStartedData(kind="tool"),
                run_id="run_1",
            )

        def close(self) -> None:
            pass

    def fail_event_adapter():
        raise AssertionError("event payload should already be typed before transport write")

    monkeypatch.setattr(worker_module, "Runner", EventRunner)
    monkeypatch.setattr(runtime_schema, "_event_adapter", fail_event_adapter)
    transport = RecordingTransport()

    exit_code = asyncio.run(worker_module._run_spec(_runtime_spec(), transport))

    assert exit_code == 0
    assert [event.type for event in transport.events] == ["run.started"]
    assert transport.frames[-1].bye_payload().process_status == "completed"


def test_worker_main_sends_failure_bye_after_spec_is_accepted(monkeypatch) -> None:
    import dagent.worker as worker_module

    spec_frame = RuntimeFrame(
        type="spec",
        payload=_runtime_spec().model_dump(mode="json"),
    )
    transport = RecordingTransport(spec_frame)

    monkeypatch.setattr(worker_module, "_transport_from_args", lambda args: transport)
    monkeypatch.setattr(
        worker_module,
        "_runner_from_spec",
        lambda spec: (_ for _ in ()).throw(ValueError("setup failed")),
    )

    exit_code = worker_module.main(["--transport", "stdio"])

    assert exit_code == 1
    assert transport.closed is True
    bye = transport.frames[-1].bye_payload()
    assert bye.process_status == "failed"
    assert bye.exit_code == 1
    assert bye.error_type == "ValueError"
    assert "setup failed" in (bye.error or "")


def test_worker_main_sends_one_failure_bye_when_runner_close_fails(monkeypatch) -> None:
    import dagent.worker as worker_module

    class ClosingRunner:
        def __init__(self, **kwargs) -> None:
            pass

        async def stream(self, *args, **kwargs):
            return
            yield

        def close(self) -> None:
            raise RuntimeError("close failed")

    spec_frame = RuntimeFrame(
        type="spec",
        payload=_runtime_spec().model_dump(mode="json"),
    )
    transport = RecordingTransport(spec_frame)

    monkeypatch.setattr(worker_module, "_transport_from_args", lambda args: transport)
    monkeypatch.setattr(worker_module, "Runner", ClosingRunner)

    exit_code = worker_module.main(["--transport", "stdio"])

    bye_frames = [frame for frame in transport.frames if frame.type == "bye"]
    assert exit_code == 1
    assert len(bye_frames) == 1
    bye = bye_frames[0].bye_payload()
    assert bye.process_status == "failed"
    assert bye.exit_code == 1
    assert bye.error_type == "RuntimeError"
    assert "close failed" in (bye.error or "")


def test_worker_workspace_root_is_not_reused_as_run_workspace_root(monkeypatch, tmp_path) -> None:
    import dagent.worker as worker_module

    captured: dict[str, object] = {}

    class CapturingRunner:
        def __init__(self, **kwargs) -> None:
            captured["runner_workspace"] = kwargs["workspace"]

        async def stream(self, *args, **kwargs):
            captured["run_workspace_root"] = kwargs["workspace_root"]
            return
            yield

        def close(self) -> None:
            pass

    monkeypatch.setattr(worker_module, "Runner", CapturingRunner)
    transport = RecordingTransport()
    sdk_workspace = tmp_path / "sdk-workspace"

    asyncio.run(worker_module._run_spec(_runtime_spec(
        workspace={"workspace_root": str(sdk_workspace)},
    ), transport))

    assert captured["runner_workspace"] == str(sdk_workspace)
    assert captured["run_workspace_root"] == "runs"
