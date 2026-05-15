from pathlib import Path


def test_runnable_boundaries_are_not_split_into_top_level_runtime_package() -> None:
    root = Path(__file__).resolve().parents[1]

    assert not (root / "dagent" / "runnables").exists()
    assert (root / "dagent" / "schemas" / "runnable.py").exists()
    assert (root / "dagent" / "harness_runtime" / "runnable_executor.py").exists()
    assert (root / "dagent" / "capabilities" / "registry.py").exists()
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
