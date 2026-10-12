"""Register tools and skills at runtime.

Run from the repository root:

    uv run python -m examples.runtime_registration_and_skills
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


@dagent.tool
def summarize(text: str) -> str:
    """Return a short summary."""

    return text.split(".")[0].strip()


async def main() -> None:
    with TemporaryDirectory(prefix="dagent-runtime-") as workspace:
        workspace_path = Path(workspace)
        skill_root = workspace_path / "team-skills"
        skill_root.mkdir()
        (skill_root / "SKILL.md").write_text(
            "---\nname: terse\ndescription: Prefer short answers.\n---\nKeep responses brief.",
            encoding="utf-8",
        )

        provider = MockProvider(
            [
                ChatResponse(
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="skill_view",
                            arguments={"name": "terse"},
                        )
                    ]
                ),
                ChatResponse(
                    tool_calls=[
                        ToolCall(
                            id="call_2",
                            name="tool_summarize",
                            arguments={"text": "One sentence. Another sentence."},
                        )
                    ]
                ),
                ChatResponse(content="One sentence"),
            ]
        )

        runner = dagent.Runner(
            workspace=workspace_path,
            runtime_directory=".runtime",
            provider=provider,
        )
        runner.add_tool(summarize)
        runner.add_skill_root(skill_root)

        agent = dagent.ToolAgent(profile="conversation", skills=["terse"])
        result = await runner.run(
            agent,
            input="Summarize the text.",
        )

        store = dagent.SkillStore(
            roots=[skill_root],
            managed_root=workspace_path / "managed-skills",
        )
        installed = store.install(
            "# Drafting\n\nUse direct language.",
            name="drafting",
            description="Draft with direct language.",
            category="writing",
        )
        package = io.BytesIO()
        # Count wrappers and internal directories together: at most 10 levels.
        with ZipFile(package, "w") as archive:
            archive.writestr(
                "export/reference-guide/SKILL.md",
                "---\nname: reference-guide\ndescription: Consult the style guide.\n---\nRead references/style.md.",
            )
            archive.writestr("export/reference-guide/references/style.md", "Use direct language.")
        installed_package = store.install(package.getvalue(), filename="reference-guide.zip")

        print(result.output_text)
        print(runner.skill_store.view("terse").name)
        print(installed.skill.qualified_name)
        print(installed_package.linked_files)
        runner.close()


if __name__ == "__main__":
    asyncio.run(main())
