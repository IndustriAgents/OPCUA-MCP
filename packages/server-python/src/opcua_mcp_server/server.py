"""The MCP server: lifecycle, tool registration, and the stdio entry point."""

from __future__ import annotations

import asyncio
import json
import sys
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from opcua import Node, ua
from opcua.ua import NodeClass

from . import events
from .aggregates import validate_aggregate_function
from .capabilities import client_aggregate_functions, client_supports_history
from .config import SERVER_URL
from .contract import CONTRACT, DESC, SUBSCRIPTIONS_RESOURCE
from .datetimes import parse_iso_datetime
from .policy import describe_policy, tool_policy
from .records import history_records
from .security import create_client, describe_security, security_config, security_warnings
from .subscriptions import SUBSCRIPTIONS, unknown_subscription_message
from .variant_codec import convert_for_variant
from .version import package_version

_CAPABILITIES: dict[str, Any] = {"history": False, "aggregate_functions": {}}


def _audit_targets(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "write_opcua_node":
        return {"node_ids": [arguments.get("node_id")]}
    if name == "write_multiple_opcua_nodes":
        return {"node_ids": [item.get("node_id") for item in arguments.get("nodes_to_write", [])]}
    if name == "call_opcua_method":
        return {
            "object_node_id": arguments.get("object_node_id"),
            "method_node_id": arguments.get("method_node_id"),
        }
    if name == "acknowledge_alarm":
        return {
            "condition_id": arguments.get("condition_id"),
            "event_id": arguments.get("event_id"),
        }
    return {}


def _audit_decision(name: str, arguments: dict[str, Any], decision: str, reason: str = "") -> None:
    spec = next((tool for tool in CONTRACT["tools"] if tool["name"] == name), None)
    if spec is None or spec["accessClass"] not in {"control", "alarm-action"}:
        return
    record = {
        "event": "opcua_mcp_policy",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "profile": tool_policy().config.profile,
        "tool": name,
        "decision": decision,
        **_audit_targets(name, arguments),
    }
    if reason:
        record["reason"] = reason
    print(json.dumps(record, separators=(",", ":")), file=sys.stderr)


# Manage the lifecycle of the OPC UA client connection
@asynccontextmanager
async def opcua_lifespan(server: MCPServer) -> AsyncIterator[dict]:
    """Handle OPC UA client connection lifecycle."""
    config = security_config()
    # Log to stderr: stdout is reserved for the MCP stdio JSON-RPC transport.
    for warning in security_warnings(config):
        print(f"WARNING: {warning}", file=sys.stderr)

    # Both calls run in a thread: building a secured client fetches the server's
    # certificate from its endpoint list, so it blocks on the network too.
    client = await asyncio.to_thread(create_client, SERVER_URL)
    try:
        # Connect to OPC UA server synchronously, wrapped in a thread for async compatibility
        await asyncio.to_thread(client.connect)
        print(f"Connected to OPC UA server ({describe_security(config)})", file=sys.stderr)
        _CAPABILITIES["history"] = await asyncio.to_thread(client_supports_history, client)
        _CAPABILITIES["aggregate_functions"] = await asyncio.to_thread(
            client_aggregate_functions, client
        )
        SUBSCRIPTIONS.attach(client)
        yield {"opcua_client": client}
    finally:
        # Drop the subscriptions before the session that carries them —
        # the event ones as much as the data-change ones. Disconnecting first
        # would leave the OPC UA server publishing to nobody until each
        # subscription's lifetime expired.
        await asyncio.to_thread(SUBSCRIPTIONS.close_all)
        await asyncio.to_thread(_EVENTS.close_all)
        # Disconnect from OPC UA server on shutdown
        await asyncio.to_thread(client.disconnect)
        _CAPABILITIES["history"] = False
        _CAPABILITIES["aggregate_functions"] = {}
        print("Disconnected from OPC UA server", file=sys.stderr)


class PolicyMCPServer(MCPServer):
    """MCPServer whose advertised and callable tools obey deployment policy."""

    async def list_tools(self):
        policy = tool_policy()
        specs = {tool["name"]: tool for tool in CONTRACT["tools"]}
        listed = await super().list_tools()
        visible = []
        for tool in listed:
            spec = specs[tool.name]
            if not policy.is_visible(spec):
                continue
            capability = spec.get("capability")
            if capability == "history" and not _CAPABILITIES["history"]:
                continue
            if capability == "aggregate" and not _CAPABILITIES["aggregate_functions"]:
                continue
            annotations = ToolAnnotations(**spec["annotations"])
            output_schema = None
            if shape_name := spec.get("resultShape"):
                output_schema = {
                    "type": "object",
                    "properties": {"result": CONTRACT["resultShapes"][shape_name]},
                    "required": ["result"],
                    "additionalProperties": False,
                }
            visible.append(
                tool.model_copy(update={"annotations": annotations, "output_schema": output_schema})
            )
        return visible

    async def call_tool(self, name, arguments, context=None):
        arguments = arguments or {}
        try:
            # Catalog filtering is not authorization: clients may retain an old
            # tools/list result, so enforce the current policy again on every call.
            tool_policy().authorize(name, arguments)
            _audit_decision(name, arguments, "allowed")
            spec = next(tool for tool in CONTRACT["tools"] if tool["name"] == name)
            capability = spec.get("capability")
            if capability == "history" and not _CAPABILITIES["history"]:
                raise ToolError("OPC UA server does not advertise history support")
            if capability == "aggregate" and not _CAPABILITIES["aggregate_functions"]:
                raise ToolError("OPC UA server does not advertise aggregate support")
        except (PermissionError, ValueError) as exc:
            _audit_decision(name, arguments, "denied", str(exc))
            raise ToolError(str(exc)) from exc
        return await super().call_tool(name, arguments, context)


# Create an MCP server instance. The server identity must match the Node server's
# so both runtimes present themselves as the same product to MCP clients, and the
# version must be a real one rather than the null the Node server never reports.
mcp = PolicyMCPServer("opcua-mcp-server", version=package_version(), lifespan=opcua_lifespan)


# Tool: Read the value of an OPC UA node
@mcp.tool(description=DESC["read_opcua_node"])
def read_opcua_node(node_id: str, ctx: Context) -> str:
    """
    Read the value of a specific OPC UA node.

    Parameters:
        node_id (str): The OPC UA node ID in the format 'ns=<namespace>;i=<identifier>'.
                       Example: 'ns=2;i=2'.

    Returns:
        str: The value of the node as a string, prefixed with the node ID.
    """
    client = ctx.request_context.lifespan_context["opcua_client"]
    node = client.get_node(node_id)
    value = node.get_value()  # Synchronous call to get node value
    return f"Node {node_id} value: {value}"


# Tool: Read historical values of an OPC UA node.
# Registered only when the server supports historical data access (see below),
# mirroring the Node server's capability gating.
def read_history_opcua_node(
    node_id: str,
    ctx: Context,
    start_time: str | None = None,
    end_time: str | None = None,
    num_values: int = 0,
) -> list[dict]:
    """
    Read the historical values of a specific OPC UA node.

    Parameters:
        node_id (str): The OPC UA node ID in the format 'ns=<namespace>;i=<identifier>'.
                       Example: 'ns=2;i=2'.
        start_time (str): Start time (ISO 8601).
                          Example: '2026-04-22T18:50:00Z'
        end_time (str): End time (ISO 8601).
                        Example: '2026-04-22T18:51:00Z'
        num_values (int): Number of values to read (default: unlimited)

    Returns:
        list[dict]: One record per historical value, shaped
            `{ "value": <value>, "timestamp": "<ISO-8601 UTC>", "status": "Good" }`
            — the shared shape defined in `contract/tools.json`
            (`resultShapes.historyRecords`) and matched by the Node server.
    """
    client = ctx.request_context.lifespan_context["opcua_client"]
    # `ToolError`, not a bare exception: the SDK forwards a ToolError's message to
    # the client and withholds anything else as a crash. A bad node ID or an
    # unparseable timestamp is the caller's to fix, so it has to reach them —
    # worded exactly as the Node server words it.
    try:
        node = client.get_node(node_id)
        values = node.read_raw_history(
            starttime=parse_iso_datetime(start_time),
            endtime=parse_iso_datetime(end_time),
            numvalues=num_values,
        )
    except Exception as e:
        raise ToolError(f"Failed to read node {node_id}: {e!s}") from e
    return history_records(values)


# Register optional tools once; tools/list gates them using the capabilities read
# from the lifecycle's active session. This avoids network I/O during import and
# prevents startup from opening throwaway OPC UA sessions.
read_history_opcua_node = mcp.tool(description=DESC["read_history_opcua_node"])(
    read_history_opcua_node
)


# Tool: Read server-computed aggregates over a node's history.
# Registered only when the server advertises aggregate functions, mirroring the
# Node server's capability gating.
def read_aggregate_opcua_node(
    node_id: str,
    ctx: Context,
    start_time: str,
    aggregate_function: str,
    end_time: str | None = None,
    processing_interval: float = 0,
) -> list[dict]:
    """
    Calculate historical aggregates over a time range, in fixed-size intervals.

    Parameters:
        node_id (str): The OPC UA node ID in the format 'ns=<namespace>;i=<identifier>'.
                       Example: 'ns=2;i=2'.
        start_time (str): Beginning of the retrieval (ISO 8601).
        aggregate_function (str): The specific formula, e.g. 'Average'.
        end_time (str): End of the retrieval (ISO 8601, defaults to 'now').
        processing_interval (float): Duration (ms) for each computed value. 0 asks
                                     the server for a single value over the range.

    Returns:
        list[dict]: One record per interval, shaped
            `{ "value": <value>, "timestamp": "<ISO-8601 UTC>", "status": "Good" }`
            — the shared shape defined in `contract/tools.json`
            (`resultShapes.historyRecords`) and matched by the Node server. An
            interval the server holds no data for has a null `value` and a
            non-Good `status`.
    """
    aggregate_functions = _CAPABILITIES["aggregate_functions"]
    # Both runtimes reject an unsupported function with the same sentence, so the
    # message is part of the contract and must reach the client rather than be
    # masked as a crash — hence ToolError. See `validate_aggregate_function`.
    try:
        validate_aggregate_function(aggregate_function, aggregate_functions)
    except ValueError as e:
        raise ToolError(str(e)) from e

    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        details = ua.ReadProcessedDetails()
        details.StartTime = parse_iso_datetime(start_time)
        # UTC, not naive local time: `parse_iso_datetime` yields aware UTC, so a
        # naive `datetime.now()` here would shift the window end by the host's UTC
        # offset and pad the result with an empty bucket per interval in between.
        details.EndTime = parse_iso_datetime(end_time) or datetime.now(timezone.utc)
        details.ProcessingInterval = processing_interval
        details.AggregateType = [aggregate_functions[aggregate_function]]

        result = client.get_node(node_id).history_read(details)
        if not result.StatusCode.is_good():
            raise ValueError(f"Read aggregate failed with status: {result.StatusCode}")

        return history_records(result.HistoryData.DataValues)
    except Exception as e:
        raise ToolError(f"Failed to read node {node_id}: {e!s}") from e


read_aggregate_opcua_node = mcp.tool(description=DESC["read_aggregate_opcua_node"])(
    read_aggregate_opcua_node
)


# Tool: Write a value to an OPC UA node
@mcp.tool(description=DESC["write_opcua_node"])
def write_opcua_node(node_id: str, value: Any, ctx: Context) -> str:
    """
    Write a value to a specific OPC UA node.

    Parameters:
        node_id (str): The OPC UA node ID in the format 'ns=<namespace>;i=<identifier>'.
                       Example: 'ns=2;i=3'.
        value (str): The value to write to the node. Will be converted based on node type.

    Returns:
        str: A message confirming the write. A failure is raised as a `ToolError`,
             which reaches the client as an MCP error result rather than as text.
    """
    client = ctx.request_context.lifespan_context["opcua_client"]
    node = client.get_node(node_id)
    try:
        target = node.get_data_value().Value
        converted = convert_for_variant(value, target.VariantType, target.is_array)
        node.set_value(ua.Variant(converted, target.VariantType))
        return f"Successfully wrote {value} to node {node_id}"
    # `ToolError`, not a returned string: a returned string is a *successful* tool
    # result, so a client had to read the prose to notice the write never landed.
    # Worded as the Node server words it. See #63.
    except Exception as e:
        raise ToolError(f"Failed to write to node {node_id}: {e!s}") from e


def browse_children(node: Node) -> list[Node]:
    """Browse a node's references, failing on a bad browse status.

    python-opcua cannot do this itself: `Node.get_children()` reaches
    `get_references()`, which reads `BrowseResult.References` and never looks at
    the sibling `BrowseResult.StatusCode`. Browsing a node the server does not
    have therefore yields an empty list, so `browse_opcua_node_children` reported
    `Children of ns=2;i=999999: []` — "this node has no children", for a node
    that does not exist. The Node server checks the status and fails
    (`browseOpcuaNodeChildren` in `packages/server-node/src/tools.ts`), so this
    does too, with the same sentence.

    Otherwise a faithful copy of what `get_children()` asks for, which is not
    what `get_references()` defaults to: *hierarchical* references, *forward*
    only. Browsing `References`/`Both` instead — the `get_references()` defaults —
    walks back up to the parent and out to the type definition, so `ns=2;i=1`
    answers `0:Objects` and `0:FolderType` rather than its own `2:Sensors`.
    """
    description = ua.BrowseDescription()
    description.NodeId = node.nodeid
    description.BrowseDirection = ua.BrowseDirection.Forward
    description.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HierarchicalReferences)
    description.IncludeSubtypes = True
    description.NodeClassMask = ua.NodeClass.Unspecified
    description.ResultMask = ua.BrowseResultMask.All

    params = ua.BrowseParameters()
    params.View.Timestamp = ua.get_win_epoch()
    params.NodesToBrowse.append(description)
    params.RequestedMaxReferencesPerNode = 0

    # A server may cap how many references one response carries whatever we ask
    # for, so drain the continuation point as `get_references()` does — otherwise
    # a large node silently browses short. Every result is status-checked, the
    # continued ones included: a server that expires or refuses a continuation
    # point answers with a bad status and no references, which unchecked would
    # end the loop and return a *truncated* child list as a success — the same
    # class of silent wrong answer this function exists to stop.
    references = []
    results = node.server.browse(params)
    while True:
        result = results[0]
        if not result.StatusCode.is_good():
            # `.name`, not the whole StatusCode: node-opcua renders the same
            # rejection as `BadNodeIdUnknown (0x80340000)` and python-opcua as
            # `StatusCode(BadNodeIdUnknown)`. Neither server controls the other's
            # spelling, but both can name the status plainly.
            raise ValueError(f"Browse failed with status: {result.StatusCode.name}")

        references.extend(result.References)
        if not result.ContinuationPoint:
            break

        next_params = ua.BrowseNextParameters()
        next_params.ContinuationPoints = [result.ContinuationPoint]
        next_params.ReleaseContinuationPoints = False
        results = node.server.browse_next(next_params)

    # `get_children()` returns Nodes, not ReferenceDescriptions, and the caller
    # reads `.nodeid` and browse names off them.
    return [Node(node.server, reference.NodeId) for reference in references]


