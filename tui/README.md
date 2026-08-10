# dagent TUI

`dagent-tui` is a terminal client for the existing dagent FastAPI host. It does
not embed the SDK or duplicate host persistence. Conversations, run state,
reviews, and cancellation continue to be owned by the API.

## Run locally

Start the existing API from the repository root:

```bash
uv run --extra dev uvicorn api.app:app --port 8001
```

In another terminal, install and run the TUI:

```bash
uv run --project tui dagent-tui --api-url http://127.0.0.1:8001
```

The API URL can also be set with `DAGENT_API_URL`.

## First-version scope

- standalone and project conversation navigation;
- persisted message history;
- streamed answer and reasoning display;
- capability activity plus DAG/trace inspection;
- approve/reject review flow with optional feedback;
- active-run cancellation and request retry.

The TUI deliberately renders DAGs as terminal-friendly node/edge summaries. It
does not attempt to reproduce the graphical DAG editor or rich document
previewer from the WebUI.

See [CHANGELOG.md](CHANGELOG.md) for version notes and known limitations.

## Keys

| Key | Action |
| --- | --- |
| `Ctrl+N` | Start a new standalone conversation |
| `Ctrl+R` | Retry the last prompt |
| `Ctrl+C` | Cancel the active run |
| `F5` | Refresh projects and conversations |
| `Ctrl+Q` | Quit |

## Test

```bash
uv run --project tui --extra dev pytest tui/tests
```
