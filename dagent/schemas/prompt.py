"""Host-supplied system prompt extension contracts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


PromptExtensionTarget = Literal[
    "tool_agent",
    "dag_planner",
    "registered_agent",
]

MAX_PROMPT_EXTENSION_CONTENT_CHARS = 16_384
MAX_PROMPT_EXTENSIONS_TOTAL_CONTENT_CHARS = 32_768
_DEFAULT_PROMPT_EXTENSION_TARGETS: tuple[PromptExtensionTarget, ...] = (
    "dag_planner",
    "registered_agent",
    "tool_agent",
)
_PROMPT_EXTENSION_TARGETS = frozenset(_DEFAULT_PROMPT_EXTENSION_TARGETS)
_PROMPT_EXTENSION_ID_RE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"
)


class PromptExtension(BaseModel):
    """Trusted, already-rendered host Markdown for selected model surfaces."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=128)
    content: str = Field(
        min_length=1,
        max_length=MAX_PROMPT_EXTENSION_CONTENT_CHARS,
    )
    targets: tuple[PromptExtensionTarget, ...] = _DEFAULT_PROMPT_EXTENSION_TARGETS

    @field_validator("id", mode="before")
    @classmethod
    def normalize_id(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Prompt extension ids must be strings.")
        extension_id = value.strip().lower()
        if not _PROMPT_EXTENSION_ID_RE.fullmatch(extension_id):
            raise ValueError(
                "Prompt extension ids must use lowercase letters, numbers, "
                "dots, underscores, or hyphens separated by alphanumeric segments."
            )
        return extension_id

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Prompt extension content must be a string.")
        if not value.strip():
            raise ValueError("Prompt extension content must not be empty.")
        if len(value) > MAX_PROMPT_EXTENSION_CONTENT_CHARS:
            raise ValueError(
                "Prompt extension content exceeds the maximum size of "
                f"{MAX_PROMPT_EXTENSION_CONTENT_CHARS} characters."
            )
        return value

    @field_validator("targets", mode="before")
    @classmethod
    def normalize_targets(
        cls,
        value: Any,
    ) -> tuple[PromptExtensionTarget, ...]:
        if isinstance(value, str):
            raise ValueError(
                "Prompt extension targets must be a collection of target names."
            )
        raw_targets = tuple(value or ())
        if any(not isinstance(target, str) for target in raw_targets):
            raise ValueError("Prompt extension targets must be strings.")
        targets = tuple(
            sorted({target.strip().lower() for target in raw_targets})
        )
        if not targets:
            raise ValueError("Prompt extension targets must not be empty.")
        unknown = sorted(set(targets) - _PROMPT_EXTENSION_TARGETS)
        if unknown:
            raise ValueError(
                "Unknown prompt extension targets: " + ", ".join(unknown) + "."
            )
        return targets  # type: ignore[return-value]


def normalize_prompt_extensions(
    value: Iterable[PromptExtension | Mapping[str, Any]] | None,
) -> tuple[PromptExtension, ...]:
    """Validate one canonical, bounded extension collection."""

    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)):
        raise ValueError(
            "prompt_extensions must be a collection of PromptExtension values."
        )
    extensions = tuple(
        item
        if isinstance(item, PromptExtension)
        else PromptExtension.model_validate(item)
        for item in value
    )
    id_counts = Counter(extension.id for extension in extensions)
    duplicates = sorted({
        extension_id
        for extension_id, count in id_counts.items()
        if count > 1
    })
    if duplicates:
        raise ValueError(
            "Prompt extension ids must be unique: " + ", ".join(duplicates) + "."
        )
    total_content_chars = sum(len(extension.content) for extension in extensions)
    if total_content_chars > MAX_PROMPT_EXTENSIONS_TOTAL_CONTENT_CHARS:
        raise ValueError(
            "Prompt extension content exceeds the total maximum size of "
            f"{MAX_PROMPT_EXTENSIONS_TOTAL_CONTENT_CHARS} characters."
        )
    return tuple(sorted(extensions, key=lambda extension: extension.id))


def prompt_extensions_for_target(
    extensions: Iterable[PromptExtension],
    target: PromptExtensionTarget,
) -> tuple[PromptExtension, ...]:
    """Return canonical extensions applicable to one model surface."""

    return tuple(
        extension
        for extension in normalize_prompt_extensions(extensions)
        if target in extension.targets
    )


__all__ = [
    "PromptExtension",
    "PromptExtensionTarget",
]
