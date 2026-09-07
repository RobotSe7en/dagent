"""Source-coordinate invariants for file reads and budgeted model displays."""

import codecs
import json
import re

import pytest

from dagent.capabilities.tools.file_tools import MAX_READ_BYTES, read_file
from dagent.harness_runtime.context import ContextAssembler
from dagent.harness_runtime.result_projection import project_result, project_results
from dagent.schemas.context import ContextPolicy
from dagent.schemas.conversation import InlineContent, ResultObservation, ToolResultMessage
from dagent.schemas.retention import ResultRetention


COUNTER = ContextAssembler().token_counter


def result_item(output, *, observation=False):
    fields = dict(name="read_file", capability_id="tool.read_file", status="completed",
                  content=InlineContent(text=output.content),
                  retention=ResultRetention.model_validate(output.retention))
    if observation:
        return ResultObservation(id="observation", arguments={"path": "sample.txt"}, **fields)
    return ToolResultMessage(call_id="read", **fields)


def displayed(text, source):
    match = re.search(r"^\[Displayed: chars=\[(\d+),(\d+)\)\]$", text, re.MULTILINE)
    assert match, text[:300]
    start, end = map(int, match.groups())
    body = text[-(end - start):] if end > start else ""
    assert body == source[start:end]
    cursor = re.search(r"^\[Continue with read_file: (.*)\]$", text, re.MULTILINE)
    args = json.loads(cursor.group(1)) if cursor else None
    if args:
        assert args["offset_chars"] == end
        assert "offset" not in args
    return start, end, body, args


@pytest.mark.parametrize("observation", [False, True])
def test_report_size_complete_source_partial_display(tmp_path, observation):
    source = "".join("文" * (39 + (i < 314)) + ("a" if i < 38 else "") + "\n"
                     for i in range(500))
    assert (len(source.encode()), len(source), source.count("\n")) == (59980, 20352, 500)
    path = tmp_path / "report.txt"
    path.write_bytes(source.encode())
    output = read_file(path)
    assert output.content == source
    item = result_item(output, observation=observation)
    projection = project_result(item, 2048, COUNTER, read_available=True)
    start, end, body, cursor = displayed(projection.text, source)
    assert start == 0 and 0 < end < len(source)
    assert cursor and len(body) == end
    assert "source_eof=true" in projection.text
    assert "[SOURCE_TRUNCATED]" not in projection.text
    assert "[TRUNCATED] model display shortened" in projection.text
    assert COUNTER.count_text(projection.text) <= 2048
    assert item.retention.window_length == len(source)


@pytest.mark.parametrize("source,bom,first_window", [
    ("甲乙🙂abcdef\n" * 500, False, {}),
    ("头\r\n" + "中文🙂" * 3000 + "\r\n末尾\r\n", True, {"offset": 2, "limit": 1}),
    ("文🙂" * 35000 + "\n末尾\n", False, {}),
    ("first\nlast\n\n", False, {"offset": 2, "limit": 2}),
])
def test_follow_display_cursors_reconstructs_source_suffix(tmp_path, source, bom, first_window):
    path = tmp_path / "unicode.txt"
    path.write_bytes((codecs.BOM_UTF8 if bom else b"") + source.encode())
    output = read_file(path, **first_window)
    original_start = output.retention["window_start"]
    parts = []
    previous_end = original_start
    for _ in range(1000):
        item = result_item(output)
        projection = project_result(item, 1024, COUNTER, read_available=True)
        start, end, body, cursor = displayed(projection.text, source)
        assert start == previous_end
        assert COUNTER.count_text(projection.text) <= 1024
        parts.append(body)
        if cursor is None:
            assert end == len(source)
            break
        assert end > start, "The chosen budget must allow reading progress."
        previous_end = end
        output = read_file(**cursor)
    else:
        pytest.fail("Continuation did not reach EOF.")
    assert "".join(parts) == source[original_start:]


@pytest.mark.parametrize("source,offset", [("", 0), ("中文🙂\r\n", 5)])
def test_character_eof_is_empty_and_has_no_display_cursor(tmp_path, source, offset):
    path = tmp_path / "empty.txt"
    path.write_bytes(source.encode())
    item = result_item(read_file(path, offset_chars=offset))
    projection = project_result(item, 1024, COUNTER, read_available=True)
    assert displayed(projection.text, source) == (offset, offset, "", None)
    assert "source_eof=true" in projection.text
    assert not projection.truncated
    with pytest.raises(ValueError, match="beyond end"):
        read_file(path, offset_chars=offset + 1)


