from __future__ import annotations

import json
import subprocess
import sys


def test_runtime_entrypoint_rejects_non_spec_first_frame() -> None:
    process = subprocess.run(
        [sys.executable, "-m", "dagent.runtime", "--transport", "stdio"],
        input=json.dumps({"type": "hello", "payload": {}}) + "\n",
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert process.returncode == 2
    assert "first frame must be spec" in process.stderr


def test_runtime_entrypoint_rejects_empty_stdin() -> None:
    process = subprocess.run(
        [sys.executable, "-m", "dagent.runtime", "--transport", "stdio"],
        input="",
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert process.returncode == 2
    assert "first frame must be spec" in process.stderr


def test_runtime_target_builds_tool_agent() -> None:
    from dagent.runtime import _target_from_spec
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
