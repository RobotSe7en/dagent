"""Read a real MCP image result. Run: uv run --extra mcp python -m examples.image_mcp"""

import asyncio
import sys
from contextlib import closing
from tempfile import TemporaryDirectory

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall
from examples.image_uploads import sample_image


def serve() -> None:
    from mcp.server.fastmcp import FastMCP, Image

    server = FastMCP("image-example")

    @server.tool()
    def screenshot() -> Image:
        return Image(data=sample_image(), format="png")

    server.run(transport="stdio")


async def main() -> None:
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="capture", name="mcp_images_screenshot", arguments={})]),
        ChatResponse(content="The MCP screenshot was submitted as a visual input."),
    ])
    with TemporaryDirectory() as workspace, closing(dagent.Runner(provider=provider, workspace=workspace)) as runner:
        runner.add_mcp_server("images", {
            "command": sys.executable,
            "args": ["-m", "examples.image_mcp", "--server"],
            "image_results_as_input": True,
        })
        result = await runner.run(
            dagent.ToolAgent(profile="conversation", capabilities=["mcp.images.screenshot"]),
            input="Take a screenshot and inspect it.",
        )
        assert result.conversation.items[-2].images
        print(result.output_text)


if __name__ == "__main__":
    if "--server" in sys.argv:
        serve()
    else:
        asyncio.run(main())
