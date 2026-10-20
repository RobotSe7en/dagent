"""Offline checks for the opt-in live harness; these do not measure vLLM APC."""

import hashlib
import json
from types import SimpleNamespace

import pytest

import dagent
from examples import vllm_context_soak as soak


def test_default_command_never_contacts_a_provider(monkeypatch, tmp_path, capsys):
    def forbidden(*_args, **_kwargs):
        pytest.fail("The default command must not start a live request")

    monkeypatch.setattr(soak, "run_live", forbidden)
    report = tmp_path / "report.json"
    assert soak.main(["--report", str(report)]) == 0
    assert "No requests sent" in capsys.readouterr().out
    assert not report.exists()
    options = soak.parser().parse_args([])
    assert (options.seconds, options.steps, options.min_compactions) == (3600, 200, 5)
    assert options.context_window == 128 and options.max_model_steps == 600


def test_live_requires_explicit_connection_and_valid_limits():
    with pytest.raises(SystemExit):
        soak.main(["--live"])
    with pytest.raises(SystemExit):
        soak.main(["--live", "--base-url", "http://localhost/v1", "--model", "test", "--steps", "0"])


@pytest.mark.asyncio
@pytest.mark.parametrize("cached,expected", [((0, 4096), "observed_improvement"),
                                            ((0, 0), "no_observed_improvement"),
                                            ((None, 4096), "unobserved"),
                                            ((0, None), "unobserved")])
async def test_probe_repeats_identical_nonstream_request_and_preserves_unknown(cached, expected):
    class Provider:
        def __init__(self):
            self.requests = []

        async def chat(self, messages):
            self.requests.append(json.loads(json.dumps(messages)))
            return SimpleNamespace(status="completed", usage=dagent.ModelTokenUsage(
                input_tokens=16000, output_tokens=1, cached_input_tokens=cached[len(self.requests) - 1]))

    provider = Provider()
    report = await soak.cache_probe(provider, "new-private-logical-prefix")
    assert report["status"] == expected
    assert provider.requests[0] == provider.requests[1]
    assert len(provider.requests[0][1]["content"]) > 60000
    assert "new-private-logical-prefix" not in json.dumps(report)


def test_evidence_paces_actual_calls_and_only_evaluates_repetition():
    state = soak.EvidenceState(steps=2, seconds=60, minimum_compactions=1, invariant="keep", started=100)
    assert state.delay(110) == 20
    first = state.record(1, "keep", 130)
    assert first["next_index"] == 2 and not first["ready"]
    assert state.delay(145) == 15
    state.compactions = 1
    second = state.record(1, "lost", 160)  # This is recorded, never blocked or deduplicated.
    assert second["ready"] and second["next_index"] == 3
    metrics = state.metrics(160)
    assert metrics["tool_calls"] == 2
    assert metrics["duplicate_indices"] == 1
    assert metrics["out_of_sequence_calls"] == 1
    assert metrics["invariant_mismatches"] == 1
    body = soak.evidence_body(second)
    assert json.loads(body.splitlines()[0]) == second
    assert len(body) > 100000


def test_smoke_settings_cannot_claim_full_soak_pass():
    args = soak.parser().parse_args(["--seconds", "0", "--steps", "1", "--min-compactions", "0"])
    metrics = {"elapsed_seconds": 0.1, "tool_calls": 1, "compactions": 0,
               "out_of_sequence_calls": 0, "invariant_mismatches": 0}
    checks = soak.evaluate(metrics, args, completed=True, budget_safe=True)
    assert checks["requested_duration"] and checks["requested_tools"]
    assert not checks["full_soak_duration"] and not checks["full_soak_tools"]
    assert not checks["full_soak_compactions"]


def test_generation_metrics_use_trace_and_never_copy_model_text():
    usage = soak.usage_dict(dagent.ModelTokenUsage(input_tokens=4000, cached_input_tokens=0))
    child = SimpleNamespace(kind="model_call", children=[], status="completed", output={
        "content": "DO NOT COPY FULL CONTENT", "reasoning": "PRIVATE REASONING",
        "usage": usage, "model_call": {"finish_reason": "tool_calls", "attempts": []}})
    result = SimpleNamespace(trace=SimpleNamespace(root=SimpleNamespace(kind="run", children=[child])))
    calls = soak.generation_calls(result)
    assert calls[0]["usage"]["cached_input_tokens"] == 0
    assert "PRIVATE" not in json.dumps(calls) and "CONTENT" not in json.dumps(calls)
    totals = soak.usage_totals(calls + [{"usage": soak.usage_dict(None)}])
    assert totals["cached_input_tokens"] == {"known_sum": 0, "unknown_calls": 1}
    assert totals["input_tokens"] == {"known_sum": 4000, "unknown_calls": 1}


