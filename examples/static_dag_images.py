"""Explicitly read an artifact in an Agent node. Run: uv run python -m examples.static_dag_images"""

import asyncio
from contextlib import closing
from tempfile import TemporaryDirectory

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall
from examples.image_uploads import sample_image


async def main() -> None:
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="read", name="tool_read_image", arguments={"path": "inputs/screenshot.png"})]),
        ChatResponse(content="The declared artifact was read as an image."),
    ])
    viewer = dagent.ToolAgent(name="viewer", profile="conversation", capabilities=["tool.read_image"])
    graph = dagent.Dag("inspect_image")
    image = graph.artifact("image", "inputs/screenshot.png")
    node = graph.add_node(dagent.Node("describe", target=viewer,
                                     inputs={"prompt": "Read inputs/screenshot.png and describe it."},
                                     artifact_inputs=[image]))
    graph.output = node.content
    with TemporaryDirectory() as workspace, closing(dagent.Runner(provider=provider, workspace=workspace)) as runner:
        runner.add_image_tools()
        result = await runner.run(graph, artifact_uploads={"image": [
            dagent.ArtifactUpload("screenshot.png", sample_image()),
        ]})
        assert result.status == "completed"
        print(result.node_output("describe"))


if __name__ == "__main__":
    asyncio.run(main())
