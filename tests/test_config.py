from pathlib import Path

from dagent.config import load_config


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
