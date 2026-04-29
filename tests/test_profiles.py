from pathlib import Path

from dagent.profiles import ProfileStore


def test_profile_store_loads_yaml_profile(tmp_path: Path) -> None:
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "planner.yaml").write_text(
        "\n".join(
            [
                "name: planner",
                "role: planner",
                "description: Test planner",
                "system_prompt: system text",
                "user_prompt: 'Task {{ task_id }}: {{ user_request }}'",
            ]
        ),
        encoding="utf-8",
    )

    profile = ProfileStore(profiles_dir).load("planner")

    assert profile.name == "planner"
    assert profile.role == "planner"
    assert profile.system_prompt == "system text"
    assert profile.render_user_prompt(task_id="t1", user_request="hello") == (
        "Task t1: hello"
    )

