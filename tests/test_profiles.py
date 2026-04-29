from pathlib import Path

from dagent.profiles import ProfileStore


def test_profile_store_loads_yaml_profile(tmp_path: Path) -> None:
    profiles_dir = tmp_path / "profiles"
    planner_dir = profiles_dir / "planner"
    planner_dir.mkdir(parents=True)
    (planner_dir / "profile.yaml").write_text(
        "\n".join(
            [
                "name: planner",
                "role: planner",
                "description: Test planner",
                "layers:",
                "  - soul.md",
                "  - agent.md",
                "memory_file: memory.md",
                "output_format: json",
            ]
        ),
        encoding="utf-8",
    )
    (planner_dir / "soul.md").write_text("soul text", encoding="utf-8")
    (planner_dir / "agent.md").write_text("agent text", encoding="utf-8")
    (planner_dir / "memory.md").write_text("memory text", encoding="utf-8")

    profile = ProfileStore(profiles_dir).load("planner")

    assert profile.name == "planner"
    assert profile.role == "planner"
    assert profile.render_layers() == ["soul text", "agent text"]
    assert profile.memory == "memory text"
    assert profile.output_format == "json"
