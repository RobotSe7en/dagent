"""Prompt assembly for profile-backed agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dagent.profiles import AgentProfile
from dagent.schemas import CapabilityDefinition


@dataclass(frozen=True)
class PromptRequest:
    profile: AgentProfile
    task_content: str
    tools: list[CapabilityDefinition] = field(default_factory=list)
    context: str = ""
    variables: dict[str, Any] = field(default_factory=dict)
    workspace_path: str | Path | None = None


class PromptBuilder:
    """Builds provider messages from a profile prompt and dynamic context."""

    def build(self, request: PromptRequest) -> list[dict[str, str]]:
        return [
            self.build_system_message(request),
            self.build_user_message(request.task_content, request.variables),
        ]

    def build_system_message(self, request: PromptRequest) -> dict[str, str]:
        system_sections = []
        system_sections.append(request.profile.render())
        if request.workspace_path is not None:
            system_sections.append(_runtime_context_section(request.workspace_path))
        if request.tools:
            system_sections.append(_tools_section(request.tools))
        if request.context:
            system_sections.append(_named_section("Context", request.context))

        return {"role": "system", "content": "\n\n".join(s for s in system_sections if s).strip()}

    def build_user_message(
        self,
        task_content: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        return {"role": "user", "content": _render_template(task_content, variables or {})}


def _tools_section(tools: list[CapabilityDefinition]) -> str:
    lines = ["## Available Tools"]
    for tool in tools:
        args = _parameter_names(tool.parameters)
        args_text = f" Args: {', '.join(args)}." if args else ""
        lines.append(
            f"- {tool.name} ({tool.kind}, id: {tool.id}): "
            f"{tool.description or 'No description.'}{args_text}"
        )
    return "\n".join(lines)


def _parameter_names(parameters: dict[str, Any] | None) -> list[str]:
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    if not isinstance(properties, dict):
        return []
    return [str(name) for name in properties]


def _named_section(title: str, content: str) -> str:
    return f"## {title}\n{content.strip()}"


def _runtime_context_section(workspace_path: str | Path) -> str:
    workspace_root = Path(workspace_path).expanduser().resolve()
    return _named_section(
        "Runtime Context",
        (
            f"- Workspace root: {workspace_root}\n"
            "- Resolve relative file paths from this workspace root."
        ),
    )


def _render_template(template: str, values: dict[str, Any]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{ " + key + " }}", str(value))
        rendered = rendered.replace("{{" + key + "}}", str(value))
    return rendered
