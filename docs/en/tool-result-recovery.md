# Recover large tool results

Unreleased: tool results use a minimum-information-first display budget. This
applies to ToolAgent replies and structured DAG planner observations.

## Configure display and storage separately

```python
import dagent

context = dagent.ContextPolicy(
    max_tool_result_tokens=2048,
    max_total_tool_result_tokens=16384,
)
storage = dagent.ResultStoragePolicy(
    max_inline_bytes=256 * 1024,
    max_shell_output_bytes=64 * 1024 * 1024,
)
```

Pass `context` to your agent and `result_storage_policy=storage` to `Runner`.
The display limits include status, excerpt, truncation notices and recovery
references. Each result receives a short deterministic excerpt before remaining
tokens are assigned newest-first. No extra model call generates these excerpts.
If minimum information cannot fit, completed atomic exchanges are compacted. If
the protected latest exchange still cannot fit, generation raises
`ContextWindowExceeded`; results are never silently replaced with empty strings.

The inline threshold is independent: a smaller result can also be written before
its model display is truncated. Projection does not replace a structured tool
value with its excerpt. Static DAG value expressions retain their full-data
rehydration behavior.

## Read the original

Enable `tool.read_file` explicitly when the agent should recover saved text.
The SDK does not automatically add capabilities or expand path permissions.
Result references are relative to the run workspace, under the configured
runtime directory's `results` directory. The model is told when direct reading
is unavailable, denied or missing. A reference is not a permanent download URL.

`tool.read_file` keeps its existing one-based line `offset` and `limit`.
For long lines and exact continuation, use `offset_chars` (zero-based Unicode
characters, excluding an initial UTF-8 BOM) and `limit_chars` (default 1024).
Do not combine character offsets with non-default line parameters. Character
pages return exact text. Runtime retention metadata carries the continuation;
model display adjusts it to the prefix actually shown. Reading a result file
does not recursively create another result file.

`tool.grep` and `tool.list_files` accept a zero-based entry `offset` and `limit`.
Their maximum page sizes remain 200 matches and 500 entries. `list_files.value`
is still a list containing only the current page's entries. Query pages are
fresh filesystem queries, not snapshots: files may change between calls.

## Interpret completeness and errors

Execution status and output retention are independent. A completed tool can
have a partial saved record or a storage warning.

- `[EMPTY_RESULT]`: the tool genuinely returned no display content or artifacts.
- `[TRUNCATED]`: the model sees only an excerpt of received content.
- `[SOURCE_TRUNCATED]`: the source was bounded or returned a window.
- `[RECOVERY_UNAVAILABLE]`: a saved original or the required reading access is
  unavailable; do not assume the omitted data can be reconstructed.

Shell stdout and stderr are drained concurrently with bounded memory and saved
under a shared per-call capture limit (64 MiB by default). Above that limit the
command continues, pipes keep draining, and a bounded tail remains visible.
The saved record is explicitly partial. Exit codes, timeouts and cancellations
are preserved. This capture limit is not a workspace-wide disk quota.

Text-storage I/O failure produces `tool_result_storage_warning` through the
runtime event callback (`capability.result.storage_warning` in `Runner.stream`),
retains the actual execution status, and allows bounded
model display to continue. Received text may remain inline beyond the normal
threshold in this degraded case. It does not rerun the tool. Failure to store a
required binary value or artifact instead raises a result-storage error and
blocks dependent execution; the original execution result is retained on the
exception. A failed DAG result returns `checkpoint=None`. Its serializable audit
retains execution status and marks any omitted in-memory binary value in
`retention.unavailable_fields`; that marker is not a recovery reference.

MCP retention covers what the SDK actually receives. It cannot restore content
already discarded by a remote server or infer remote completeness. No remote
resource is downloaded automatically.

## Inspect results after compaction

`ContextSummary.result_manifest` references an immutable, line-oriented index
of earlier tool results and calls. Runtime code retains this reference separately
from model-generated summary prose. `result_archive_incomplete` explicitly marks
an archival failure. Later compactions retain access to earlier index entries.

`CapabilityResult.retention` and `ToolResultMessage.retention` contain typed
source completeness, continuation and storage warnings. Their types live in
`dagent.schemas.retention`. Planner observation entries live in
`UserMessage.result_observations`; they are data, not synthetic tool replies.
`ContextUsage` distinguishes result body/metadata tokens, source truncation and
unavailable recovery. References alone do not count as body truncation.

Keep the workspace with a checkpoint when resuming elsewhere. Hosts own copying,
retention periods, cleanup, redaction and disk quotas. More eager preservation
uses disk space and can persist sensitive tool output; files are not automatically
deleted when the runner closes.

Run the offline example with `uv run python -m examples.tool_result_recovery`.
