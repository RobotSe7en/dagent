"""Static DAG control flow: conditional branches, map fan-out, a subgraph, and a loop.

Run from the repository root:

    uv run python -m examples.control_flow
"""

from __future__ import annotations

import asyncio

import dagent
from dagent.providers import MockProvider


@dagent.tool
def find_urls(topic: str) -> dict:
    """Return urls and a confidence score for a topic."""

    return {"urls": [f"{topic}/a", f"{topic}/b"], "score": 0.9}


@dagent.tool
def fetch(url: str) -> str:
    """Fetch one url."""

    return f"page:{url}"


@dagent.tool
def summarize(pages: list) -> str:
    """Summarize fetched pages."""

    return " + ".join(pages)


@dagent.tool
def escalate(topic: str) -> str:
    """Fallback when confidence is low."""

    return f"escalated:{topic}"


@dagent.tool
def polish(draft: str) -> dict:
    """Improve a draft and rate it."""

    improved = f"{draft}!"
    return {"draft": improved, "quality": improved.count("!")}


def polish_dag() -> dagent.Dag:
    body = dagent.Dag("polish_pass", input=str)
    node = dagent.Node("polish", target=polish, inputs={"draft": body.input})
    body.add_node(node)
    body.output = node.output["draft"]
    return body


def build_dag() -> dagent.Dag:
    dag = dagent.Dag("control_flow", input=str)

    research = dagent.Node("research", target=find_urls, inputs={"topic": dag.input})
    fetch_all = dagent.MapNode(
        "fetch_all",
        target=fetch,
        over=research.output["urls"],
        inputs={"url": dagent.item},
    )
    summarize_node = dagent.Node("summarize", target=summarize, inputs={"pages": fetch_all.output})
    escalate_node = dagent.Node("escalate", target=escalate, inputs={"topic": dag.input})
    refine = dagent.LoopNode(
        "refine",
        body=polish_dag(),
        until=dagent.item == "page:topic/a + page:topic/b!!!",
        max_iterations=5,
        input=summarize_node.output,
    )

    dag.add_node(research)
    dag.add_node(fetch_all)
    dag.add_node(summarize_node)
    dag.add_node(escalate_node)
    dag.add_node(refine)
    # Branch: fan out only when confident, otherwise escalate; skips cascade.
    dag.add_edge(research, fetch_all, when=research.output["score"] >= 0.5)
    dag.add_edge(research, escalate_node, when=research.output["score"] < 0.5)
    dag.add_edge(fetch_all, summarize_node)
    dag.add_edge(summarize_node, refine)
    return dag


async def main() -> None:
    dag = build_dag()
    dagent.validate_dag_spec(dag.to_dag_spec())

    runner = dagent.Runner(provider=MockProvider([]))
    result = await runner.run(dag, graph_input="topic")

    print(result.status)
    print(result.node_value("fetch_all"))
    print(result.trace.dag_node_traces()["escalate"].status)
    print(result.node_value("refine"))
    runner.close()


if __name__ == "__main__":
    asyncio.run(main())
