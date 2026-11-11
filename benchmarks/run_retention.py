"""Offline root-run heap and wire-size baseline.

Run from the repository: uv run python -m benchmarks.run_retention --runs 24
Results are JSON. No requests, batches, events or old results are accumulated.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import gc
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import tracemalloc
from typing import Any

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


class SyntheticProvider(MockProvider):
    """Only integer counters survive a request; MockProvider's log stays empty."""

    def __init__(self, steps: int):
        super().__init__()
        self.steps = steps
        self.round = 0

    async def chat(self, messages, tools=None, *, response_format=None):
        if messages[0]["content"].startswith("Summarize earlier conversation data"):
            return ChatResponse(content="Earlier evidence collected; continue.")
        self.round = (self.round + 1) % (self.steps + 1)
        if self.round == 0:
            return ChatResponse(content="done")
        return ChatResponse(tool_calls=[ToolCall(id=f"call_{self.round}", name="tool_evidence",
                                               arguments={"index": self.round})])


def json_values(value: Any) -> int:
    """Count containers and scalar values; object keys are not values."""
    if isinstance(value, dict):
        return 1 + sum(json_values(item) for item in value.values())
    if isinstance(value, (tuple, list)):
        return 1 + sum(json_values(item) for item in value)
    return 1


def wire_size(value: Any) -> dict[str, int]:
    payload = value.model_dump(mode="json")
    return {"utf8_bytes": len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()),
            "json_values": json_values(payload)}


def owned_bytes(*roots: Any) -> int:
    """Approximate Python object graph size, deduplicating shared references.

    Excludes classes, functions and globals; tracemalloc reports the actual heap
    sample separately. This attribution is not serialized size or process RSS.
    """
    seen = set()

    def size(value):
        identity = id(value)
        if identity in seen:
            return 0
        seen.add(identity)
        total = sys.getsizeof(value)
        if isinstance(value, dict):
            total += sum(size(key) + size(item) for key, item in value.items())
        elif isinstance(value, (tuple, list, set, frozenset)):
            total += sum(size(item) for item in value)
        elif hasattr(value, "__dict__") and not isinstance(value, type):
            total += size(vars(value))
        return total

    return sum(size(root) for root in roots)


async def measure(*, workspace: Path, runs: int, limit: int | None, mode: str,
                  steps: int, output_chars: int, onchange: bool) -> dict[str, Any]:
    @dagent.tool
    def evidence(index: int) -> str:
        return f"page {index}: " + "e" * output_chars

    provider = SyntheticProvider(steps)
    samples = []
    sizes = {}
    async def acknowledge(batch):
        pass

    tracemalloc.start()
    try:
        with closing(dagent.Runner(provider=provider, workspace=workspace, skill_roots=[], max_retained_runs=limit)) as runner:
            agent = dagent.ToolAgent(profile="conversation", name="helper", capabilities=[evidence])
            target: dagent.ToolAgent | dagent.Dag = agent
            if mode == "dag":
                target = dagent.Dag("child_cache")
                target.add_node(dagent.Node("worker", target=agent, inputs={"prompt": "Collect evidence."}))
            for index in range(runs):
                kwargs = {"run_id": f"sample_{index}"}
                if mode == "tool":
                    kwargs["input"] = "Collect evidence."
                async for event in runner.stream(target, on_conversation_change=acknowledge if onchange else None, **kwargs):
                    if event.type == "run.finished":
                        result = event.data.result
                        if result.status != "completed":
                            raise RuntimeError(f"Benchmark run failed: {result.error}")
                        if index == runs - 1:
                            sizes = {"RunState": wire_size(result.state), "checkpoint": wire_size(result.checkpoint),
                                     "run.finished": wire_size(event)}
                        del result
                    elif event.type == "run.failed":
                        raise RuntimeError(event.data.message)
                del event
                assert not provider.requests
                gc.collect()
                current, peak = tracemalloc.get_traced_memory()
                caches = [store._conversations for store in runner._agent_session_stores.values()]
                samples.append({"run": index + 1, "heap_current_bytes": current, "heap_peak_bytes": peak,
                    "run_states": len(runner.runtime.runs), "checkpoints": len(runner._run_checkpoints),
                    "child_sessions": sum(map(len, caches))})
            attribution = {"run_states_bytes": owned_bytes(runner.runtime.runs),
                           "checkpoints_bytes": owned_bytes(runner._run_checkpoints),
                           "child_sessions_bytes": owned_bytes(*caches),
                           "combined_unique_bytes": owned_bytes(runner.runtime.runs, runner._run_checkpoints, *caches)}
    finally:
        tracemalloc.stop()
    return {"mode": mode, "max_retained_runs": limit, "wire_sizes_last_result": sizes,
            "retained_object_graph": attribution, "samples": samples}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=24)
    parser.add_argument("--steps", type=int, default=4)
    parser.add_argument("--output-chars", type=int, default=32768)
    parser.add_argument("--onchange", action="store_true")
    args = parser.parse_args()
    if args.runs < 1 or args.steps < 1 or args.output_chars < 1:
        parser.error("--runs, --steps and --output-chars must be positive")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    source = hashlib.sha256()
    for path in sorted(Path("dagent").rglob("*.py")):
        source.update(str(path).encode() + b"\0" + path.read_bytes() + b"\0")
    report = {"revision": revision, "working_tree_differs": bool(subprocess.check_output(["git", "status", "--porcelain"], text=True)),
              "sdk_source_sha256": source.hexdigest(),
              "python": platform.python_version(), "configuration": vars(args),
              "data": "Fresh text tool results; no read_file; legacy context; offline synthetic provider.",
              "references": "Provider logs remain empty; host only acknowledges; events are drained; old results are released.",
              "json_counting": "Containers and scalar values included; object keys excluded.",
              "measurements": []}
    with tempfile.TemporaryDirectory(prefix="dagent-retention-bench-") as directory:
        for mode in ("tool", "dag"):
            for limit in (None, 2, 0):
                report["measurements"].append(await measure(workspace=Path(directory) / f"{mode}_{limit}",
                    runs=args.runs, limit=limit, mode=mode, steps=args.steps,
                    output_chars=args.output_chars, onchange=args.onchange))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