@pytest.mark.parametrize("ending", ["\n", "\r\n", ""])
def test_last_line_keeps_exact_terminator(tmp_path, ending):
    source = "first\n中文🙂" + ending
    path = tmp_path / "last.txt"
    path.write_bytes(source.encode())
    output = read_file(path, offset=2, limit=1)
    assert output.content == "中文🙂" + ending
    projection = project_result(result_item(output), 1024, COUNTER, read_available=True)
    assert displayed(projection.text, source) == (6, len(source), source[6:], None)
    assert "source_eof=true" in projection.text


def test_crlf_read_cap_counts_bytes_without_skipping(tmp_path):
    source = ("文" * 100 + "\r\n") * 1000
    path = tmp_path / "bytes.txt"
    path.write_bytes(source.encode())
    output = read_file(path)
    length = output.retention["window_length"]
    body = output.content[:length]
    assert len(body.encode()) <= MAX_READ_BYTES
    assert body == source[:length]
    assert output.retention["source_completeness"] == "partial"
    following = read_file(path, offset_chars=length)
    assert following.content == source[length:length + len(following.content)]


@pytest.mark.parametrize("bom,first_line", [
    (True, "文🙂" * 40000 + "\n"),
    (False, "x" * MAX_READ_BYTES + "\r\n"),
])
def test_bounded_first_line_does_not_change_later_line_offsets(tmp_path, bom, first_line):
    source = first_line + "z\n"
    path = tmp_path / "bounded.txt"
    path.write_bytes((codecs.BOM_UTF8 if bom else b"") + source.encode())
    output = read_file(path)
    length = output.retention["window_length"]
    assert output.content[:length] == source[:length]
    assert length < len(first_line)
    assert output.retention["source_completeness"] == "partial"
    second = read_file(path, offset=2, limit=1)
    assert second.content == "z\n"
    assert second.retention["window_start"] == len(first_line)


@pytest.mark.parametrize("observation", [False, True])
def test_total_budget_reprojects_history_cursor_without_mutating_source(tmp_path, observation):
    source = "中文🙂abcdef\n" * 1500
    path = tmp_path / "history.txt"
    path.write_bytes(source.encode())
    first = result_item(read_file(path), observation=observation)
    second = result_item(read_file(path), observation=observation).model_copy(update={"id": "newer"})
    policy = ContextPolicy(max_tool_result_tokens=1024, max_total_tool_result_tokens=1024)
    one = project_results([first], policy, COUNTER, read_available=True)
    two = project_results([first, second], policy, COUNTER, read_available=True)
    earlier_end = displayed(one[first.id].text, source)[1]
    current_end = displayed(two[first.id].text, source)[1]
    assert 0 < current_end < earlier_end
    assert first.retention.window_start == 0
    assert first.retention.window_length == len(source)
    for projection in two.values():
        displayed(projection.text, source)
    assert sum(COUNTER.count_text(p.text) for p in two.values()) <= 1024


def test_source_pagination_does_not_claim_budget_truncation(tmp_path):
    path = tmp_path / "page.txt"
    source = "first\nsecond\n"
    path.write_text(source)
    projection = project_result(result_item(read_file(path, limit=1)), 1024,
                                COUNTER, read_available=True)
    assert "[SOURCE_TRUNCATED]" in projection.text
    assert "source_eof=false" in projection.text
    assert "[TRUNCATED]" not in projection.text
    assert not projection.truncated
    assert displayed(projection.text, source)[:3] == (0, 6, "first\n")


def test_zero_body_never_claims_progress(tmp_path):
    class Counter:
        def count_text(self, text):
            # Make every displayed source character prohibitively expensive.
            return 1000 if "文" in text else 1

    path = tmp_path / "zero.txt"
    path.write_text("文")
    projection = project_result(result_item(read_file(path)), 64, Counter(), read_available=True)
    start, end, body, cursor = displayed(projection.text, "文")
    assert start == end == cursor["offset_chars"] == 0
    assert not body
    assert "[Displayed: chars=[0,0)]" in projection.text
