# Installation

dagent is published on PyPI as `dagent-ai` and imported in Python as `dagent`.

## Requirements

- Python 3.11 or newer
- A private vLLM OpenAI-compatible Chat Completions or Responses endpoint for
  model-backed runs
- Optional: Node.js if you register MCP servers that are distributed through
  `npx`

## Install From PyPI

```bash
pip install dagent-ai
```

Install the MCP optional extra when you want `Runner` to register MCP servers:

```bash
pip install "dagent-ai[mcp]"
```

## Provider Credentials

`dagent.Provider` accepts either a direct `api_key` or an `api_key_env` name. The
environment-variable form is preferred for applications and examples:

```bash
export VLLM_API_KEY="local"
```

```python
import dagent


provider = dagent.Provider(
    base_url="http://localhost:8000/v1",
    model="your-vllm-model",
    api_key_env="VLLM_API_KEY",
)
```

If neither `api_key` nor `api_key_env` resolves to a value, the provider uses a
placeholder key. This is useful for local or test providers that do not require
authentication.

## Local Development

From a checkout of this repository:

```bash
uv sync --extra dev
uv run --extra dev pytest
```

The `dev` extra includes `pip`, so shell-enabled local agents can install a
task-specific Python package after the configured review policy permits it.
Do not create a venv manually and then skip `uv sync --extra dev`.

Run offline examples from the repository root:

```bash
uv run python -m examples.tool_agent
uv run python -m examples.static_dag
uv run python -m examples.streaming
```

Frontend checks live under `web/`:

```bash
npm --prefix web test
npm --prefix web run build
```

## Next Steps

- Continue with the [Quick Start](quick-start.md).
- Configure providers, MCP servers, validation, and profiles in
  [Runner and Configuration](runner-and-configuration.md).
- See all public SDK names in the [Python SDK Reference Map](python-sdk.md).
