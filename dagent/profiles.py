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
        profile_name = _checked_profile_name(name)
        path = self.directory / f"{profile_name}.md"
        content = path.read_text(encoding="utf-8")
        return AgentProfile(
            name=profile_name,
            description=_markdown_title(content),
            content=content,
        )

    def list_names(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(path.stem for path in self.directory.glob("*.md"))


def _checked_profile_name(name: str) -> str:
    raw_name = str(name).strip()
    if not raw_name:
        raise ValueError("Profile name must not be empty.")
    if "/" in raw_name or "\\" in raw_name:
        raise ValueError("Profile name must not contain path separators.")
    requested = Path(raw_name)
    if requested.is_absolute() or requested.name in {".", ".."}:
        raise ValueError("Profile name must not be a filesystem path.")
    if requested.suffix and requested.suffix != ".md":
        raise ValueError("Profile files must use the .md extension.")
    profile_name = requested.stem if requested.suffix == ".md" else requested.name
    if not profile_name or profile_name in {".", ".."}:
        raise ValueError("Profile name must not be a filesystem path.")
    return profile_name


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
