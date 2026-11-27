"""Read an existing workspace image. Run: uv run python -m examples.image_reading"""

import asyncio
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall
from examples.image_uploads import sample_image


async def main() -> None:
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="read", name="tool_read_image", arguments={"path": "screenshot.png"})]),
        ChatResponse(content="The image tool returned a visual input."),
    ])
    with TemporaryDirectory() as workspace, closing(dagent.Runner(provider=provider, workspace=workspace)) as runner:
        Path(workspace, "screenshot.png").write_bytes(sample_image())
        runner.add_image_tools()
        result = await runner.run(
            dagent.ToolAgent(profile="conversation", capabilities=["tool.read_image"]),
            input="View screenshot.png.", workspace_path=workspace,
        )
        assert result.conversation.items[-2].images
        print(result.output_text)


if __name__ == "__main__":
    asyncio.run(main())
