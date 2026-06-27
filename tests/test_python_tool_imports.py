from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

import api.app as app_module
import api.python_tools as python_tools_module
import dagent.config as config_module
from api.app import app, state
from api.python_tools import load_python_tool_sources
from dagent.config import (
    UserDagentConfig,
    UserPythonToolConfig,
    load_user_config,
    save_user_config,
)
from dagent.providers import MockProvider
from dagent.runner import Runner


@pytest.fixture(autouse=True)
def reset_api_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state.close_runner()
    state.custom_capabilities.clear()
    state.custom_mcp_servers.clear()
    state.custom_mcp_conflicts.clear()
    state.custom_mcp_registered_names.clear()
    state.custom_mcp_errors.clear()
    state.custom_mcp_conflict_errors.clear()
    state.custom_model_providers.clear()
    state.active_model_id = None
    monkeypatch.setattr(state, "get_user_config_path", lambda: tmp_path / ".dagent" / "config.yaml")
    monkeypatch.setattr(state, "get_managed_python_tool_root", lambda: tmp_path / ".dagent" / "python-tools", raising=False)
    yield
    state.close_runner()


def test_user_config_persists_python_tools(tmp_path: Path) -> None:
    config_path = tmp_path / ".dagent" / "config.yaml"
    config = UserDagentConfig(
        python_tools=[
            UserPythonToolConfig(
                id="local_docs",
                source="path",
                path="/Users/olivia/tools/local_tools.py",
                names=["search_docs"],
                enabled=True,
            )
        ]
    )

    save_user_config(config, config_path)
    loaded = load_user_config(config_path)

    assert loaded.python_tools == config.python_tools
    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["python_tools"] == [
        {
            "id": "local_docs",
            "source": "path",
            "path": "/Users/olivia/tools/local_tools.py",
            "names": ["search_docs"],
            "enabled": True,
        }
    ]


def test_user_config_with_python_tool_errors_tolerates_only_python_tool_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / ".dagent" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    (config_path.parent / ".env").write_text("ENV_MODEL_API_KEY=dotenv-secret\n", encoding="utf-8")
    monkeypatch.delenv("ENV_MODEL_API_KEY", raising=False)
    config_path.write_text(
        "model_providers:\n"
        "  local:\n"
        "    base_url: https://example.test/v1\n"
        "    model: local-model\n"
        "    api_key_env: ENV_MODEL_API_KEY\n"
        "python_tools:\n"
        "  - id: bad-id\n"
        "    source: path\n"
        "    path: missing.py\n"
        "    names: [missing]\n",
        encoding="utf-8",
    )

    config, errors = config_module.load_user_config_with_python_tool_errors(config_path)

    assert config.model_providers["local"].api_key == "dotenv-secret"
    assert config.python_tools[0].id == "python_tool_1"
    assert errors == {
        "python_tool_1": "Python tool source ids may contain only letters, numbers, and underscores."
    }


def test_user_config_with_python_tool_errors_keeps_other_sections_strict(tmp_path: Path) -> None:
    config_path = tmp_path / ".dagent" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "model_providers:\n"
        "  broken:\n"
        "    model: local-model\n"
        "python_tools:\n"
        "  - id: bad-id\n"
        "    source: path\n"
        "    path: missing.py\n"
        "    names: [missing]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        config_module.load_user_config_with_python_tool_errors(config_path)


def test_python_tool_loader_imports_explicit_path_bindings(tmp_path: Path) -> None:
    script = tmp_path / "local_tools.py"
    script.write_text(
        "from dagent import tool\n"
        "@tool(description='Echo text')\n"
        "def echo_text(text: str) -> str:\n"
        "    return text\n",
        encoding="utf-8",
    )

    result = load_python_tool_sources(
        [UserPythonToolConfig(id="local", source="path", path=str(script), names=["echo_text"])],
        user_config_dir=tmp_path,
    )

    assert [binding.definition.id for binding in result.bindings] == ["tool.echo_text"]
    assert result.errors == {}


def test_python_tool_loader_reports_non_binding_export(tmp_path: Path) -> None:
    script = tmp_path / "bad_tools.py"
    script.write_text("def helper():\n    return 'not a tool'\n", encoding="utf-8")

    result = load_python_tool_sources(
        [UserPythonToolConfig(id="bad", source="path", path=str(script), names=["helper"])],
        user_config_dir=tmp_path,
    )

    assert result.bindings == []
    assert "CapabilityBinding" in result.errors["bad"]


