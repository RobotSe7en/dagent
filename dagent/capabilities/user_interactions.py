"""Optional model tools backed by the public invocation interaction client."""

from typing import Any, Literal
from functools import cache

from dagent.capabilities.decorator import CapabilityBinding, tool
from dagent.capabilities.tools.registry import ToolOutput, content_and_value_from_result
from dagent.schemas.user_interactions import UserInteractionOption, SelectParameters
from dagent.user_interactions import ToolContext, UserInteractionCancelled, UserInteractionUnsupported


@cache
def interaction_tools() -> tuple[CapabilityBinding, ...]:
    async def call(context: ToolContext, method: str, **arguments: Any) -> ToolOutput:
        if context is None:
            raise UserInteractionUnsupported("User interaction requires a root ToolLoop context.")
        try:
            value = await getattr(context.ui, method)(**arguments)
        except UserInteractionCancelled:
            return ToolOutput("[USER_INTERACTION_CANCELLED] The user abandoned this interaction.",
                              {"status": "cancelled"})
        content, _ = content_and_value_from_result(value)
        return ToolOutput(content, value)

    @tool(supports_context=True)
    async def ui_confirm(title: str, description: str | None = None, *, context: ToolContext, callbacks: Any) -> Any:
        """Ask the user for confirmation. False is a valid answer."""
        return await call(context, "confirm", title=title, description=description)

    @tool(supports_context=True, parameters=SelectParameters.model_json_schema())
    async def ui_select(title: str, options: list[UserInteractionOption], *, context: ToolContext, callbacks: Any) -> Any:
        """Ask the user to select one of the supplied stable option IDs."""
        return await call(context, "select", title=title, options=options)

    @tool(supports_context=True)
    async def ui_input(title: str, prompt: str = "", initial: str = "", *, context: ToolContext, callbacks: Any) -> Any:
        """Ask the user for text. An empty string is a valid answer."""
        return await call(context, "input", title=title, prompt=prompt, initial=initial)

    @tool(supports_context=True)
    async def ui_editor(title: str, content: str, format: Literal["text", "markdown", "html"] = "text", *, context: ToolContext, callbacks: Any) -> Any:
        """Ask the host to edit full text, markdown, or HTML content."""
        return await call(context, "editor", title=title, content=content, format=format)

    return ui_confirm, ui_select, ui_input, ui_editor
