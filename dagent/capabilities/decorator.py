"""Public capability decorator for the dagent SDK."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, get_args, get_origin

from dagent.capabilities.catalog import CapabilityHandler
from dagent.schemas import (
    CapabilityDefinition,
    CapabilityInvocation,
    CapabilityKind,
    CapabilityPolicy,
    CapabilityResult,
    RiskLevel,
)


@dataclass(frozen=True)
class CapabilityBinding:
    """A public SDK binding of a capability definition and executable handler."""

    definition: CapabilityDefinition
    handler: CapabilityHandler
    supports_context: bool = False


def capability(
    fn: Callable[..., Any] | None = None,
    *,
    id: str | None = None,
    name: str | None = None,
    description: str = "",
    kind: CapabilityKind = "tool",
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
    """Decorate a Python function as a dagent capability."""

    def decorate(func: Callable[..., Any]) -> CapabilityBinding:
        capability_name = name or func.__name__
        capability_id = id or f"{kind}.{capability_name}"
        definition = CapabilityDefinition(
            id=capability_id,
            name=capability_name,
            kind=kind,
            description=description or inspect.getdoc(func) or "",
            parameters=parameters or _schema_from_signature(func),
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
                return CapabilityResult(
                    invocation_id=invocation.invocation_id,
                    capability_id=invocation.capability_id,
                    kind=invocation.kind,
                    status="completed",
                    content=_content_from_result(result),
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


def tool(
    fn: Callable[..., Any] | None = None,
    **kwargs: Any,
) -> CapabilityBinding | Callable[[Callable[..., Any]], CapabilityBinding]:
    """Decorate a Python function as a dagent tool capability.

    Shorthand for ``@capability(kind="tool", ...)``. Use this for the common
    case of exposing a Python function as an LLM-callable tool; reach for
    ``@capability`` only when you need to set a non-tool ``kind``.
    """

    kwargs.setdefault("kind", "tool")
    return capability(fn, **kwargs)


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


def _content_from_result(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, bytes):
        return result.decode("utf-8", errors="replace")
    if isinstance(result, (dict, list, tuple, bool, int, float)):
        return json.dumps(result, ensure_ascii=False)
    return str(result)


def _schema_from_signature(func: Callable[..., Any]) -> dict[str, Any]:
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
        schema = _schema_for_annotation(parameter.annotation)
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


def _schema_for_annotation(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {list, tuple, set}:
        item_schema = _schema_for_annotation(args[0]) if args else {}
        return {"type": "array", "items": item_schema}
    if origin is dict:
        return {"type": "object"}
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation in {dict, Any}:
        return {"type": "object"}
    if annotation in {list, tuple, set}:
        return {"type": "array"}
    return {"type": "string"}