def test_python_tool_loader_reports_import_errors(tmp_path: Path) -> None:
    result = load_python_tool_sources(
        [UserPythonToolConfig(id="missing", source="path", path=str(tmp_path / "missing.py"), names=["x"])],
        user_config_dir=tmp_path,
    )

    assert result.bindings == []
    assert "does not exist" in result.errors["missing"]


def test_python_tool_loader_fails_source_closed_on_duplicate_exports(tmp_path: Path) -> None:
    script = tmp_path / "local_tools.py"
    script.write_text(
        "from dagent import tool\n"
        "@tool(description='First')\n"
        "def first(text: str) -> str:\n"
        "    return text\n",
        encoding="utf-8",
    )

    result = load_python_tool_sources(
        [UserPythonToolConfig(id="local", source="path", path=str(script), names=["first", "first"])],
        user_config_dir=tmp_path,
    )

    assert result.bindings == []
    assert result.statuses[0].capability_ids == []
    assert "exported more than once" in result.errors["local"]


def test_python_tool_loader_imports_module_bindings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "tools.py").write_text(
        "from dagent import tool\n"
        "@tool(description='Hello')\n"
        "def hello(name: str) -> str:\n"
        "    return f'hello {name}'\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    result = load_python_tool_sources(
        [UserPythonToolConfig(id="package", source="module", module="pkg.tools", names=["hello"])],
        user_config_dir=tmp_path,
    )

    assert [binding.definition.id for binding in result.bindings] == ["tool.hello"]
    assert result.errors == {}


def test_python_tool_loader_reuses_existing_module_without_reload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_dir = tmp_path / "pkg_reload"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    module_file = package_dir / "tools.py"
    module_file.write_text(
        "from dagent import tool\n"
        "@tool(description='First version')\n"
        "def hello(name: str) -> str:\n"
        "    return f'v1 {name}'\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    config = UserPythonToolConfig(id="package", source="module", module="pkg_reload.tools", names=["hello"])

    first = load_python_tool_sources([config], user_config_dir=tmp_path)
    monkeypatch.setattr(
        python_tools_module.importlib,
        "reload",
        lambda module: (_ for _ in ()).throw(AssertionError("module sources must not reload existing modules")),
    )
    module_file.write_text(
        "from dagent import tool\n"
        "@tool(description='Second version')\n"
        "def hello(name: str) -> str:\n"
        "    return f'v2 {name}'\n",
        encoding="utf-8",
    )
    second = load_python_tool_sources([config], user_config_dir=tmp_path)

    assert first.bindings[0].definition.description == "First version"
    assert second.bindings[0].definition.description == "First version"
    assert second.errors == {}


def test_api_loads_user_python_tools_from_config(tmp_path: Path) -> None:
    script = tmp_path / "tools.py"
    script.write_text(
        "from dagent import tool\n"
        "@tool(description='Echo text')\n"
        "def echo_text(text: str) -> str:\n"
        "    return text\n",
        encoding="utf-8",
    )
    config_path = state.get_user_config_path()
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "python_tools:\n"
        "  - id: local\n"
        "    source: path\n"
        f"    path: {script}\n"
        "    names: [echo_text]\n",
        encoding="utf-8",
    )

    response = TestClient(app).get("/capabilities")

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["capabilities"]}
    assert "tool.echo_text" in ids


def test_api_reports_python_tool_errors_without_startup_failure(tmp_path: Path) -> None:
    config_path = state.get_user_config_path()
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "python_tools:\n"
        "  - id: bad\n"
        "    source: path\n"
        "    path: missing.py\n"
        "    names: [missing]\n",
        encoding="utf-8",
    )

    client = TestClient(app)
    listed = client.get("/python-tools")
    capabilities = client.get("/capabilities")

    assert capabilities.status_code == 200
    assert listed.status_code == 200
    assert listed.json()["tools"][0]["status"] == "error"
    assert "does not exist" in listed.json()["tools"][0]["error"]