# Tool: Browse the children of a specific OPC UA node
@mcp.tool(description=DESC["browse_opcua_node_children"])
def browse_opcua_node_children(node_id: str, ctx: Context) -> str:
    """
    Browse the children of a specific OPC UA node.

    Parameters:
        node_id (str): The OPC UA node ID to browse (e.g., 'ns=0;i=85' for Objects folder).

    Returns:
        str: A string representation of a list of child nodes, including their
             NodeId and BrowseName. A failure is raised as a `ToolError`, which
             reaches the client as an MCP error result rather than as text.
    """
    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        node = client.get_node(node_id)
        children = browse_children(node)

        children_info = []
        for child in children:
            try:
                browse_name = child.get_browse_name()
                children_info.append(
                    {
                        "node_id": child.nodeid.to_string(),
                        "browse_name": f"{browse_name.NamespaceIndex}:{browse_name.Name}",
                    }
                )
            except Exception as e:
                children_info.append(
                    {"node_id": child.nodeid.to_string(), "browse_name": f"Error getting name: {e}"}
                )

        # import json
        # return json.dumps(children_info, indent=2)
        return f"Children of {node_id}: {children_info!r}"

    except Exception as e:
        raise ToolError(f"Failed to browse children of node {node_id}: {e!s}") from e


