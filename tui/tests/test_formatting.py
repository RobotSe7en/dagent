from rich.text import Text

from dagent_tui.app import _conversation_title
from dagent_tui.formatting import dag_text, trace_text


def plain(value: Text) -> str:
    return value.plain


def test_dag_text_lists_nodes_capabilities_and_edges() -> None:
    dag = {
        "dag_id": "dag-1",
        "status": "running",
        "nodes": [
            {"id": "start", "status": "completed", "payload": {"type": "start"}},
            {
                "id": "inspect",
                "title": "Inspect repo",
                "status": "running",
                "payload": {
                    "type": "capability",
                    "invocation": {"capability_id": "tool.inspect"},
                },
            },
        ],
        "edges": [{"source": "start", "target": "inspect"}],
    }

    rendered = plain(dag_text(dag))

    assert "dag-1  [running]" in rendered
    assert "inspect — Inspect repo" in rendered
    assert "tool.inspect" in rendered
    assert "start → inspect" in rendered


def test_trace_text_walks_nested_trace_nodes() -> None:
    trace = {
        "root": {
            "kind": "run",
            "status": "running",
            "ref": {"dag_id": "dag-1"},
            "children": [
                {
                    "kind": "dag_node",
                    "status": "completed",
                    "ref": {"node_id": "inspect"},
                    "children": [],
                }
            ],
        }
    }

    rendered = plain(trace_text(trace))

    assert "run dag-1 [running]" in rendered
    assert "dag_node inspect [completed]" in rendered


def test_conversation_title_is_single_line_and_bounded() -> None:
    title = _conversation_title("  a long\n" + "prompt " * 20)

    assert "\n" not in title
    assert len(title) == 48
    assert title.endswith("…")
