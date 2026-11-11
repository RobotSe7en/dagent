# Run retention heap and wire baseline

Measured on 2026-11-11 with Python 3.11.14, package 0.10.4, from
`2626349daf48ae3c38b1850d8ba6642c3c484234` plus the retention implementation in this change.
All three raw reports record `working_tree_differs=true` and the exact SDK source
SHA-256 `c449255a5175f47b0cd50f052bb48726ea3ed35efe10d11f186410b1bea52e7d`. This identifies the measured source independently
of the subsequent documentation/report commit. Reproduction commands:

```bash
uv run python -m benchmarks.run_retention --runs 24
uv run python -m benchmarks.run_retention --runs 16 --onchange
uv run python -m benchmarks.run_retention --runs 1 --steps 300 --output-chars 1024
```

The [script](../../benchmarks/run_retention.py) uses a synthetic offline provider,
legacy context, no `read_file`, four tool calls per root run, and 32,768 fresh text
characters per tool result. DAG mode has one child agent per root. No network,
request log, event log or batch log is retained. The host only acknowledges when
enabled; all events are drained and old results released before GC and sampling.

## Heap after 24 root runs

| Mode | Limit | Current heap MiB | Peak heap MiB | Last 8 spread bytes | States/checkpoints/child sessions | Unique attributed bytes |
| --- | --- | ---: | ---: | ---: | --- | ---: |
| tool | None | 7.030 | 8.577 | 2087727 | 24/24/0 | 5675302 |
| tool | 2 | 0.743 | 2.291 | 11904 | 2/2/0 | 485034 |
| tool | 0 | 0.171 | 1.864 | 12759 | 0/0/0 | 368 |
| dag | None | 8.175 | 9.201 | 2452655 | 24/24/24 | 6661564 |
| dag | 2 | 0.833 | 1.859 | 7749 | 2/2/2 | 571976 |
| dag | 0 | 0.166 | 1.337 | 7914 | 0/0/0 | 592 |

`None` grows with history. Limits 2 and 0 reach a plateau for this workload, apart
from lightweight ids and measurement overhead. The 16-run host subscription
variant's largest last-eight spread with a finite limit was 13763 bytes.
These are Python `tracemalloc` current/peak measurements, not RSS or serialized
sizes. Attribution recursively estimates the retained object graphs and counts
shared references once across all three stores; separate store estimates in the
raw reports must not be added together.

## One 300-tool run, 1,024 characters per tool result

| Mode | Record | UTF-8 bytes | JSON values |
| --- | --- | ---: | ---: |
| tool | RunState | 1498709 | 50156 |
| tool | checkpoint | 1505574 | 50204 |
| tool | run.finished | 2066509 | 63690 |
| dag | RunState | 2291976 | 76558 |
| dag | checkpoint | 2298942 | 76608 |
| dag | run.finished | 2292261 | 76571 |

Wire counts include containers and scalar values, exclude object keys, and use
compact UTF-8 JSON. Retention does not reduce the returned result or its
checkpoint. This is the next trace-design baseline; trace structure and
`new_items` semantics were not changed. A single long run still has a substantial
heap peak even with limit 0 (see raw samples); there is no claim of constant
within-run memory. Path lengths can change serialized sizes slightly.

Raw data: [24-run heap](../../benchmarks/run_retention_report.json),
[host subscription](../../benchmarks/run_retention_onchange_report.json),
[300-tool wire and heap](../../benchmarks/run_retention_long_report.json).