# Tool: Call an OPC UA method
@mcp.tool(description=DESC["call_opcua_method"])
def call_opcua_method(
    object_node_id: str, method_node_id: str, ctx: Context, arguments: list[Any] | None = None
) -> str:
    """
    Call a method on a specific OPC UA object node.

    Parameters:
        object_node_id (str): The OPC UA node ID of the object that contains the method.
                             Example: 'ns=2;i=1' for the Methods folder.
        method_node_id (str): The OPC UA node ID of the method to call.
                             Example: 'ns=2;i=2' for StartProduction method.
        ctx (Context): The context for the request.
        arguments (List[Any], optional): List of arguments to pass to the method.
                                       Arguments will be converted to appropriate OPC UA variants.

    Returns:
        str: The result of the method call. A failure is raised as a `ToolError`,
             which reaches the client as an MCP error result rather than as text.
    """
    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        # Get the object and method nodes
        object_node = client.get_node(object_node_id)
        method_node = client.get_node(method_node_id)

        # Prepare arguments
        method_args = []
        if arguments:
            for arg in arguments:
                # Convert arguments to appropriate types
                if isinstance(arg, str):
                    # Try to convert string to appropriate type
                    try:
                        # Try float first
                        method_args.append(float(arg))
                    except ValueError:
                        try:
                            # Try int
                            method_args.append(int(arg))
                        except ValueError:
                            # Keep as string
                            method_args.append(arg)
                else:
                    method_args.append(arg)

        # Call the method on the object node. python-opcua exposes call_method on Node
        # (not Client), and a string methodid is treated as a child browse-name, so pass
        # the resolved method Node to call it by node id.
        result = object_node.call_method(method_node, *method_args)

        return (
            f"Method call successful. Object: {object_node_id}, "
            f"Method: {method_node_id}, Result: {result}"
        )

    except Exception as e:
        raise ToolError(
            f"Failed to call method {method_node_id} on object {object_node_id}: {e!s}"
        ) from e