def test_summary_metrics_follow_declared_record_chain_once(tmp_path):
    def save(record):
        raw = json.dumps(record).encode()
        digest = hashlib.sha256(raw).hexdigest()
        path = f"{digest}.json"
        (tmp_path / path).write_bytes(raw)
        return dagent.ContentReference(path=path, sha256=digest, byte_length=len(raw))

    old = save({"type": "dagent_compaction_record", "schema_version": 1, "status": "complete",
                "calls": [{"kind": "prefix", "http_attempts": 1, "usage": {"input_tokens": 8000,
                           "cached_input_tokens": 7000}, "model_call": {"finish_reason": "stop"}}]})
    current = save({"type": "dagent_compaction_record", "schema_version": 1, "status": "partial",
                    "previous_record": old.model_dump(), "reused_from": old.model_dump(),
                    "calls": [{"kind": "chunk", "http_attempts": 1, "error": "PRIVATE ERROR TEXT"}]})
    result = SimpleNamespace(workspace_path=tmp_path, conversation=SimpleNamespace(
        summary=SimpleNamespace(compaction_record=current),
        compaction_state=SimpleNamespace(failed_records=[SimpleNamespace(record=old)])))
    calls, statuses = soak.compaction_calls(result)
    assert len(calls) == 2 and sorted(statuses) == ["complete", "partial"]
    assert sum(call["usage"]["cached_input_tokens"] or 0 for call in calls) == 7000
    assert "PRIVATE" not in json.dumps(calls)
    (tmp_path / current.path).write_text("corrupt")
    with pytest.raises(ValueError, match="checksum"):
        soak.compaction_calls(result)


@pytest.mark.asyncio
async def test_live_failure_still_writes_safe_report_and_snapshot(monkeypatch, tmp_path):
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "secret-key"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Provider:
        def __init__(self, **kwargs):
            assert kwargs["capture_cache_usage"] and kwargs["token_counting"] == "vllm"

        async def chat(self, _messages):
            return SimpleNamespace(status="completed", usage=dagent.ModelTokenUsage(input_tokens=100))

    conversation = dagent.ConversationState(items=(dagent.UserMessage(content="SYNTHETIC FULL TASK"),))
    result = SimpleNamespace(conversation=conversation, checkpoint=None, status="failed", context_usage=(),
                             trace=SimpleNamespace(root=SimpleNamespace(kind="run", children=[])))

    class Runner:
        def __init__(self, **_kwargs):
            pass

        def close(self):
            pass

        async def stream(self, _agent, **kwargs):
            result.workspace_path = kwargs["workspace_path"]
            result.workspace_path.mkdir()
            yield SimpleNamespace(type="run.failed", data=SimpleNamespace(result=result, error_type="SyntheticFailure"))

    monkeypatch.setenv("SOAK_TEST_KEY", "secret-key")
    monkeypatch.setattr(soak, "AsyncOpenAI", Client)
    monkeypatch.setattr(soak.dagent, "Provider", Provider)
    monkeypatch.setattr(soak.dagent, "Runner", Runner)
    args = soak.parser().parse_args(["--live", "--base-url", "http://private-host/v1?key=secret-query",
                                    "--model", "test", "--api-key-env", "SOAK_TEST_KEY",
                                    "--workspace", str(tmp_path / "workspace"), "--report", str(tmp_path / "report.json")])
    report = await soak.run_live(args)
    assert report["status"] == "not_passed" and report["cache_probe"]["status"] == "unobserved"
    assert not report["checks"]["completed"]
    text = args.report.read_text()
    assert all(value not in text for value in ("secret-key", "secret-query", "private-host", "SYNTHETIC FULL TASK"))
    assert "SYNTHETIC FULL TASK" in soak.Path(report["conversation_snapshot"]).read_text()
