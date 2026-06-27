from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from api.app import app, state
from api.python_tools import load_python_tool_sources
from dagent.config import (
    UserDagentConfig,
    UserPythonToolConfig,
    load_user_config,
    save_user_config,
)


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


def test_python_tool_loader_reloads_existing_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    module_file.write_text(
        "from dagent import tool\n"
        "@tool(description='Second version')\n"
        "def hello(name: str) -> str:\n"
        "    return f'v2 {name}'\n",
        encoding="utf-8",
    )
    second = load_python_tool_sources([config], user_config_dir=tmp_path)

    assert first.bindings[0].definition.description == "First version"
    assert second.bindings[0].definition.description == "Second version"


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
