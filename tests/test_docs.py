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

    english_unreleased = _section(english, "## Unreleased", "## 0.7.4")
    english_074 = _section(english, "## 0.7.4", "## 0.7.3")
    english_073 = _section(english, "## 0.7.3", "## 0.7.2")
    english_072 = _section(english, "## 0.7.2", "## 0.7.1")
    english_071 = _section(english, "## 0.7.1", "## 0.7.0")
    english_070 = _section(english, "## 0.7.0", "## 0.6.8")
    english_068 = _section(english, "## 0.6.8", "## 0.6.7")
    english_067 = _section(english, "## 0.6.7", "## 0.6.6")
    english_066 = _section(english, "## 0.6.6", "## 0.6.5")
    english_065 = _section(english, "## 0.6.5", "## 0.6.4")
    english_064 = _section(english, "## 0.6.4", "## 0.6.3")
    english_063 = _section(english, "## 0.6.3", "## 0.6.2")
    english_released = _section(english, "## 0.6.1", "## 0.6.0")
    chinese_unreleased = _section(chinese, "## Unreleased", "## 0.7.4")
    chinese_074 = _section(chinese, "## 0.7.4", "## 0.7.3")
    chinese_073 = _section(chinese, "## 0.7.3", "## 0.7.2")
    chinese_072 = _section(chinese, "## 0.7.2", "## 0.7.1")
    chinese_071 = _section(chinese, "## 0.7.1", "## 0.7.0")
    chinese_070 = _section(chinese, "## 0.7.0", "## 0.6.8")
    chinese_068 = _section(chinese, "## 0.6.8", "## 0.6.7")
    chinese_067 = _section(chinese, "## 0.6.7", "## 0.6.6")
    chinese_066 = _section(chinese, "## 0.6.6", "## 0.6.5")
    chinese_065 = _section(chinese, "## 0.6.5", "## 0.6.4")
    chinese_064 = _section(chinese, "## 0.6.4", "## 0.6.3")
    chinese_063 = _section(chinese, "## 0.6.3", "## 0.6.2")
    chinese_released = _section(chinese, "## 0.6.1", "## 0.6.0")

    collapsed_english_unreleased = _collapsed(english_unreleased)
    assert "`reference_content`" in collapsed_english_unreleased
    assert "separate user-message section" in collapsed_english_unreleased
    assert "`{{ variable }}` templates" in collapsed_english_unreleased
    assert "manually typed placeholders require an explicit picker selection" in collapsed_english_unreleased
    assert 'mcp_stdio_stderr="inherit"' in english_074
    assert "mcp-stderr.log" in english_074
    assert "strict internal JSON Schema" in english_073
    assert "free-form PlanSpec DSL" in english_073
    assert "Runner.cancel(run_id)" in english_073
    assert 'planner_frontend="sdk_builder"' in english_073
    assert "General-Purpose Agent" in english_072
    assert "dedicated process group" in english_072
    assert "RunCheckpoint" in english_071
    assert "ResolvedRunPlan" in english_071
    assert "ExecutionLimits" in english_071
    assert "checkpoint=..." in english_071
    assert "inherit_local_tools=True" not in english_unreleased
    assert "inherit_local_tools=True" in english_070
    assert "exclude_local_tool_ids" in english_070
    assert "caller-supplied Run IDs" in english_070
    assert "RunState" in english_070
    assert "capability-reference validation" in english_070
    assert "MCP snapshots" in english_070
    assert "dagent.capabilities.python_tools" in english_068
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
    collapsed_chinese_unreleased = _collapsed(chinese_unreleased)
    assert "`reference_content`" in collapsed_chinese_unreleased
    assert "独立的 user-message 区块" in collapsed_chinese_unreleased
    assert "`{{ variable }}` 模板" in collapsed_chinese_unreleased
    assert "手动输入的占位符必须通过选择器显式绑定" in collapsed_chinese_unreleased
    assert 'mcp_stdio_stderr="inherit"' in chinese_074
    assert "mcp-stderr.log" in chinese_074
    assert "internal strict JSON Schema" in chinese_073
    assert "Free-form PlanSpec DSL" in chinese_073
    assert "Runner.cancel(run_id)" in chinese_073
    assert 'planner_frontend="sdk_builder"' in chinese_073
    assert "通用智能体" in chinese_072
    assert "专用进程组" in chinese_072
    assert "RunCheckpoint" in chinese_071
    assert "ResolvedRunPlan" in chinese_071
    assert "ExecutionLimits" in chinese_071
    assert "checkpoint=..." in chinese_071
    assert "inherit_local_tools=True" not in chinese_unreleased
    assert "inherit_local_tools=True" in chinese_070
    assert "exclude_local_tool_ids" in chinese_070
    assert "调用方提供的 Run ID" in chinese_070
    assert "RunState" in chinese_070
    assert "capability reference 校验" in chinese_070
    assert "MCP snapshots" in chinese_070
    assert "dagent.capabilities.python_tools" in chinese_068
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


def test_070_docs_describe_library_boundary_and_removed_process_api() -> None:
    english_migration = Path("docs/en/migration.md").read_text(
        encoding="utf-8"
    )
    chinese_migration = Path("docs/zh-CN/migration.md").read_text(
        encoding="utf-8"
    )

    for value in [
        "0.7.0",
        "RuntimeRunSpec",
        "RuntimeFrame",
        "dagent.worker",
        "Runner.stream",
        "Runner.resume_stream",
    ]:
        assert value in english_migration
        assert value in chinese_migration

    for path in [
        Path("docs/en/python-sdk.md"),
        Path("docs/zh-CN/python-sdk.md"),
        Path("docs/en/runner-and-configuration.md"),
        Path("docs/zh-CN/runner-and-configuration.md"),
    ]:
        text = path.read_text(encoding="utf-8")
        assert "RuntimeRunSpec" not in text
        assert "RuntimeFrame" not in text
        assert "python -m dagent.worker" not in text
        assert "serve loop" not in text
