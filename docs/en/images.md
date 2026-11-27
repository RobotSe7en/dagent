# Give models images

**Available since 0.11.4.** Install normally; Pillow is a
base dependency. Choose a model and endpoint that accept visual inputs.

## Upload images explicitly

Use the existing upload API and mark each image the model should see:

```python
from pathlib import Path
import dagent

runner = dagent.Runner(provider=provider, workspace="agent-workspace")
agent = dagent.ToolAgent(profile="conversation", capabilities=[])
result = await runner.run(
    agent,
    input="Compare these screenshots.",
    input_uploads=[
        dagent.ArtifactUpload("before.png", Path("before.png").read_bytes(), model_input="image"),
        dagent.ArtifactUpload("after.png", Path("after.png").read_bytes(), model_input="image"),
    ],
)
```

`model_input` is keyword-only and defaults to `None`. Unmarked files retain the
released upload behavior: the model receives file metadata, including for image
files. Marked images follow the request text, in upload order. Mixed image/file
uploads and multiple images are supported. `Runner.stream` accepts the same
uploads. Pass `input=""` for an image-only turn; `input=None` keeps its existing
persistent-continuation meaning and cannot carry new uploads.

PNG, JPEG, WebP and non-animated GIF are supported. SDK validation uses actual
file contents; an explicitly supplied `media_type` must match. Corrupt files,
animations and unsupported formats fail explicitly. Images are sent without
automatic resizing, conversion or a `detail` override.

For an explicit visual request, overrides of `messages` or `input` must retain
the selected images in order; conflicting overrides fail before generation.
Other override precedence remains unchanged.

## Let an agent read a workspace image

Image reading is an explicit capability registration:

```python
runner.add_image_tools()
agent = dagent.ToolAgent(
    profile="conversation",
    capabilities=["tool.read_image"],
)
result = await runner.run(agent, input="Read uploads/screenshot.png and describe it.")
```

`tool.read_image(path)` uses existing file boundaries and review rules. It is a
parallel-safe read and supports the built-in sandbox transport. Register it in
each fresh Runner that resumes a checkpoint using that tool. It is absent from
the default tool catalog until registration.

A Python function can return a typed image directly:

```python
@dagent.tool
def screenshot() -> dagent.ImageData:
    return dagent.ImageData.from_bytes(capture_png(), media_type="image/png")
```

Custom capability handlers can return `CapabilityResult.completed(...,
images=[ImageData.from_bytes(...)])`. A validated existing snapshot can be
returned as an `ImageReference` instead. Ordinary bytes and Pydantic tool
results retain their existing meanings.

## Receive images from MCP

Enable visual tool results per MCP server:

```python
runner.add_mcp_server("browser", {
    "command": "your-browser-mcp",
    "args": [],
    "image_results_as_input": True,
})
```

The option defaults to `False`. Existing image artifacts continue to be saved
without being submitted as visual inputs. When enabled, image blocks are
validated and their snapshots are shared with artifact references. Audio and
other artifact behavior is unchanged. Image results remain associated with the
originating tool call. Responses uses native image tool outputs; Chat
Completions places associated image data after all replies in the tool group.

## Use images in a static DAG

`artifact_uploads` retains its file semantics. Declare the artifact as an input
of the Agent node and give that agent `tool.read_image`. The node can then read
its permitted file paths. `model_input="image"` is only valid in `input_uploads`;
using it in `artifact_uploads` raises an error. Dependencies and edges remain
explicit. See the [static DAG example](../../examples/static_dag_images.py).

## Configure limits and context budgets

```python
runner = dagent.Runner(
    provider=provider,
    image_input_policy=dagent.ImageInputPolicy(
        max_image_bytes=20 * 1024 * 1024,
        max_image_pixels=40_000_000,
        max_images_per_request=16,
        max_total_image_bytes=100 * 1024 * 1024,
        estimated_tokens_per_image=8192,
    ),
)
```

