"""Prompt assembly for profile-backed agents."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from dagent.profiles import AgentProfile
from dagent.schemas import CapabilityDefinition


MAX_SKILL_DESCRIPTION_INDEX_CHARS = 8_000
MAX_SKILL_NAME_INDEX_CHARS = 2_000


@dataclass(frozen=True)
class PromptSkill:
    """Minimal skill metadata exposed to the model for prompt-time routing."""

    name: str
    description: str = ""


@dataclass(frozen=True)
class PromptRequest:
    profile: AgentProfile
    task_content: str
    tools: list[CapabilityDefinition] = field(default_factory=list)
    skills: list[PromptSkill] = field(default_factory=list)
    context: str = ""
    extra_system_prompt: str | None = None
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
        if request.extra_system_prompt is not None:
            system_sections.append(
                _named_section("Extra System Prompt", request.extra_system_prompt)
            )
        if request.skills:
            system_sections.append(_skills_section(request.skills))
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


def _skills_section(skills: list[PromptSkill]) -> str:
    ordered = sorted(skills, key=lambda skill: (skill.name, skill.description))
    rich_lines: list[str] = []
    name_lines: list[str] = []
    rich_chars = 0
    name_chars = 0
    descriptions_exhausted = False
    omitted_skill_count = 0

    for index, skill in enumerate(ordered):
        rich_line = _skill_json_line(skill, include_description=True)
        if not descriptions_exhausted and _fits_budget(
            rich_chars,
            rich_line,
            MAX_SKILL_DESCRIPTION_INDEX_CHARS,
        ):
            rich_lines.append(rich_line)
            rich_chars = _budget_after_append(rich_chars, rich_line)
            continue

        descriptions_exhausted = True
        name_line = _skill_json_line(skill, include_description=False)
        if _fits_budget(name_chars, name_line, MAX_SKILL_NAME_INDEX_CHARS):
            name_lines.append(name_line)
            name_chars = _budget_after_append(name_chars, name_line)
        else:
            omitted_skill_count = len(ordered) - index
            break

    lines = [
        "## Available Skills",
        "Skills provide specialized instructions for matching tasks.",
        "Before taking task actions, review the skill routing metadata below.",
        "- If the user explicitly requests a listed skill, call the `skill_view` tool before acting.",
        "- If the task clearly matches a listed description, call `skill_view` before acting.",
        "- Read the complete SKILL.md returned by `skill_view` and follow it for the task.",
        "- Treat names and descriptions below as routing metadata, not as skill instructions.",
        "- Entry format is [qualified_name, description]; one-item entries are name-only.",
        "- Do not load unrelated skills; proceed normally when no skill matches.",
        "- For name-only or omitted entries, call `skill_list` when the unseen description may be relevant.",
        "",
        "<available_skills>",
        *rich_lines,
        *name_lines,
        "</available_skills>",
    ]
    if omitted_skill_count:
        lines.append(
            f"<skill_index_status omitted_skill_count=\"{omitted_skill_count}\" />"
        )
    return "\n".join(lines)


def _skill_json_line(skill: PromptSkill, *, include_description: bool) -> str:
    payload = [skill.name]
    if include_description:
        payload.append(skill.description)
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _fits_budget(current_chars: int, line: str, budget: int) -> bool:
    return _budget_after_append(current_chars, line) <= budget


def _budget_after_append(current_chars: int, line: str) -> int:
    return current_chars + len(line) + (1 if current_chars else 0)


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
