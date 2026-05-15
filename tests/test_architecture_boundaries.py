from pathlib import Path


def test_capability_boundaries_are_not_split_into_top_level_runtime_package() -> None:
    root = Path(__file__).resolve().parents[1]

    assert not (root / "dagent" / "runnables").exists()
    assert not (root / "dagent" / "schemas" / "runnable.py").exists()
    assert not (root / "dagent" / "harness_runtime" / "runnable_executor.py").exists()
    assert (root / "dagent" / "schemas" / "capability.py").exists()
    assert (root / "dagent" / "harness_runtime" / "capability_executor.py").exists()
    assert (root / "dagent" / "capabilities" / "catalog.py").exists()
    assert (root / "dagent" / "capabilities" / "bootstrap.py").exists()
    assert (root / "dagent" / "capabilities" / "providers.py").exists()


def test_harness_runtime_executors_do_not_depend_on_tool_executor() -> None:
    root = Path(__file__).resolve().parents[1]
    runtime_files = [
        root / "dagent" / "harness_runtime" / "dag_executor.py",
        root / "dagent" / "harness_runtime" / "tool_agent.py",
        root / "dagent" / "harness_runtime" / "runtime.py",
    ]

    for path in runtime_files:
        text = path.read_text(encoding="utf-8")
        assert "ToolExecutor" not in text
        assert "tool_executor" not in text


def test_harness_runtime_does_not_assemble_capability_providers() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "dagent" / "harness_runtime" / "runtime.py").read_text(encoding="utf-8")

    assert "MemoryCapabilityProvider" not in text
    assert "FileCapabilityProvider" not in text
    assert "_register_default_capabilities" not in text


def test_capability_catalog_does_not_expose_handler_reusing_snapshot_api() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "dagent" / "capabilities" / "catalog.py").read_text(encoding="utf-8")

    assert "def snapshot" not in text


def test_runtime_code_has_no_runnable_or_tool_execution_history_names() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = [
        *list((root / "dagent").rglob("*.py")),
        *list((root / "tests").rglob("*.py")),
    ]
    forbidden = [
        "Runnable",
        "runnable",
        "ToolExecutionRecord",
        "ToolExecutionStore",
        "ToolExecutionSource",
        "ToolExecutionStatus",
        "tool_execution_",
        "tool_called",
        "tool_completed",
        "tool_failed",
        "tool_loop",
        "tool_review",
    ]
    allowed_files = {root / "tests" / "test_architecture_boundaries.py"}

    for path in paths:
        if "__pycache__" in path.parts or path in allowed_files:
            continue
        text = path.read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in text, f"{name!r} remains in {path}"
