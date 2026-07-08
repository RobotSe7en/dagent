import pytest

from examples import local_test_mcp


def test_ping_returns_diagnostic_payload() -> None:
    result = local_test_mcp.ping("hello")

    assert result["ok"] is True
    assert result["message"] == "hello"
    assert result["server"] == "dagent-local-test-mcp"
    assert isinstance(result["pid"], int)


def test_echo_waits_130_seconds_before_returning(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    monkeypatch.setattr(local_test_mcp.time, "sleep", delays.append)

    assert local_test_mcp.echo("hello", uppercase=False) == "hello"
    assert local_test_mcp.echo("hello", uppercase=True) == "HELLO"
    assert delays == [130, 130]


def test_add_returns_numeric_sum() -> None:
    assert local_test_mcp.add(2, 3.5) == 5.5


def test_env_check_reports_presence_without_revealing_non_test_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVATE_TOKEN", "secret-value")
    monkeypatch.setenv("DAGENT_TEST_TOKEN", "visible-value")

    private_result = local_test_mcp.env_check("PRIVATE_TOKEN")
    test_result = local_test_mcp.env_check("DAGENT_TEST_TOKEN", reveal=True)

    assert private_result == {
        "name": "PRIVATE_TOKEN",
        "present": True,
        "length": len("secret-value"),
        "value": None,
    }
    assert test_result["value"] == "visible-value"


def test_env_check_reports_missing_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAGENT_TEST_MISSING", raising=False)

    assert local_test_mcp.env_check("DAGENT_TEST_MISSING") == {
        "name": "DAGENT_TEST_MISSING",
        "present": False,
        "length": 0,
        "value": None,
    }


def test_delay_seconds_are_clamped() -> None:
    assert local_test_mcp.clamp_delay(-1) == 0
    assert local_test_mcp.clamp_delay(0.25) == 0.25
    assert local_test_mcp.clamp_delay(99) == 5
