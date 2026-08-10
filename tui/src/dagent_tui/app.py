"""Textual application for the dagent API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Select, Static, Tree

from dagent_tui.client import DagentApi, DagentApiError
from dagent_tui.formatting import activity_text, dag_text, trace_text
from dagent_tui.models import (
    Conversation,
    ConversationMessage,
    Navigation,
    ReviewDecision,
    ReviewLevel,
    RunTarget,
    StreamEnvelope,
)


class ChatMessage(Static):
    """A mutable chat message used while stream deltas arrive."""

    def __init__(self, role: str, content: str = "") -> None:
        super().__init__(classes=f"chat-message {role}")
        self.role = role
        self.content = content

    def append(self, delta: str) -> None:
        self.content += delta
        self.refresh(layout=True)

    def render(self) -> Text:
        labels = {
            "user": ("You", "bold cyan"),
            "assistant": ("Agent", "bold green"),
            "reasoning": ("Reasoning", "bold magenta"),
            "system": ("System", "bold yellow"),
            "error": ("Error", "bold red"),
        }
        label, style = labels.get(self.role, (self.role.title(), "bold"))
        text = Text()
        text.append(f"{label}\n", style=style)
        text.append(self.content or "…", style="dim italic" if self.role == "reasoning" else "")
        return text


class ChatView(VerticalScroll):
    async def add_message(self, role: str, content: str = "") -> ChatMessage:
        message = ChatMessage(role, content)
        await self.mount(message)
        self.scroll_end(animate=False)
        return message

    async def reset(self) -> None:
        await self.remove_children()


class ReviewScreen(ModalScreen[ReviewDecision | None]):
    """Approve or reject one host-owned pending review."""

    BINDINGS = [Binding("escape", "dismiss_review", "Close")]

    def __init__(self, review: dict[str, Any], dag: dict[str, Any] | None) -> None:
        super().__init__()
        self.review = review
        self.dag = dag

    def compose(self) -> ComposeResult:
        kind = str(self.review.get("kind") or "review")
        message = str(self.review.get("message") or "This run requires review.")
        detail = Text()
        detail.append(f"{kind}\n", style="bold yellow")
        detail.append(message)
        capability_call = self.review.get("capability_call")
        if isinstance(capability_call, dict):
            detail.append("\n\nCapability\n", style="bold")
            detail.append(str(capability_call.get("capability_id") or capability_call.get("tool_name") or ""))
            arguments = capability_call.get("arguments")
            if arguments:
                detail.append(f"\nArguments: {arguments}", style="dim")
        elif self.dag:
            detail.append("\n\n")
            detail.append_text(dag_text(self.dag))
        with Vertical(id="review-dialog"):
            yield Label("Human review", id="review-title")
            yield Static(detail, id="review-detail")
            yield Input(placeholder="Optional reviewer feedback", id="review-feedback")
            with Horizontal(id="review-actions"):
                yield Button("Reject", id="reject-review", variant="error")
                yield Button("Approve", id="approve-review", variant="success")

    def on_mount(self) -> None:
        self.query_one("#review-feedback", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        feedback = self.query_one("#review-feedback", Input).value
        if event.button.id == "approve-review":
            self.dismiss(ReviewDecision(approved=True, feedback=feedback))
        elif event.button.id == "reject-review":
            self.dismiss(ReviewDecision(approved=False, feedback=feedback))

    def action_dismiss_review(self) -> None:
        self.dismiss(None)


class DagentTui(App[None]):
    """A terminal workbench backed entirely by the existing HTTP API."""

    TITLE = "dagent"
    SUB_TITLE = "Terminal workbench"

    CSS = """
    Screen {
        background: $surface;
    }

    #workspace {
        height: 1fr;
    }

    #sidebar {
        width: 28;
        min-width: 20;
        border-right: solid $primary-background;
    }

    #sidebar-title, #activity-title, #graph-title {
        height: 1;
        padding: 0 1;
        text-style: bold;
        color: $text-muted;
    }

    #conversation-tree {
        height: 1fr;
        padding: 0 1;
    }

    #sidebar-actions {
        height: 3;
        padding: 0 1;
    }

    #sidebar-actions Button {
        width: 1fr;
        min-width: 8;
    }

    #chat-column {
        width: 1fr;
        min-width: 38;
    }

    #chat {
        height: 1fr;
        padding: 1 2;
    }

    .chat-message {
        width: 100%;
        height: auto;
        min-height: 3;
        margin: 0 0 1 0;
        padding: 1 2;
        background: $panel;
    }

    .chat-message.user {
        border-left: thick $accent;
    }

    .chat-message.assistant {
        border-left: thick $success;
    }

    .chat-message.reasoning {
        border-left: thick $secondary;
        background: $boost;
    }

    .chat-message.system, .chat-message.error {
        border-left: thick $warning;
    }

    #composer-options {
        height: 3;
        padding: 0 1;
    }

    #target-select {
        width: 18;
    }

    #review-select {
        width: 18;
    }

    #review-button {
        width: 18;
        display: none;
    }

    #prompt {
        dock: bottom;
        margin: 0 1 1 1;
    }

    #inspector {
        width: 38;
        min-width: 28;
        border-left: solid $primary-background;
    }

    #activity {
        height: 2fr;
        padding: 0 1;
        border-bottom: solid $primary-background;
    }

    #graph {
        height: 3fr;
        padding: 0 1;
    }

    #status {
        dock: bottom;
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: $panel;
    }

    ReviewScreen {
        align: center middle;
        background: $background 65%;
    }

    #review-dialog {
        width: 76;
        max-width: 92%;
        height: 28;
        max-height: 85%;
        padding: 1 2;
        border: heavy $warning;
        background: $panel;
    }

    #review-title {
        height: 2;
        text-style: bold;
    }

    #review-detail {
        height: 1fr;
        overflow-y: auto;
        margin-bottom: 1;
    }

    #review-feedback {
        height: 3;
    }

    #review-actions {
        height: 3;
        align-horizontal: right;
    }

    #review-actions Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+n", "new_conversation", "New"),
        Binding("ctrl+r", "retry", "Retry"),
        Binding("ctrl+c", "cancel_run", "Cancel"),
        Binding("f5", "refresh_navigation", "Refresh"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        api_url: str = "http://127.0.0.1:8001",
        api: DagentApi | None = None,
    ) -> None:
        super().__init__()
        self.api = api or DagentApi(api_url)
        self._conversation: Conversation | None = None
        self._navigation = Navigation()
        self._busy = False
        self._active_run_id: str | None = None
        self._run_worker: Any = None
        self._last_prompt: str | None = None
        self._pending_review: dict[str, Any] | None = None
        self._dag: dict[str, Any] | None = None
        self._trace: dict[str, Any] | None = None
        self._stream_messages: dict[tuple[str, str], ChatMessage] = {}
        self._content_seen = False
        self._stream_failed = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                yield Label("Conversations", id="sidebar-title")
                yield Tree("dagent", id="conversation-tree")
                with Horizontal(id="sidebar-actions"):
                    yield Button("New", id="new-conversation")
                    yield Button("Refresh", id="refresh-conversations")
            with Vertical(id="chat-column"):
                yield ChatView(id="chat")
                with Horizontal(id="composer-options"):
                    yield Select(
                        (("Auto", "auto"), ("Tool", "tool"), ("DAG", "dag")),
                        value="auto",
                        allow_blank=False,
                        id="target-select",
                    )
                    yield Select(
                        (("Fast review", "fast"), ("Careful review", "careful")),
                        value="fast",
                        allow_blank=False,
                        id="review-select",
                    )
                    yield Button("Open review", id="review-button", variant="warning")
                yield Input(placeholder="Ask dagent…", id="prompt")
            with Vertical(id="inspector"):
                yield Label("Activity", id="activity-title")
                yield RichLog(id="activity", wrap=True, markup=False)
                yield Label("DAG / Trace", id="graph-title")
                yield RichLog(id="graph", wrap=True, markup=False)
        yield Static("Connecting…", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#graph", RichLog).write(dag_text(None))
        self.run_worker(
            self._refresh_navigation(select_first=True),
            group="navigation",
            exclusive=True,
        )

    async def on_unmount(self) -> None:
        await self.api.aclose()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt":
            return
        prompt = event.value.strip()
        if not prompt or self._busy:
            return
        event.input.value = ""
        await self._start_prompt(prompt)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-conversation":
            self.action_new_conversation()
        elif event.button.id == "refresh-conversations":
            self.action_refresh_navigation()
        elif event.button.id == "review-button":
            self.action_open_review()

    def on_tree_node_selected(self, event: Tree.NodeSelected[Any]) -> None:
        conversation = event.node.data
        if not isinstance(conversation, Conversation) or self._busy:
            return
        if self._conversation is not None and conversation.id == self._conversation.id:
            return
        self.run_worker(
            self._load_conversation(conversation),
            group="conversation-load",
            exclusive=True,
        )

    def action_new_conversation(self) -> None:
        if self._busy:
            self._set_status("A run is active; cancel it before changing conversations.", error=True)
            return
        self._conversation = None
        self._pending_review = None
        self._dag = None
        self._trace = None
        self._set_review_button(False)
        self.run_worker(self._clear_workbench(), group="conversation-load", exclusive=True)

    def action_refresh_navigation(self) -> None:
        self.run_worker(self._refresh_navigation(), group="navigation", exclusive=True)

    def action_retry(self) -> None:
        if self._busy:
            self._set_status("A run is already active.", error=True)
            return
        if not self._last_prompt:
            self._set_status("There is no prompt to retry.", error=True)
            return
        self.run_worker(self._start_prompt(self._last_prompt), group="retry", exclusive=True)

    def action_cancel_run(self) -> None:
        if not self._busy or not self._active_run_id:
            self._set_status("There is no active run to cancel.")
            return
        self.run_worker(self._cancel_active_run(), group="cancel", exclusive=True)

    def action_open_review(self) -> None:
        if self._busy or self._pending_review is None or self._conversation is None:
            return
        self._busy = True
        self._update_controls()
        self._run_worker = self.run_worker(
            self._resume_pending_review(),
            group="run",
            exclusive=True,
        )

    async def _refresh_navigation(self, *, select_first: bool = False) -> None:
        self._set_status(f"Connecting to {self.api.base_url}…")
        try:
            await self.api.health()
            navigation = await self.api.navigation()
        except DagentApiError as exc:
            self._set_status(str(exc), error=True)
            return
        self._navigation = navigation
        tree = self.query_one("#conversation-tree", Tree)
        tree.clear()
        standalone = tree.root.add("Standalone", expand=True)
        nodes: list[tuple[Conversation, Any]] = []
        for conversation in navigation.standalone:
            nodes.append(
                (
                    conversation,
                    standalone.add_leaf(_conversation_label(conversation), data=conversation),
                )
            )
        for project, conversations in navigation.projects:
            project_node = tree.root.add(project.name, expand=True)
            for conversation in conversations:
                nodes.append(
                    (
                        conversation,
                        project_node.add_leaf(_conversation_label(conversation), data=conversation),
                    )
                )
        tree.root.expand()
        self._set_status(
            f"Connected to {self.api.base_url} · {len(nodes)} conversation(s)"
        )
        selected = None
        if self._conversation is not None:
            selected = next(
                ((conversation, node) for conversation, node in nodes if conversation.id == self._conversation.id),
                None,
            )
        if selected is None and select_first and nodes:
            selected = nodes[0]
        if selected is not None:
            conversation, node = selected
            tree.select_node(node)
            if self._conversation is None or self._conversation.id != conversation.id:
                await self._load_conversation(conversation)

    async def _load_conversation(self, conversation: Conversation) -> None:
        self._set_status(f"Loading {conversation.title}…")
        try:
            messages = await self.api.messages(conversation)
        except DagentApiError as exc:
            self._set_status(str(exc), error=True)
            return
        self._conversation = conversation
        target_select = self.query_one("#target-select", Select)
        target_select.value = "dag" if conversation.kind == "dynamic_dag" else "auto"
        self._pending_review = None
        self._dag = None
        self._trace = None
        self._stream_messages.clear()
        chat = self.query_one("#chat", ChatView)
        await chat.reset()
        activity = self.query_one("#activity", RichLog)
        activity.clear()
        for message in messages:
            if message.role == "assistant":
                for item in message.timeline:
                    if item.get("type") == "reasoning" and item.get("content"):
                        await chat.add_message("reasoning", str(item["content"]))
            await chat.add_message(message.role, message.content)
            if message.role == "assistant":
                self._restore_inspector(message)
        self._render_graph()
        self._set_review_button(self._pending_review is not None)
        self._set_status(f"{conversation.title} · {len(messages)} message(s)")
        self.query_one("#prompt", Input).focus()

    async def _clear_workbench(self) -> None:
        await self.query_one("#chat", ChatView).reset()
        self.query_one("#activity", RichLog).clear()
        self.query_one("#graph", RichLog).clear()
        self.query_one("#graph", RichLog).write(dag_text(None))
        self._set_status("New conversation · it will be created when you send the first prompt.")
        self.query_one("#prompt", Input).focus()

    async def _start_prompt(self, prompt: str) -> None:
        if self._busy:
            return
        target = self._selected_target()
        expected_kind = "dynamic_dag" if target == "dag" else "chat"
        if self._conversation is not None and self._conversation.kind != expected_kind:
            expected_label = "a dynamic DAG" if expected_kind == "dynamic_dag" else "a chat"
            self._set_status(
                f"This target requires {expected_label} conversation; press Ctrl+N first.",
                error=True,
            )
            return
        self._busy = True
        self._last_prompt = prompt
        self._pending_review = None
        self._set_review_button(False)
        self._update_controls()
        await self.query_one("#chat", ChatView).add_message("user", prompt)
        self._run_worker = self.run_worker(
            self._run_prompt(prompt),
            group="run",
            exclusive=True,
        )

    async def _run_prompt(self, prompt: str) -> None:
        try:
            if self._conversation is None:
                title = _conversation_title(prompt)
                target = self._selected_target()
                kind = "dynamic_dag" if target == "dag" else "chat"
                self._conversation = await self.api.create_conversation(title, kind=kind)
                await self._refresh_navigation()
            self._set_status("Running…")
            await self._consume_stream(
                self.api.stream_message(
                    self._conversation,
                    prompt,
                    target=self._selected_target(),
                    review_level=self._selected_review_level(),
                )
            )
            if self._pending_review is not None:
                await self._review_and_resume()
        except DagentApiError as exc:
            await self.query_one("#chat", ChatView).add_message("error", str(exc))
            self._set_status(str(exc), error=True)
        except Exception as exc:
            await self.query_one("#chat", ChatView).add_message("error", str(exc))
            self._set_status(f"TUI error: {exc}", error=True)
        finally:
            self._busy = False
            self._active_run_id = None
            self._update_controls()
            self._set_review_button(self._pending_review is not None)
            if not self._stream_failed and self._pending_review is None:
                self._set_status("Ready")
            await self._refresh_navigation()

    async def _review_and_resume(self) -> None:
        try:
            while self._pending_review is not None and self._conversation is not None:
                review = self._pending_review
                decision = await self.push_screen_wait(ReviewScreen(review, self._review_dag(review)))
                if decision is None:
                    self._set_status("Run is waiting for review.")
                    return
                self._pending_review = None
                self._set_review_button(False)
                self._set_status("Resuming reviewed run…")
                dag = self._review_dag(review) if decision.approved else None
                await self._consume_stream(
                    self.api.resume_review(
                        self._conversation,
                        review,
                        approved=decision.approved,
                        feedback=decision.feedback,
                        review_level=self._selected_review_level(),
                        dag=dag,
                    )
                )
        finally:
            if self._run_worker is not None and self._pending_review is not None:
                self._set_review_button(True)

    async def _resume_pending_review(self) -> None:
        try:
            await self._review_and_resume()
        except DagentApiError as exc:
            await self.query_one("#chat", ChatView).add_message("error", str(exc))
            self._set_status(str(exc), error=True)
        finally:
            self._busy = False
            self._active_run_id = None
            self._update_controls()
            self._set_review_button(self._pending_review is not None)

    async def _consume_stream(self, events: AsyncIterator[StreamEnvelope]) -> None:
        self._stream_messages.clear()
        self._content_seen = False
        self._stream_failed = False
        async for event in events:
            await self._handle_event(event)

    async def _handle_event(self, event: StreamEnvelope) -> None:
        data = event.data
        if event.type == "run.started":
            self._active_run_id = event.run_id
            self.query_one("#activity", RichLog).write(
                Text(f"Run {event.run_id or ''} started ({data.get('kind', '')})", style="bold cyan")
            )
            self._set_status(f"Running {event.run_id or ''}…")
            return
        if event.type in {"response.reasoning.delta", "response.content.delta"}:
            channel = "reasoning" if event.type == "response.reasoning.delta" else "assistant"
            response_id = str(data.get("response_id") or "response")
            key = (response_id, channel)
            message = self._stream_messages.get(key)
            if message is None:
                message = await self.query_one("#chat", ChatView).add_message(channel)
                self._stream_messages[key] = message
            message.append(str(data.get("delta") or ""))
            if channel == "assistant":
                self._content_seen = True
            self.query_one("#chat", ChatView).scroll_end(animate=False)
            return
        if event.type == "dag.updated":
            dag = data.get("dag")
            if isinstance(dag, dict):
                self._dag = dag
                self._render_graph()
            return
        if event.type == "trace.updated":
            trace = data.get("trace")
            if isinstance(trace, dict):
                self._trace = trace
                self._render_graph()
            return
        if event.type == "review.required":
            self._pending_review = {**(self._pending_review or {}), **data}
            self._set_review_button(True)
        if event.type == "run.finished":
            result = data.get("result")
            if isinstance(result, dict):
                state = result.get("state")
                if isinstance(state, dict):
                    pending = state.get("pending_review")
                    self._pending_review = pending if isinstance(pending, dict) else None
                    dag = state.get("dag")
                    trace = state.get("trace")
                    if isinstance(dag, dict):
                        self._dag = dag
                    if isinstance(trace, dict):
                        self._trace = trace
                    status = str(state.get("status") or "finished")
                    self._set_status(f"Run {status}")
                output = str(result.get("output_text") or "")
                if output and not self._content_seen:
                    await self.query_one("#chat", ChatView).add_message("assistant", output)
                self._render_graph()
            self._set_review_button(self._pending_review is not None)
            return
        if event.type == "run.failed":
            self._stream_failed = True
            message = str(data.get("message") or "Run failed.")
            await self.query_one("#chat", ChatView).add_message("error", message)
            self._set_status(message, error=True)
        if event.type.startswith("capability.") or event.type.startswith("validation.") or event.type in {
            "review.required",
            "run.failed",
        }:
            self.query_one("#activity", RichLog).write(activity_text(event.type, data))

    async def _cancel_active_run(self) -> None:
        assert self._active_run_id is not None
        run_id = self._active_run_id
        self._set_status(f"Cancelling {run_id}…")
        try:
            cancelled = await self.api.cancel_run(run_id)
        except DagentApiError as exc:
            self._set_status(str(exc), error=True)
            return
        if self._run_worker is not None:
            self._run_worker.cancel()
        self._busy = False
        self._active_run_id = None
        self._update_controls()
        await self.query_one("#chat", ChatView).add_message(
            "system",
            f"Run {run_id} cancellation {'requested' if cancelled else 'was no longer active'}.",
        )
        self._set_status("Cancelled")

    def _restore_inspector(self, message: ConversationMessage) -> None:
        if message.dag is not None:
            self._dag = message.dag
        if message.trace is not None:
            self._trace = message.trace
        if message.pending_review is not None:
            self._pending_review = message.pending_review
        activity = self.query_one("#activity", RichLog)
        for item in message.timeline:
            item_type = str(item.get("type") or "")
            event = item.get("event")
            if item_type == "capability" and isinstance(event, dict):
                result = item.get("result")
                payload = result if isinstance(result, dict) else event
                event_type = str(payload.get("type") or "capability")
                activity.write(activity_text(event_type, payload))
            elif item_type in {"validation", "validating"}:
                payload = event if isinstance(event, dict) else item
                event_type = str(payload.get("type") or "validation.started")
                activity.write(activity_text(event_type, payload))

    def _render_graph(self) -> None:
        graph = self.query_one("#graph", RichLog)
        graph.clear()
        graph.write(dag_text(self._dag))
        if self._trace is not None:
            graph.write(Text(""))
            graph.write(trace_text(self._trace))

    def _review_dag(self, review: dict[str, Any]) -> dict[str, Any] | None:
        if review.get("kind") == "capability_review":
            return None
        proposed = review.get("proposed_dag")
        return proposed if isinstance(proposed, dict) else self._dag

    def _selected_target(self) -> RunTarget:
        value = self.query_one("#target-select", Select).value
        return value if value in {"auto", "tool", "dag"} else "auto"

    def _selected_review_level(self) -> ReviewLevel:
        value = self.query_one("#review-select", Select).value
        return value if value in {"fast", "careful"} else "fast"

    def _update_controls(self) -> None:
        self.query_one("#prompt", Input).disabled = self._busy
        self.query_one("#target-select", Select).disabled = self._busy
        self.query_one("#review-select", Select).disabled = self._busy
        self.query_one("#new-conversation", Button).disabled = self._busy

    def _set_review_button(self, visible: bool) -> None:
        button = self.query_one("#review-button", Button)
        button.display = visible
        button.disabled = self._busy

    def _set_status(self, message: str, *, error: bool = False) -> None:
        status = self.query_one("#status", Static)
        status.update(Text(message, style="bold red" if error else "dim"))


def _conversation_title(prompt: str) -> str:
    single_line = " ".join(prompt.split())
    if len(single_line) <= 48:
        return single_line
    return f"{single_line[:47]}…"


def _conversation_label(conversation: Conversation) -> str:
    prefix = "[DAG] " if conversation.kind == "dynamic_dag" else ""
    return f"{prefix}{conversation.title}"
