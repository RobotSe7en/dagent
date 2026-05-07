from pathlib import Path

from dagent.profiles import ProfileStore


def test_profile_store_loads_yaml_profile(tmp_path: Path) -> None:
    profiles_dir = tmp_path / "profiles"
    dag_agent_dir = profiles_dir / "dag_agent"
    dag_agent_dir.mkdir(parents=True)
    (dag_agent_dir / "profile.yaml").write_text(
        "\n".join(
            [
                "name: dag_agent",
                "role: dag_agent",
                "description: Test dag_agent",
                "layers:",
                "  - soul.md",
                "  - agent.md",
                "memory_file: memory.md",
                "output_format: json",
            ]
        ),
        encoding="utf-8",
    )
    (dag_agent_dir / "soul.md").write_text("soul text", encoding="utf-8")
    (dag_agent_dir / "agent.md").write_text("agent text", encoding="utf-8")
    (dag_agent_dir / "memory.md").write_text("memory text", encoding="utf-8")

    profile = ProfileStore(profiles_dir).load("dag_agent")

    assert profile.name == "dag_agent"
    assert profile.role == "dag_agent"
    assert profile.render_layers() == ["soul text", "agent text"]
    assert profile.memory == "memory text"
    assert profile.output_format == "json"
