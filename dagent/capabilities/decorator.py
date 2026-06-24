"""Public capability decorator for the dagent SDK."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, get_type_hints

from pydantic import BaseModel

from dagent.capabilities.catalog import CapabilityHandler
from dagent.schemas import (
    CapabilityDefinition,
    CapabilityInvocation,
    CapabilityPolicy,
    CapabilityResult,
    RiskLevel,
    validate_capability_id,
    validate_capability_id_segment,
)
from dagent.schemas.common import json_schema_for_type


@dataclass(frozen=True)
class CapabilityBinding:
    """A public SDK binding of a capability definition and executable handler."""

    definition: CapabilityDefinition
    handler: CapabilityHandler
    supports_context: bool = False


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    id: str | None = None,
    name: str | None = None,
    description: str = "",
    risk: RiskLevel = "low",
    requires_review: bool = False,
    sandbox_required: bool = False,
    network: bool = False,
    secrets: list[str] | None = None,
    parameters: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    enabled: bool = True,
    supports_context: bool = False,
) -> CapabilityBinding | Callable[[Callable[..., Any]], CapabilityBinding]:
    """Decorate a Python function as a dagent tool capability.

    This is the public way to expose a Python function as an LLM-callable
    tool. MCP and skill capabilities are registered through
    ``Runner.add_mcp_server`` and ``Runner.add_skill_root``.
    """

    def decorate(func: Callable[..., Any]) -> CapabilityBinding:
        type_hints = _type_hints_for(func)
        capability_name = validate_capability_id_segment(name or func.__name__, label="Tool names")
        capability_id = validate_capability_id(id or f"tool.{capability_name}", kind="tool")
        definition = CapabilityDefinition(
            id=capability_id,
            name=capability_name,
            kind="tool",
            description=description or inspect.getdoc(func) or "",
            parameters=parameters or _schema_from_signature(func, type_hints),
            output_schema=_output_schema_from_signature(func, type_hints),
            policy=CapabilityPolicy(
                risk=risk,
                requires_review=requires_review,
                sandbox_required=sandbox_required,
                network=network,
                secrets=secrets or [],
            ),
            config=config or {},
            enabled=enabled,
        )

        async def handler(
            invocation: CapabilityInvocation,
            *,
            context: Any = None,
            callbacks: Any = None,
        ) -> CapabilityResult:
            try:
                result = _invoke_function(
                    func,
                    invocation.arguments,
                    context=context,
                    callbacks=callbacks,
                    supports_context=supports_context,
                )
                if inspect.isawaitable(result):
                    result = await result
                if isinstance(result, CapabilityResult):
                    return result
                content, value = _content_and_value_from_result(result)
                return CapabilityResult(
                    invocation_id=invocation.invocation_id,
                    capability_id=invocation.capability_id,
                    kind=invocation.kind,
                    status="completed",
                    content=content,
                    value=value,
                )
            except Exception as exc:
                return CapabilityResult(
                    invocation_id=invocation.invocation_id,
                    capability_id=invocation.capability_id,
                    kind=invocation.kind,
                    status="failed",
                    error=str(exc),
                    stop_reason=type(exc).__name__,
                )

        return CapabilityBinding(
            definition=definition,
            handler=handler,
            supports_context=supports_context,
        )

    if fn is not None:
        return decorate(fn)
    return decorate


def _invoke_function(
    func: Callable[..., Any],
    arguments: dict[str, Any],
    *,
    context: Any,
    callbacks: Any,
    supports_context: bool,
) -> Any:
    if supports_context:
        return func(**arguments, context=context, callbacks=callbacks)
    return func(**arguments)


def _content_and_value_from_result(result: Any) -> tuple[str, Any]:
    """Split a tool return into string content and a structured value (None for plain text)."""
    if result is None:
        return "", None
    if isinstance(result, BaseModel):
        value = result.model_dump(mode="json")
        return result.model_dump_json(), value
    if isinstance(result, str):
        return result, None
    if isinstance(result, bytes):
        return result.decode("utf-8", errors="replace"), None
    if isinstance(result, tuple):
        value = list(result)
        return json.dumps(value, ensure_ascii=False), value
    if isinstance(result, (dict, list, bool, int, float)):
        return json.dumps(result, ensure_ascii=False), result
    return str(result), None


def _schema_from_signature(func: Callable[..., Any], type_hints: dict[str, Any]) -> dict[str, Any]:
    signature = inspect.signature(func)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in signature.parameters.values():
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            raise ValueError(f"Cannot infer schema for variadic parameter '{parameter.name}'.")
        if parameter.name in {"context", "callbacks"}:
            continue
        schema = _schema_for_annotation(type_hints.get(parameter.name, parameter.annotation))
        if parameter.default is inspect.Parameter.empty:
            required.append(parameter.name)
        else:
            schema["default"] = parameter.default
        properties[parameter.name] = schema
    output: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        output["required"] = required
    return output


def _output_schema_from_signature(func: Callable[..., Any], type_hints: dict[str, Any]) -> dict[str, Any]:
    annotation = type_hints.get("return", inspect.signature(func).return_annotation)
    return json_schema_for_type(annotation)


def _type_hints_for(func: Callable[..., Any]) -> dict[str, Any]:
    try:
        return get_type_hints(func, include_extras=True)
    except (NameError, TypeError, AttributeError):
        return {}


def _schema_for_annotation(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}
    return json_schema_for_type(annotation) or {"type": "string"}
