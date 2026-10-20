r"""Opt-in real vLLM cache/long-task check; no endpoint is contacted without --live.

Example (the operator enables APC and selects a tool-capable chat template):
  uv run python -m examples.vllm_context_soak --live \
    --base-url http://localhost:8000/v1 --model MODEL --context-window 128 \
    --workspace /tmp/dagent-soak --report /tmp/dagent-soak-report.json

Defaults require 200 evidence calls, five compactions and one hour. This uses
synthetic data, never flushes a shared cache, and does not read config.yaml.
Lower --seconds/--steps values are useful smoke checks, not a full soak pass.
API keys come only from --api-key-env (OPENAI_API_KEY by default). Reports contain
numeric usage, not prompts, responses, keys or endpoint URLs. Conversation and
checkpoint artifacts are saved separately for failure investigation/continuation.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Sequence
from uuid import uuid4

from openai import AsyncOpenAI

import dagent


USAGE_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens", "cached_input_tokens")


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    command.add_argument("--live", action="store_true", help="Explicitly allow real model requests.")
    command.add_argument("--base-url")
    command.add_argument("--model")
    command.add_argument("--context-window", type=int, choices=(128, 256), default=128, help="Context window in Ki tokens.")
    command.add_argument("--seconds", type=float, default=3600)
    command.add_argument("--steps", type=int, default=200, help="Minimum executed evidence calls.")
    command.add_argument("--min-compactions", type=int, default=5)
    command.add_argument("--max-model-steps", type=int, default=600, help="Hard generation-step limit, including a final response.")
    command.add_argument("--timeout-seconds", type=float, default=600, help="Provider timeout per HTTP request.")
    command.add_argument("--api-key-env", default="OPENAI_API_KEY")
    command.add_argument("--workspace", type=Path, default=Path(".dagent-vllm-soak"))
    command.add_argument("--report", type=Path, default=Path("vllm-context-soak.json"))
    return command


def usage_dict(usage: Any) -> dict[str, int | None]:
    value = usage.model_dump() if hasattr(usage, "model_dump") else usage or {}
    return {name: value.get(name) if type(value.get(name)) is int else None for name in USAGE_FIELDS}


def usage_totals(calls: list[dict[str, Any]]) -> dict[str, Any]:
    # Missing fields remain distinguishable from a server-reported zero.
    return {name: {"known_sum": sum(call["usage"][name] or 0 for call in calls),
                   "unknown_calls": sum(call["usage"][name] is None for call in calls)}
            for name in USAGE_FIELDS}


def cache_status(cold: dict[str, Any], warm: dict[str, Any]) -> str:
    before, after = cold["usage"]["cached_input_tokens"], warm["usage"]["cached_input_tokens"]
    if before is None or after is None:
        return "unobserved"
    return "observed_improvement" if after > before else "no_observed_improvement"


async def cache_probe(provider: dagent.Provider, nonce: str) -> dict[str, Any]:
    # A fresh early nonce gives this test a distinct prefix without clearing APC.
    messages = [{"role": "system", "content": f"Cache probe {nonce}. Reply only OK."},
                {"role": "user", "content": "Reference data; acknowledge only.\n" + "stable cache evidence 0123456789\n" * 2048}]
    calls = []
    for _ in range(2):
        started = time.monotonic()
        response = await provider.chat(messages)  # Nonstream, identical messages/tools/controls.
        calls.append({"usage": usage_dict(response.usage), "elapsed_seconds": time.monotonic() - started,
                      "status": response.status})
    return {"status": cache_status(*calls), "calls": calls}


@dataclass
class EvidenceState:
    steps: int
    seconds: float
    minimum_compactions: int
    invariant: str
    started: float = field(default_factory=time.monotonic)
    indices: list[int] = field(default_factory=list)
    invariant_mismatches: int = 0
    compactions: int = 0

    def delay(self, now: float) -> float:
        # Pace the first steps over the requested duration, rather than spend
        # thousands of model calls polling a wall clock. No tool call is skipped.
        target = self.started + min(len(self.indices) + 1, self.steps) * self.seconds / self.steps
        return max(0.0, target - now)

    def record(self, index: int, invariant: str, now: float) -> dict[str, Any]:
        self.indices.append(index)
        self.invariant_mismatches += invariant != self.invariant
        elapsed = now - self.started
        return {"received_index": index, "executed_calls": len(self.indices),
                "next_index": len(self.indices) + 1, "elapsed_seconds": round(elapsed, 3),
                "compactions": self.compactions,
                "ready": len(self.indices) >= self.steps and elapsed >= self.seconds
                         and self.compactions >= self.minimum_compactions}

    def metrics(self, now: float) -> dict[str, Any]:
        return {"tool_calls": len(self.indices), "indices": self.indices,
                "duplicate_indices": len(self.indices) - len(set(self.indices)),
                "out_of_sequence_calls": sum(index != expected for expected, index in enumerate(self.indices, 1)),
                "invariant_mismatches": self.invariant_mismatches,
                "compactions": self.compactions, "elapsed_seconds": now - self.started}


def evidence_body(observation: dict[str, Any]) -> str:
    # High-entropy synthetic evidence makes context pressure realistic. The
    # frozen display cap bounds what is sent; the complete body remains readable.
    index = observation["executed_calls"]
    rows = (f"observation {index} row {row}: {hashlib.sha256(f'{index}:{row}'.encode()).hexdigest()}\n"
            for row in range(1536))
    return json.dumps(observation, sort_keys=True) + "\n" + "".join(rows)


def generation_calls(result: dagent.RunResult) -> list[dict[str, Any]]:
    calls = []
    pending = [result.trace.root]
    while pending:
        node = pending.pop()
        pending.extend(reversed(node.children))
        if node.kind == "model_call":
            output = node.output or {}
            metadata = output.get("model_call") or {}
            calls.append({"usage": usage_dict(output.get("usage")), "status": node.status,
                          "finish_reason": metadata.get("finish_reason"),
                          "attempts": metadata.get("attempts", [])})
    return calls


def compaction_calls(result: dagent.RunResult) -> tuple[list[dict[str, Any]], list[str]]:
    """Read declared record links to include real summary usage, once per record."""
    conversation = result.conversation
    roots = [conversation.summary.compaction_record] if conversation.summary and conversation.summary.compaction_record else []
    if conversation.compaction_state:
        roots.extend(failure.record for failure in conversation.compaction_state.failed_records)
    workspace = Path(result.workspace_path).resolve()
    seen: set[str] = set()
    calls, statuses = [], []
    pending = [reference.model_dump() for reference in roots]
    while pending:
        reference = pending.pop()
        if reference["sha256"] in seen:
            continue
        seen.add(reference["sha256"])
        path = (workspace / reference["path"]).resolve()
        if not path.is_relative_to(workspace):
            raise ValueError("Compaction metric resource escapes its workspace.")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise ValueError("Compaction metric resource checksum mismatch.")
        record = json.loads(raw)
        if record.get("type") != "dagent_compaction_record" or record.get("schema_version") != 1:
            raise ValueError("Unknown compaction metric resource.")
        statuses.append(record["status"])
        for call in record.get("calls", []):
            metadata = call.get("model_call") or {}
            calls.append({"kind": call["kind"], "usage": usage_dict(call.get("usage")),
                          "http_attempts": call["http_attempts"], "failed": call.get("error") is not None,
                          "finish_reason": metadata.get("finish_reason"), "attempts": metadata.get("attempts", [])})
        pending.extend(record[key] for key in ("previous_record", "reused_from") if record.get(key))
    return calls, statuses


def evaluate(metrics: dict[str, Any], args: argparse.Namespace, *, completed: bool, budget_safe: bool) -> dict[str, bool]:
    return {"completed": completed, "within_input_budgets": budget_safe,
            "requested_duration": metrics["elapsed_seconds"] >= args.seconds,
            "requested_tools": metrics["tool_calls"] >= args.steps,
            "requested_compactions": metrics["compactions"] >= args.min_compactions,
            "ordered_progress": metrics["out_of_sequence_calls"] == 0,
            "task_constraint_retained": metrics["invariant_mismatches"] == 0,
            "full_soak_duration": metrics["elapsed_seconds"] >= 3600,
            "full_soak_tools": metrics["tool_calls"] >= 200,
            "full_soak_compactions": metrics["compactions"] >= 5}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def run_live(args: argparse.Namespace) -> dict[str, Any]:
    nonce = uuid4().hex
    workspace = args.workspace.resolve() / nonce
    workspace.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {"schema_version": 1, "run_directory": str(workspace),
                              "requested": {name: getattr(args, name) for name in
                                            ("seconds", "steps", "min_compactions", "context_window", "max_model_steps")}}
    result = None
    state = None
    try:
        async with AsyncOpenAI(base_url=args.base_url, api_key=os.environ.get(args.api_key_env) or "not-required",
                               timeout=args.timeout_seconds, max_retries=0) as client:
            provider = dagent.Provider(base_url=args.base_url, model=args.model, client=client,
                                       timeout_seconds=args.timeout_seconds, protocol="chat_completions",
                                       token_counting="vllm", context_window_tokens=args.context_window * 1024,
                                       max_output_tokens=1024, capture_cache_usage=True, stream_include_usage=True)
            report["cache_probe"] = await cache_probe(provider, nonce)
            state = EvidenceState(args.steps, args.seconds, args.min_compactions, f"amber-{nonce[:12]}")

            @dagent.tool
            async def context_evidence(index: int, invariant: str) -> str:
                """Receive synthetic evidence; use next_index and stop only when ready is true."""
                delay = state.delay(time.monotonic())
                if delay:
                    await asyncio.sleep(delay)
                return evidence_body(state.record(index, invariant, time.monotonic()))

            agent = dagent.ToolAgent(
                profile=dagent.AgentProfile(name="vllm_context_soak", content=(
                    "Follow the user's synthetic evidence task. Call context_evidence once per turn. "
                    "Keep the exact invariant and monotonic next index through context maintenance. "
                    "When the runtime requests a task checkpoint instead, follow its checkpoint "
                    "format and preserve progress, evidence and the remaining stop conditions.")),
                capabilities=[context_evidence, "tool.read_file"], max_steps=args.max_model_steps,
                context=dagent.ContextPolicy(compaction_strategy="cache_aware", summary_max_tokens=4096,
                                             max_tool_result_tokens=8192, allow_reference_fallback=True),
            )
            task = (f"Call context_evidence starting at index=1, always invariant={state.invariant!r}. "
                    f"Collect at least {args.steps} observations over at least {args.seconds} seconds, "
                    f"with at least {args.min_compactions} context compactions. Each result gives "
                    "next_index, elapsed_seconds, compactions and ready. Continue with next_index "
                    "until ready=true; then reply SOAK_COMPLETE. Do not repeat earlier indices. "
                    "Evidence calls are deliberately paced; their runtime is expected. "
                    "Preserve these requirements and progress in every checkpoint.")
            with closing(dagent.Runner(provider=provider, workspace=workspace, skill_roots=[])) as runner:
                async for event in runner.stream(agent, input=task, workspace_path=workspace / "run"):
                    if event.type == "context.compaction.finished":
                        state.compactions += 1
                        print(f"Compactions={state.compactions}; evidence calls={len(state.indices)}", flush=True)
                    elif event.type in {"run.finished", "run.failed"}:
                        result = event.data.result
                        if event.type == "run.failed":
                            report["error_type"] = event.data.error_type
    except Exception as exc:
        result = getattr(exc, "result", None) or result
        report["error_type"] = type(exc).__name__
    if state:
        report["metrics"] = state.metrics(time.monotonic())
    if result is not None:
        # Save the recoverable SDK records without putting their text in the report.
        conversation_path = workspace / "conversation.json"
        write_json(conversation_path, result.conversation.model_dump(mode="json"))
        report["conversation_snapshot"] = str(conversation_path)
        if result.checkpoint:
            checkpoint_path = workspace / "checkpoint.json"
            write_json(checkpoint_path, result.checkpoint.model_dump(mode="json"))
            report["checkpoint_snapshot"] = str(checkpoint_path)
        generations = generation_calls(result)
        try:
            summaries, statuses = compaction_calls(result)
            report["compaction_record_statuses"] = statuses
        except (OSError, ValueError, KeyError) as exc:
            summaries = []
            report["summary_metrics_error_type"] = type(exc).__name__
        report["generation_calls"] = generations
        report["summary_calls"] = summaries
        report["generation_usage"] = usage_totals(generations)
        report["summary_usage"] = usage_totals(summaries)
        report["context_usage"] = [usage.model_dump(mode="json") for usage in result.context_usage]
        report["result_status"] = result.status
        report["checks"] = evaluate(report["metrics"], args, completed=result.status == "completed",
                                    budget_safe=bool(result.context_usage) and all(
                                        usage.estimated_input_tokens <= usage.input_budget_tokens for usage in result.context_usage))
    checks = report.get("checks", {})
    observed = report.get("cache_probe", {}).get("status") == "observed_improvement"
    report["status"] = ("passed" if checks and all(checks.values()) and observed
                        and "error_type" not in report and "summary_metrics_error_type" not in report else "not_passed")
    write_json(args.report, report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    command = parser()
    args = command.parse_args(argv)
    if not args.live:
        print("No requests sent. Use --live --base-url URL --model MODEL to run the vLLM soak.")
        return 0
    if not args.base_url or not args.model:
        command.error("--live requires --base-url and --model")
    if args.steps < 1 or args.seconds < 0 or args.min_compactions < 0 or args.max_model_steps < 1 or args.timeout_seconds <= 0:
        command.error("steps/max-model-steps/timeout must be positive; seconds/min-compactions must be nonnegative")
    report = asyncio.run(run_live(args))
    print(f"{report['status']}; cache={report.get('cache_probe', {}).get('status', 'unobserved')}; report={args.report}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