# Tool: Read multiple OPC UA nodes
@mcp.tool(description=DESC["read_multiple_opcua_nodes"])
def read_multiple_opcua_nodes(node_ids: list[str], ctx: Context) -> str:
    """
    Read the values of multiple OPC UA nodes in a single request.

    Parameters:
        node_ids (List[str]): A list of OPC UA node IDs to read (e.g., ['ns=2;i=2', 'ns=2;i=3']).

    Returns:
        str: A string representation of a dictionary mapping node IDs to their
             values, or an error message.
    """
    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        nodes = [client.get_node(node_id) for node_id in node_ids]
        values = client.uaclient.get_attributes(
            [node.nodeid for node in nodes], ua.AttributeIds.Value
        )
        results = {}
        for node_id, data_value in zip(node_ids, values, strict=True):
            if data_value.StatusCode.is_good():
                results[node_id] = data_value.Value.Value
            else:
                results[node_id] = f"Error: {data_value.StatusCode}"

        return json.dumps(results, indent=2, default=str)

    except Exception as e:
        return f"Error reading multiple nodes: {e!s}"


# Tool: Write multiple OPC UA nodes
@mcp.tool(description=DESC["write_multiple_opcua_nodes"])
def write_multiple_opcua_nodes(nodes_to_write: list[dict[str, Any]], ctx: Context) -> str:
    """
    Write values to multiple OPC UA nodes in a single request.

    Parameters:
        nodes_to_write (List[Dict[str, Any]]): A list of dictionaries, where each dictionary
                                               contains 'node_id' (str) and 'value' (Any).
                                               The value will be wrapped in an OPC UA Variant.
                                               Example: [{'node_id': 'ns=2;i=2', 'value': 10.5},
                                                         {'node_id': 'ns=2;i=3', 'value': 'active'}]

    Returns:
        str: A status code per write attempt. A node the server rejects is one
             `Error: …` status among them; only a failure of the whole operation
             is raised as a `ToolError`.
    """
    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        nodes = [client.get_node(item["node_id"]) for item in nodes_to_write]
        current = client.uaclient.get_attributes(
            [node.nodeid for node in nodes], ua.AttributeIds.Value
        )
        results = [None] * len(nodes_to_write)
        writable_nodes = []
        writable_values = []
        writable_indices = []

        for index, (item, node, data_value) in enumerate(
            zip(nodes_to_write, nodes, current, strict=True)
        ):
            if not data_value.StatusCode.is_good():
                results[index] = {
                    "node_id": item["node_id"],
                    "status": f"Error: {data_value.StatusCode}",
                }
                continue
            try:
                target = data_value.Value
                converted = convert_for_variant(item["value"], target.VariantType, target.is_array)
                writable_nodes.append(node.nodeid)
                writable_values.append(ua.DataValue(ua.Variant(converted, target.VariantType)))
                writable_indices.append(index)
            except Exception as e:
                results[index] = {"node_id": item["node_id"], "status": f"Error: {e!s}"}

        if writable_nodes:
            statuses = client.uaclient.set_attributes(
                writable_nodes, writable_values, ua.AttributeIds.Value
            )
            for index, status in zip(writable_indices, statuses, strict=True):
                results[index] = {
                    "node_id": nodes_to_write[index]["node_id"],
                    "status": "Success" if status.is_good() else f"Error: {status}",
                }

        return f"Write operation results:\n{json.dumps(results, indent=2)}"

    # Only reached when the whole operation fails rather than one node in it —
    # a per-node rejection is a `status` in the list above, on both servers, and
    # stays a successful result. This is the Node server's outer catch.
    except Exception as e:
        raise ToolError(f"Failed to write multiple nodes: {e!s}") from e


