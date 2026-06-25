"""Register tools and skills at runtime.

Run from the repository root:

    uv run python -m examples.runtime_registration_and_skills
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

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
                            name="tool_summarize",
                            arguments={"text": "One sentence. Another sentence."},
                        )
                    ]
                ),
                ChatResponse(content="One sentence"),
            ]
        )

        runner = dagent.Runner(workspace=workspace_path / ".dagent", provider=provider)
        runner.add_tool(summarize)
        runner.add_skill_root(skill_root)

        agent = dagent.ToolAgent(profile="conversation", skills=["terse"])
        result = await runner.run(
            agent,
            messages=[{"role": "user", "content": "Summarize the text."}],
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

        print(result.output_text)
        print(runner.skill_store.view("terse").name)
        print(installed.skill.qualified_name)
        runner.close()


if __name__ == "__main__":
    asyncio.run(main())
