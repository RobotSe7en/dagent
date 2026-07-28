# 故障排查

本页列出已发布 dagent 用户常见的安装和运行时问题。

## 安装后导入失败

安装包名是 `dagent-ai`，导入名是 `dagent`：

```bash
pip install dagent-ai
```

```python
import dagent
```

确认你的 Python 版本是 3.11 或更新。

## Provider 认证失败

确认传给 `api_key_env` 的环境变量存在于启动应用的同一个进程中：

```bash
export OPENAI_API_KEY="..."
```

```python
provider = dagent.Provider(
    base_url="https://api.openai.com/v1",
    model="your-model",
    api_key_env="OPENAI_API_KEY",
)
```

只有当目标 endpoint 明确文档化对应字段时，才使用 provider-specific
`extra_request_args` 或 `extra_body`。

## `Runner.from_config(...)` 找不到配置

如果没有传入 path，配置解析顺序是：

1. `DAGENT_CONFIG`
2. `./config.yaml`

从不同 working directory 运行时，请显式传入 path：

```python
runner = dagent.Runner.from_config(
    "/path/to/config.yaml",
    workspace="agent-workspace",
    runtime_directory=".runtime",
)
```

## MCP 注册失败

安装 MCP extra：

```bash
pip install "dagent-ai[mcp]"
```

然后确认配置的 stdio server command 可以在 dagent 外部正常运行，或者确认
Streamable HTTP `url` 和 `headers` 可以访问远程 MCP server。如果 server 无法连接或
某个 discovered tool 无法注册，MCP registration 会回滚。

如果启动很慢，调大 `connect_timeout`；如果工具已经启动但运行中失败，调大
`tool_timeout`。超时失败会带明确文案，例如 `timed out after 60 seconds` 或
`MCP tool 'search' on server 'docs' timed out after 300 seconds`。

## Unknown Capability

列出已注册 capabilities：

```python
for definition in runner.list_capabilities():
    print(definition.id, definition.enabled)
```

常见 id 格式：

- Python tools: `tool.<name>`
- MCP tools: `mcp.<server>.<tool>`
- Skill accessors: `skill.list`, `skill.view`

## Agent 看不到 Tool

Agents 使用 `capabilities` 字段作为 allowlist。传入显式列表会缩小 agent 能调用的集合：

```python
agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=["tool.search"],
)
```

如果在 agent 的 `capabilities` 中直接传入 `@dagent.tool` binding，runner 会在解析该
agent 时注册它。

## Skill 不可见

把 skill root 添加到 runner，并检查 agent 的 `skills` filter：

```python
runner.add_skill_root("team-skills")
agent = dagent.ToolAgent(profile="conversation", skills=["writing/terse"])
```

使用 `skills=None` 允许所有已配置 skills，`skills=[]` 隐藏 skill tools，
`skills=[...]` 只暴露指定 skills。

## 静态 DAG Validation 失败

静态 DAG 节点输出引用不会推断 edges。需要显式添加依赖：

```python
dag.add_edge(search_node, render_node)
```

对于 non-upstream reads、unknown artifacts、malformed expressions、
unsafe artifact boundaries 和 invalid control-flow references，validation 会 fail closed。

## Review Resume 失败

如果持久化任务正在等待 review，请恢复 SDK checkpoint，并使用
`resume(..., checkpoint=...)`，不要使用 `run(..., state=...)`：

```python
restored = dagent.RunCheckpoint.model_validate_json(saved_json)
pending = restored.state.pending_review
if pending is None:
    raise RuntimeError("Checkpoint is not awaiting review")
result = await runner.resume(
    dagent.ReviewHandle(pending).approve(),
    checkpoint=restored,
)
```

0.8 不再提供 `run(..., state=...)` 和 `resume(..., state=...)`。如果 checkpoint
resume 报告 capability ID 缺失或被禁用，或 skill 缺失，请构造兼容的 `Runner`；
SDK 不会静默扩大保存的 scope。

## Streaming 文本交错

并行 DAG nodes 可能同时 streaming。请按 `response_id` 聚合 text deltas，而不是只依赖
event ordering 或 `model_step`。