# --- Data-change subscriptions -------------------------------------------------
# A tool call is request/response, so a subscription cannot answer its caller:
# the notifications arrive whenever the OPC UA server publishes. The changes are
# buffered instead (see subscriptions.py) and read back through
# `list_subscriptions` or the `opcua://subscriptions` resource.
#
# Every OPC UA call here runs in a thread: python-opcua is synchronous, and
# creating a subscription blocks on a round trip to the server.


# Tool: Subscribe to data changes on an OPC UA node
@mcp.tool(description=DESC["subscribe_opcua_node"])
async def subscribe_opcua_node(
    node_id: str,
    publishing_interval: float = 1000,
    sampling_interval: float = 0,
    buffer_size: int = 20,
) -> list[dict]:
    """
    Subscribe to data changes on a specific OPC UA node.

    Parameters:
        node_id (str): The OPC UA node ID to monitor, in the format
                       'ns=<namespace>;i=<identifier>'. Example: 'ns=2;i=3'.
        publishing_interval (float): How often (ms) the server publishes queued changes.
        sampling_interval (float): How often (ms) the server samples the node;
                                   0 means sample at `publishing_interval`.
        buffer_size (int): How many of the most recent changes to retain.

    Returns:
        list[dict]: A single record for the new subscription, shaped by
            `contract/tools.json` -> `resultShapes.subscriptionRecords`.
    """
    # `ToolError`, not a bare exception: the SDK forwards a ToolError's message
    # to the client and withholds anything else as a crash. A bad node ID is the
    # caller's to fix, so it has to reach them — worded as the Node server words it.
    try:
        record = await asyncio.to_thread(
            SUBSCRIPTIONS.subscribe,
            node_id,
            publishing_interval,
            sampling_interval,
            buffer_size,
        )
    except Exception as e:
        raise ToolError(f"Failed to subscribe to node {node_id}: {e!s}") from e
    return [record]


