from pathlib import Path

from dagent.profiles import ProfileStore


def test_profile_store_loads_yaml_profile(tmp_path: Path) -> None:
    profiles_dir = tmp_path / "profiles"
    dag_creator_dir = profiles_dir / "dag_creator"
    dag_creator_dir.mkdir(parents=True)
    (dag_creator_dir / "profile.yaml").write_text(
        "\n".join(
            [
                "name: dag_creator",
                "role: dag_creator",
                "description: Test dag_creator",
                "layers:",
                "  - soul.md",
                "  - agent.md",
                "memory_file: memory.md",
                "output_format: json",
            ]
        ),
        encoding="utf-8",
    )
    (dag_creator_dir / "soul.md").write_text("soul text", encoding="utf-8")
    (dag_creator_dir / "agent.md").write_text("agent text", encoding="utf-8")
    (dag_creator_dir / "memory.md").write_text("memory text", encoding="utf-8")

    profile = ProfileStore(profiles_dir).load("dag_creator")

    assert profile.name == "dag_creator"
    assert profile.role == "dag_creator"
    assert profile.render_layers() == ["soul text", "agent text"]
    assert profile.memory == "memory text"
    assert profile.output_format == "json"
