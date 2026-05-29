import asyncio
import io
import json
from zipfile import ZipFile

import pytest

from dagent.capabilities import CapabilityCatalog
from dagent.capabilities.skills import (
    SkillPermissionError,
    SkillStore,
    SkillsCapabilityProvider,
    scan_skill_roots,
)
from dagent.harness_runtime import CapabilityExecutor
from dagent.schemas import CapabilityInvocation


def run(coro):
    return asyncio.run(coro)


def test_skills_provider_registers_progressive_disclosure_tools(tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "writing" / "summarize"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: summarize\ndescription: Summarize carefully.\n---\nUse concise summaries.",
        encoding="utf-8",
    )
    (skill_dir / "references" / "style.md").write_text("Prefer short prose.", encoding="utf-8")
    catalog = CapabilityCatalog()
    executor = CapabilityExecutor(catalog)

    SkillsCapabilityProvider([tmp_path / "skills"]).register_into(catalog)

    assert catalog.ids() == {"skill.list", "skill.view"}
    list_result = run(executor.execute(
        CapabilityInvocation(capability_id="skill.list", kind="skill")
    ))
    view_result = run(executor.execute(
        CapabilityInvocation(
            capability_id="skill.view",
            kind="skill",
            arguments={"name": "summarize"},
        )
    ))
    reference_result = run(executor.execute(
        CapabilityInvocation(
            capability_id="skill.view",
            kind="skill",
            arguments={"name": "summarize", "file_path": "references/style.md"},
        )
    ))

    listed = json.loads(list_result.content)
    viewed = json.loads(view_result.content)
    reference = json.loads(reference_result.content)
    assert listed["skills"] == [
        {
            "name": "summarize",
            "description": "Summarize carefully.",
            "category": "writing",
            "path": str(skill_dir / "SKILL.md"),
            "managed": False,
        }
    ]
    assert viewed["linked_files"] == {"references": ["references/style.md"]}
    assert viewed["skill_dir"] == str(skill_dir.resolve())
    assert "Use concise summaries." in viewed["content"]
    assert reference["content"] == "Prefer short prose."
    assert reference["skill_dir"] == str(skill_dir.resolve())


def test_skill_view_rejects_ambiguous_names_and_path_traversal(tmp_path) -> None:
    for category in ("a", "b"):
        skill_dir = tmp_path / "skills" / category / "summarize"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: summarize\ndescription: Duplicate.\n---\nBody.",
            encoding="utf-8",
        )
    catalog = CapabilityCatalog()
    executor = CapabilityExecutor(catalog)
    SkillsCapabilityProvider([tmp_path / "skills"]).register_into(catalog)

    ambiguous = run(executor.execute(
        CapabilityInvocation(
            capability_id="skill.view",
            kind="skill",
            arguments={"name": "summarize"},
        )
    ))
    traversal = run(executor.execute(
        CapabilityInvocation(
            capability_id="skill.view",
            kind="skill",
            arguments={"name": "a/summarize", "file_path": "../secret.txt"},
        )
    ))

    assert ambiguous.status == "failed"
    assert "ambiguous" in (ambiguous.error or "")
    assert traversal.status == "failed"
    assert "Path traversal" in (traversal.error or "")


def test_scan_skill_roots_matches_api_shape(tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "summarize"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: summarize\ndescription: Summarize text.\n---\nBody.",
        encoding="utf-8",
    )

    skills = scan_skill_roots([tmp_path / "skills"])

    assert [skill.as_list_item() for skill in skills] == [
        {
            "name": "summarize",
            "description": "Summarize text.",
            "category": None,
            "path": str(skill_dir / "SKILL.md"),
            "managed": False,
        }
    ]


def test_skill_store_installs_markdown_skill(tmp_path) -> None:
    managed_root = tmp_path / "managed"
    store = SkillStore([managed_root], managed_root=managed_root)

    view = store.install(
        "# Draft Skill\n\nUse this installed instruction.",
        name="draft",
        description="Draft skill.",
        category="writing",
    )

    assert view.skill.qualified_name == "writing/draft"
    assert view.skill.managed is True
    assert view.skill.skill_file == (managed_root / "writing" / "draft" / "SKILL.md").resolve()
    assert view.content.startswith("# Draft Skill")
    assert store.view("writing/draft").content.startswith("# Draft Skill")


def test_skill_store_installs_zip_package_with_linked_files(tmp_path) -> None:
    managed_root = tmp_path / "managed"
    store = SkillStore([managed_root], managed_root=managed_root)
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(
            "bundle/SKILL.md",
            "---\nname: summarize\ndescription: Summarize with helper.\ncategory: writing\n---\nUse helper.",
        )
        archive.writestr("bundle/scripts/run.py", "print('ok')\n")

    view = store.install(buffer.getvalue(), filename="bundle.zip")
    script = store.view("writing/summarize", file_path="scripts/run.py")

    assert view.linked_files == {"scripts": ["scripts/run.py"]}
    assert script.content == "print('ok')\n"


def test_skill_store_deletes_only_managed_skills(tmp_path) -> None:
    local_dir = tmp_path / "skills" / "local"
    local_dir.mkdir(parents=True)
    (local_dir / "SKILL.md").write_text("---\nname: local\ndescription: Local.\n---\nBody.", encoding="utf-8")
    managed_root = tmp_path / "managed"
    store = SkillStore([tmp_path / "skills", managed_root], managed_root=managed_root)
    store.install("Managed body.", name="managed")

    store.delete("managed")

    assert not (managed_root / "managed").exists()
    with pytest.raises(SkillPermissionError):
        store.delete("local")