# Tool: List the active data-change subscriptions
@mcp.tool(description=DESC["list_subscriptions"])
def list_subscriptions() -> list[dict]:
    """
    List the active OPC UA data-change subscriptions and their buffered changes.

    Returns:
        list[dict]: One record per active subscription, shaped by
            `contract/tools.json` -> `resultShapes.subscriptionRecords`. An empty
            list when nothing is subscribed.
    """
    # No thread hop and no OPC UA call: this reads buffers already filled by
    # python-opcua's publishing thread, so it answers even if the server is down.
    return SUBSCRIPTIONS.list()


# Tool: Cancel a data-change subscription
@mcp.tool(description=DESC["unsubscribe_opcua_node"])
async def unsubscribe_opcua_node(subscription_id: str) -> str:
    """
    Cancel an active OPC UA data-change subscription.

    Parameters:
        subscription_id (str): The ID returned by `subscribe_opcua_node`.

    Returns:
        str: A confirmation naming the node and how many changes it delivered.
    """
    try:
        record = await asyncio.to_thread(SUBSCRIPTIONS.unsubscribe, subscription_id)
    except KeyError as e:
        # Both runtimes word an unknown ID identically; see subscriptions.py.
        raise ToolError(unknown_subscription_message(subscription_id)) from e
    except RuntimeError as e:
        # The OPC UA server refused the delete. Already worded for the caller by
        # `delete_failed_message`, and shared with the Node server.
        raise ToolError(str(e)) from e
    return (
        f"Unsubscribed {record['subscription_id']} from node {record['node_id']} "
        f"after {record['change_count']} value changes"
    )


# Resource: the same subscription records, re-readable without a tool call.
#
# No `ctx: Context` parameter — MCPServer refuses to inject one into a static
# resource — which is why the manager is module-level state rather than
# something held in the lifespan context.
@mcp.resource(
    SUBSCRIPTIONS_RESOURCE["uri"],
    name=SUBSCRIPTIONS_RESOURCE["name"],
    description=SUBSCRIPTIONS_RESOURCE["description"],
    mime_type=SUBSCRIPTIONS_RESOURCE["mimeType"],
)
def subscriptions_resource() -> str:
    """The active subscriptions and their buffered changes, as a JSON document."""
    key = SUBSCRIPTIONS_RESOURCE["body"]["recordsKey"]
    return json.dumps({key: SUBSCRIPTIONS.list()}, indent=2)