def test_api_python_tool_reload_keeps_runner_instance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = tmp_path / "tools.py"
    script.write_text(
        "from dagent import tool\n"
        "@tool(description='Echo text')\n"
        "def echo_text(text: str) -> str:\n"
        "    return text\n",
        encoding="utf-8",
    )
    config_path = state.get_user_config_path()
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "python_tools:\n"
        "  - id: local\n"
        "    source: path\n"
        f"    path: {script}\n"
        "    names: [echo_text]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(state, "_create_runner", lambda: Runner(provider=MockProvider([])))
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/python-tools").status_code == 200
    original_runner = state.runner
    original_close_runner = state.close_runner

    def fail_close_runner() -> None:
        raise AssertionError("python tool reload must not close the runner")

    monkeypatch.setattr(state, "close_runner", fail_close_runner)
    response = client.post("/python-tools/reload")
    monkeypatch.setattr(state, "close_runner", original_close_runner)

    assert response.status_code == 200
    assert state.runner is original_runner


def test_api_python_tool_reload_reads_updated_user_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = tmp_path / "tools.py"
    script.write_text(
        "from dagent import tool\n"
        "@tool(description='Echo text')\n"
        "def echo_text(text: str) -> str:\n"
        "    return text\n",
        encoding="utf-8",
    )
    config_path = state.get_user_config_path()
    config_path.parent.mkdir(parents=True)
    save_user_config(UserDagentConfig(), config_path)
    monkeypatch.setattr(state, "_create_runner", lambda: Runner(provider=MockProvider([])))
    client = TestClient(app)

    assert client.get("/python-tools").json()["tools"] == []
    original_runner = state.runner
    config_path.write_text(
        "python_tools:\n"
        "  - id: local\n"
        "    source: path\n"
        f"    path: {script}\n"
        "    names: [echo_text]\n",
        encoding="utf-8",
    )

    response = client.post("/python-tools/reload")

    assert response.status_code == 200
    assert state.runner is original_runner
    assert response.json()["tools"][0]["id"] == "local"
    assert response.json()["tools"][0]["capabilities"] == ["tool.echo_text"]
    assert state.runner.get_capability("tool.echo_text") is not None