These are the defaults. The SDK prefers counters that explicitly include image
tokens. Otherwise ordinary requests use the configured per-image estimate plus
the text estimate and safety margin. Base64 length is not counted as text.
The per-image estimate is a planning value, not actual usage or a guaranteed
upper bound. `ContextUsage.image_count` and `estimated_image_tokens` expose the
visual budget; absent images leave released statistics unchanged.

Unverified image counts do not discard a server's discovered context window.
The SDK keeps that limit while estimating the visual input locally. Available
image-aware exact counters are called even when the per-image estimate exceeds
the window; the estimate alone cannot prove that an image is too large.

Image count and total-byte limits apply to each model request. When older
unsummarized images exceed either limit, preparation first summarizes eligible
history and then checks the final request. The initiating user input and latest
atomic tool exchange remain protected; if those inputs alone exceed a limit,
preparation fails explicitly. Single-image byte, pixel and format checks always
run immediately. Legacy compaction includes visual tokens in retention and
summary budgets, batching sources between complete tool exchanges when needed.

Strict budgets, including `chunked` and `cache_aware` context strategies,
require a counter declaring both `exact=True` and `includes_image_tokens=True`.
A plain tokenizer/render response without that assurance cannot prove a visual
budget; those requests fail before generation. Custom counters return
`dagent.providers.model_io.ModelTokenCount` with this coverage declaration.
Actual billing usage remains the provider's `ModelTokenUsage`.

All three compaction strategies send selected image content to the summarizer.
A successful visual summary preserves snapshot references. Failed visual summaries
never replace selected images with a metadata-only fallback, including when
reference fallback is enabled for `chunked` or `cache_aware`. If continuation needs
that fallback, preparation fails explicitly and retains the original items.
Already summarized images remain recoverable when later text-only history uses fallback.
The summary contributes text and an image-index notice to later requests;
archived images are not automatically resubmitted as visual inputs. See the
[image history example](../../examples/image_history.py) for a twenty-turn run.

`ContextSummary.image_manifest` points to a JSONL index of archived `ImageReference`
records. Requests contain one bounded index notice instead of every snapshot path,
so recovery metadata does not grow with the image history. An agent needs
`read_file` to read the index and `read_image` to inspect a selected snapshot;
both tools must be allowed by its file boundaries. Preserve the index resource
alongside the image snapshots when restoring a conversation. Earlier visual
summaries without an index acquire one during context preparation.

Configured result validation also receives the images returned by dynamic DAG
nodes in a v5 validator conversation. Missing or corrupt evidence raises
`ImageInputError` instead of skipping validation. See the offline
[visual validation example](../../examples/image_validation.py).

## Persist and restore what the model saw

Uploaded workspace files remain mutable. Each explicitly selected visual input
has a separate content-addressed snapshot containing its original bytes.
Editing or deleting the workspace file does not change earlier visual inputs;
call `read_image` again to inspect a new version.

`ImageReference` contains a `ContentReference` with a required SHA-256 digest,
media type, byte length and image dimensions. Runner-recorded messages, normalized tool results
and checkpoints carry references rather than Base64. Restore those snapshot
resources when moving a conversation to a new workspace. Missing or modified
snapshots fail explicitly. Catch `dagent.ImageInputError` for invalid visual inputs;
invalid tool images become failed tool results with rejected image artifacts removed.
Image context errors carry the existing failed-run audit through their `result`
attribute once execution has started, including completed work when resuming DAG reviews.

Visual conversations use `ConversationState.schema_version=5`; text-only
conversations retain v4. Hosts applying conversation-change batches must use
`batch.conversation_schema_version` when reconstructing state. Existing resource
delivery and ACK rules also apply to image snapshots. Long-term storage and
resource retention remain host responsibilities.

Offline wiring examples: [upload](../../examples/image_uploads.py),
[read a file](../../examples/image_reading.py), [MCP](../../examples/image_mcp.py)
and [static DAG](../../examples/static_dag_images.py). Their MockProvider replies
are deterministic; replace the provider with a visual model for image analysis.
