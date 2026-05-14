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
        return [
            self.build_system_message(request),
            self.build_user_message(request.task_content, request.variables),
        ]

    def build_system_message(self, request: PromptRequest) -> dict[str, str]:
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

        return {"role": "system", "content": "\n\n".join(s for s in system_sections if s).strip()}

    def build_user_message(
        self,
        task_content: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        return {"role": "user", "content": _render_template(task_content, variables or {})}

    def build_initial_messages(
        self,
        *,
        system_message: dict[str, str],
        conversation_history: list[dict[str, Any]] | None,
        current_user_message: dict[str, str],
        runtime_context: str = "",
    ) -> list[dict[str, Any]]:
        user_message = dict(current_user_message)
        if runtime_context.strip():
            user_message["content"] = _append_runtime_context(
                user_message.get("content", ""),
                runtime_context,
            )
        return [
            dict(system_message),
            *_conversation_messages(conversation_history or []),
            user_message,
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


def _conversation_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role in {"user", "assistant"} and content:
            history.append({"role": role, "content": content})
    return history[-20:]


def _append_runtime_context(content: str, runtime_context: str) -> str:
    context = runtime_context.strip()
    if not context:
        return content
    return f"{content.strip()}\n\n## Runtime Context\n{context}" if content.strip() else f"## Runtime Context\n{context}"
