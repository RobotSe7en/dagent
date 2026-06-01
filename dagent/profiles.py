"""Load editable single-file Markdown agent profiles."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel

BUILTIN_PROFILE_NAMES = frozenset({
    "conversation",
    "dag_agent",
    "validator_agent",
    "feedback_learner",
})


class AgentProfile(BaseModel):
    name: str
    description: str = ""
    content: str

    def render(self) -> str:
        return self.content.strip()


class ProfileStore:
    """Filesystem-backed profile store.

    Each profile is a single Markdown file named ``<profile>.md``.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def load(self, name: str) -> AgentProfile:
        requested = Path(name)
        if requested.suffix == ".md":
            path = requested if requested.is_absolute() else self.directory / requested
        else:
            path = self.directory / f"{name}.md"
        content = path.read_text(encoding="utf-8")
        profile_name = path.stem
        return AgentProfile(
            name=profile_name,
            description=_markdown_title(content),
            content=content,
        )

    def list_names(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(path.stem for path in self.directory.glob("*.md"))


def _markdown_title(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def load_builtin_profile(name: str) -> AgentProfile:
    if name not in BUILTIN_PROFILE_NAMES:
        raise FileNotFoundError(f"Built-in profile '{name}' not found.")
    resource = files("dagent.resources.profiles").joinpath(f"{name}.md")
    content = resource.read_text(encoding="utf-8")
    return AgentProfile(
        name=name,
        description=_markdown_title(content),
        content=content,
    )


def list_builtin_profiles() -> list[AgentProfile]:
    return [load_builtin_profile(name) for name in sorted(BUILTIN_PROFILE_NAMES)]
