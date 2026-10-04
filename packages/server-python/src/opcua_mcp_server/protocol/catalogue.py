"""Contract-derived MCP advertisement and safe SDK error-frame normalization."""

from __future__ import annotations

import copy

from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import ToolAnnotations

from ..contract import CONTRACT, error_message


def _advertised_tool(tool, spec: dict):
    """One tool as advertised: the contract's own definition, never the derived one."""
    output_schema = None
    if shape_name := spec.get("resultShape"):
        output_schema = {
            "type": "object",
            "properties": {"result": CONTRACT["resultShapes"][shape_name]},
            "required": ["result"],
            "additionalProperties": False,
        }
        # Beside `result`, for a tool that can return fewer records than it was
        # asked for (issue #137). `outputSchema` in tools.ts builds the same object.
        if spec.get("reportsCompleteness"):
            output_schema["properties"]["completeness"] = CONTRACT["completeness"]["schema"]
            output_schema["required"] = ["result", "completeness"]
    return tool.model_copy(
        update={
            "annotations": ToolAnnotations(**spec["annotations"]),
            "output_schema": output_schema,
            # A copy: the contract is shared by every request, and nothing a
            # caller does to what it was handed may change the next catalogue.
            "input_schema": copy.deepcopy(spec["inputSchema"]),
        }
    )


_SDK_TOOL_ERROR_PREFIX = "Error executing tool "


def _without_sdk_prefix(name: str, error: BaseException) -> BaseException:
    """A tool failure worded as the contract words it, not as the SDK frames it."""
    if isinstance(error, UnexpectedToolError):
        reported = UnexpectedToolError(error_message("unexpectedToolError", tool=name))
        reported.__cause__ = error.__cause__
        return reported
    if not isinstance(error, ToolError):
        return error
    prefix = f"{_SDK_TOOL_ERROR_PREFIX}{name}: "
    text = str(error)
    if not text.startswith(prefix):
        return error
    unwrapped = ToolError(text[len(prefix) :])
    unwrapped.__cause__ = error.__cause__
    return unwrapped
