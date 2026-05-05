import asyncio
import json

import pytest

from dagent.harness_runtime import (
    LLMLocalDAGReplanner,
    NodeExecutionResult,
    ReplanDecision,
    affected_node_ids_for_patch,
    apply_node_patch_decision,
    apply_replan_decision,
)
from dagent.providers import ChatResponse, MockProvider
from dagent.profiles import AgentProfile
from dagent.schemas import Boundary, DAG, DAGEdge, DAGNode


def run(coro):
    return asyncio.run(coro)


def test_llm_replanner_parses_replace_plan() -> None:
    provider = MockProvider(
        [
            ChatResponse(
                content=json.dumps(
                    {
                        "action": "replace",
                        "reason": "Use observed output.",
                        "plan": {
                            "task": "continue",
                            "nodes": [
                                {
                                    "id": "answer",
                                    "goal": "Echo observed output.",
                                    "tool": "echo",
                                    "args": {"text": "{{inspect.output}}"},
                                    "depends_on": ["inspect"],
                                }
                            ],
                        },
                    }
                )
            )
        ]
    )
    replanner = LLMLocalDAGReplanner(
        provider,
        profile=_profile(),
    )

    decision = run(
        replanner.replan(
            context=type(
                "Context",
                (),
                {
                    "task_id": "task_1",
                    "user_request": "answer",
                    "dag": _dag(
                        [
                            _node("inspect", "echo", {"text": "ls"}),
                            _node("old_answer", "echo", {"text": "old"}),
                        ],
                        [DAGEdge(source="inspect", target="old_answer")],
                    ),
                    "node_results": {
                        "inspect": NodeExecutionResult(
                            node_id="inspect",
                            final_response="files",
                            completed=True,
                            stop_reason="completed",
                            steps=1,
                        )
                    },
                    "trace_records": [],
                    "failed_node_id": None,
                    "last_error": None,
                },
            )()
        )
    )

    assert decision.action == "replace"
    assert decision.dag is not None
    assert decision.dag.nodes[0].id == "answer"
    assert decision.dag.edges[0].source == "inspect"


def test_llm_replanner_parses_patch_node() -> None:
    provider = MockProvider(
        [
            ChatResponse(
                content=json.dumps(
                    {
                        "action": "patch_node",
                        "reason": "Fix the command args.",
                        "node_id": "list_files",
                        "tool": "echo",
                        "args": {"text": "fixed"},
                    }
                )
            )
        ]
    )
    replanner = LLMLocalDAGReplanner(provider, profile=_profile())

    decision = run(
        replanner.replan(
            context=type(
                "Context",
                (),
                {
                    "task_id": "task_1",
                    "user_request": "answer",
                    "dag": _dag([_node("list_files", "echo", {"text": "bad"})], []),
                    "node_results": {},
                    "trace_records": [],
                    "failed_node_id": "list_files",
                    "last_error": "bad args",
                },
            )()
        )
    )

    assert decision.action == "patch_node"
    assert decision.node_id == "list_files"
    assert decision.tool == "echo"
    assert decision.args == {"text": "fixed"}


def test_apply_node_patch_updates_unfinished_node() -> None:
    current = _dag([_node("list_files", "echo", {"text": "bad"})], [])

    patched = apply_node_patch_decision(
        current=current,
        decision=ReplanDecision(
            action="patch_node",
            reason="Fix args.",
            node_id="list_files",
            args={"text": "fixed"},
        ),
        completed_node_results={},
    )

    assert patched.version == current.version + 1
    assert patched.nodes[0].args == {"text": "fixed"}
    assert patched.nodes[0].status == "ready"


def test_apply_node_patch_allows_completed_node_for_rerun() -> None:
    current = _dag([_node("list_current_files", "echo", {"text": "old"})], [])

    patched = apply_node_patch_decision(
        current=current,
        decision=ReplanDecision(
            action="patch_node",
            reason="Rerun with corrected args.",
            node_id="list_current_files",
            args={"text": "fixed"},
        ),
        completed_node_results={
            "list_current_files": NodeExecutionResult(
                node_id="list_current_files",
                final_response="old-output",
                completed=True,
                stop_reason="completed",
                steps=1,
            )
        },
    )

    assert patched.nodes[0].args == {"text": "fixed"}
    assert patched.nodes[0].status == "ready"


def test_affected_node_ids_for_patch_includes_downstream_nodes() -> None:
    dag = _dag(
        [
            _node("list_current_files", "echo", {"text": "old"}),
            _node("summarize", "echo", {"text": "{{list_current_files.output}}"}),
            _node("unrelated", "echo", {"text": "ok"}),
        ],
        [DAGEdge(source="list_current_files", target="summarize")],
    )

    assert affected_node_ids_for_patch(dag, "list_current_files") == {
        "list_current_files",
        "summarize",
    }


def test_apply_replan_preserves_completed_nodes_and_replaces_pending() -> None:
    current = _dag(
        [
            _node("inspect", "echo", {"text": "ls"}),
            _node("old_answer", "echo", {"text": "old"}),
        ],
        [DAGEdge(source="inspect", target="old_answer")],
    )
    replacement = _dag(
        [_node("answer", "echo", {"text": "{{inspect.output}}"})],
        [DAGEdge(source="inspect", target="answer")],
    )

    merged = apply_replan_decision(
        current=current,
        decision=ReplanDecision(
            action="replace",
            reason="Use observation.",
            dag=replacement,
        ),
        node_results={
            "inspect": NodeExecutionResult(
                node_id="inspect",
                final_response="files",
                completed=True,
                stop_reason="completed",
                steps=1,
            )
        },
    )

    assert merged.version == current.version + 1
    assert [node.id for node in merged.nodes] == ["inspect", "answer"]
    assert merged.nodes[0].args == {"text": "ls"}
    assert merged.nodes[1].args == {"text": "{{inspect.output}}"}


def test_apply_replan_rejects_edges_into_completed_nodes() -> None:
    current = _dag([_node("inspect", "echo", {"text": "ls"})], [])
    replacement = _dag(
        [_node("answer", "echo", {"text": "bad"})],
        [DAGEdge(source="answer", target="inspect")],
    )

    with pytest.raises(ValueError, match="already completed"):
        apply_replan_decision(
            current=current,
            decision=ReplanDecision(action="replace", dag=replacement),
            node_results={
                "inspect": NodeExecutionResult(
                    node_id="inspect",
                    final_response="files",
                    completed=True,
                    stop_reason="completed",
                    steps=1,
                )
            },
        )


def _dag(nodes: list[DAGNode], edges: list[DAGEdge]) -> DAG:
    return DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=nodes,
        edges=edges,
    )


def _node(node_id: str, tool: str, args: dict) -> DAGNode:
    return DAGNode(
        id=node_id,
        title=node_id,
        goal=node_id,
        kind="tool",
        tool=tool,
        args=args,
        tools=[tool],
        boundary=Boundary(mode="read_only"),
    )


def _profile() -> AgentProfile:
    return AgentProfile(
        name="dag_replanner",
        role="dag_replanner",
        layers=["soul"],
        layer_contents={"soul": "Return JSON."},
    )
