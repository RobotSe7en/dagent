"""Progressive-disclosure skill capabilities and local skill storage."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

import yaml

from dagent.capabilities.catalog import CapabilityCatalog
from dagent.schemas import CapabilityDefinition, CapabilityInvocation, CapabilityPolicy, CapabilityResult


MAX_SKILL_PACKAGE_BYTES = 10 * 1024 * 1024
MAX_SKILL_PACKAGE_FILES = 256
MAX_SKILL_PACKAGE_FILE_BYTES = 1024 * 1024


class SkillStoreError(ValueError):
    """Raised when local skill storage cannot satisfy a request."""


class SkillNotFoundError(SkillStoreError):
    """Raised when a requested skill cannot be found."""


class SkillAmbiguousError(SkillStoreError):
    """Raised when a skill query matches more than one skill."""

    def __init__(self, name: str, matches: list[SkillEntry]) -> None:
        super().__init__(f"Skill '{name}' is ambiguous.")
        self.matches = matches


class SkillPermissionError(SkillStoreError):
    """Raised when a skill store operation targets a non-managed skill."""


@dataclass(frozen=True)
class SkillEntry:
    name: str
    description: str
    category: str | None
    skill_dir: Path
    skill_file: Path
    metadata: dict[str, Any]
    managed: bool = False

    def as_list_item(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "path": str(self.skill_file),
            "managed": self.managed,
        }

    @property
    def qualified_name(self) -> str:
        return f"{self.category}/{self.name}" if self.category else self.name


@dataclass(frozen=True)
class SkillView:
    skill: SkillEntry
    content: str
    path: Path
    metadata: dict[str, Any]
    linked_files: dict[str, list[str]]
    file_path: str | None = None

    @property
    def name(self) -> str:
        return self.skill.name

    @property
    def description(self) -> str:
        return self.skill.description

    @property
    def category(self) -> str | None:
        return self.skill.category

    @property
    def skill_dir(self) -> Path:
        return self.skill.skill_dir

    def as_payload(self) -> dict[str, Any]:
        payload = {
            "skill": self.skill.as_list_item(),
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "path": str(self.path),
            "skill_dir": str(self.skill_dir),
            "metadata": self.metadata,
            "content": self.content,
            "linked_files": self.linked_files,
        }
        if self.file_path is not None:
            payload["file_path"] = self.file_path
        return payload


class SkillStore:
    """Filesystem-backed skill listing, reading, installing, and deletion."""

    def __init__(
        self,
        roots: list[str | Path] | None = None,
        *,
        managed_root: str | Path | None = None,
    ) -> None:
        self.roots = [Path(root) for root in (roots if roots is not None else default_skill_roots())]
        self.managed_root = Path(managed_root) if managed_root is not None else default_managed_skill_root()

    def list(self) -> list[SkillEntry]:
        return scan_skill_roots(self.roots, managed_root=self.managed_root)

    def view(self, name: str, file_path: str | None = None) -> SkillView:
        skill = self._resolve_skill(name)
        if file_path:
            target = _resolve_skill_file(skill, file_path)
            return SkillView(
                skill=skill,
                content=target.read_text(encoding="utf-8"),
                path=target,
                metadata={},
                linked_files={},
                file_path=Path(file_path).as_posix(),
            )
        metadata, body = read_skill_markdown(skill.skill_file)
        return SkillView(
            skill=skill,
            content=body,
            path=skill.skill_file,
            metadata=metadata,
            linked_files=list_linked_files(skill.skill_dir),
        )

    def install(
        self,
        content: bytes | str,
        *,
        filename: str | None = None,
        name: str | None = None,
        description: str | None = None,
        category: str | None = None,
    ) -> SkillView:
        self._ensure_managed_root()
        if _is_zip_install(content, filename):
            if name or description or category:
                raise SkillStoreError("Skill package metadata overrides are only supported for Markdown installs.")
            return self._install_package(_ensure_bytes(content), filename or "skill.zip")
        return self._install_markdown(
            _ensure_text(content),
            filename=filename,
            name=name,
            description=description,
            category=category,
        )

    def delete(self, name: str) -> None:
        self._ensure_managed_root()
        skill = self._resolve_skill(name)
        managed_root = self.managed_root.resolve()
        skill_dir = skill.skill_dir.resolve()
        if not skill.managed or not _is_relative_to(skill_dir, managed_root) or skill_dir == managed_root:
            raise SkillPermissionError("Only skills installed under the managed skill root can be deleted.")
        shutil.rmtree(skill_dir)
        parent = skill_dir.parent
        if parent != managed_root:
            try:
                parent.rmdir()
            except OSError:
                pass

    def exists(self, qualified_name: str) -> bool:
        return any(skill.qualified_name == qualified_name for skill in self.list())

    def _resolve_skill(self, name: str) -> SkillEntry:
        matches = _find_skill_matches(name, self.list())
        if len(matches) > 1:
            raise SkillAmbiguousError(name, matches)
        if not matches:
            raise SkillNotFoundError(f"Skill '{name}' was not found.")
        return matches[0]

    def _install_markdown(
        self,
        content: str,
        *,
        filename: str | None,
        name: str | None,
        description: str | None,
        category: str | None,
    ) -> SkillView:
        metadata, body = read_skill_markdown_text(content)
        filename_name = _filename_skill_name(filename)
        resolved_name = _clean_name(name or metadata.get("name") or filename_name, field="Skill name")
        resolved_category = _clean_optional_name(
            category if category is not None else metadata.get("category"),
            field="Skill category",
        )
        resolved_description = _skill_description(
            body,
            description=description,
            metadata_description=metadata.get("description"),
        )
        normalized_metadata = dict(metadata)
        normalized_metadata["name"] = resolved_name
        normalized_metadata["description"] = resolved_description
        if resolved_category:
            normalized_metadata["category"] = resolved_category
        elif "category" in normalized_metadata:
            normalized_metadata.pop("category", None)

        destination = self._skill_destination(resolved_name, resolved_category)
        qualified_name = f"{resolved_category}/{resolved_name}" if resolved_category else resolved_name
        self._ensure_install_available(destination, qualified_name)
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text(
            _format_skill_markdown(normalized_metadata, body),
            encoding="utf-8",
        )
        return self._view_installed(destination)

    def _install_package(self, content: bytes, filename: str) -> SkillView:
        if len(content) > MAX_SKILL_PACKAGE_BYTES:
            raise SkillStoreError("Skill package is too large.")
        try:
            with ZipFile(io.BytesIO(content)) as archive:
                members = _skill_package_members(archive)
                skill_md_member = next((member for member, path in members if path.as_posix() == "SKILL.md"), None)
                if skill_md_member is None:
                    raise SkillStoreError("Skill package must contain SKILL.md.")
                metadata, _body = read_skill_markdown_text(archive.read(skill_md_member).decode("utf-8"))
                package_name = Path(filename).stem
                name = _clean_name(metadata.get("name") or package_name, field="Skill name")
                category = _clean_optional_name(metadata.get("category"), field="Skill category")
                qualified_name = f"{category}/{name}" if category else name
                destination = self._skill_destination(name, category)
                self._ensure_install_available(destination, qualified_name)
                self.managed_root.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=".skill-install-", dir=self.managed_root) as temp:
                    staged = Path(temp) / "skill"
                    staged.mkdir()
                    staged_root = staged.resolve()
                    for member, relative_path in members:
                        target = (staged / relative_path).resolve()
                        if not _is_relative_to(target, staged_root):
                            raise SkillStoreError("Skill package contains an unsafe path.")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(archive.read(member))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(staged), str(destination))
                return self._view_installed(destination)
        except UnicodeDecodeError as exc:
            raise SkillStoreError("SKILL.md must be UTF-8 text.") from exc
        except BadZipFile as exc:
            raise SkillStoreError("Skill package is not a valid zip file.") from exc

    def _skill_destination(self, name: str, category: str | None) -> Path:
        managed_root = self.managed_root.resolve()
        base = managed_root / category if category else managed_root
        destination = (base / name).resolve()
        if not _is_relative_to(destination, managed_root):
            raise SkillStoreError("Skill destination must stay inside the managed skill root.")
        return destination

    def _ensure_install_available(self, destination: Path, qualified_name: str) -> None:
        if self.exists(qualified_name):
            raise SkillStoreError(f"Skill '{qualified_name}' collides with an existing skill.")
        if destination.exists():
            raise SkillStoreError(f"Skill directory already exists: {destination}")

    def _view_installed(self, skill_dir: Path) -> SkillView:
        resolved_dir = skill_dir.resolve()
        skill_file = (resolved_dir / "SKILL.md").resolve()
        metadata, body = read_skill_markdown(skill_file)
        skill = SkillEntry(
            name=str(metadata.get("name") or resolved_dir.name),
            description=str(metadata.get("description") or ""),
            category=_category_for(self.managed_root.resolve(), skill_file),
            skill_dir=resolved_dir,
            skill_file=skill_file,
            metadata=metadata,
            managed=True,
        )
        return SkillView(
            skill=skill,
            content=body,
            path=skill_file,
            metadata=metadata,
            linked_files=list_linked_files(resolved_dir),
        )

    def _ensure_managed_root(self) -> None:
        managed = self.managed_root.resolve()
        if not any(root.resolve() == managed for root in self.roots):
            self.roots.append(self.managed_root)


class SkillsCapabilityProvider:
    """Registers the low-risk skill discovery tools."""

    def __init__(self, skill_roots: list[str | Path] | None = None) -> None:
        self.store = SkillStore(skill_roots)

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
                config={"roots": [str(root) for root in self.store.roots]},
            ),
            self._list,
            supports_context=True,
        )
        catalog.register(
            CapabilityDefinition(
                id="skill.view",
                name="skill_view",
                kind="skill",
                description=(
                    "Read a skill's SKILL.md or a linked file inside the skill directory. "
                    "If scripts are present, inspect them here and run them through the tool.shell system shell."
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
                config={"roots": [str(root) for root in self.store.roots]},
            ),
            self._view,
            supports_context=True,
        )

    def _list(
        self,
        invocation: CapabilityInvocation,
        *,
        context: Any = None,
        callbacks: Any = None,
    ) -> CapabilityResult:
        category = invocation.arguments.get("category")
        try:
            skills = _visible_skills(self.store.list(), _context_skills(context))
        except SkillStoreError as exc:
            return _failed(invocation, str(exc), _error_payload(exc))
        if category:
            skills = [skill for skill in skills if skill.category == str(category)]
        payload = {
            "skills": [skill.as_list_item() for skill in skills],
            "categories": sorted({skill.category for skill in skills if skill.category}),
            "count": len(skills),
        }
        return _completed(invocation, payload)

    def _view(
        self,
        invocation: CapabilityInvocation,
        *,
        context: Any = None,
        callbacks: Any = None,
    ) -> CapabilityResult:
        name = str(invocation.arguments.get("name", ""))
        visible_names = _context_skills(context)
        try:
            if visible_names is not None:
                visible = _visible_skills(self.store.list(), visible_names)
                matches = _find_skill_matches(name, visible)
                if not matches:
                    return _failed(
                        invocation,
                        f"Skill '{name}' is not visible.",
                        {"type": "skill_not_visible", "name": name},
                    )
                if len(matches) > 1:
                    raise SkillAmbiguousError(name, matches)
                name = matches[0].qualified_name
            view = self.store.view(
                name,
                file_path=invocation.arguments.get("file_path"),
            )
        except SkillStoreError as exc:
            return _failed(invocation, str(exc), _error_payload(exc))
        return _completed(invocation, view.as_payload())


def default_managed_skill_root() -> Path:
    return Path.home() / ".dagent" / "skills"


def default_skill_roots() -> list[Path]:
    return [Path("skills"), default_managed_skill_root()]


def scan_skill_roots(skill_roots: list[str | Path], *, managed_root: str | Path | None = None) -> list[SkillEntry]:
    entries: list[SkillEntry] = []
    seen: set[Path] = set()
    managed = Path(managed_root).resolve() if managed_root is not None else None
    for root_value in skill_roots:
        root = Path(root_value)
        for skill_file in _skill_files(root):
            resolved = skill_file.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            metadata, _body = read_skill_markdown(skill_file)
            name = str(metadata.get("name") or skill_file.parent.name)
            description = str(metadata.get("description") or "")
            skill_dir = skill_file.parent.resolve()
            entries.append(
                SkillEntry(
                    name=name,
                    description=description,
                    category=_category_for(root, skill_file),
                    skill_dir=skill_dir,
                    skill_file=resolved,
                    metadata=metadata,
                    managed=managed is not None and _is_relative_to(skill_dir, managed),
                )
            )
    return sorted(entries, key=lambda skill: (skill.category or "", skill.name, str(skill.skill_file)))


def read_skill_markdown(path: Path) -> tuple[dict[str, Any], str]:
    return read_skill_markdown_text(path.read_text(encoding="utf-8"))


def read_skill_markdown_text(text: str) -> tuple[dict[str, Any], str]:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            try:
                metadata = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError as exc:
                raise SkillStoreError("Skill frontmatter is invalid YAML.") from exc
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


def _find_skill_matches(name: str, skills: list[SkillEntry]) -> list[SkillEntry]:
    query = name.strip()
    matches: list[SkillEntry] = []
    for skill in skills:
        candidates = {skill.name, skill.qualified_name, skill.skill_dir.name}
        if query in candidates:
            matches.append(skill)
    return matches


def _context_skills(context: Any) -> tuple[str, ...] | None:
    value = getattr(context, "skills", None)
    if value is None:
        return None
    return tuple(str(skill) for skill in value)


def _visible_skills(skills: list[SkillEntry], visible_names: tuple[str, ...] | None) -> list[SkillEntry]:
    if visible_names is None:
        return skills
    visible: list[SkillEntry] = []
    seen_paths: set[Path] = set()
    for name in visible_names:
        matches = _find_skill_matches(name, skills)
        if not matches:
            raise SkillNotFoundError(name)
        if len(matches) > 1:
            raise SkillAmbiguousError(name, matches)
        skill = matches[0]
        if skill.skill_file not in seen_paths:
            seen_paths.add(skill.skill_file)
            visible.append(skill)
    return visible


def _resolve_skill_file(skill: SkillEntry, file_path: str) -> Path:
    requested = Path(file_path)
    if requested.is_absolute() or ".." in requested.parts:
        raise SkillStoreError("Path traversal is not allowed for skill files.")
    target = (skill.skill_dir / requested).resolve()
    if not _is_relative_to(target, skill.skill_dir):
        raise SkillStoreError("Path traversal is not allowed for skill files.")
    if not target.is_file():
        raise SkillNotFoundError(f"Skill file '{file_path}' was not found.")
    return target


def _is_zip_install(content: bytes | str, filename: str | None) -> bool:
    return isinstance(content, bytes) and bool(filename and filename.lower().endswith(".zip"))


def _ensure_bytes(content: bytes | str) -> bytes:
    return content if isinstance(content, bytes) else content.encode("utf-8")


def _ensure_text(content: bytes | str) -> str:
    if isinstance(content, str):
        return content
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillStoreError("Skill Markdown must be UTF-8 text.") from exc


def _filename_skill_name(filename: str | None) -> str:
    if not filename:
        return ""
    return Path(filename).stem


def _skill_package_members(archive: ZipFile) -> list[tuple[Any, Path]]:
    files = [member for member in archive.infolist() if not member.is_dir()]
    if len(files) > MAX_SKILL_PACKAGE_FILES:
        raise SkillStoreError("Skill package contains too many files.")
    raw_paths: list[tuple[Any, Path]] = []
    for member in files:
        if member.file_size > MAX_SKILL_PACKAGE_FILE_BYTES:
            raise SkillStoreError(f"Skill package file is too large: {member.filename}")
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            raise SkillStoreError(f"Skill package contains a symlink: {member.filename}")
        text_path = member.filename.replace("\\", "/")
        path = Path(text_path)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise SkillStoreError(f"Skill package contains an unsafe path: {member.filename}")
        raw_paths.append((member, path))
    if not raw_paths:
        raise SkillStoreError("Skill package is empty.")
    if any(path.as_posix() == "SKILL.md" for _member, path in raw_paths):
        members = raw_paths
    else:
        roots = {path.parts[0] for _member, path in raw_paths}
        if len(roots) != 1:
            raise SkillStoreError("Skill package must contain SKILL.md at the root or inside one top-level directory.")
        members = [
            (member, Path(*path.parts[1:]))
            for member, path in raw_paths
            if len(path.parts) > 1
        ]
    seen: set[str] = set()
    normalized: list[tuple[Any, Path]] = []
    for member, path in members:
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise SkillStoreError(f"Skill package contains an unsafe path: {member.filename}")
        key = path.as_posix()
        if key in seen:
            raise SkillStoreError(f"Skill package contains duplicate path: {key}")
        seen.add(key)
        normalized.append((member, path))
    return normalized


def _clean_name(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SkillStoreError(f"{field} is required.")
    if text in {".", ".."}:
        raise SkillStoreError(f"{field} cannot be a dot path segment.")
    if "/" in text or "\\" in text:
        raise SkillStoreError(f"{field} cannot contain path separators.")
    return text


def _clean_optional_name(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in {".", ".."}:
        raise SkillStoreError(f"{field} cannot be a dot path segment.")
    if "/" in text or "\\" in text:
        raise SkillStoreError(f"{field} cannot contain path separators.")
    return text


def _skill_description(body: str, *, description: str | None, metadata_description: Any) -> str:
    if description is not None and description.strip():
        return description.strip()
    if metadata_description:
        return str(metadata_description)
    for line in body.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:160]
    return ""


def _format_skill_markdown(metadata: dict[str, Any], body: str) -> str:
    frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{frontmatter}\n---\n{body.strip()}\n"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _error_payload(exc: SkillStoreError) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": str(exc)}
    if isinstance(exc, SkillAmbiguousError):
        payload["matches"] = [match.as_list_item() for match in exc.matches]
    return payload


def _completed(invocation: CapabilityInvocation, payload: dict[str, Any]) -> CapabilityResult:
    return CapabilityResult.completed(
        invocation,
        json.dumps(payload, ensure_ascii=False),
        value=payload,
        policy_decision=invocation.boundary.policy_decision(),
    )


def _failed(invocation: CapabilityInvocation, error: str, payload: dict[str, Any]) -> CapabilityResult:
    return CapabilityResult.failed(
        invocation,
        error,
        stop_reason="skill_error",
        content=json.dumps(payload, ensure_ascii=False),
        policy_decision=invocation.boundary.policy_decision(),
    )


__all__ = [
    "SkillAmbiguousError",
    "SkillEntry",
    "SkillNotFoundError",
    "SkillPermissionError",
    "SkillStore",
    "SkillStoreError",
    "SkillView",
    "SkillsCapabilityProvider",
    "default_managed_skill_root",
    "default_skill_roots",
    "list_linked_files",
    "scan_skill_roots",
]
