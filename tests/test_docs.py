from pathlib import Path


def _section(text: str, heading: str, next_heading: str) -> str:
    start = text.index(heading)
    end = text.index(next_heading, start + len(heading))
    return text[start:end]


def test_migration_notes_archive_capability_identity_changes_in_current_release() -> None:
    english = Path("docs/en/migration.md").read_text(encoding="utf-8")
    chinese = Path("docs/zh-CN/migration.md").read_text(encoding="utf-8")

    english_unreleased = _section(english, "## Unreleased", "## 0.6.1")
    english_released = _section(english, "## 0.6.1", "## 0.6.0")
    chinese_unreleased = _section(chinese, "## Unreleased", "## 0.6.1")
    chinese_released = _section(chinese, "## 0.6.1", "## 0.6.0")

    assert "No unreleased changes" in english_unreleased
    assert "Capability definitions now separate stable ids from call names" in english_released
    assert "Runner.add_tools is now atomic" in english_released
    assert "Capability definitions now separate stable ids from call names" not in english_unreleased
    assert "当前没有未发布变更" in chinese_unreleased
    assert "Capability definitions 现在把稳定 id 和调用名分开" in chinese_released
    assert "Runner.add_tools 现在是原子的" in chinese_released
    assert "Capability definitions 现在把稳定 id 和调用名分开" not in chinese_unreleased
