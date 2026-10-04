"""MCP result construction and per-request context lookup."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent

from ..notices import notice
from ..state import ServerState


def _state(ctx: Context) -> ServerState:
    """This server's state, as a tool body reaches it."""
    return ctx.request_context.lifespan_context["state"]


def _records_result(
    records: list[dict], completeness: dict | None = None, text: str | None = None
) -> CallToolResult:
    """Records, one text block each, and whether they are all of them."""
    content = [TextContent(type="text", text=json.dumps(record, indent=2)) for record in records]
    if text is not None:
        content.append(TextContent(type="text", text=text))
    structured: dict[str, Any] = {"result": records}
    if completeness is not None:
        structured["completeness"] = completeness
    return CallToolResult(content=content, structured_content=structured)


def _history_result(records: list[dict], completeness: dict, cap_notice: str) -> CallToolResult:
    """History records (``resultShapes.historyRecords``), and whether they are all."""
    text = None
    if "contractLimit" in completeness["reasons"]:
        count = completeness["limit"] if completeness["limit"] is not None else len(records)
        text = notice(cap_notice, count=count)
    elif "serverLimit" in completeness["reasons"]:
        text = notice("serverTruncated", count=completeness["returned"])
    return _records_result(records, completeness, text)


def _object_result(record: Any, completeness: dict | None = None) -> CallToolResult:
    """A result that is one object rather than a list of records."""
    structured: dict[str, Any] = {"result": record}
    if completeness is not None:
        structured["completeness"] = completeness
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(record, indent=2))],
        structured_content=structured,
    )
