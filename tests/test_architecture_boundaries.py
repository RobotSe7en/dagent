from pathlib import Path


def test_dag_loop_public_names_use_one_loop_with_explicit_static_and_dynamic_entries() -> None:
    import dagent.harness_runtime as runtime
    from dagent.harness_runtime.dag_agent import DAGAgent, DAGAgentLoop

    assert hasattr(runtime, "DAGAgentLoop")
    assert not hasattr(runtime, "DynamicDAGLoop")
    assert not hasattr(runtime, "StaticDAGLoop")
    assert hasattr(DAGAgentLoop, "run_dynamic")
    assert hasattr(DAGAgentLoop, "run_static")
    assert hasattr(DAGAgentLoop, "execute")
    assert "run_spec" not in DAGAgent.__dict__


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
    assert (root / "dagent" / "capabilities" / "mcp" / "__init__.py").exists()
    assert (root / "dagent" / "capabilities" / "skills.py").exists()
    assert (root / "dagent" / "capabilities" / "toolsets.py").exists()


def test_public_tool_decorator_and_tools_live_under_capabilities() -> None:
    root = Path(__file__).resolve().parents[1]

    assert not (root / "dagent" / "capability.py").exists()
    assert (root / "dagent" / "capabilities" / "decorator.py").exists()
    assert not (root / "dagent" / "tools").exists()
    assert (root / "dagent" / "capabilities" / "tools" / "registry.py").exists()
    assert (root / "dagent" / "capabilities" / "tools" / "file_tools.py").exists()
    assert (root / "dagent" / "capabilities" / "tools" / "shell_tools.py").exists()
    assert (root / "dagent" / "capabilities" / "tools" / "boundary.py").exists()


def test_source_no_longer_imports_top_level_tools_package() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = [
        *list((root / "dagent").rglob("*.py")),
        *list((root / "tests").rglob("*.py")),
    ]
    allowed_files = {root / "tests" / "test_architecture_boundaries.py"}

    for path in paths:
        if "__pycache__" in path.parts or path in allowed_files:
            continue
        text = path.read_text(encoding="utf-8")
        assert "dagent.tools" not in text, f"top-level tools import remains in {path}"


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


def test_review_policy_lives_with_public_review_types() -> None:
    root = Path(__file__).resolve().parents[1]
    review_text = (root / "dagent" / "review.py").read_text(encoding="utf-8")

    assert not (root / "dagent" / "harness_runtime" / "review_policy.py").exists()
    assert "ReviewLevel" in review_text
    assert "class _ReviewPolicy" in review_text
    assert "def _review_policy" in review_text


def test_capability_catalog_does_not_expose_handler_reusing_snapshot_api() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "dagent" / "capabilities" / "catalog.py").read_text(encoding="utf-8")

    assert "def snapshot" not in text


def test_tool_and_dag_agents_get_visible_capabilities_from_tool_adapter() -> None:
    root = Path(__file__).resolve().parents[1]
    tool_agent = (root / "dagent" / "harness_runtime" / "tool_agent.py").read_text(encoding="utf-8")
    dag_agent = (root / "dagent" / "harness_runtime" / "dag_agent.py").read_text(encoding="utf-8")

    assert "catalog.list(kind=\"tool\"" not in tool_agent
    assert "catalog.list(kind=\"tool\"" not in dag_agent
    assert "tool_adapter.capabilities" in dag_agent
    assert "tool_adapter.definitions" in tool_agent


def test_tool_agent_has_no_secondary_llm_tool_injection_path() -> None:
    root = Path(__file__).resolve().parents[1]
    tool_agent = (root / "dagent" / "harness_runtime" / "tool_agent.py").read_text(encoding="utf-8")

    assert "extra_tools" not in tool_agent


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
