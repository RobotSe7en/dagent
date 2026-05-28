"""Progressive-disclosure skill capabilities."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from dagent.capabilities.catalog import CapabilityCatalog
from dagent.schemas import CapabilityDefinition, CapabilityInvocation, CapabilityPolicy, CapabilityResult


@dataclass(frozen=True)
class SkillIndexEntry:
    name: str
    description: str
    category: str | None
    skill_dir: Path
    skill_file: Path
    metadata: dict[str, Any]

    def as_list_item(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "path": str(self.skill_file),
        }

    @property
    def qualified_name(self) -> str:
        return f"{self.category}/{self.name}" if self.category else self.name


@dataclass(frozen=True)
class ImportedSkillEntry:
    name: str
    description: str
    category: str | None
    content: str
    metadata: dict[str, Any]

    def as_list_item(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "path": f"memory://skills/{self.qualified_name}",
        }

    @property
    def qualified_name(self) -> str:
        return f"{self.category}/{self.name}" if self.category else self.name


SkillEntry = SkillIndexEntry | ImportedSkillEntry


class SkillsCapabilityProvider:
    """Registers the low-risk skill discovery tools."""

    def __init__(
        self,
        skill_roots: list[str | Path] | None = None,
        *,
        imported_skills: Mapping[str, ImportedSkillEntry] | Iterable[ImportedSkillEntry] | None = None,
    ) -> None:
        self.skill_roots = [Path(root) for root in (skill_roots or default_skill_roots())]
        self.imported_skills = imported_skills

    def register_into(self, catalog: CapabilityCatalog) -> None:
        catalog.register(
            CapabilityDefinition(
                id="skill.list",
                name="skills_list",
                kind="skill",
                description="List locally configured skills.",
                parameters={
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "description": "Optional category filter."},
                    },
                },
                policy=CapabilityPolicy(risk="low"),
                config={"roots": [str(root) for root in self.skill_roots]},
            ),
            self._list,
        )
        catalog.register(
            CapabilityDefinition(
                id="skill.view",
                name="skill_view",
                kind="skill",
                description=(
                    "Read a skill's SKILL.md or a linked file inside the skill directory. "
                    "If scripts are present, inspect them here and run them through tool.run_command's system shell."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Skill name or category/name."},
                        "file_path": {"type": "string", "description": "Optional linked file path."},
                    },
                    "required": ["name"],
                },
                policy=CapabilityPolicy(risk="low"),
                config={"roots": [str(root) for root in self.skill_roots]},
            ),
            self._view,
        )

    def _list(self, invocation: CapabilityInvocation) -> CapabilityResult:
        category = invocation.arguments.get("category")
        skills = list_skill_entries(self.skill_roots, self.imported_skills)
        if category:
            skills = [skill for skill in skills if skill.category == str(category)]
        payload = {
            "skills": [skill.as_list_item() for skill in skills],
            "categories": sorted({skill.category for skill in skills if skill.category}),
            "count": len(skills),
        }
        return _completed(invocation, payload)

    def _view(self, invocation: CapabilityInvocation) -> CapabilityResult:
        return skill_view_result(
            invocation,
            self.skill_roots,
            str(invocation.arguments.get("name", "")),
            file_path=invocation.arguments.get("file_path"),
            imported_skills=self.imported_skills,
        )


def default_skill_roots() -> list[Path]:
    return [Path("skills"), Path.home() / ".dagent" / "skills"]


def scan_skill_roots(skill_roots: list[str | Path]) -> list[SkillIndexEntry]:
    entries: list[SkillIndexEntry] = []
    seen: set[Path] = set()
    for root_value in skill_roots:
        root = Path(root_value)
        candidates = _skill_files(root)
        for skill_file in candidates:
            resolved = skill_file.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            metadata, _body = read_skill_markdown(skill_file)
            name = str(metadata.get("name") or skill_file.parent.name)
            description = str(metadata.get("description") or "")
            entries.append(
                SkillIndexEntry(
                    name=name,
                    description=description,
                    category=_category_for(root, skill_file),
                    skill_dir=skill_file.parent.resolve(),
                    skill_file=resolved,
                    metadata=metadata,
                )
            )
    return sorted(entries, key=lambda skill: (skill.category or "", skill.name, str(skill.skill_file)))


def list_skill_entries(
    skill_roots: list[str | Path],
    imported_skills: Mapping[str, ImportedSkillEntry] | Iterable[ImportedSkillEntry] | None = None,
) -> list[SkillEntry]:
    entries: list[SkillEntry] = [*scan_skill_roots(skill_roots), *_imported_skill_values(imported_skills)]
    return sorted(entries, key=lambda skill: (skill.category or "", skill.name, skill.qualified_name))


def skill_view_payload(
    name: str,
    skill_roots: list[str | Path] | None = None,
    *,
    file_path: str | None = None,
    imported_skills: Mapping[str, ImportedSkillEntry] | Iterable[ImportedSkillEntry] | None = None,
) -> dict[str, Any]:
    matches = _find_skill_matches(name, list_skill_entries(skill_roots or default_skill_roots(), imported_skills))
    if len(matches) > 1:
        return {
            "success": False,
            "error": "Ambiguous skill name.",
            "matches": [match.as_list_item() for match in matches],
        }
    if not matches:
        return {"success": False, "error": f"Skill '{name}' was not found.", "matches": []}
    skill = matches[0]
    if isinstance(skill, ImportedSkillEntry):
        if file_path:
            return {
                "success": False,
                "error": "Imported in-memory skills do not include linked files.",
                "skill": skill.as_list_item(),
            }
        return {
            "success": True,
            "skill": skill.as_list_item(),
            "name": skill.name,
            "description": skill.description,
            "category": skill.category,
            "path": skill.as_list_item()["path"],
            "skill_dir": None,
            "metadata": skill.metadata,
            "content": skill.content,
            "linked_files": {},
        }
    if file_path:
        try:
            target = _resolve_skill_file(skill, file_path)
        except ValueError as exc:
            return {"success": False, "error": str(exc), "skill": skill.as_list_item()}
        return {
            "success": True,
            "skill": skill.as_list_item(),
            "file_path": str(Path(file_path).as_posix()),
            "path": str(target),
            "skill_dir": str(skill.skill_dir),
            "content": target.read_text(encoding="utf-8"),
        }
    metadata, body = read_skill_markdown(skill.skill_file)
    return {
        "success": True,
        "skill": skill.as_list_item(),
        "name": skill.name,
        "description": skill.description,
        "category": skill.category,
        "path": str(skill.skill_file),
        "skill_dir": str(skill.skill_dir),
        "metadata": metadata,
        "content": body,
        "linked_files": list_linked_files(skill.skill_dir),
    }


def skill_view_result(
    invocation: CapabilityInvocation,
    skill_roots: list[str | Path],
    name: str,
    *,
    file_path: str | None = None,
    imported_skills: Mapping[str, ImportedSkillEntry] | Iterable[ImportedSkillEntry] | None = None,
) -> CapabilityResult:
    payload = skill_view_payload(name, skill_roots, file_path=file_path, imported_skills=imported_skills)
    if payload.get("success") is False:
        return _failed(invocation, str(payload.get("error") or "Skill view failed."), payload)
    return _completed(invocation, payload)


def read_skill_markdown(path: Path) -> tuple[dict[str, Any], str]:
    return read_skill_markdown_text(path.read_text(encoding="utf-8"))


def read_skill_markdown_text(text: str) -> tuple[dict[str, Any], str]:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            metadata = yaml.safe_load(parts[1]) or {}
            return metadata if isinstance(metadata, dict) else {}, parts[2].strip()
    return {}, text.strip()


def list_linked_files(skill_dir: Path) -> dict[str, list[str]]:
    linked: dict[str, list[str]] = {}
    for folder in ("references", "templates", "assets", "scripts"):
        root = skill_dir / folder
        if not root.exists():
            continue
        files = [
            path.relative_to(skill_dir).as_posix()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        ]
        if files:
            linked[folder] = files
    return linked


def _skill_files(root: Path) -> list[Path]:
    if root.is_file() and root.name == "SKILL.md":
        return [root]
    if not root.exists():
        return []
    if (root / "SKILL.md").is_file():
        return [root / "SKILL.md"]
    return sorted(path for path in root.rglob("SKILL.md") if path.is_file())


def _category_for(root: Path, skill_file: Path) -> str | None:
    try:
        rel = skill_file.parent.relative_to(root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) >= 2:
        return parts[0]
    return None


def _imported_skill_values(
    imported_skills: Mapping[str, ImportedSkillEntry] | Iterable[ImportedSkillEntry] | None,
) -> list[ImportedSkillEntry]:
    if imported_skills is None:
        return []
    if isinstance(imported_skills, Mapping):
        return list(imported_skills.values())
    return list(imported_skills)


def _find_skill_matches(name: str, skills: list[SkillEntry]) -> list[SkillEntry]:
    query = name.strip()
    matches: list[SkillEntry] = []
    for skill in skills:
        candidates = {skill.name, skill.qualified_name}
        if isinstance(skill, SkillIndexEntry):
            candidates.add(skill.skill_dir.name)
        if query in candidates:
            matches.append(skill)
    return matches


def _resolve_skill_file(skill: SkillIndexEntry, file_path: str) -> Path:
    requested = Path(file_path)
    if requested.is_absolute() or ".." in requested.parts:
        raise ValueError("Path traversal is not allowed for skill files.")
    target = (skill.skill_dir / requested).resolve()
    try:
        target.relative_to(skill.skill_dir)
    except ValueError as exc:
        raise ValueError("Path traversal is not allowed for skill files.") from exc
    if not target.is_file():
        raise ValueError(f"Skill file '{file_path}' was not found.")
    return target


def _completed(invocation: CapabilityInvocation, payload: dict[str, Any]) -> CapabilityResult:
    return CapabilityResult(
        invocation_id=invocation.invocation_id,
        capability_id=invocation.capability_id,
        kind=invocation.kind,
        status="completed",
        content=json.dumps(payload, ensure_ascii=False),
        policy_decision=_policy_decision(invocation),
    )


def _failed(invocation: CapabilityInvocation, error: str, payload: dict[str, Any]) -> CapabilityResult:
    return CapabilityResult(
        invocation_id=invocation.invocation_id,
        capability_id=invocation.capability_id,
        kind=invocation.kind,
        status="failed",
        error=error,
        content=json.dumps(payload, ensure_ascii=False),
        stop_reason="skill_error",
        policy_decision=_policy_decision(invocation),
    )


def _policy_decision(invocation: CapabilityInvocation) -> dict[str, Any]:
    return {
        "boundary_mode": invocation.boundary.mode,
        "allowed_paths": invocation.boundary.allowed_paths or [],
        "allowed_commands": invocation.boundary.allowed_commands or [],
    }


__all__ = [
    "ImportedSkillEntry",
    "SkillIndexEntry",
    "SkillEntry",
    "SkillsCapabilityProvider",
    "default_skill_roots",
    "list_linked_files",
    "list_skill_entries",
    "read_skill_markdown_text",
    "scan_skill_roots",
    "skill_view_payload",
]
