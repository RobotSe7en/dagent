from pathlib import Path


def _section(text: str, heading: str, next_heading: str) -> str:
    start = text.index(heading)
    end = text.index(next_heading, start + len(heading))
    return text[start:end]


def _collapsed(text: str) -> str:
    return " ".join(text.split())


def test_migration_notes_record_release_history() -> None:
    english = Path("docs/en/migration.md").read_text(encoding="utf-8")
    chinese = Path("docs/zh-CN/migration.md").read_text(encoding="utf-8")

    english_unreleased = _section(english, "## Unreleased", "## 0.6.8")
    english_068 = _section(english, "## 0.6.8", "## 0.6.7")
    english_067 = _section(english, "## 0.6.7", "## 0.6.6")
    english_066 = _section(english, "## 0.6.6", "## 0.6.5")
    english_065 = _section(english, "## 0.6.5", "## 0.6.4")
    english_064 = _section(english, "## 0.6.4", "## 0.6.3")
    english_063 = _section(english, "## 0.6.3", "## 0.6.2")
    english_released = _section(english, "## 0.6.1", "## 0.6.0")
    chinese_unreleased = _section(chinese, "## Unreleased", "## 0.6.8")
    chinese_068 = _section(chinese, "## 0.6.8", "## 0.6.7")
    chinese_067 = _section(chinese, "## 0.6.7", "## 0.6.6")
    chinese_066 = _section(chinese, "## 0.6.6", "## 0.6.5")
    chinese_065 = _section(chinese, "## 0.6.5", "## 0.6.4")
    chinese_064 = _section(chinese, "## 0.6.4", "## 0.6.3")
    chinese_063 = _section(chinese, "## 0.6.3", "## 0.6.2")
    chinese_released = _section(chinese, "## 0.6.1", "## 0.6.0")

    assert "inherit_local_tools=True" in english_unreleased
    assert "exclude_local_tool_ids" in english_unreleased
    assert "RuntimeRunSpec" in english_unreleased
    assert "python -m dagent.worker" in english_unreleased
    assert "python -m dagent.runtime" not in english_unreleased
    collapsed_english_unreleased = _collapsed(english_unreleased)
    collapsed_chinese_unreleased = _collapsed(chinese_unreleased)
    assert "host-provided `run_id`" in english_unreleased
    assert "within the same `Runner` session" in collapsed_english_unreleased
    assert "lazy MCP" in english_unreleased
    assert "validate_capability_refs" in english_unreleased
    assert "Runner.reload_python_tool_sources" in english_068
    assert "Tool-agent and dynamic DAG LLM calls now retry transient provider failures" in english_067
    assert "Permanent LLM provider failures" in english_067
    assert "Streaming LLM calls no longer retry after response tokens have been emitted" in english_067
    assert "MCP connection and tool-call timeouts now report explicit timeout messages" in english_066
    assert "MCP tool calls now default to a `300` second timeout" in english_066
    assert "Dynamic and static orchestration workspaces now persist run history" in english_066
    assert "local WebUI can edit Office documents through ONLYOFFICE" in english_066
    collapsed_english_065 = _collapsed(english_065)
    assert "recursive project file tree" in collapsed_english_065
    assert "MCP server registration and tool calls now share explicit default timeouts" in english_065
    assert "Windows workspace paths" in english_065
    assert "local API/WebUI store now isolates chat" in english_064
    assert "orchestration sessions" in english_064
    assert "Incompatible pre-release local SQLite API databases are recreated" in english_064
    assert "Static DAG runs and the dynamic orchestration workspace still use the existing" in english_063
    assert "not yet been folded into project/conversation persistence" in english_063
    assert "orchestration drafts are stored through the API persistence layer" not in english_063
    assert "Capability definitions now separate stable ids from call names" in english_released
    assert "Runner.add_tools is now atomic" in english_released
    assert "Capability definitions now separate stable ids from call names" not in english_unreleased
    assert "inherit_local_tools=True" in chinese_unreleased
    assert "exclude_local_tool_ids" in chinese_unreleased
    assert "RuntimeRunSpec" in chinese_unreleased
    assert "python -m dagent.worker" in chinese_unreleased
    assert "python -m dagent.runtime" not in chinese_unreleased
    assert "host 提供的 `run_id`" in chinese_unreleased
    assert "同一个 `Runner` session 内" in collapsed_chinese_unreleased
    assert "lazy MCP" in chinese_unreleased
    assert "validate_capability_refs" in chinese_unreleased
    assert "Runner.reload_python_tool_sources" in chinese_068
    assert "Tool-agent 和动态 DAG 的 LLM 调用现在会在 provider 瞬态失败或请求超时时按递增等待重试" in chinese_067
    assert "永久性 LLM provider 失败" in chinese_067
    assert "已经输出 response token 后不再重试" in chinese_067
    assert "MCP 连接和工具调用超时现在会返回明确的 timeout 文案" in chinese_066
    assert "MCP 工具调用现在默认使用 `300` 秒超时" in chinese_066
    assert "动态和静态编排 workspace 现在会持久化 run history" in chinese_066
    assert "通过 ONLYOFFICE 编辑项目文件" in chinese_066
    assert "递归项目文件树" in chinese_065
    assert "MCP server 注册和工具调用现在使用统一的显式默认 timeout" in chinese_065
    assert "Windows workspace" in chinese_065
    assert "本地 API/WebUI store 现在按 kind 隔离普通 chat" in chinese_064
    assert "编排 session" in chinese_064
    assert "不兼容的未发布本地 SQLite API 旧库时会直接重建数据库" in chinese_064
    assert "仍使用现有 run workspace 模型" in chinese_063
    assert "尚未并入项目/会话持久化" in chinese_063
    assert "编排 draft 已通过 API 持久化层保存" not in chinese_063
    assert "Capability definitions 现在把稳定 id 和调用名分开" in chinese_released
    assert "Runner.add_tools 现在是原子的" in chinese_released
    assert "Capability definitions 现在把稳定 id 和调用名分开" not in chinese_unreleased


