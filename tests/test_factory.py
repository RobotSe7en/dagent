from pathlib import Path

from dagent.config import DagentConfig, ProfilesConfig, ProviderConfig
from dagent.factory import create_harness_runtime


def test_factory_registers_profile_backed_agent_capabilities(tmp_path: Path) -> None:
    profile_root = tmp_path / "profiles"
    for name in ["conversation", "dag_agent", "validator_agent", "feedback_learner"]:
        profile_dir = profile_root / name
        profile_dir.mkdir(parents=True)
        (profile_dir / "profile.yaml").write_text(
            "\n".join([
                f"name: {name}",
                "role: agent",
                "layers:",
                "  - agent.md",
            ]),
            encoding="utf-8",
        )
        (profile_dir / "agent.md").write_text(f"You are {name}.", encoding="utf-8")

    runtime = create_harness_runtime(
        config=DagentConfig(
            provider=ProviderConfig(base_url="https://example.test/v1", model="test", api_key="test"),
            profiles=ProfilesConfig(directory=str(profile_root)),
        ),
        workspace_root=tmp_path,
    )

    conversation_agent = runtime.capability_catalog.get("agent.conversation")
    assert conversation_agent is not None
    assert conversation_agent.kind == "agent"
    assert conversation_agent.parameters["properties"]["prompt"]["type"] == "string"
    assert "agent.dag_agent" not in runtime.capability_catalog.ids()
