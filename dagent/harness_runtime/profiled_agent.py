"""Generic LLM-backed agent role loaded from an editable profile."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from dagent.profiles import AgentProfile
from dagent.providers import ChatProvider
from dagent.harness_runtime.execution_budget import reserve_model_turn
from dagent.state import PromptBuilder, PromptRequest


class ProfiledAgent:
    """Runs a single profile-defined LLM role."""

    def __init__(
        self,
        *,
        provider: ChatProvider,
        profile: AgentProfile,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        self.provider = provider
        self.profile = profile
        self.prompt_builder = prompt_builder or PromptBuilder()

    async def run_text(
        self,
        *,
        task_content: str,
        workspace_path: str | Path | None = None,
        **prompt_values: Any,
    ) -> str:
        reserve_model_turn()
        response = await self.provider.chat(
            self.prompt_builder.build(
                PromptRequest(
                    profile=self.profile,
                    task_content=task_content,
                    workspace_path=workspace_path,
                    variables=prompt_values,
                )
            )
        )
        return response.content

    async def run_json(
        self,
        *,
        task_content: str,
        workspace_path: str | Path | None = None,
        **prompt_values: Any,
    ) -> dict[str, Any]:
        return extract_json_object(
            await self.run_text(
                task_content=task_content,
                workspace_path=workspace_path,
                **prompt_values,
            )
        )


def extract_json_object(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = _decode_first_json_object(stripped)

    if not isinstance(parsed, dict):
        raise ValueError("Agent response JSON must be an object.")
    return parsed


def _decode_first_json_object(content: str) -> Any:
    decoder = json.JSONDecoder()
    cursor = 0
    while True:
        start = content.find("{", cursor)
        if start == -1:
            raise ValueError("Agent response did not contain a JSON object.")
        try:
            parsed, _ = decoder.raw_decode(content[start:])
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        return parsed