# Tool: Get all variables information
@mcp.tool(description=DESC["get_all_variables"])
def get_all_variables(
    ctx: Context,
    root_node_id: str = "ns=0;i=85",
    max_depth: int = 8,
    max_nodes: int = 500,
    include_values: bool = True,
) -> str:
    """
    Discover variables below a root node within a bounded traversal budget.

    Returns:
        str: Discovered variables plus whether the traversal was truncated.
    """
    client = ctx.request_context.lifespan_context["opcua_client"]
    variables_info = []
    max_depth = max(0, min(max_depth, 64))
    max_nodes = max(1, min(max_nodes, 5000))

    try:
        root = client.get_node(root_node_id)
        queue = deque([(root, 0)])
        visited = {root.nodeid.to_string()}
        inspected = 0
        truncated = False

        while queue:
            node, depth = queue.popleft()
            try:
                children = browse_children(node)
            except Exception:
                if node.nodeid.to_string() == root_node_id:
                    raise
                continue

            for child in children:
                child_id = child.nodeid.to_string()
                if child_id in visited:
                    continue
                visited.add(child_id)
                if inspected >= max_nodes:
                    truncated = True
                    break
                inspected += 1

                try:
                    node_class = child.get_node_class()
                except Exception:
                    continue

                # Skip the entire "Server" subtree
                try:
                    child_browse_name = child.get_browse_name().Name
                    if child_browse_name == "Server":
                        continue
                except Exception:
                    continue

                if node_class == NodeClass.Variable:
                    browse_name = child_browse_name

                    value = None
                    if include_values:
                        try:
                            value = child.get_value()
                        except Exception:
                            value = None

                    try:
                        data_type = child.get_data_type().to_string()
                    except Exception:
                        data_type = ""

                    try:
                        desc = child.get_description().Text
                    except Exception:
                        desc = ""

                    variables_info.append(
                        {
                            "name": browse_name,
                            "nodeid": child_id,
                            "object_id": node.nodeid.to_string(),
                            "value": value,
                            "data_type": data_type,
                            "description": desc,
                        }
                    )
                elif node_class == NodeClass.Object and depth < max_depth:
                    queue.append((child, depth + 1))

            if truncated:
                break

        if variables_info:
            suffix = f" after inspecting {inspected} nodes"
            if truncated:
                suffix += f" (truncated at max_nodes={max_nodes})"
            result = f"Found {len(variables_info)} variables{suffix}:\n"
            for var in variables_info:
                result += f"\n- Name: {var['name']}\n"
                result += f"  NodeID: {var['nodeid']}\n"
                result += f"  Object ID: {var['object_id']}\n"
                result += f"  Value: {var['value']}\n"
                result += f"  Data Type: {var['data_type']}\n"
                result += f"  Description: {var['description']}\n"
            return result
        else:
            suffix = f" after inspecting {inspected} nodes"
            if truncated:
                suffix += f" (truncated at max_nodes={max_nodes})"
            return f"No variables found{suffix}."

    except Exception as e:
        raise ToolError(f"Failed to discover variables below {root_node_id}: {e!s}") from e


# --- events and Alarms & Conditions --------------------------------------------
# The wording of every message below is shared with the Node server's `events`
# tools, so a model that has learned one runtime's replies reads the other's the
# same way. See packages/server-node/src/tools.ts.

#: Event subscriptions live for as long as the process does, not for one tool
#: call: `subscribe_events` starts them and `read_events` drains them later.
_EVENTS = events.EventSubscriptions()


@mcp.tool(description=DESC["subscribe_events"])
def subscribe_events(
    ctx: Context,
    node_id: str = events.DEFAULT_NOTIFIER,
    severity_min: int = events.DEFAULTS["severityMin"],
    buffer_size: int = events.DEFAULTS["bufferSize"],
) -> str:
    """
    Start buffering OPC UA events from a notifier node.

    Parameters:
        node_id (str): The notifier node to subscribe to (default: the Server object).
        severity_min (int): Buffer only events of at least this severity (1-1000).
        buffer_size (int): How many events to hold before dropping the oldest.

    Returns:
        str: Confirmation that the subscription is running.
    """
    # 0 means "unset" for a size, as it does everywhere else in both servers:
    # the Node side gets this from `||`, and a buffer that keeps nothing would be
    # a strange thing to have asked for.
    buffer_size = buffer_size or events.DEFAULTS["bufferSize"]
    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        _EVENTS.subscribe(client, node_id, severity_min, buffer_size)
    except Exception as e:
        raise ToolError(f"Failed to subscribe to events from node {node_id}: {e!s}") from e
    return (
        f"Subscribed to events from node {node_id}, buffering up to {buffer_size} "
        f"events of severity {severity_min} or above. Read them with read_events."
    )


