import pytest

from dagent.capabilities.bootstrap import create_default_capability_catalog
from dagent.capabilities.skills import SkillsCapabilityProvider
from dagent.schemas import Boundary, CapabilityInvocation


def test_create_default_rejects_both_skill_roots_and_provider(tmp_path) -> None:
    with pytest.raises(ValueError, match="not both"):
        create_default_capability_catalog(
            workspace_root=tmp_path,
            skill_roots=[tmp_path / "skills"],
            skills_provider=SkillsCapabilityProvider([tmp_path / "skills"]),
        )


def test_default_capability_catalog_registers_base_capabilities(tmp_path) -> None:
    catalog = create_default_capability_catalog(workspace_root=tmp_path)

    assert {"tool.read_file", "memory.write", "memory.search", "file.read", "file.write"}.issubset(
        catalog.ids()
    )
    assert "tool.dag_start" not in catalog.ids()


def test_default_file_capabilities_publish_argument_schemas(tmp_path) -> None:
    catalog = create_default_capability_catalog(workspace_root=tmp_path)

    read_definition = catalog.get("file.read")
    write_definition = catalog.get("file.write")

    assert read_definition is not None
    assert write_definition is not None
    assert read_definition.parameters["properties"]["path"]["type"] == "string"
    assert read_definition.parameters["required"] == ["path"]
    assert write_definition.parameters["properties"]["content"]["type"] == "string"
    assert write_definition.parameters["required"] == ["path", "content"]


def test_default_capability_catalog_uses_workspace_root_for_file_capabilities(tmp_path) -> None:
    catalog = create_default_capability_catalog(workspace_root=tmp_path)
    write_entry = catalog.get_entry("file.write")
    read_entry = catalog.get_entry("file.read")
    assert write_entry is not None
    assert read_entry is not None

    write_result = write_entry.handler(
        CapabilityInvocation(
            capability_id="file.write",
            kind="file",
            arguments={"path": "notes.txt", "content": "saved"},
            boundary=Boundary(mode="write_limited", allowed_paths=["."]),
        )
    )
    read_result = read_entry.handler(
        CapabilityInvocation(
            capability_id="file.read",
            kind="file",
            arguments={"path": "notes.txt"},
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
        )
    )

    assert write_result.status == "completed"
    assert read_result.content == "saved"


def test_default_capability_catalogs_do_not_share_memory_state(tmp_path) -> None:
    first = create_default_capability_catalog(workspace_root=tmp_path)
    second = create_default_capability_catalog(workspace_root=tmp_path)
    first_write = first.get_entry("memory.write")
    second_search = second.get_entry("memory.search")
    assert first_write is not None
    assert second_search is not None

    first_write.handler(
        CapabilityInvocation(
            capability_id="memory.write",
            kind="memory",
            arguments={"key": "color", "value": "blue"},
        )
    )
    result = second_search.handler(
        CapabilityInvocation(
            capability_id="memory.search",
            kind="memory",
            arguments={"query": "color"},
        )
    )

    assert result.content == ""
