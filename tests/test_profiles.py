from pathlib import Path

import pytest

from dagent.profiles import ProfileStore, list_builtin_profiles, load_builtin_profile


def test_profile_store_loads_markdown_profile(tmp_path: Path) -> None:
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "dag_agent.md").write_text(
        "# DAG Agent\n\nGenerate compact PlanSpec DSL.",
        encoding="utf-8",
    )

    profile = ProfileStore(profiles_dir).load("dag_agent")

    assert profile.name == "dag_agent"
    assert profile.description == "DAG Agent"
    assert profile.render() == "# DAG Agent\n\nGenerate compact PlanSpec DSL."


def test_profile_store_saves_and_deletes_markdown_profile(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles")

    profile = store.save("analyst", "# Analyst\n\nRead carefully.")

    assert profile.name == "analyst"
    assert profile.description == "Analyst"
    assert store.list_names() == ["analyst"]
    assert store.load("analyst").content == "# Analyst\n\nRead carefully."

    store.delete("analyst")

    assert store.list_names() == []


def test_profile_store_rejects_path_like_profile_names(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles")

    for name in ("../outside", "nested/profile", "/tmp/profile.md", r"nested\profile", "profile.txt"):
        with pytest.raises(ValueError):
            store.load(name)


def test_dag_agent_prompt_requires_typed_planning_actions() -> None:
    prompt = load_builtin_profile("dag_agent").content

    assert "propose_plan" in prompt
    assert "no_change" in prompt
    assert "final_answer" in prompt
    assert "stable capability `id`" in prompt
    assert "Value AST" in prompt


def test_dag_agent_prompt_does_not_ask_for_reserved_dag_start() -> None:
    prompt = load_builtin_profile("dag_agent").content

    assert "dag_start" not in prompt


def test_conversation_prompt_prefers_relevant_available_tools() -> None:
    prompt = load_builtin_profile("conversation").content

    assert "General-Purpose Agent" in prompt
    assert "When an available tool directly fulfills the user's request" in prompt
    assert "Do not call a tool merely because it is available" in prompt
    assert "single\n  tool calls" not in prompt
    assert "Prefer direct tool use or a direct answer over `dag_agent`" in prompt
    assert "Prefer direct answers unless DAG" not in prompt


def test_builtin_profiles_load_from_package_resources() -> None:
    profile = load_builtin_profile("conversation")

    assert profile.name == "conversation"
    assert "General-Purpose Agent" in profile.content
    assert "conversation" in {profile.name for profile in list_builtin_profiles()}
