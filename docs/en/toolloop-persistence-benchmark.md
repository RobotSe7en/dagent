# ToolLoop persistence V2: wire and memory measurements

Measured on 2026-11-11 with Python 3.11.14. This is the
unreleased implementation, not a published package benchmark. The full
[raw report](../../benchmarks/toolloop_persistence_report.json) records source
revision `4ac6bcdce2b198868e2daad4ceb9f635a7d7e783`, a modified worktree, SDK source SHA-256
`57058eea757a0c72cd3003a723cf891eae048ea49db483e2419b941ead412d4a`, and the benchmark/example harness hash. All cases use the same hashes.

## Method

An offline synthetic provider performs four tool calls per ordinary root run,
returning 32,768 text characters each, with an 8,192-token context window, 512-token
output allowance, existing legacy compaction and no read_file. A second profile
uses one delegated leaf ToolAgent. Held-review runs do not execute any tool.
The script measures None/2/0 retention; tables below use 0 unless stated.
No request, event or historical batch log is held in memory. The host reducer
retains only active context, rather than a complete durable store. Events are
drained, references released, and GC run before sampling.

UTF-8 sizes are compact JSON of SDK stream events, including display and durable
batches. The report also measures observer serialization time under tracemalloc;
those timings are diagnostic, not production latency guarantees. Heap values are
Python allocations, not RSS. They include the active host projection, replay IDs,
SDK caches and serialization observation. Attributed SDK object graphs count
shared references once. Default DAG measurements in the normal case are historical
API comparisons only; there is no DAG persistent mode.

## Stream traffic

| Workload | Normal stream bytes | Persistent stream bytes | Reduction |
| --- | ---: | ---: | ---: |
| 16 independent runs | 8,945,268 | 2,619,994 | 70.7% |
| 24 turns, one conversation | 13,444,404 | 4,031,250 | 70.0% |
| 16 delegated ToolLoop runs | 4,697,942 | 2,638,022 | 43.8% |

For the last independent run, the full terminal event was 422,567
bytes; the persistent terminal batch was 5,050 bytes.
A 1 ms delay at every ACK produced the same 2,619,994 stream bytes as the
undelayed ordinary case, with a measured ready-queue high-water mark of
2 events. This workload does not exercise the
maximum frame size; a separate slow-token-reader regression checks backpressure
and exact content preservation at a two-event/256-byte ready-queue budget.

## Heap and retention

| Workload | Normal peak MiB | Persistent peak MiB | Normal post-GC bytes | Persistent post-GC bytes |
| --- | ---: | ---: | ---: | ---: |
| Independent | 1.665 | 0.823 | 163,679 | 170,139 |
| Same conversation | 1.675 | 0.883 | 343,359 | 348,797 |
| Delegation | 1.255 | 0.876 | 178,398 | 178,731 |

Both modes already release ordinary history with retention 0. Persistent mode
reduces transient trace/result/serialization cost; it does not promise a lower
post-GC heap in every case. Same-conversation samples retain the active context.
Unlimited ordinary retention still grows and is not silently overridden.

For 24 held reviews with unlimited ordinary retention, normal mode retained
24 states/24 checkpoints and
1,650,831 post-GC bytes. Persistent mode retained
0 states/0 checkpoints and
267,901 bytes after checkpoint ACKs. All retention
settings release persistent review state. Replay IDs remain, so total memory is
not strictly constant with time.

## Short review traffic tradeoff

Normal review stream traffic was 194,756 bytes; persistent
review traffic was 379,024 bytes. Persistent review batches include the
portable V9 checkpoint and frozen plan. A normal serialized RunResult excludes
its plan, so the normal stream alone cannot provide that portable recovery record.
A host's additional checkpoint transport is outside this SDK-event measurement.
Consequently short reviews can have larger SDK event traffic even though their
checkpoint is trace-free and their retained memory is smaller. V9 full state and
its duplicated fields remain; this change adds no compact checkpoint protocol.

## Reproduce

```bash
uv run python -m benchmarks.run_retention --runs 16 --steps 4 --output-chars 32768
uv run python -m benchmarks.run_retention --runs 16 --steps 4 --output-chars 32768 --persistent
uv run python -m benchmarks.run_retention --runs 24 --steps 4 --output-chars 32768 --same-conversation
uv run python -m benchmarks.run_retention --runs 24 --steps 4 --output-chars 32768 --same-conversation --persistent
uv run python -m benchmarks.run_retention --runs 16 --steps 4 --output-chars 32768 --delegate
uv run python -m benchmarks.run_retention --runs 16 --steps 4 --output-chars 32768 --delegate --persistent
uv run python -m benchmarks.run_retention --runs 24 --hold-reviews
uv run python -m benchmarks.run_retention --runs 24 --hold-reviews --persistent
uv run python -m benchmarks.run_retention --runs 16 --steps 4 --output-chars 32768 --persistent --ack-delay 0.001
```

See [persistent streams](conversation-changes.md) for contracts and
[earlier retention measurements](run-retention-benchmark.md) for the prior
callback/trace baseline. Exact request-prefix regression tests, rather than
this offline byte benchmark, verify host-rebuilt cache inputs. Provider cache
hits are not measured here. No hard frame bound or chunking is introduced.
