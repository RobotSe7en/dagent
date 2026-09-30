# Recover large tool results

Since 0.9.9, tool results use a minimum-information-first display budget. This
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

Display budgets are upper bounds. After history and reasoning reductions, a
request that still exceeds its hard input budget gets a smaller request-local
tool-result budget. This targets the configured compaction trigger while
preserving the same minimum-information and newest-first allocation rules.
The current user input, latest reasoning and tool-call/result pairing remain.
Only the final display changes; configured policy and structured values do not.
Newly shortened text is saved through the existing storage policy, and the
complete request is recounted with the resulting recovery references. If the
minimum display fits only above the soft trigger, the request may still run.
If it exceeds the hard budget, generation fails explicitly.
If a result's reference manifest cannot be saved, its warning is retained and
refitting does not retry the failed write or append duplicate warnings.

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
required binary value or artifact instead stops execution. DAG runs return a
failed result; Tool runs raise public `dagent.RunExecutionError` with a failed
`exc.result`, or emit `run.failed` with `event.data.result` when streaming.
These failure results expose `checkpoint=None`. Their serializable trace audit
retains execution status and marks any omitted in-memory binary value in
`retention.unavailable_fields`; that marker is not a recovery reference.
See [failure auditing](results-streaming-review.md#audit-a-tool-execution-failure).

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
Its first run uses display ceilings larger than the model window and verifies
that automatic shortening preserves the report for later reading.

## Distinguish source windows from model display

Since 0.9.11, file results show separate source and display coordinates:

```text
[status=completed]
[TRUNCATED] model display shortened; received result has undisplayed text
[File window: chars=[0,20352); source_eof=true; unit=Unicode code points, zero-based, end-exclusive]
[Displayed: chars=[0,1900)]
[Continue with read_file: {"path": "report.txt", "offset_chars": 1900, "limit_chars": 1024}]
...exact source characters 0 through 1899...
```

The numbers illustrate a projection, not a fixed token-to-character conversion.
A 59,980-byte, 20,352-character, 500-line file can be completely returned by the
tool yet only partly displayed under a 2048-token budget. Metadata also consumes
that budget. `source_eof=true` means the **returned window** reached EOF; it does
not mean the model saw the whole file or that a window beginning later covered
the file prefix. `source_eof=false` and `[SOURCE_TRUNCATED]` mean more source
follows the returned window. `[TRUNCATED]` independently means model display was
shortened. Ordinary source pagination alone does not set display truncation.

Ranges are `[start,end)` in Unicode code points, not bytes, lines, grapheme
clusters or tokens. An initial UTF-8 BOM is excluded; LF/CRLF terminators are
preserved (CRLF counts as two code points). The displayed file body is always
a continuous prefix of the returned window. The continuation uses its actual
displayed end, including when a line window is cropped mid-line. Complete
display at EOF has no model-facing continuation. A zero-body projection reports
an empty displayed range and does not advance its cursor. Minimum information still
raises the existing budget error if it cannot fit.

The total tool-result budget can shorten an earlier result in later requests.
Its displayed range and cursor are recomputed for that request;
`ResultRetention.window_start` / `window_length` continue to describe the
original returned source window. This is not a global reading-progress ledger
and does not undo earlier calls. Track progress from calls and results actually
observed, avoid identical file/interval requests within a batch, and do not
restart or change tools solely because a display was shortened when the file
has not changed. Reads are fresh filesystem queries, not snapshots.

These are tool descriptions and default prompt guidance, not execution deduplication,
automatic reading, retries or recovery. Global display budgets are unchanged.
The offline recovery example now prints these model-facing ranges and cursors.
