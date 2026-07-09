from __future__ import annotations

import dagent
from dagent.providers.mock import MockProvider
from dagent.schemas import CapabilityDefinition, CapabilityResult


def _noop(invocation):
    return CapabilityResult.completed(invocation, "ok")


def _runner(tmp_path) -> dagent.Runner:
    return dagent.Runner(workspace=tmp_path, provider=MockProvider([]))


def test_validate_capability_refs_reports_unknown_ids_without_raising(tmp_path) -> None:
    runner = _runner(tmp_path)

    result = runner.validate_capability_refs(["tool.missing"])

    assert result.passed is False
    assert result.issues[0].capability_id == "tool.missing"
    assert result.issues[0].code == "unknown_capability"
    runner.close()


def test_validate_capability_refs_rejects_disabled_when_enabled_only(tmp_path) -> None:
    runner = _runner(tmp_path)
    runner.register_capability(
        CapabilityDefinition(id="tool.hidden", kind="tool", enabled=False),
        _noop,
    )

    result = runner.validate_capability_refs(["tool.hidden"])

    assert result.passed is False
    assert result.issues[0].capability_id == "tool.hidden"
    assert result.issues[0].code == "disabled_capability"
    runner.close()


def test_validate_capability_refs_does_not_register_bindings(tmp_path) -> None:
    @dagent.tool
    def local_tool() -> str:
        return "ok"

    runner = _runner(tmp_path)

    result = runner.validate_capability_refs([local_tool])

    assert result.passed is False
    assert runner.get_capability("tool.local_tool") is None
    assert result.issues[0].capability_id == "tool.local_tool"
    assert result.issues[0].code == "unknown_capability"
    runner.close()


def test_validate_capability_refs_none_checks_default_visible_capabilities(tmp_path) -> None:
    runner = _runner(tmp_path)
    runner.set_capability_enabled("tool.read_file", False)

    result = runner.validate_capability_refs(None)

    assert result.passed is False
    assert any(
        issue.capability_id == "tool.read_file" and issue.code == "disabled_capability"
        for issue in result.issues
    )
    runner.close()


def test_validate_capability_refs_respects_allowed_kinds(tmp_path) -> None:
    runner = _runner(tmp_path)

    result = runner.validate_capability_refs(["skill.list"], allowed_kinds={"tool"})

    assert result.passed is False
    assert result.issues[0].capability_id == "skill.list"
    assert result.issues[0].code == "unsupported_kind"
    runner.close()
