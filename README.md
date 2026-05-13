# dagent

> **Plan globally. Re-plan locally.**

**dagent** is a *Dynamic DAG Agent* framework. Given a goal, it generates an executable DAG and runs it layer by layer. After each layer completes, it applies the lightest re-planning strategy that suffices - from zero-LLM placeholder substitution, to local parameter reasoning, to full downstream re-generation - updating only what needs to change, freezing everything already done.

Traditional agent frameworks choose one of two extremes: a free-running ReAct loop with
no structure, or a rigid static pipeline with no adaptability. dagent rejects both. Every
task gets a reviewable, auditable plan up front. That plan evolves as execution proceeds -
only the parts that need changing are changed, and everything already executed is frozen
and immutable.

> **Design origin:** The self-planning dynamic DAG agent loop - tool-node DAG with
> three-level incremental re-planning, frozen Trace DB as the context boundary, and
> automatic DAG-vs-tool task routing - was conceived and first implemented by the
> author of this repository. First committed: **2026-05-01**.

---

## Core Ideas

**1. Self-planning DAG, not a static pipeline.**
The agent generates the initial DAG from the goal. After each node executes, it observes
the output and decides whether to inject values, re-reason locally, or re-generate the
downstream subgraph. The plan is always the latest best understanding of how to reach
the goal.

**2. Tool nodes, not agent nodes.**
Every DAG node is a deterministic tool call. Intelligence lives in the re-planner between
nodes, not inside them. Nodes are cheap, testable, and auditable.

**3. Three-level re-planning with minimal context.**
After each node completes, the executor picks the lightest strategy that suffices -
from zero-LLM placeholder substitution up to full downstream re-generation. Each level
receives only the context it actually needs.

**4. Frozen trace as the context boundary.**
Completed nodes are immediately written to an immutable Trace DB and dropped from the
active LLM context. The re-planner reads goal-aligned summaries, never raw history.
Context stays bounded regardless of task length.

**5. Human review as a first-class checkpoint.**
Medium/high-risk DAGs require explicit approval before execution. Re-planned DAGs can
trigger a second review. The human is never bypassed.

---

## Architecture

### Harness Runtime

```mermaid
flowchart TD
  U["User"] --> R["HarnessRuntime"]
  R -->|"auto mode"| ROUTE["Route\ndag or tool"]
  R -->|"tool mode"| TA["ToolAgentLoop"]
  R -->|"dag mode"| DA["DAGAgentLoop"]

  ROUTE -->|"tool"| TA
  ROUTE -->|"dag"| DA

  TA -->|"bounded tool calls"| T["ToolExecutor"]
  TA -->|"loop result"| G["Human Review Gate"]

  DA --> D["Create Initial DAG\ntool nodes + placeholders"]

  D -->|"review required"| UI["Human Review"]
  D -->|"approved / auto safe"| E["DAGExecutor"]
  UI -->|"approve"| E
  UI -->|"reject / modify"| D

  E --> N["Execute Node"]
  N --> T
  N --> OBS["Observe Output"]
  N -->|"node complete"| TR[("Trace DB\nfrozen node + I/O")]

  OBS -->|"Level 1\ndata contract known"| INJ["Placeholder Injection\nno LLM"]
  OBS -->|"Level 2\ntool/params need reasoning"| RP["Light Re-planner\ncontext = current node output"]
  OBS -->|"Level 3\nstructure must change"| RG["DAG Re-generator\ncontext = goal + summaries"]

  INJ --> NXT["Update pending_nodes"]
  RP --> NXT
  RG --> NXT

  NXT -->|"has next node"| E
  NXT -->|"review required"| UI2["Human Review\nre-planned DAG"]
  UI2 -->|"approve"| E

  NXT -->|"DAG complete"| DR["DAGAgentLoop Result"]
  DR --> G

  G -->|"approval needed"| UI3["Human Review"]
  UI3 -->|"approve / reject"| R
  G -->|"no human gate"| V["Result Validation\noptional LLM validator"]
  V -->|"issues found"| RTRY["Retry with validation feedback"]
  RTRY --> TA
  RTRY --> DA
  V -->|"passed / disabled"| O["Return final_answer\nto user"]
```

`HarnessRuntime` is the top-level control layer. It owns routing, session state,
human review gates, optional result validation, retry feedback, and final result delivery.
The execution details stay inside `ToolAgentLoop` for tool-use work and
`DAGAgentLoop` for structured DAG work. Once review and validation pass, the runtime
returns the loop's `final_answer` directly without a separate summarization step.

### Three-Level Re-planning

After every node completes, the DAGExecutor selects the minimum re-planning strategy:

| Level | Trigger | Context passed to LLM | Cost |
|-------|---------|----------------------|------|
| **1 - Placeholder Injection** | Data contract defined at creation; only values unknown | Predecessor output -> direct substitution | No LLM call |
| **2 - Light Re-planner** | Next node's tool or params require runtime reasoning | Current node output + next node definition | Lightweight |
| **3 - DAG Re-generator** | Downstream structure must change | Original goal + per-node result summaries | Full re-plan |

Design principles:

- **Minimal context by design.** Each level receives only what it needs. Completed nodes
  live in Trace DB and are never re-injected into LLM context.
