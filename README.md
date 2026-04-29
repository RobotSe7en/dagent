# dagent

A private-deployable, human-reviewed Agent DAG framework.

`dagent` turns user requests into reviewable DAGs, lets a human approve risky
plans, then executes each DAG node through a bounded agent loop with tool
boundary checks, trace events, and OpenAI-compatible model access.

## Current Status

Implemented milestones:

- **Milestone 1**: Pydantic schemas, mock planner, DAG validation
- **Milestone 2**: tool registry, file tools, boundary enforcement
- **Milestone 3**: OpenAI-compatible provider, mock provider, bounded agent loop
- **Milestone 4**: DAG executor, topo scheduling, risk override, trace recording

Not implemented yet:

- FastAPI control plane
- DAG review UI
- persistent storage
- feedback learner

## Project Layout

```text
dagent/
  harness/      DAG planning, validation, execution, trace recording
  providers/    OpenAI-compatible and mock chat providers
  runtime/      node-level agent loop
  schemas/      DAG, node, edge, trace, feedback models
  tools/        tool registry, executor, file tools, boundary checks
tests/          pytest suite
```

## Configuration

Model settings live in `config.yaml`.

```yaml
provider:
  base_url: "https://api.minimaxi.com/v1"
  model: "MiniMax-M2.1"
  api_key_env: "MINIMAX_API_KEY"
  timeout_seconds: 60
  strip_thinking: true
```

Secrets should live in `.env`, which is ignored by git:

```env
MINIMAX_API_KEY=your-api-key
```

You can point to another config file with:

```powershell
$env:DAGENT_CONFIG="C:\path\to\config.yaml"
```

## Development

Install and test with `uv`:

```powershell
uv run --extra dev pytest
```

Expected result:

```text
31 passed
```

## Safety Model

The runtime is intentionally layered:

- `Planner` proposes a DAG but does not grant permissions.
- `DAGExecutor` validates the DAG, applies hard risk overrides, and blocks
  medium/high risk DAGs until they are approved.
- `AgentLoop` only runs a single bounded node.
- `ToolExecutor` enforces boundaries before every tool call.
- `Skills` are intended to be prompt instructions, not permissions.

Boundary checks currently cover:

- `read_only` nodes cannot write files
- `allowed_paths` prevents path traversal and absolute path escape
- `forbidden_tools` blocks specific tools
- unregistered tools fail closed

## Quick Smoke Test

With a valid `.env` and `config.yaml`, this verifies the OpenAI-compatible
provider:

```powershell
$env:PYTHONIOENCODING="utf-8"
@'
import asyncio
from dagent.config import load_config
from dagent.providers import OpenAICompatibleProvider

async def main():
    config = load_config()
    provider = OpenAICompatibleProvider(config.provider)
    response = await provider.chat([
        {"role": "user", "content": "Reply with exactly: OK"}
    ])
    print(response.content)

asyncio.run(main())
'@ | uv run python -
```

