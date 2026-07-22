import stat
from pathlib import Path

import yaml

import dagent
from dagent.config import load_config, resolve_config_relative_path


def test_load_config_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "provider:",
                '  base_url: "http://localhost:8000/v1"',
                '  model: "qwen3"',
                '  api_key: "local-key"',
                "  timeout_seconds: 12",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.provider.base_url == "http://localhost:8000/v1"
    assert config.provider.model == "qwen3"
    assert config.provider.api_key == "local-key"
    assert config.provider.timeout_seconds == 12
    assert config.provider.strip_thinking is False
    assert config.provider.structured_output_mode == "json_schema"
    assert config.provider.reasoning is None
    assert config.provider.extra_request_args == {}
    assert config.provider.extra_body == {}
    assert config.profiles.directory is None
    assert config.planner_frontend == "typed_spec"


def test_load_config_parses_sdk_builder_planner_frontend(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join([
            "provider:",
            '  base_url: "http://localhost:8000/v1"',
            '  model: "qwen3"',
            "planner_frontend: sdk_builder",
        ]),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.planner_frontend == "sdk_builder"

    runner = dagent.Runner.from_config(
        config_path,
        workspace=tmp_path / "workspace",
        mcp_stdio_stderr="inherit",
    )
    try:
        assert runner.runtime.dag_agent.loop.planner_frontend == "sdk_builder"
        assert runner.runtime.dag_agent.loop.planner_skill is not None
        assert runner._mcp_stdio_stderr == "inherit"
    finally:
        runner.close()


def test_resolve_config_relative_path_uses_config_directory(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"

    assert resolve_config_relative_path("profiles", config_path) == tmp_path / "profiles"


def test_load_config_resolves_api_key_from_dotenv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    (tmp_path / ".env").write_text("MINIMAX_API_KEY=secret-key\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "provider:",
                '  base_url: "https://api.minimaxi.com/v1"',
                '  model: "MiniMax-M2.1"',
                '  api_key_env: "MINIMAX_API_KEY"',
                "  timeout_seconds: 60",
                "  strip_thinking: true",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.provider.base_url == "https://api.minimaxi.com/v1"
    assert config.provider.model == "MiniMax-M2.1"
    assert config.provider.api_key == "secret-key"
    assert config.provider.strip_thinking is True


def test_load_config_parses_reasoning_and_extra_provider_options(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "provider:",
                '  base_url: "https://api.deepseek.com"',
                '  model: "deepseek-v4-pro"',
                '  api_key: "local-key"',
                "  structured_output_mode: json_object",
                "  reasoning:",
                "    enabled: true",
                '    effort: "high"',
                "    budget_tokens: 512",
                "  extra_request_args:",
                "    temperature: 0",
                "  extra_body:",
                "    chat_template_kwargs:",
                "      enable_thinking: true",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.provider.structured_output_mode == "json_object"
    assert config.provider.reasoning is not None
    assert config.provider.reasoning.enabled is True
    assert config.provider.reasoning.effort == "high"
    assert config.provider.reasoning.budget_tokens == 512
    assert config.provider.extra_request_args == {"temperature": 0}
    assert config.provider.extra_body == {
        "chat_template_kwargs": {"enable_thinking": True},
    }


def test_user_config_round_trips_runtime_models_without_materializing_env_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from dagent.config import (
        UserDagentConfig,
        UserModelProviderConfig,
        load_user_config,
        save_user_config,
    )

    monkeypatch.setenv("LOCAL_QWEN_API_KEY", "secret-from-env")
    config_path = tmp_path / ".dagent" / "config.yaml"
    config = UserDagentConfig(
        model_providers={
            "local-qwen": UserModelProviderConfig(
                name="Local Qwen",
                base_url="http://localhost:8000/v1",
                model="qwen3-coder",
                api_key_env="LOCAL_QWEN_API_KEY",
            ),
            "saved-key": UserModelProviderConfig(
                name="Saved Key",
                base_url="https://api.example.test/v1",
                model="saved-key-model",
                api_key="literal-secret",
            ),
        },
        active_model="local-qwen",
        mcp_servers={"search": {"command": "fake", "args": ["--stdio"]}},
    )

    save_user_config(config, config_path)
    loaded = load_user_config(config_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert loaded.active_model == "local-qwen"
    assert loaded.model_providers["local-qwen"].api_key == "secret-from-env"
    assert loaded.model_providers["local-qwen"].api_key_env == "LOCAL_QWEN_API_KEY"
    assert raw["model_providers"]["local-qwen"]["api_key_env"] == "LOCAL_QWEN_API_KEY"
    assert "api_key" not in raw["model_providers"]["local-qwen"]
    assert raw["model_providers"]["saved-key"]["api_key"] == "literal-secret"
    assert raw["mcp_servers"]["search"] == {"command": "fake", "args": ["--stdio"]}
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_user_config_round_trips_onlyoffice_preview_config(tmp_path: Path) -> None:
    from dagent.config import (
        UserDagentConfig,
        UserOnlyOfficeConfig,
        load_user_config,
        save_user_config,
    )

    config_path = tmp_path / ".dagent" / "config.yaml"
    config = UserDagentConfig(
        onlyoffice=UserOnlyOfficeConfig(
            enabled=True,
            document_server_url="http://192.168.31.219:8089",
            public_api_base="http://192.168.31.10:8000",
            jwt_secret="onlyoffice-jwt",
            lang="zh-CN",
            project_file_edit_enabled=True,
            run_artifact_edit_enabled=True,
        )
    )

    save_user_config(config, config_path)
    loaded = load_user_config(config_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert loaded.onlyoffice.enabled is True
    assert loaded.onlyoffice.document_server_url == "http://192.168.31.219:8089"
    assert loaded.onlyoffice.public_api_base == "http://192.168.31.10:8000"
    assert loaded.onlyoffice.jwt_secret == "onlyoffice-jwt"
    assert loaded.onlyoffice.lang == "zh-CN"
    assert loaded.onlyoffice.project_file_edit_enabled is True
    assert loaded.onlyoffice.run_artifact_edit_enabled is True
    assert raw["onlyoffice"] == {
        "enabled": True,
        "document_server_url": "http://192.168.31.219:8089",
        "public_api_base": "http://192.168.31.10:8000",
        "jwt_secret": "onlyoffice-jwt",
        "lang": "zh-CN",
        "project_file_edit_enabled": True,
        "run_artifact_edit_enabled": True,
    }
