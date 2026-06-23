# 安装

dagent 在 PyPI 上的包名是 `dagent-ai`，在 Python 中导入时使用 `dagent`。

## 环境要求

- Python 3.11 或更新版本
- 运行模型驱动任务时，需要一个 OpenAI-compatible chat completions endpoint
- 可选：如果注册通过 `npx` 分发的 MCP server，需要 Node.js

## 从 PyPI 安装

```bash
pip install dagent-ai
```

如果希望 `Runner` 注册 MCP servers，请安装 MCP 可选依赖：

```bash
pip install "dagent-ai[mcp]"
```

## Provider 凭证

`dagent.Provider` 支持直接传入 `api_key`，也支持传入环境变量名
`api_key_env`。应用和示例通常推荐使用环境变量：

```bash
export OPENAI_API_KEY="..."
```

```python
import dagent


provider = dagent.Provider(
    base_url="https://api.openai.com/v1",
    model="your-model",
    api_key_env="OPENAI_API_KEY",
)
```

如果 `api_key` 和 `api_key_env` 都没有解析出值，provider 会使用一个占位 key。
这对本地 provider 或不需要认证的测试 provider 很有用。

## 本地开发

在仓库 checkout 中运行：

```bash
uv sync --extra dev
uv run --extra dev pytest
```

从仓库根目录运行离线示例：

```bash
uv run python -m examples.tool_agent
uv run python -m examples.static_dag
uv run python -m examples.streaming
```

前端检查位于 `web/`：

```bash
npm --prefix web test
npm --prefix web run build
```

## 下一步

- 继续阅读[快速开始](quick-start.md)。
- 在 [Runner 和配置](runner-and-configuration.md)中配置 providers、MCP servers、
  validation 和 profiles。
- 在 [Python SDK 参考地图](python-sdk.md)中查看所有公开 SDK 名称。