- **Incremental re-planning.** Level 3 re-generates only the pending subgraph.
  Completed nodes are preserved as-is.
- **Frozen nodes are immutable.** Once written to Trace DB, a node's record cannot be
  modified. Audit integrity is guaranteed.

### When to Use DAG vs. Tool Mode

| Task shape | Path |
|------------|------|
| Subtasks that can run in parallel | DAG |
| Sequential steps with known structure, runtime values only | DAG + placeholder injection |
| Exploratory - next action depends on observation | `ToolAgentLoop` |
| Dynamic fan-out - node count unknown until runtime | `ToolAgentLoop` |

Forcing exploratory tasks into a DAG produces worse results than leaving them as
sequential tool calls. In `auto` mode, `HarnessRuntime` makes this routing judgment
before dispatching to `ToolAgentLoop` or `DAGAgentLoop`.

### Trace DB

Every completed node is written immediately on completion:

```
{ node_id, tool, params, output, summary, timestamp, status }
```

Trace DB serves three purposes:

1. **Audit log** - immutable record of what ran, with what inputs, and what it returned.
2. **Re-planning source** - Level 3 re-planner reads summaries, not raw outputs, keeping
   context bounded regardless of how many nodes have executed.
3. **Human review** - the WebUI surfaces the trace timeline alongside the DAG graph.

---

## Safety Model

The runtime is intentionally layered:

- `HarnessRuntime` routes requests, manages runtime session state, applies human
  review gates, optionally validates final results, and returns the loop's final answer.
- DAG Agent proposes a DAG but does not grant permissions.
- `DAGExecutor` validates the DAG, applies hard risk overrides, and blocks medium/high
  risk DAGs until explicitly approved.
- Each node is a bounded tool call - no nested agent loop inside a node.
- `ToolExecutor` enforces boundaries before every tool call.
- Human review can be triggered by tool-mode calls, initial DAG creation, and after
  any Level 3 re-plan.
- Optional result validation uses a separate `validator_agent` profile to check the
  final answer against the original user request and execution context. If validation
  finds issues, the runtime retries once with validator feedback.

Boundary checks:

- `read_only` nodes cannot write files
- `allowed_paths` prevents path traversal and absolute path escape
- `forbidden_tools` blocks specific tools
- unregistered tools fail closed

---

## Project Layout

```text
dagent/
  api/              FastAPI app - task, DAG, run, and trace endpoints
  harness_runtime/  runtime orchestration, ToolAgentLoop, DAGAgentLoop, validation,
                    session state, event adapters, trace recording, DAG execution
  providers/        OpenAI-compatible and mock chat providers
  schemas/          DAG, node, edge, trace, feedback models
  tools/            tool registry, executor, file tools, boundary checks
  state/            prompt assembly and context management
profiles/           editable agent profiles (dag_agent, validator_agent, feedback_learner)
tests/              pytest suite
```

## Configuration

```yaml
provider:
  base_url: "https://api.minimaxi.com/v1"
  model: "MiniMax-M2.1"
  api_key_env: "MINIMAX_API_KEY"
  timeout_seconds: 60
  strip_thinking: false
enable_result_validation: false
profiles:
  directory: "profiles"
  conversation: "conversation"
  dag_agent: "dag_agent"
  validator_agent: "validator_agent"
  feedback_learner: "feedback_learner"
```

Secrets in `.env` (git-ignored):

```env
MINIMAX_API_KEY=your-api-key
```

Override config path:

```powershell
$env:DAGENT_CONFIG="C:\path\to\config.yaml"
```

## Agent Profiles

Each role has an editable profile directory:

```text
profiles/
  conversation/       soul.md  guideline.md  agent.md  memory.md  profile.yaml
  dag_agent/          soul.md  guideline.md  agent.md  memory.md  profile.yaml
  validator_agent/    soul.md  guideline.md  agent.md  memory.md  profile.yaml
  feedback_learner/   soul.md  guideline.md  agent.md  memory.md  profile.yaml
```

`profile.yaml` defines ordered prompt layers. Dynamic content (tools, task context,
trace data) is injected at runtime and never stored in profile files.

---

## Development

```powershell
uv run --extra dev pytest
```

Real provider integration tests:

```powershell
$env:DAGENT_RUN_MINIMAX_TESTS="1"
uv run --extra dev pytest tests/test_minimax_integration.py
```

Run API + frontend:

```powershell
uv run uvicorn dagent.api.app:app --host 127.0.0.1 --port 8001

cd web && npm install && npm run dev
```

## Quick Start

Verify provider connection:

```python
import asyncio
from dagent.config import load_config
from dagent.providers import OpenAICompatibleProvider

async def main():
    config = load_config()
    provider = OpenAICompatibleProvider(config.provider)
    response = await provider.chat([{"role": "user", "content": "Reply with exactly: OK"}])
    print(response.content)

asyncio.run(main())
```

Run the harness runtime:

```python
import asyncio
from dagent.factory import create_harness_runtime

async def main():
    runtime = create_harness_runtime(workspace_root=".")
    result = await runtime.handle_message(
        "Read README and summarize the implemented milestones.",
        mode="auto",
        review_level="fast",
    )
    print(result.status)
    print(result.final_answer)

asyncio.run(main())
```
