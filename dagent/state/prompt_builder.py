"""Prompt assembly for profile-backed agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dagent.profiles import AgentProfile
from dagent.tools.registry import Tool


@dataclass(frozen=True)
class PromptRequest:
    profile: AgentProfile
    task_content: str
    tools: list[Tool] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    memory: str = ""
    context: str = ""
    variables: dict[str, Any] = field(default_factory=dict)


class PromptBuilder:
    """Builds provider messages from stable profile layers and dynamic context."""

    def build(self, request: PromptRequest) -> list[dict[str, str]]:
        system_sections = []
        system_sections.extend(request.profile.render_layers())
        if request.tools:
            system_sections.append(_tools_section(request.tools))
        if request.skills:
            system_sections.append(_skills_section(request.skills))
        if request.memory:
            system_sections.append(_named_section("Memory", request.memory))
        if request.context:
            system_sections.append(_named_section("Context", request.context))

        return [
            {"role": "system", "content": "\n\n".join(s for s in system_sections if s).strip()},
            {"role": "user", "content": _render_template(request.task_content, request.variables)},
        ]


def _tools_section(tools: list[Tool]) -> str:
    lines = ["## Available Tools"]
    for tool in tools:
        lines.append(f"- {tool.name}: {tool.description or 'No description.'}")
    return "\n".join(lines)


def _skills_section(skills: list[str]) -> str:
    return "\n".join(["## Skills", *[f"- {skill}" for skill in skills]])


def _named_section(title: str, content: str) -> str:
    return f"## {title}\n{content.strip()}"


def _render_template(template: str, values: dict[str, Any]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{ " + key + " }}", str(value))
        rendered = rendered.replace("{{" + key + "}}", str(value))
    return rendered