def test_api_delete_python_tool_reports_dependent_preset_error_without_rebuilding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = tmp_path / "tools.py"
    script.write_text(
        "from dagent import tool\n"
        "@tool(description='Echo text')\n"
        "def echo_text(text: str) -> str:\n"
        "    return text\n",
        encoding="utf-8",
    )
    config_path = state.get_user_config_path()
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "python_tools:\n"
        "  - id: local\n"
        "    source: path\n"
        f"    path: {script}\n"
        "    names: [echo_text]\n",
        encoding="utf-8",
    )
    agent_root = tmp_path / "agents"
    agent_root.mkdir()
    (agent_root / "helper.json").write_text(
        json.dumps({
            "name": "helper",
            "profile": "conversation",
            "description": "",
            "max_steps": 1,
            "capabilities": ["tool.echo_text"],
            "skills": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    monkeypatch.setattr(state, "_create_runner", lambda: Runner(provider=MockProvider([])))
    client = TestClient(app)

    assert client.get("/python-tools").status_code == 200
    original_runner = state.runner
    assert original_runner is not None
    assert original_runner.get_capability("agent.helper") is not None

    response = client.delete("/python-tools/local")

    assert response.status_code == 200
    assert state.runner is original_runner
    assert load_user_config(config_path).python_tools == []
    assert state.runner.get_capability("tool.echo_text") is None
    assert state.runner.get_capability("agent.helper") is None
    assert "helper" in state.agent_preset_errors
    assert "tool.echo_text" in state.agent_preset_errors["helper"]


def test_api_delete_python_tool_removes_multiple_dependent_presets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = tmp_path / "tools.py"
    script.write_text(
        "from dagent import tool\n"
        "@tool(description='Echo text')\n"
        "def echo_text(text: str) -> str:\n"
        "    return text\n",
        encoding="utf-8",
    )
    config_path = state.get_user_config_path()
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "python_tools:\n"
        "  - id: local\n"
        "    source: path\n"
        f"    path: {script}\n"
        "    names: [echo_text]\n",
        encoding="utf-8",
    )
    agent_root = tmp_path / "agents"
    agent_root.mkdir()
    for name in ("helper_one", "helper_two"):
        (agent_root / f"{name}.json").write_text(
            json.dumps({
                "name": name,
                "profile": "conversation",
                "description": "",
                "max_steps": 1,
                "capabilities": ["tool.echo_text"],
                "skills": [],
            }),
            encoding="utf-8",
        )
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    monkeypatch.setattr(state, "_create_runner", lambda: Runner(provider=MockProvider([])))
    client = TestClient(app)

    assert client.get("/python-tools").status_code == 200
    assert state.runner.get_capability("agent.helper_one") is not None
    assert state.runner.get_capability("agent.helper_two") is not None

    response = client.delete("/python-tools/local")

    assert response.status_code == 200
    assert state.runner.get_capability("tool.echo_text") is None
    assert state.runner.get_capability("agent.helper_one") is None
    assert state.runner.get_capability("agent.helper_two") is None
    assert "tool.echo_text" in state.agent_preset_errors["helper_one"]
    assert "tool.echo_text" in state.agent_preset_errors["helper_two"]


def test_api_python_tool_reload_replaces_changed_agent_preset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_script = tmp_path / "first_tools.py"
    first_script.write_text(
        "from dagent import tool\n"
        "@tool(description='First')\n"
        "def first_tool() -> str:\n"
        "    return 'first'\n",
        encoding="utf-8",
    )
    second_script = tmp_path / "second_tools.py"
    second_script.write_text(
        "from dagent import tool\n"
        "@tool(description='Second')\n"
        "def second_tool() -> str:\n"
        "    return 'second'\n",
        encoding="utf-8",
    )
    config_path = state.get_user_config_path()
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "python_tools:\n"
        "  - id: first\n"
        "    source: path\n"
        f"    path: {first_script}\n"
        "    names: [first_tool]\n"
        "  - id: second\n"
        "    source: path\n"
        f"    path: {second_script}\n"
        "    names: [second_tool]\n",
        encoding="utf-8",
    )
    agent_root = tmp_path / "agents"
    agent_root.mkdir()
    preset_path = agent_root / "helper.json"
    preset_path.write_text(
        json.dumps({
            "name": "helper",
            "profile": "conversation",
            "description": "",
            "max_steps": 1,
            "capabilities": ["tool.first_tool"],
            "skills": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    monkeypatch.setattr(state, "_create_runner", lambda: Runner(provider=MockProvider([])))
    client = TestClient(app)

    assert client.get("/python-tools").status_code == 200
    assert state.runner.get_capability("agent.helper") is not None
    preset_path.write_text(
        json.dumps({
            "name": "helper",
            "profile": "conversation",
            "description": "",
            "max_steps": 1,
            "capabilities": ["tool.second_tool"],
            "skills": [],
        }),
        encoding="utf-8",
    )

    response = client.post("/python-tools/reload")

    assert response.status_code == 200
    assert "helper" not in state.agent_preset_errors
    assert state.runner.get_capability("agent.helper") is not None


def test_api_delete_python_tool_removes_agent_created_through_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = tmp_path / "tools.py"
    script.write_text(
        "from dagent import tool\n"
        "@tool(description='Echo text')\n"
        "def echo_text(text: str) -> str:\n"
        "    return text\n",
        encoding="utf-8",
    )
    config_path = state.get_user_config_path()
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "python_tools:\n"
        "  - id: local\n"
        "    source: path\n"
        f"    path: {script}\n"
        "    names: [echo_text]\n",
        encoding="utf-8",
    )
    agent_root = tmp_path / "agents"
    monkeypatch.setattr(state, "get_agent_preset_root", lambda: agent_root)
    monkeypatch.setattr(state, "_create_runner", lambda: Runner(provider=MockProvider([])))
    client = TestClient(app)

    assert client.get("/python-tools").status_code == 200
    created = client.post(
        "/agents",
        json={
            "name": "helper",
            "profile": "conversation",
            "capabilities": ["tool.echo_text"],
            "skills": [],
        },
    )
    assert created.status_code == 200
    assert state.runner.get_capability("agent.helper") is not None

    deleted = client.delete("/python-tools/local")

    assert deleted.status_code == 200
    assert state.runner.get_capability("agent.helper") is None
    assert "helper" in state.agent_preset_errors
    assert "tool.echo_text" in state.agent_preset_errors["helper"]


def test_api_uploads_python_tool_file_and_persists_managed_config(tmp_path: Path) -> None:
    content = (
        "from dagent import tool\n"
        "@tool(description='Echo text')\n"
        "def echo_text(text: str) -> str:\n"
        "    return text\n"
    )

    client = TestClient(app)
    response = client.post(
        "/python-tools/upload",
        data={"id": "uploaded", "names": "echo_text"},
        files={"file": ("uploaded.py", content.encode("utf-8"), "text/x-python")},
    )

    assert response.status_code == 200
    payload = response.json()["tool"]
    assert payload["id"] == "uploaded"
    assert payload["source"] == "managed"
    assert payload["status"] == "loaded"
    assert payload["capabilities"] == ["tool.echo_text"]
    loaded = load_user_config(state.get_user_config_path())
    assert loaded.python_tools[0].source == "managed"
    assert loaded.python_tools[0].path == "python-tools/uploaded.py"
    assert (tmp_path / ".dagent" / "python-tools" / "uploaded.py").exists()


def test_api_upload_rolls_back_file_and_config_when_reload_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = state.get_user_config_path()
    config_path.parent.mkdir(parents=True)
    save_user_config(UserDagentConfig(), config_path)
    content = (
        "from dagent import tool\n"
        "@tool(description='Echo text')\n"
        "def echo_text(text: str) -> str:\n"
        "    return text\n"
    )

    def fail_reload() -> None:
        raise RuntimeError("reload failed")

    closed: list[bool] = []
    state.runner = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(app_module, "_reload_python_tools", fail_reload)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/python-tools/upload",
        data={"id": "uploaded", "names": "echo_text"},
        files={"file": ("uploaded.py", content.encode("utf-8"), "text/x-python")},
    )

    assert response.status_code == 500
    assert not (tmp_path / ".dagent" / "python-tools" / "uploaded.py").exists()
    assert load_user_config(config_path).python_tools == []
    assert closed == [True]
    assert state.runner is None


def test_api_updates_uploaded_python_tool_without_changing_managed_path(tmp_path: Path) -> None:
    content = (
        "from dagent import tool\n"
        "@tool(description='Echo text')\n"
        "def echo_text(text: str) -> str:\n"
        "    return text\n"
    )
    client = TestClient(app)
    uploaded = client.post(
        "/python-tools/upload",
        data={"id": "uploaded", "names": "echo_text"},
        files={"file": ("uploaded.py", content.encode("utf-8"), "text/x-python")},
    )
    assert uploaded.status_code == 200

    disabled = client.put(
        "/python-tools/uploaded",
        json={
            "id": "uploaded",
            "source": "managed",
            "path": "python-tools/uploaded.py",
            "names": ["echo_text"],
            "enabled": False,
        },
    )
    changed_path = client.put(
        "/python-tools/uploaded",
        json={
            "id": "uploaded",
            "source": "managed",
            "path": "config.yaml",
            "names": ["echo_text"],
            "enabled": False,
        },
    )

    assert disabled.status_code == 200
    assert disabled.json()["tool"]["status"] == "disabled"
    assert changed_path.status_code == 400


def test_api_rejects_json_managed_python_tool_sources(tmp_path: Path) -> None:
    response = TestClient(app).post(
        "/python-tools",
        json={
            "id": "bad",
            "source": "managed",
            "path": "config.yaml",
            "names": ["echo_text"],
            "enabled": True,
        },
    )

    assert response.status_code == 400
    assert "uploaded" in response.json()["detail"]


def test_api_delete_managed_python_tool_does_not_remove_user_config(tmp_path: Path) -> None:
    config_path = state.get_user_config_path()
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "model_providers:\n"
        "  local:\n"
        "    base_url: http://localhost:8000/v1\n"
        "    model: local\n"
        "python_tools:\n"
        "  - id: bad\n"
        "    source: managed\n"
        "    path: config.yaml\n"
        "    names: [missing]\n",
        encoding="utf-8",
    )

    response = TestClient(app).delete("/python-tools/bad")

    assert response.status_code == 200
    assert config_path.exists()
    assert "model_providers" in config_path.read_text(encoding="utf-8")


def test_api_upload_rejects_empty_names_without_writing_file(tmp_path: Path) -> None:
    response = TestClient(app).post(
        "/python-tools/upload",
        data={"id": "uploaded", "names": " , \n"},
        files={"file": ("uploaded.py", b"from dagent import tool\n", "text/x-python")},
    )

    assert response.status_code == 400
    assert not (tmp_path / ".dagent" / "python-tools" / "uploaded.py").exists()


def test_api_rejects_generic_mutation_of_imported_python_tool(tmp_path: Path) -> None:
    script = tmp_path / "tools.py"
    script.write_text(
        "from dagent import tool\n"
        "@tool(description='Echo text')\n"
        "def echo_text(text: str) -> str:\n"
        "    return text\n",
        encoding="utf-8",
    )
    config_path = state.get_user_config_path()
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "python_tools:\n"
        "  - id: local\n"
        "    source: path\n"
        f"    path: {script}\n"
        "    names: [echo_text]\n",
        encoding="utf-8",
    )

    client = TestClient(app)
    assert client.get("/capabilities").status_code == 200
    disabled = client.post("/capabilities/tool.echo_text/disable")
    deleted = client.delete("/capabilities/tool.echo_text")

    assert disabled.status_code == 400
    assert deleted.status_code == 400
    assert "Python tool source" in disabled.json()["detail"]


