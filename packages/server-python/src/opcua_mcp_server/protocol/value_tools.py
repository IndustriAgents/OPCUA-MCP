"""MCP tool signatures and result conversion around injected application ports."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from ..adapters.opcua_browse import PythonOpcuaBrowsePort
from ..adapters.opcua_diagnostics import PythonOpcuaDiagnosticsPort
from ..adapters.opcua_history import PythonOpcuaHistoryPort
from ..adapters.opcua_methods import PythonOpcuaMethodPort
from ..adapters.opcua_read import PythonOpcuaReadPort
from ..adapters.opcua_write import PythonOpcuaWritePort
from ..application.browse import browse_nodes
from ..application.diagnostics import get_server_status as get_server_status_use_case
from ..application.history import read_history
from ..application.methods import call_method
from ..application.read import read_nodes
from ..application.write import write_nodes
from ..capabilities import (
    capability_status,
)
from ..contract import CONTRACT
from ..errors import AdapterFailure, ApplicationRefusal
from ..errors import message as error_message
from ..limits import (
    LimitExceeded,
)
from ..operation_limits import (
    read_chunk,
    write_limit,
)
from ..policy import (
    server_identity_record,
)
from ..security import describe_security, security_config
from .results import _history_result, _object_result, _state

_TRAVERSAL = CONTRACT["traversal"]


async def read_opcua_nodes(node_ids: list[str], ctx: Context) -> list[dict]:
    """Read the current value of one or more OPC UA nodes in a single request."""
    client = ctx.request_context.lifespan_context["opcua_client"]
    state = _state(ctx)
    port = PythonOpcuaReadPort(client, state.node_metadata)
    try:
        return await read_nodes(port, node_ids, read_chunk(state.operation_limits))
    except (AdapterFailure, ApplicationRefusal) as error:
        raise ToolError(str(error)) from error


async def read_opcua_history(
    node_id: str,
    ctx: Context,
    start_time: str | None = None,
    end_time: str | None = None,
    num_values: int = 0,
    aggregate_function: str | None = None,
    processing_interval: float = 0,
) -> CallToolResult:
    """Read a node's stored history, raw or summarised by a server-side aggregate."""
    state = _state(ctx)
    offered = state.capabilities.aggregate_functions
    port = PythonOpcuaHistoryPort(ctx.request_context.lifespan_context["opcua_client"], offered)
    try:
        result = await read_history(
            port,
            {
                "node_id": node_id,
                "start": start_time,
                "end": end_time,
                "num_values": num_values,
                "aggregate_function": aggregate_function,
                "processing_interval": processing_interval,
            },
            list(offered),
        )
        return _history_result(result["records"], result["completeness"], "historyTruncated")
    except (ApplicationRefusal, AdapterFailure, LimitExceeded) as error:
        raise ToolError(str(error)) from error


async def get_server_status(ctx: Context) -> CallToolResult:
    """Report status without joining an existing connection round."""
    state = _state(ctx)
    connection = ctx.request_context.lifespan_context["opcua_connection"]
    port = PythonOpcuaDiagnosticsPort(connection, lambda: capability_status(state.capabilities))
    return _object_result(
        await get_server_status_use_case(
            port, describe_security(security_config()), server_identity_record(state.policy.config)
        )
    )


async def browse_opcua_nodes(
    ctx: Context,
    node_id: str = _TRAVERSAL["rootNodeId"],
    browse_path: str | None = None,
    depth: int = _TRAVERSAL["defaultDepth"],
    node_class: str | None = None,
    name_filter: str | None = None,
    include_values: bool = False,
    max_nodes: int = _TRAVERSAL["defaultMaxNodes"],
) -> CallToolResult:
    """Explore the address space: list children, walk a subtree, resolve a path, search."""
    client = ctx.request_context.lifespan_context["opcua_client"]
    port = PythonOpcuaBrowsePort(client, _state(ctx).operation_limits)
    try:
        result = await browse_nodes(
            port,
            node_id=node_id,
            browse_path=browse_path,
            depth=depth,
            node_class=node_class,
            name_filter=name_filter,
            include_values=include_values,
            max_nodes=max_nodes,
        )
        return _object_result(result["result"], result["completeness"])
    except (ApplicationRefusal, AdapterFailure) as error:
        raise ToolError(str(error)) from error


async def write_opcua_nodes(nodes: list[dict[str, Any]], ctx: Context) -> list[dict]:
    """Write a value to one or more OPC UA nodes."""
    if not nodes:
        raise ToolError(error_message("emptyArray", tool="write_opcua_nodes", argument="nodes"))
    state = _state(ctx)
    port = PythonOpcuaWritePort(
        ctx.request_context.lifespan_context["opcua_client"], state.node_metadata
    )
    bounds = {}
    for index, node in enumerate(nodes):
        bound = state.policy.bound_for(str(node.get("node_id", "")))
        bounds[index] = bound.max_change if bound else None
    try:
        return await write_nodes(
            port,
            nodes,
            {
                "write": write_limit(state.operation_limits),
                "read": read_chunk(state.operation_limits),
            },
            bounds,
            state.policy.config.allow_out_of_range_writes,
        )
    except (ApplicationRefusal, AdapterFailure) as error:
        raise ToolError(str(error)) from error


async def call_opcua_method(
    object_node_id: str,
    method_node_id: str,
    ctx: Context,
    arguments: list[Any] | None = None,
) -> CallToolResult:
    """Call a method on an OPC UA object, with arguments of the types it declares."""
    port = PythonOpcuaMethodPort(ctx.request_context.lifespan_context["opcua_client"])
    try:
        return _object_result(await call_method(port, object_node_id, method_node_id, arguments))
    except (ApplicationRefusal, AdapterFailure) as error:
        raise ToolError(str(error)) from error
