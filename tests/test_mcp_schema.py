from dagent.capabilities.mcp.schema import normalize_mcp_input_schema, normalize_mcp_output_schema


def test_normalize_mcp_input_schema_rewrites_defs_and_repairs_objects() -> None:
    schema = {
        "type": "object",
        "definitions": {
            "Filter": {
                "properties": {"name": {"type": "string"}},
                "required": ["name", "missing"],
            }
        },
        "properties": {
            "filter": {"$ref": "#/definitions/Filter"},
            "empty": {"type": "object", "required": ["ghost"]},
        },
        "required": ["filter", "unknown"],
    }

    normalized = normalize_mcp_input_schema(schema)

    assert "definitions" not in normalized
    assert normalized["properties"]["filter"]["$ref"] == "#/$defs/Filter"
    assert normalized["$defs"]["Filter"]["type"] == "object"
    assert normalized["$defs"]["Filter"]["required"] == ["name"]
    assert normalized["properties"]["empty"]["properties"] == {}
    assert "required" not in normalized["properties"]["empty"]
    assert normalized["required"] == ["filter"]


def test_normalize_mcp_input_schema_collapses_nullable_anyof() -> None:
    schema = {
        "properties": {
            "limit": {
                "anyOf": [{"type": "integer"}, {"type": "null"}],
                "default": None,
            }
        },
        "required": ["limit"],
    }

    normalized = normalize_mcp_input_schema(schema)

    assert normalized["type"] == "object"
    assert normalized["properties"]["limit"]["type"] == "integer"
    assert normalized["properties"]["limit"]["nullable"] is True
    assert "anyOf" not in normalized["properties"]["limit"]


def test_normalize_mcp_output_schema_preserves_json_schema_constraints() -> None:
    schema = {
        "type": "object",
        "required": ["id"],
        "allOf": [{"$ref": "#/definitions/Result"}],
        "definitions": {
            "Result": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            }
        },
    }

    normalized = normalize_mcp_output_schema(schema)

    assert "definitions" not in normalized
    assert normalized["allOf"][0]["$ref"] == "#/$defs/Result"
    assert normalized["required"] == ["id"]
    assert "properties" not in normalized
    assert normalized["$defs"]["Result"]["required"] == ["value"]