def test_api_python_tool_validation_rejects_existing_capability_name(tmp_path: Path) -> None:
    script = tmp_path / "tools.py"
    script.write_text(
        "from dagent import tool\n"
        "@tool(name='tool_read_file')\n"
        "def custom_search() -> str:\n"
        "    return 'search'\n",
        encoding="utf-8",
    )
    client = TestClient(app)

    response = client.post(
        "/python-tools/validate",
        json={
            "id": "local",
            "source": "path",
            "path": str(script),
            "names": ["custom_search"],
            "enabled": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()["tool"]
    assert payload["status"] == "error"
    assert payload["capabilities"] == []
    assert "Capability name 'tool_read_file' is already registered" in payload["error"]


def test_api_python_tool_source_fails_closed_on_partial_registration_error(tmp_path: Path) -> None:
    script = tmp_path / "tools.py"
    script.write_text(
        "from dagent import tool\n"
        "@tool\n"
        "def custom_ok() -> str:\n"
        "    return 'ok'\n"
        "@tool(name='tool_read_file')\n"
        "def custom_collision() -> str:\n"
        "    return 'collision'\n",
        encoding="utf-8",
    )
    config_path = state.get_user_config_path()
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "python_tools:\n"
        "  - id: local\n"
        "    source: path\n"
        f"    path: {script}\n"
        "    names: [custom_ok, custom_collision]\n",
        encoding="utf-8",
    )
    client = TestClient(app)

    listed = client.get("/python-tools")
    capabilities = client.get("/capabilities")

    assert listed.status_code == 200
    payload = listed.json()["tools"][0]
    assert payload["status"] == "error"
    assert payload["capabilities"] == []
    assert "Capability name 'tool_read_file' is already registered" in payload["error"]
    assert capabilities.status_code == 200
    ids = {item["id"] for item in capabilities.json()["capabilities"]}
    assert "tool.custom_ok" not in ids
    assert state.runner.get_capability("tool.custom_ok") is None


def test_api_reports_malformed_python_tool_config_without_startup_failure(tmp_path: Path) -> None:
    config_path = state.get_user_config_path()
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "python_tools:\n"
        "  - id: bad\n"
        "    source: nope\n"
        "    path: missing.py\n"
        "    names: [missing]\n",
        encoding="utf-8",
    )

    client = TestClient(app)
    listed = client.get("/python-tools")
    capabilities = client.get("/capabilities")

    assert listed.status_code == 200
    assert capabilities.status_code == 200
    assert listed.json()["tools"][0]["status"] == "error"
    assert "source" in listed.json()["tools"][0]["error"]


def test_api_reports_invalid_python_tool_source_id_as_manageable_placeholder(tmp_path: Path) -> None:
    config_path = state.get_user_config_path()
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "python_tools:\n"
        "  - id: bad-id\n"
        "    source: path\n"
        "    path: missing.py\n"
        "    names: [missing]\n",
        encoding="utf-8",
    )
    client = TestClient(app)

    listed = client.get("/python-tools")

    assert listed.status_code == 200
    payload = listed.json()["tools"][0]
    assert payload["id"] == "python_tool_1"
    assert payload["status"] == "error"
    assert "Python tool source ids" in payload["error"]

    deleted = client.delete("/python-tools/python_tool_1")

    assert deleted.status_code == 200
    assert client.get("/python-tools").json()["tools"] == []
