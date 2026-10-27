"""Conservative inspection of reasoning fields in request JSON Schemas.

Open schemas permit a field without promising its semantics. Keep that distinct
from both an explicitly declared field and a field forbidden by every variant.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from dagent.providers.capabilities import CapabilitySupport


@dataclass(frozen=True)
class _Evidence:
    allowed: CapabilitySupport
    declared: bool = False


def schema_field_support(
    document: Mapping[str, Any], schema: Any, path: tuple[str, ...],
    *, value_type: str = "string", assistant: bool = False,
) -> CapabilitySupport:
    """Inspect a property path; ``[]`` traverses array items.

    This is not a payload validator. Unsupported constraints and unresolved
    references remain unknown rather than establishing model capabilities.
    Boolean switch inspection checks whether enabling the switch is allowed.
    """
    evidence = _inspect(document, schema, path, value_type, assistant, frozenset())
    if evidence is None:
        return "unsupported"
    if evidence.allowed == "supported" and not evidence.declared:
        return "unknown"
    return evidence.allowed


def _reference(document: Mapping[str, Any], reference: str) -> Any:
    if not reference.startswith("#/"):
        return None
    value: Any = document
    for segment in reference[2:].split("/"):
        if not isinstance(value, Mapping):
            return None
        value = value.get(segment.replace("~1", "/").replace("~0", "~"))
    return value


def _intersect(values: Sequence[_Evidence | None]) -> _Evidence | None:
    if any(value is None for value in values):
        return None
    present = [value for value in values if value is not None]
    allowed: CapabilitySupport = (
        "unsupported" if any(value.allowed == "unsupported" for value in present)
        else "unknown" if any(value.allowed == "unknown" for value in present)
        else "supported"
    )
    return _Evidence(allowed, any(value.declared for value in present))


def _inspect(
    document: Mapping[str, Any], schema: Any, path: tuple[str, ...],
    value_type: str, assistant: bool, seen: frozenset[str],
) -> _Evidence | None:
    if schema is False:
        return _Evidence("unsupported")
    if schema is True:
        return _Evidence("supported")
    if not isinstance(schema, Mapping):
        return _Evidence("unknown")

    reference = schema.get("$ref")
    if isinstance(reference, str):
        if reference in seen:
            return _Evidence("unknown")
        target = _inspect(document, _reference(document, reference), path,
                          value_type, assistant, seen | {reference})
        siblings = {key: value for key, value in schema.items() if key != "$ref"}
        return _intersect([target, _inspect(document, siblings, path, value_type, assistant, seen)])

    # Other message roles must not make an assistant-only field look forbidden.
    if assistant and len(path) == 1:
        properties = schema.get("properties")
        role = properties.get("role") if isinstance(properties, Mapping) else None
        if isinstance(role, Mapping) and isinstance(role.get("$ref"), str):
            role = _reference(document, role["$ref"])
        if role is False or (isinstance(role, Mapping) and (
            ("const" in role and role["const"] != "assistant")
            or (isinstance(role.get("enum"), list) and "assistant" not in role["enum"])
        )):
            return None

    values = [_local(document, schema, path, value_type, assistant, seen)]
    for keyword in ("allOf", "anyOf", "oneOf"):
        children = schema.get(keyword)
        if not isinstance(children, list):
            continue
        branches = [_inspect(document, child, path, value_type, assistant, seen) for child in children]
        if keyword == "allOf":
            values.append(_intersect(branches))
            continue
        applicable = [branch for branch in branches if branch is not None]
        if not applicable:
            values.append(None)
            continue
        possible = [branch for branch in applicable if branch.allowed != "unsupported"]
        if not possible:
            values.append(_Evidence("unsupported"))
        elif keyword == "oneOf" and len(possible) > 1:
            values.append(_Evidence("unknown"))
        elif any(branch.allowed == "supported" for branch in possible):
            values.append(_Evidence("supported", any(
                branch.allowed == "supported" and branch.declared for branch in possible
            )))
        else:
            values.append(_Evidence("unknown"))
    return _intersect(values)


def _local(
    document: Mapping[str, Any], schema: Mapping[str, Any], path: tuple[str, ...],
    value_type: str, assistant: bool, seen: frozenset[str],
) -> _Evidence | None:
    expected = ("array" if path[0] == "[]" else "object") if path else value_type
    types = schema.get("type")
    if isinstance(types, str) and types != expected:
        return _Evidence("unsupported")
    if isinstance(types, list) and expected not in types:
        return _Evidence("unsupported")
    # These constraints need more than property inspection to establish support.
    if any(key in schema for key in ("not", "if", "then", "else", "unevaluatedProperties")):
        return _Evidence("unknown")
    if not path:
        if value_type == "boolean":
            if "const" in schema and schema["const"] is not True:
                return _Evidence("unsupported")
            if isinstance(schema.get("enum"), list) and not any(value is True for value in schema["enum"]):
                return _Evidence("unsupported")
        return _Evidence("supported")
    head, *tail = path
    if head == "[]":
        if "prefixItems" in schema:
            return _Evidence("unknown")
        child = schema.get("items", True)
    else:
        properties = schema.get("properties")
        if isinstance(properties, Mapping) and head in properties:
            evidence = _inspect(document, properties[head], tuple(tail), value_type, assistant, seen)
            if evidence is not None and not tail:
                return _Evidence(evidence.allowed, True)
            return evidence
        if schema.get("patternProperties"):
            return _Evidence("unknown")
        child = schema.get("additionalProperties", True)
    return _inspect(document, child, tuple(tail), value_type, assistant, seen)