@mcp.tool(description=DESC["read_events"])
def read_events(
    node_id: str = events.DEFAULT_NOTIFIER,
    limit: int = events.DEFAULTS["readLimit"],
) -> CallToolResult:
    """
    Read and drain the events buffered by subscribe_events.

    Parameters:
        node_id (str): The subscribed notifier node (default: the Server object).
        limit (int): Maximum number of events to return.

    Returns:
        CallToolResult: Event records in text and structured form, plus a
            plain-text compatibility notice when the buffer overflowed.
    """
    drained = _EVENTS.drain(node_id, limit or events.DEFAULTS["readLimit"])
    if drained is None:
        raise ToolError(
            f"Not subscribed to events from node {node_id}. Call subscribe_events first."
        )
    records, _remaining, dropped, size = drained
    content = [TextContent(type="text", text=json.dumps(record, indent=2)) for record in records]
    if dropped:
        # The notice remains visible to models in compatibility content, but is
        # not an event record and therefore stays outside structuredContent.
        content.append(TextContent(type="text", text=events.dropped_events_message(dropped, size)))
    return CallToolResult(content=content, structured_content={"result": records})


@mcp.tool(description=DESC["list_active_alarms"])
def list_active_alarms(
    ctx: Context,
    node_id: str = events.DEFAULT_NOTIFIER,
    timeout_seconds: float = events.DEFAULTS["refreshTimeoutSeconds"],
) -> list[dict]:
    """
    List the alarm/condition instances the server is currently retaining.

    Parameters:
        node_id (str): The notifier node whose conditions to list.
        timeout_seconds (float): How long to wait for the server to finish.

    Returns:
        list[dict]: One record per retained condition, shaped by the shared
            ``resultShapes.eventRecords`` in ``contract/tools.json``.
    """
    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        alarms = events.list_active_alarms(client, node_id, timeout_seconds)
    except Exception as e:
        raise ToolError(f"Failed to list active alarms from node {node_id}: {e!s}") from e
    _EVENTS.remember(alarms)
    return alarms


@mcp.tool(description=DESC["acknowledge_alarm"])
def acknowledge_alarm(
    event_id: str,
    ctx: Context,
    comment: str = "",
    condition_id: str | None = None,
) -> str:
    """
    Acknowledge an alarm or condition by the event_id that reported it.

    Parameters:
        event_id (str): The reported event's ``event_id`` (base64).
        comment (str): Comment to record with the acknowledgement.
        condition_id (str): NodeId of the condition, when this server has not
                            seen the event itself.

    Returns:
        str: Confirmation naming the condition that was acknowledged.
    """
    condition = condition_id or _EVENTS.condition_for(event_id)
    if not condition:
        raise ToolError(
            f'Unknown event_id "{event_id}". Call list_active_alarms first, or pass the '
            "condition_id of the alarm to acknowledge."
        )

    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        events.acknowledge_alarm(client, condition, event_id, comment)
    except Exception as e:
        raise ToolError(f"Failed to acknowledge alarm {condition}: {e!s}") from e
    return f"Acknowledged alarm {condition} (event {event_id})"


# Run the server
def main() -> None:
    """Run the MCP server on stdio.

    The console script points at `cli.main`, which dispatches CLI flags first and
    only imports this module — and so only probes the OPC UA server — when it is
    actually going to serve. So this runs on the serving path only: `--help` and
    `--install` must stay usable while the security configuration is still being
    got right.
    """
    # Fail fast and readably on a bad security configuration: an MCP client only
    # ever shows the server's stderr, so letting it surface from a best-effort
    # capability probe (which swallows it) would leave nothing to go on.
    try:
        security_config()
        policy = tool_policy()
    except ValueError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        raise SystemExit(1) from None

    print(f"Tool policy: {describe_policy(policy)}", file=sys.stderr)

    mcp.run(transport="stdio")
