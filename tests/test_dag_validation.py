import pytest

from dagent.harness_runtime.dag_validation import DAGValidationError, validate_dag
from dagent.harness_runtime import MockPlanner
from dagent.schemas import DAG, DAGEdge, DAGNode


def make_node(node_id: str) -> DAGNode:
    return DAGNode(id=node_id, title=node_id, goal=f"Complete {node_id}")


def test_valid_dag_passes_validation() -> None:
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[make_node("a"), make_node("b")],
        edges=[DAGEdge(source="a", target="b")],
    )

    validate_dag(dag)


def test_dag_must_have_at_least_one_node() -> None:
    dag = DAG(dag_id="dag_1", task_id="task_1")

    with pytest.raises(DAGValidationError, match="at least one node"):
        validate_dag(dag)


def test_node_ids_must_be_unique() -> None:
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[make_node("a"), make_node("a")],
    )

    with pytest.raises(DAGValidationError, match="Duplicate node IDs: a"):
        validate_dag(dag)


def test_edge_source_must_exist() -> None:
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[make_node("b")],
        edges=[DAGEdge(source="a", target="b")],
    )

    with pytest.raises(DAGValidationError, match="source 'a'"):
        validate_dag(dag)


def test_edge_target_must_exist() -> None:
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[make_node("a")],
        edges=[DAGEdge(source="a", target="b")],
    )

    with pytest.raises(DAGValidationError, match="target 'b'"):
        validate_dag(dag)


def test_dag_must_be_acyclic() -> None:
    dag = DAG(
        dag_id="dag_1",
        task_id="task_1",
        nodes=[make_node("a"), make_node("b"), make_node("c")],
        edges=[
            DAGEdge(source="a", target="b"),
            DAGEdge(source="b", target="c"),
            DAGEdge(source="c", target="a"),
        ],
    )

    with pytest.raises(DAGValidationError, match="acyclic"):
        validate_dag(dag)


def test_mock_planner_returns_valid_dag() -> None:
    dag = MockPlanner().plan("Summarize the repo", task_id="task_1")

    validate_dag(dag)
    assert dag.task_id == "task_1"
    assert dag.status == "draft"
    assert [node.risk for node in dag.nodes] == ["low", "low"]
