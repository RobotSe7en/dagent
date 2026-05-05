import pytest

from dagent.harness_runtime.profiled_agent import extract_json_object


def test_extract_json_object_accepts_trailing_text() -> None:
    assert extract_json_object('{"action": "keep"}\n\nDone.') == {"action": "keep"}


def test_extract_json_object_uses_first_complete_object_when_multiple_are_present() -> None:
    assert extract_json_object('{"action": "keep"}\n{"ignored": true}') == {"action": "keep"}


def test_extract_json_object_rejects_non_object_json() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        extract_json_object('[{"action": "keep"}]')
