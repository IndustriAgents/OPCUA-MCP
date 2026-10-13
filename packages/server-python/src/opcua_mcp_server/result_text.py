"""Deterministic JSON text for tool results; structured values are left intact."""

from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, TextContent

from .numeric import array_index, json_text


def _keys(value: dict):
    def order(key):
        # ECMAScript enumerates array-index keys first, even after sorting.
        index = array_index(key)
        return (
            (0, index)
            if index is not None
            else (1, key.encode("utf-16-be", errors="surrogatepass"))
        )

    return sorted(value, key=order)


def pretty_json(value: Any, level: int = 0) -> str:
    """JSON.stringify-compatible scalars, sorted object keys and two-space indent."""
    if isinstance(value, (list, dict)):
        if not value:
            return "[]" if isinstance(value, list) else "{}"
        indent = "  " * (level + 1)
        if isinstance(value, list):
            parts = [pretty_json(item, level + 1) for item in value]
            opening, closing = "[", "]"
        else:
            parts = [
                json_text(key) + ": " + pretty_json(value[key], level + 1) for key in _keys(value)
            ]
            opening, closing = "{", "}"
        return (
            opening
            + "\n"
            + ",\n".join(indent + part for part in parts)
            + "\n"
            + "  " * level
            + closing
        )
    return json_text(value)


def normalize_result_text(result):
    """Keep notices and structured content while giving every tool the same text."""
    if not isinstance(result, CallToolResult) or result.is_error:
        return result
    structured = result.structured_content
    if not isinstance(structured, dict) or "result" not in structured:
        return result
    value = structured["result"]
    records = value if isinstance(value, list) else [value]
    trailing = result.content[len(records) :]
    if not records:
        trailing = [
            block
            for block in trailing
            if not isinstance(block, TextContent) or block.text.strip() != "[]"
        ]
    blocks = [TextContent(type="text", text=pretty_json(record)) for record in records]
    return result.model_copy(update={"content": blocks + trailing})
