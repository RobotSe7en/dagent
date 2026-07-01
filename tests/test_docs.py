from pathlib import Path


def _section(text: str, heading: str, next_heading: str) -> str:
    start = text.index(heading)
    end = text.index(next_heading, start + len(heading))
    return text[start:end]


def test_migration_notes_record_064_persistence_release_history() -> None:
    english = Path("docs/en/migration.md").read_text(encoding="utf-8")
    chinese = Path("docs/zh-CN/migration.md").read_text(encoding="utf-8")

    english_unreleased = _section(english, "## Unreleased", "## 0.6.4")
    english_064 = _section(english, "## 0.6.4", "## 0.6.3")
    english_063 = _section(english, "## 0.6.3", "## 0.6.2")
    english_released = _section(english, "## 0.6.1", "## 0.6.0")
    chinese_unreleased = _section(chinese, "## Unreleased", "## 0.6.4")
    chinese_064 = _section(chinese, "## 0.6.4", "## 0.6.3")
    chinese_063 = _section(chinese, "## 0.6.3", "## 0.6.2")
    chinese_released = _section(chinese, "## 0.6.1", "## 0.6.0")

    assert "No unreleased changes" in english_unreleased
    assert "local API/WebUI store now isolates chat" in english_064
    assert "orchestration sessions" in english_064
    assert "Incompatible pre-release local SQLite API databases are recreated" in english_064
    assert "Static DAG runs and the dynamic orchestration workspace still use the existing" in english_063
    assert "not yet been folded into project/conversation persistence" in english_063
    assert "orchestration drafts are stored through the API persistence layer" not in english_063
    assert "Capability definitions now separate stable ids from call names" in english_released
    assert "Runner.add_tools is now atomic" in english_released
    assert "Capability definitions now separate stable ids from call names" not in english_unreleased
    assert "暂无未发布变更" in chinese_unreleased
    assert "本地 API/WebUI store 现在按 kind 隔离普通 chat" in chinese_064
    assert "编排 session" in chinese_064
    assert "不兼容的未发布本地 SQLite API 旧库时会直接重建数据库" in chinese_064
    assert "仍使用现有 run workspace 模型" in chinese_063
    assert "尚未并入项目/会话持久化" in chinese_063
    assert "编排 draft 已通过 API 持久化层保存" not in chinese_063
    assert "Capability definitions 现在把稳定 id 和调用名分开" in chinese_released
    assert "Runner.add_tools 现在是原子的" in chinese_released
    assert "Capability definitions 现在把稳定 id 和调用名分开" not in chinese_unreleased
