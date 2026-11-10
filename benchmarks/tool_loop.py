"""Local, network-free tool-loop measurements (timings are not test assertions).

Run from the repository root:
    uv run python -m benchmarks.tool_loop --case loop --steps 800 --publish
    uv run python -m benchmarks.tool_loop --case adapter --steps 200 --tools 200

Use --snapshot batches.json --steps 20 --repeats 1 --publish to compare the
delivered batch stream across revisions. UUIDs are normalized in that snapshot.
"""

import argparse
import asyncio
from contextlib import closing
import json
from pathlib import Path
import re
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

import dagent
from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.providers import ChatResponse, ToolCall
from dagent.schemas import CapabilityDefinition


class LoopProvider:
    configured_context_window_tokens = 1_000_000
    max_output_tokens = 1024

    def __init__(self, steps):
        self.steps = steps
        self.index = 0

    async def chat(self, messages, tools=None, *, response_format=None):
        self.index += 1
        return (ChatResponse(tool_calls=[ToolCall(id=f"call_{self.index}", name="tool_echo", arguments={})])
                if self.index <= self.steps else ChatResponse(content="done"))


async def measure_loop(steps, publish, snapshot):
    @dagent.tool
    def echo() -> str:
        return "ok"

    batches = []

    async def save(batch):
        if snapshot:
            batches.append(batch.model_dump(mode="json"))

    with TemporaryDirectory(prefix="dagent-loop-") as workspace:
        with closing(dagent.Runner(workspace=workspace, provider=LoopProvider(steps), skill_roots=[])) as runner:
            agent = dagent.ToolAgent(profile="conversation", capabilities=[echo], max_steps=steps + 1)
            started = perf_counter()
            result = await runner.run(agent, input="repeat", run_id="benchmark",
                                      on_conversation_change=save if publish else None)
            elapsed = perf_counter() - started
            assert result.status == "completed" and result.usage.capability_calls == steps
    if snapshot:
        ids = {}

        def stable(match):
            value = match.group()
            return ids.setdefault(value, f"uuid{len(ids)}")

        snapshot.write_text(re.sub(r"[0-9a-f]{32}", stable, json.dumps(batches, sort_keys=True, indent=2)) + "\n")
    return elapsed


def measure_adapter(steps, tool_count):
    catalog = CapabilityCatalog()
    for index in range(tool_count):
        catalog.register(CapabilityDefinition(
            id=f"tool.item_{index}", kind="tool",
            parameters={"type": "object", "properties": {
                f"field_{field}": {"type": "array", "items": {"type": "string"}, "description": "Input values"}
                for field in range(12)
            }},
        ), lambda invocation: dagent.CapabilityResult.completed(invocation, "ok"))
    adapter = CapabilityToolAdapter(catalog, toolsets=[CapabilityToolset("bench", tuple(sorted(catalog.ids())))])
    call = ToolCall(id="call", name="tool_item_0", arguments={})
    started = perf_counter()
    for _ in range(steps):
        adapter.definitions(("bench",))
        # The initial preflight and the execution-time preflight each resolve
        # an invocation and the definition used for boundary/review checks.
        for _ in range(2):
            adapter.invocation_from_tool_call(call, dagent.Boundary(), enabled_toolsets=("bench",))
            adapter.definition_from_tool_call(call, enabled_toolsets=("bench",))
        adapter.ensure_allowed("tool.item_0", enabled_toolsets=("bench",))
    return perf_counter() - started


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("loop", "adapter"), default="loop")
    parser.add_argument("--steps", type=int, default=800)
    parser.add_argument("--tools", type=int, default=200)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    if min(args.steps, args.tools, args.repeats) < 1:
        parser.error("steps, tools and repeats must be positive")
    if args.snapshot and (args.case != "loop" or not args.publish or args.repeats != 1):
        parser.error("snapshot requires --case loop --publish --repeats 1")
    samples = []
    for _ in range(args.repeats):
        elapsed = (asyncio.run(measure_loop(args.steps, args.publish, args.snapshot))
                   if args.case == "loop" else measure_adapter(args.steps, args.tools))
        samples.append(elapsed)
        print(json.dumps({"sample_seconds": round(elapsed, 6)}), flush=True)
    print(json.dumps({"case": args.case, "steps": args.steps, "tools": args.tools if args.case == "adapter" else 1,
                      "publish": args.publish, "median_seconds": round(median(samples), 6),
                      "median_ms_per_step": round(median(samples) * 1000 / args.steps, 6)}))


if __name__ == "__main__":
    main()
