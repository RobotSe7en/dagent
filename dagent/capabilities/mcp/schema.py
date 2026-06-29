"""MCP tool schema normalization helpers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def normalize_mcp_input_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Return an OpenAI/tool-call friendly JSON schema for an MCP input schema."""

    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}}
    normalized = _normalize_node(deepcopy(schema))
    if not isinstance(normalized, dict):
        return {"type": "object", "properties": {}}
    if "type" not in normalized:
        normalized["type"] = "object"
    if normalized.get("type") == "object":
        normalized.setdefault("properties", {})
        _prune_required(normalized)
    return normalized


def normalize_mcp_output_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Return a JSON schema suitable for describing structured MCP output."""

    if not isinstance(schema, dict) or not schema:
        return {}
    return _rewrite_legacy_defs(deepcopy(schema))


def _rewrite_legacy_defs(value: Any) -> Any:
    if isinstance(value, list):
        return [_rewrite_legacy_defs(item) for item in value]
    if not isinstance(value, dict):
        return value

    node: dict[str, Any] = {}
    for key, child in value.items():
        if key == "definitions":
            key = "$defs"
        elif key == "$ref" and isinstance(child, str):
            child = child.replace("#/definitions/", "#/$defs/")
        node[key] = _rewrite_legacy_defs(child)
    return node


def _normalize_node(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalize_node(item) for item in value]
    if not isinstance(value, dict):
        return value

    node: dict[str, Any] = {}
    for key, child in value.items():
        if key == "definitions":
            key = "$defs"
        elif key == "$ref" and isinstance(child, str):
            child = child.replace("#/definitions/", "#/$defs/")
        node[key] = _normalize_node(child)

    collapsed = _collapse_nullable_union(node)
    if collapsed is not None:
        node = collapsed

    if ("properties" in node or "required" in node) and "type" not in node:
        node["type"] = "object"
    if node.get("type") == "object":
        properties = node.get("properties")
        if not isinstance(properties, dict):
            node["properties"] = {}
        _prune_required(node)
    return node


def _collapse_nullable_union(node: dict[str, Any]) -> dict[str, Any] | None:
    for union_key in ("anyOf", "oneOf"):
        options = node.get(union_key)
        if not isinstance(options, list) or len(options) != 2:
            continue
        non_null = [option for option in options if not _is_null_schema(option)]
        if len(non_null) != 1:
            continue
        merged = dict(non_null[0])
        for key, value in node.items():
            if key == union_key:
                continue
            if key not in merged:
                merged[key] = value
        merged["nullable"] = True
        return merged
    return None


def _is_null_schema(schema: Any) -> bool:
    return isinstance(schema, dict) and schema.get("type") == "null"


def _prune_required(node: dict[str, Any]) -> None:
    required = node.get("required")
    properties = node.get("properties")
    if not isinstance(required, list) or not isinstance(properties, dict):
        node.pop("required", None)
        return
    pruned = [name for name in required if isinstance(name, str) and name in properties]
    if pruned:
        node["required"] = pruned
    else:
        node.pop("required", None)