def test_runtime_process_docs_describe_actual_worker_frame_sequence() -> None:
    english = Path("docs/en/runner-and-configuration.md").read_text(encoding="utf-8")
    chinese = Path("docs/zh-CN/runner-and-configuration.md").read_text(encoding="utf-8")

    english_transport = _section(english, "## Worker Process Transports", "## Provider Options")
    chinese_transport = _section(chinese, "## Worker 进程传输", "## Provider 选项")

    collapsed_english_transport = _collapsed(english_transport)
    collapsed_chinese_transport = _collapsed(chinese_transport)

    assert "hello" in collapsed_english_transport
    assert "zero or more `event` frames" in collapsed_english_transport
    assert "`state_snapshot` only after `run.finished`" in collapsed_english_transport
    assert "hello" in collapsed_chinese_transport
    assert "零个或多个 `event` frames" in collapsed_chinese_transport
    assert "仅在 `run.finished` 之后发送 `state_snapshot`" in collapsed_chinese_transport


def test_python_sdk_docs_list_runtime_contract_schema_exports() -> None:
    english = Path("docs/en/python-sdk.md").read_text(encoding="utf-8")
    chinese = Path("docs/zh-CN/python-sdk.md").read_text(encoding="utf-8")

    for name in [
        "RuntimeAgentSpec",
        "RuntimeByePayload",
        "RuntimeFrame",
        "RuntimeLogPayload",
        "RuntimeReviewDecision",
        "RuntimeRunSpec",
        "RuntimeRunTarget",
        "RuntimeValidationSpec",
        "RuntimeWorkspaceSpec",
    ]:
        assert f"`{name}`" in english
        assert f"`{name}`" in chinese
