"""The MCP server: lifecycle, tool registration, and the stdio entry point."""

from __future__ import annotations

import asyncio
import copy
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

from . import events
from .aggregates import validate_aggregate_function
from .capabilities import client_aggregate_functions, client_supports_history
from .config import SERVER_URL, describe_reconnect, reconnect_config
from .connection import (
    OpcuaConnection,
    describe_error,
    is_connection_error,
    not_connected_message,
)
from .contract import CONTRACT, DESC, SUBSCRIPTIONS_RESOURCE
from .datetimes import format_iso_utc, parse_iso_datetime
from .diagnostics import disconnected_status, read_server_status
from .node_ids import canonical_node_id
from .policy import describe_policy, tool_policy, values_at
from .records import history_records, scalar_to_json, variant_to_json
from .security import describe_security, security_config
from .subscriptions import (
    SUBSCRIPTIONS,
    unknown_subscription_message,
    unknown_subscriptions_message,
)
from .variant_codec import convert_for_variant
from .version import package_version

_CAPABILITIES: dict[str, Any] = {"history": False, "aggregate_functions": {}}


def _audit_targets(spec: dict, arguments: dict[str, Any]) -> dict[str, Any]:
    """What a control call was aimed at, for the audit record.

    Derived from the tool's own ``guard``, not from a chain on tool *names*. That
    chain was the last one left after the policy layer stopped keying off names,
    and it broke silently the moment the tools were renamed: every write logged
    ``decision: "allowed"`` with no targets at all, which is an audit trail that
    records that *something* was permitted without recording what. Reading the
    same declaration the policy authorises from means the two can no longer
    disagree about which arguments matter.
    """
    guard = spec.get("guard")
    if not guard:
        return {}
    record: dict[str, Any] = {}

    node_ids = [
        value for path in guard.get("nodeIdPaths", []) for value in values_at(arguments, path)
    ]
    if node_ids:
        record["node_ids"] = node_ids

    for pair in guard.get("methodPaths", []):
        objects = values_at(arguments, pair["objectPath"])
        methods = values_at(arguments, pair["methodPath"])
        record["object_node_id"] = objects[0] if objects else None
        record["method_node_id"] = methods[0] if methods else None
        break

    for path in guard.get("auditPaths", []):
        # Only what is present: an absent optional argument is not a target, and
        # recording it as null would make every acknowledgement look
        # half-specified.
        values = values_at(arguments, path)
        if values:
            record[path] = values[0]
    return record


def _audit_decision(name: str, arguments: dict[str, Any], decision: str, reason: str = "") -> None:
    """Write one line of the control audit trail to stderr.

    Only ``control`` and ``alarm-action`` tools: an audit trail that also
    recorded every read would bury the four lines anyone is looking for.

    Never the *values* being written, only the targets. A setpoint is process
    data, and this stream is the one an MCP client shows the user and a log
    collector ships off the machine.
    """
    spec = next((tool for tool in CONTRACT["tools"] if tool["name"] == name), None)
    if spec is None or spec["accessClass"] not in {"control", "alarm-action"}:
        return
    record = {
        "event": "opcua_mcp_policy",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "profile": tool_policy().config.profile,
        "tool": name,
        "decision": decision,
        **_audit_targets(spec, arguments),
    }
    if reason:
        record["reason"] = reason
    print(json.dumps(record, separators=(",", ":")), file=sys.stderr)


#: The one connection the tools, the lifespan and tools/list share. Module-level
#: for the same reason `SUBSCRIPTIONS` is: `list_tools` is handed no `Context`,
#: so it cannot reach the lifespan state to refresh its capability probes.
_CONNECTION: OpcuaConnection | None = None


def _bind(state: dict, client) -> None:
    """Point everything that holds a client at the one just established.

    Called by the connection whenever it produces a client — at startup and
    again after each reconnect. The lifespan state is *mutated* rather than
    replaced because the MCP server hands the same dict to every tool call, so
    this is what makes a tool that reads `lifespan_context["opcua_client"]` see
    the new session rather than the dead one.
    """
    state["opcua_client"] = client
    SUBSCRIPTIONS.reattach(client)


def _refresh_capabilities(connection: OpcuaConnection) -> None:
    """Re-probe the optional capabilities, best-effort.

    On every tools/list rather than once at startup, as the Node server does: a
    server that was unreachable when this process began must not have its history
    and aggregate tools hidden for the lifetime of the session.

    One connection attempt for both probes, not one each: against a server that
    is down, each would otherwise sit through the whole configured backoff on its
    own and double what a tools/list costs. The attempt itself is why this is
    also what the lifespan calls to open the first connection.
    """
    _CAPABILITIES["history"] = False
    _CAPABILITIES["aggregate_functions"] = {}
    try:
        client = connection.ensure_connected()
    except Exception:
        # `connect` has already said why on stderr; an optional capability must
        # never break tools/list, so a server that is down simply advertises the
        # core tools until it comes back.
        return
    _CAPABILITIES["history"] = _probe(client_supports_history, client, False)
    _CAPABILITIES["aggregate_functions"] = _probe(client_aggregate_functions, client, {})


def _probe(read, client, fallback):
    """Run one capability probe, yielding ``fallback`` on any failure."""
    try:
        return read(client)
    except Exception as error:
        print(f"OPC UA capability probe failed: {describe_error(error)}", file=sys.stderr)
        return fallback


# Manage the lifecycle of the OPC UA client connection
@asynccontextmanager
async def opcua_lifespan(server: MCPServer) -> AsyncIterator[dict]:
    """Handle OPC UA client connection lifecycle."""
    global _CONNECTION
    connection = OpcuaConnection(SERVER_URL)
    _CONNECTION = connection
    state: dict = {"opcua_client": None, "opcua_connection": connection}
    connection.on_client_replaced = lambda client: _bind(state, client)

    # In a thread: python-opcua is synchronous, and for a secured connection even
    # building the client fetches the server's certificate from its endpoint
    # list, so this blocks on the network too. Connecting and probing are one
    # call so a server that is down costs one round of backoff, not two.
    await asyncio.to_thread(_refresh_capabilities, connection)
    if not connection.connected:
        # Deliberately not fatal. An MCP client starts this server when *it*
        # starts, which may be long before the plant network is reachable; dying
        # here would mean a restart of the MCP client for every OPC UA outage.
        # Every tool call retries the connection, and `get_server_status` reports
        # what is wrong in the meantime.
        print(
            f"Starting without an OPC UA connection: {connection.last_error}. "
            f"Tools will retry on each call.",
            file=sys.stderr,
        )

    try:
        yield state
    finally:
        # Drop the subscriptions before the session that carries them —
        # the event ones as much as the data-change ones. Disconnecting first
        # would leave the OPC UA server publishing to nobody until each
        # subscription's lifetime expired.
        await asyncio.to_thread(SUBSCRIPTIONS.close_all)
        await asyncio.to_thread(_EVENTS.close_all)
        # Disconnect from OPC UA server on shutdown
        await asyncio.to_thread(connection.disconnect)
        _CAPABILITIES["history"] = False
        _CAPABILITIES["aggregate_functions"] = {}
        _CONNECTION = None


def _available_capabilities() -> set[str]:
    """What the connected OPC UA server reports it can do."""
    available = set()
    if _CAPABILITIES["history"]:
        available.add("history")
    if _CAPABILITIES["aggregate_functions"]:
        available.add("aggregate")
    return available


def _capabilities_met(spec: dict) -> bool:
    """Whether a tool's capability gate is satisfied.

    A tool gated on capabilities is offered when the server reports *any* of
    them. ``read_opcua_history`` lists both: a server with only aggregates can
    still answer an aggregate read, and gating it on ``history`` alone would hide
    the one thing such a server is good at.
    """
    required = spec.get("capabilities") or []
    return not required or bool(set(required) & _available_capabilities())


def _advertised_schema(schema: dict, spec: dict) -> dict:
    """A tool's input schema as advertised, with capability-gated properties removed.

    Capability gating moved down a level when the history and aggregate tools
    merged: ``read_opcua_history`` is advertised whenever the server reports
    HistoricalAccess, and its ``aggregate_function`` argument appears only if the
    server also advertises aggregates — with that server's *own* function list
    named in the description. An argument the server cannot honour is therefore
    not merely documented as unsupported; it is not offered, which is the same
    property tool-level gating had and strictly more informative, because the
    list is the live one.
    """
    properties = (schema or {}).get("properties") or {}
    if "aggregate_function" not in properties:
        return schema

    advertised = copy.deepcopy(schema)
    functions = _CAPABILITIES["aggregate_functions"]
    if not functions:
        advertised["properties"].pop("aggregate_function", None)
        advertised["properties"].pop("processing_interval", None)
        return advertised

    # The base text comes from the contract, not from this schema: FastMCP builds
    # the schema from the function *signature*, which carries no per-argument
    # descriptions at all, so there would otherwise be nothing to append the live
    # function list to — and the model would be told an aggregate exists without
    # being told which ones.
    base = spec["inputSchema"]["properties"]["aggregate_function"]["description"]
    advertised["properties"]["aggregate_function"]["description"] = (
        f"{base}, one of: {', '.join(functions)}"
    )
    return advertised


class PolicyMCPServer(MCPServer):
    """MCPServer whose advertised and callable tools obey deployment policy."""

    async def list_tools(self):
        # Re-probe before answering: what the OPC UA server supports is only
        # knowable while connected, and a catalogue frozen at startup would hide
        # the history tool for good after one unlucky moment. Best-effort, so a
        # server that is still down simply lists the core tools.
        if _CONNECTION is not None:
            await asyncio.to_thread(_refresh_capabilities, _CONNECTION)
        policy = tool_policy()
        specs = {tool["name"]: tool for tool in CONTRACT["tools"]}
        listed = await super().list_tools()
        visible = []
        for tool in listed:
            spec = specs[tool.name]
            if not policy.is_visible(spec):
                continue
            if not _capabilities_met(spec):
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
                tool.model_copy(
                    update={
                        "annotations": annotations,
                        "output_schema": output_schema,
                        "input_schema": _advertised_schema(tool.input_schema, spec),
                    }
                )
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
            if not _capabilities_met(spec):
                raise ToolError(
                    f"OPC UA server advertises none of: {', '.join(spec['capabilities'])}"
                )
        except (PermissionError, ValueError) as exc:
            _audit_decision(name, arguments, "denied", str(exc))
            raise ToolError(str(exc)) from exc

        # The outcome, not only the decision. "Permitted" and "happened" are
        # different facts, and the gap between them is where a control call that
        # reached the plant and then failed lives — which is the one an operator
        # most needs to find afterwards.
        try:
            result = await self._run_tool(name, arguments, context, spec)
        except Exception as error:
            _audit_decision(name, arguments, "failed", describe_error(error))
            raise
        _audit_decision(name, arguments, "completed")
        return result

    async def _run_tool(self, name, arguments, context, spec):
        # The one tool that must answer while the connection is down: it exists
        # to say so, and reaches for the connection itself.
        if name == "get_server_status" or _CONNECTION is None:
            return await super().call_tool(name, arguments, context)

        connection = _CONNECTION
        try:
            await asyncio.to_thread(connection.ensure_connected)
        except Exception as error:
            raise ToolError(not_connected_message(connection.url, describe_error(error))) from error

        try:
            return await super().call_tool(name, arguments, context)
        except Exception as error:
            if not is_connection_error(error):
                raise
            # A connection can die between the check above and the call: being
            # connected a moment ago is all anything can ever know. Whether
            # running it again is *safe* is settled by the contract's own
            # `idempotentHint` — a dead session almost certainly means the
            # request never reached the server, but "almost certainly" is not a
            # licence to fire `call_opcua_method` twice at a machine.
            may_repeat = bool(spec["annotations"]["idempotentHint"])
            suffix = " and retrying once" if may_repeat else ""
            print(
                f"OPC UA call failed on a dead session; reconnecting{suffix}",
                file=sys.stderr,
            )
            await asyncio.to_thread(connection.reconnect)
            if not may_repeat:
                raise
            return await super().call_tool(name, arguments, context)


# Create an MCP server instance. The server identity must match the Node server's
# so both runtimes present themselves as the same product to MCP clients, and the
# version must be a real one rather than the null the Node server never reports.
mcp = PolicyMCPServer("opcua-mcp-server", version=package_version(), lifespan=opcua_lifespan)


# --- helpers shared by the tool bodies ------------------------------------------

_TRAVERSAL = CONTRACT["traversal"]

#: The standard Root folder, which an absolute browse path is written from.
_ROOT_FOLDER = "ns=0;i=84"

#: The event buffers, module-level for the same reason `SUBSCRIPTIONS` is: a
#: resource handler and `list_tools` are handed no `Context` to reach them
#: through.
_EVENTS = events.EventSubscriptions()


def _clamp_int(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _data_type_name(variant: Any) -> str | None:
    """The OPC UA name of a variant's data type: 'Double', 'Boolean', 'Int32'."""
    variant_type = getattr(variant, "VariantType", None)
    name = getattr(variant_type, "name", None)
    return None if name in (None, "Null") else str(name)


def _node_value_record(node_id: str, data_value: Any) -> dict:
    """One node's reading as a canonical record (``resultShapes.nodeValues``).

    The value goes through the *shared* codec, so a Boolean is ``true`` on both
    runtimes rather than ``True`` here and ``true`` there, and an Int64 is a
    number or a numeric string rather than node-opcua's ``[high, low]`` pair.
    Reading used to stringify natively and so diverged by construction — the one
    thing ``value-encoding.json`` exists to prevent, just outside its reach.
    """
    status = getattr(data_value, "StatusCode", None)
    good = status is None or status.is_good()
    value = getattr(data_value, "Value", None)
    return {
        "node_id": canonical_node_id(node_id),
        "value": variant_to_json(value) if good else None,
        "data_type": _data_type_name(value) if good else None,
        # An absent status code means Good in OPC UA, so name it rather than null.
        "status": str(status.name) if status is not None else "Good",
        "source_timestamp": format_iso_utc(getattr(data_value, "SourceTimestamp", None)),
        "server_timestamp": format_iso_utc(getattr(data_value, "ServerTimestamp", None)),
    }


def _object_result(record: Any) -> CallToolResult:
    """A result that is one object rather than a list of records.

    One text block and a ``result`` that is the object itself. Used by every
    shape where a list would be a lie about the answer's structure: a browse has
    one ``truncated`` flag for the whole walk, a method call has one result, and
    a status report is one report. The Node server frames these identically.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(record, indent=2))],
        structured_content={"result": record},
    )


# --- reading --------------------------------------------------------------------


@mcp.tool(description=DESC["read_opcua_nodes"])
def read_opcua_nodes(node_ids: list[str], ctx: Context) -> list[dict]:
    """
    Read the current value of one or more OPC UA nodes in a single request.

    Parameters:
        node_ids (list[str]): The node IDs to read. Example: ['ns=2;i=2', 'ns=2;i=3'].

    Returns:
        list[dict]: One record per node, shaped by `contract/tools.json` ->
            `resultShapes.nodeValues`. A node the server rejects is one 'Bad…'
            status among the others; only a failure of the whole operation is
            raised as a `ToolError`.
    """
    if not node_ids:
        raise ToolError("read_opcua_nodes requires a non-empty node_ids array")
    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        nodes = [client.get_node(node_id) for node_id in node_ids]
        values = client.uaclient.get_attributes(
            [node.nodeid for node in nodes], ua.AttributeIds.Value
        )
        return [
            _node_value_record(node_id, data_value)
            for node_id, data_value in zip(node_ids, values, strict=True)
        ]
    except Exception as e:
        raise ToolError(f"Failed to read nodes: {e!s}") from e


def read_opcua_history(
    node_id: str,
    ctx: Context,
    start_time: str | None = None,
    end_time: str | None = None,
    num_values: int = 0,
    aggregate_function: str | None = None,
    processing_interval: float = 0,
) -> list[dict]:
    """
    Read a node's stored history, raw or summarised by a server-side aggregate.

    The two used to be separate tools with separate implementations of the same
    framing. They differ in one request and share everything else, so they are
    one tool whose ``aggregate_function`` argument decides which is sent.

    Returns:
        list[dict]: One record per reading or interval, shaped by
            ``resultShapes.historyRecords``.
    """
    client = ctx.request_context.lifespan_context["opcua_client"]

    if aggregate_function is None:
        try:
            values = client.get_node(node_id).read_raw_history(
                parse_iso_datetime(start_time),
                parse_iso_datetime(end_time),
                num_values,
            )
            return history_records(values)
        except Exception as e:
            raise ToolError(f"Failed to read history of node {node_id}: {e!s}") from e

    if start_time is None:
        raise ToolError("read_opcua_history requires start_time when aggregate_function is given")

    aggregate_functions = _CAPABILITIES["aggregate_functions"]
    # Both runtimes reject an unsupported function with the same sentence, so the
    # message is part of the contract and must reach the client rather than be
    # masked as a crash — hence ToolError. See `validate_aggregate_function`.
    try:
        validate_aggregate_function(aggregate_function, aggregate_functions)
    except ValueError as e:
        raise ToolError(str(e)) from e

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
            raise ValueError(f"Read aggregate failed with status: {result.StatusCode.name}")

        return history_records(result.HistoryData.DataValues)
    except Exception as e:
        raise ToolError(f"Failed to read history of node {node_id}: {e!s}") from e


# Registered once; tools/list gates it using the capabilities read from the
# lifecycle's active session. This avoids network I/O during import and prevents
# startup from opening throwaway OPC UA sessions.
read_opcua_history = mcp.tool(description=DESC["read_opcua_history"])(read_opcua_history)


# Tool: Report the connection and what the OPC UA server says about itself.
@mcp.tool(description=DESC["get_server_status"])
def get_server_status(ctx: Context) -> CallToolResult:
    """
    Report connection state, server status and the namespace array.

    Connecting is attempted rather than assumed, so asking for the status is also
    the cheapest way to bring a dropped connection back. A failure to connect is
    the answer, not an error — "not connected, and here is why" is exactly what
    the caller asked for, which is why this is the one tool that never raises a
    `ToolError` for a down server.

    Returns:
        CallToolResult: One record of the shared ``resultShapes.serverStatus``
            shape from ``contract/tools.json``, in text and structured form.
    """
    connection = ctx.request_context.lifespan_context["opcua_connection"]
    security = describe_security(security_config())
    try:
        # Through the same retry as every other read, so that asking for the
        # status also re-establishes a session that has silently died — which is
        # exactly the moment someone asks. python-opcua has no way to tell a
        # live socket from a dead one short of using it, so this read *is* the
        # liveness check.
        status = connection.run(
            lambda: read_server_status(connection.client, connection.url, security)
        )
    except Exception as error:
        status = disconnected_status(connection.url, security, describe_error(error))
    return _object_result(status)


# --- browsing --------------------------------------------------------------------


def browse_children(node: Node) -> list[Node]:
    """Browse a node's references, failing on a bad browse status.

    python-opcua's ``get_children()`` never looks at ``BrowseResult.StatusCode``,
    so a node the server refuses comes back as an empty child list —
    indistinguishable from a node that really has none, and a *successful* result
    besides.

    Otherwise a faithful copy of what ``get_children()`` asks for, which is not
    what ``get_references()`` defaults to: *hierarchical* references, *forward*
    only. Browsing ``References``/``Both`` instead — the ``get_references()``
    defaults — walks back up to the parent and out to the type definition, so
    ``ns=2;i=1`` answers ``0:Objects`` and ``0:FolderType`` rather than its own
    ``2:Sensors``.
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

    return references


def _browse_name_matches(segment: str, namespace_index: int, name: str) -> bool:
    """Whether a browse-path segment names this BrowseName.

    ``2:Sensors`` matches only namespace 2; a bare ``Sensors`` matches the name
    in whatever namespace it is in. The bare form is what someone types when
    they know what a thing is called and not which namespace it was loaded into
    — which is the entire reason ``browse_path`` exists.
    """
    prefix, separator, rest = segment.partition(":")
    if separator and prefix.isdigit():
        return int(prefix) == namespace_index and rest == name
    return segment == name


def _resolve_browse_path(client, start_node_id: str, browse_path: str) -> str:
    """Resolve a slash-separated browse path to a node id (issue #11).

    Matched segment by segment against the browse names of each node's children,
    rather than through TranslateBrowsePathsToNodeIds. Two reasons, and the first
    is the deciding one:

    A RelativePath element carries a *qualified* BrowseName, so translating
    ``/Objects/Plant/Temperature`` asks for those names in namespace 0 — and a
    plant's own nodes are never in namespace 0, so the server answers BadNoMatch
    for a path that is plainly right. Someone who knows the namespace index can
    write ``2:Plant``, but then they already know more than this argument exists
    to spare them. Matching here accepts either.

    Second, browsing is universal where TranslateBrowsePaths is optional, so both
    runtimes and every server behave the same way. It costs one browse per
    segment, which for a path someone typed is a handful of round trips.

    A path that does not resolve is an error naming the segment that failed,
    never an empty result: "no such path" and "a path to nothing" are different
    answers, and only one of them is the caller's mistake.
    """
    segments = [segment for segment in browse_path.split("/") if segment]
    if not segments:
        raise ValueError(f'browse_path "{browse_path}" names no elements')

    # A leading "/" is written from the Root folder, which is how a person says
    # it ("/Objects/..."); anything else is relative to node_id.
    current = _ROOT_FOLDER if browse_path.startswith("/") else canonical_node_id(start_node_id)

    for segment in segments:
        references = browse_children(client.get_node(current))
        match = next(
            (
                reference
                for reference in references
                if _browse_name_matches(
                    segment, reference.BrowseName.NamespaceIndex, reference.BrowseName.Name
                )
            ),
            None,
        )
        if match is None:
            raise ValueError(
                f'browse_path "{browse_path}" does not resolve: '
                f'no child "{segment}" under {current}'
            )
        current = canonical_node_id(match.NodeId.to_string())
    return current


def _describe_node(client, node_id: str, parent_node_id: str) -> dict:
    """The record for one node read directly, rather than off a browse reference."""
    node = client.get_node(node_id)
    browse_name = node.get_browse_name()
    node_class = node.get_node_class()
    return {
        "node_id": canonical_node_id(node_id),
        "browse_name": f"{browse_name.NamespaceIndex}:{browse_name.Name}",
        "node_class": node_class.name,
        "parent_node_id": canonical_node_id(parent_node_id),
        "data_type": None,
        "value": None,
        "description": None,
    }


def _fill_variable_detail(client, records: list[dict]) -> None:
    """Fill in value, data type and description for the Variables among ``records``.

    One batched read of each attribute rather than three reads per node: a
    500-node inventory is otherwise 1500 round trips, which is the difference
    between a tool that answers and one that times out on real equipment.
    """
    variables = [record for record in records if record["node_class"] == "Variable"]
    if not variables:
        return
    node_ids = [client.get_node(record["node_id"]).nodeid for record in variables]
    try:
        values = client.uaclient.get_attributes(node_ids, ua.AttributeIds.Value)
        descriptions = client.uaclient.get_attributes(node_ids, ua.AttributeIds.Description)
    except Exception:
        # Best-effort enrichment: the nodes were found, and reporting them
        # without their values beats failing a browse that succeeded.
        return

    for record, data_value, description in zip(variables, values, descriptions, strict=True):
        if data_value.StatusCode.is_good():
            record["value"] = variant_to_json(data_value.Value)
            record["data_type"] = _data_type_name(data_value.Value)
        text = getattr(getattr(description, "Value", None), "Value", None)
        text = getattr(text, "Text", None)
        record["description"] = text if text else None


@mcp.tool(description=DESC["browse_opcua_nodes"])
def browse_opcua_nodes(
    ctx: Context,
    node_id: str = _TRAVERSAL["rootNodeId"],
    browse_path: str | None = None,
    depth: int = _TRAVERSAL["defaultDepth"],
    node_class: str | None = None,
    name_filter: str | None = None,
    include_values: bool = False,
    max_nodes: int = _TRAVERSAL["defaultMaxNodes"],
) -> CallToolResult:
    """
    Explore the address space: list children, walk a subtree, resolve a path, search.

    One traversal serving what used to be ``browse_opcua_node_children`` and
    ``get_all_variables`` — and, with ``browse_path`` and ``name_filter``, what
    issue #11 asked two more tools for. They were two separate walks over the
    same address space, which is how the missing continuation-point drain (#75)
    reached both of them independently.

    Filtering never prunes the walk: an Object excluded by ``node_class`` is
    still descended into while ``depth`` allows, because the thing being looked
    for is usually *below* the structure, not in it.

    Returns:
        CallToolResult: One record of ``resultShapes.nodeRefs`` — the nodes
            found, whether the walk was truncated, and how many were inspected.
    """
    client = ctx.request_context.lifespan_context["opcua_client"]
    depth = _clamp_int(depth, 0, _TRAVERSAL["maxDepth"])
    max_nodes = _clamp_int(max_nodes, 1, _TRAVERSAL["maxNodes"])
    wanted_class = node_class.lower() if node_class else None
    wanted_name = name_filter.lower() if name_filter else None

    def keep(record: dict) -> bool:
        return (wanted_class is None or record["node_class"].lower() == wanted_class) and (
            wanted_name is None or wanted_name in record["browse_name"].lower()
        )

    try:
        root = (
            _resolve_browse_path(client, node_id, browse_path)
            if browse_path
            else canonical_node_id(node_id)
        )
    except ValueError as e:
        raise ToolError(str(e)) from e

    try:
        found: list[dict] = []
        inspected = 0
        truncated = False

        # `depth: 0` is "tell me about this node and nothing else" — which is how
        # a browse_path is turned into a node id without also listing everything
        # under it.
        if depth == 0:
            inspected = 1
            record = _describe_node(client, root, root)
            if keep(record):
                found.append(record)
        else:
            queue = deque([(root, 0)])
            visited = {root}
            while queue and not truncated:
                current_id, current_depth = queue.popleft()
                try:
                    references = browse_children(client.get_node(current_id))
                except Exception:
                    # The root failing is the caller's problem; a node deeper in
                    # may simply be one this session cannot read, and stopping
                    # the whole walk for it would make a large browse hostage to
                    # its worst node.
                    if current_id == root:
                        raise
                    continue

                for reference in references:
                    child_id = canonical_node_id(reference.NodeId.to_string())
                    if child_id in visited:
                        continue
                    visited.add(child_id)
                    if inspected >= max_nodes:
                        truncated = True
                        break
                    inspected += 1

                    browse_name = reference.BrowseName
                    # The built-in Server object is several hundred nodes of the
                    # server describing itself, identical everywhere, and
                    # get_server_status answers what anyone would browse it for.
                    if browse_name.Name == _TRAVERSAL["skipBrowseName"]:
                        continue

                    record = {
                        "node_id": child_id,
                        "browse_name": f"{browse_name.NamespaceIndex}:{browse_name.Name}",
                        "node_class": reference.NodeClass.name,
                        "parent_node_id": current_id,
                        "data_type": None,
                        "value": None,
                        "description": None,
                    }
                    if keep(record):
                        found.append(record)

                    # Descend through structure regardless of the class filter:
                    # what is being looked for is usually below an Object, not
                    # the Object.
                    if reference.NodeClass == ua.NodeClass.Object and current_depth + 1 < depth:
                        queue.append((child_id, current_depth + 1))

        if include_values:
            _fill_variable_detail(client, found)
        return _object_result({"nodes": found, "truncated": truncated, "inspected": inspected})
    except Exception as e:
        raise ToolError(f"Failed to browse {root}: {e!s}") from e


# --- writing ---------------------------------------------------------------------


@mcp.tool(description=DESC["write_opcua_nodes"])
def write_opcua_nodes(nodes: list[dict[str, Any]], ctx: Context) -> list[dict]:
    """
    Write a value to one or more OPC UA nodes.

    Nodes given an explicit ``data_type`` skip the read-first inference entirely,
    which is what makes a *write-only* node writable — reading it to learn its
    type is exactly what such a node refuses (issue #9). The rest are read first,
    in one batch, and converted to the type the server reports.

    Returns:
        list[dict]: One record per node, in the order asked, shaped by
            ``resultShapes.writeResults``. A node the server rejects is one
            status among them; only a failure of the whole operation is raised
            as a `ToolError`.
    """
    if not nodes:
        raise ToolError("write_opcua_nodes requires a non-empty nodes array")
    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        results: list[dict] = [
            {
                "node_id": canonical_node_id(str(node.get("node_id", ""))),
                "status": "Good",
                "error": None,
            }
            for node in nodes
        ]

        # Only the nodes without a declared type need reading, so a batch that
        # declares every type costs no extra round trip at all.
        inferred = [index for index, node in enumerate(nodes) if not node.get("data_type")]
        current: dict[int, Any] = {}
        if inferred:
            read = client.uaclient.get_attributes(
                [client.get_node(nodes[index]["node_id"]).nodeid for index in inferred],
                ua.AttributeIds.Value,
            )
            current = dict(zip(inferred, read, strict=True))

        write_ids = []
        write_values = []
        write_indices = []
        for index, node in enumerate(nodes):
            try:
                declared = node.get("data_type")
                if declared:
                    variant_type = ua.VariantType[declared]
                    is_array = isinstance(node.get("value"), (list, tuple))
                else:
                    data_value = current.get(index)
                    if data_value is None or not data_value.StatusCode.is_good():
                        status = data_value.StatusCode.name if data_value else "BadUnexpectedError"
                        results[index] = {
                            "node_id": results[index]["node_id"],
                            "status": str(status),
                            "error": (
                                "could not read the node's data type to convert the value; "
                                "give data_type to write without reading it first"
                            ),
                        }
                        continue
                    variant_type = data_value.Value.VariantType
                    is_array = data_value.Value.is_array

                converted = convert_for_variant(node.get("value"), variant_type, is_array)
                write_ids.append(client.get_node(node["node_id"]).nodeid)
                write_values.append(ua.DataValue(ua.Variant(converted, variant_type)))
                write_indices.append(index)
            except Exception as e:
                results[index] = {
                    "node_id": results[index]["node_id"],
                    "status": "BadTypeMismatch",
                    "error": str(e),
                }

        if write_ids:
            statuses = client.uaclient.set_attributes(
                write_ids, write_values, ua.AttributeIds.Value
            )
            for index, status in zip(write_indices, statuses, strict=True):
                results[index]["status"] = str(status.name)

        return results
    except Exception as e:
        raise ToolError(f"Failed to write nodes: {e!s}") from e


def _input_argument_types(client, method_node_id: str) -> list[tuple[Any, bool]]:
    """The declared type of each input argument, or [] when the method publishes none."""
    try:
        arguments = client.get_node(method_node_id).get_child(["0:InputArguments"]).get_value()
    except Exception:
        # Not every method publishes InputArguments, and a method with no
        # arguments has nothing to publish. Fall back rather than refuse.
        return []
    declared = []
    for argument in arguments or []:
        # Built-in types are numbered identically in the VariantType enum and in
        # namespace 0, which is what makes this a lookup rather than a table.
        try:
            variant_type = ua.VariantType(argument.DataType.Identifier)
        except Exception:
            return []
        declared.append((variant_type, argument.ValueRank >= 1))
    return declared


def _guess_variant(value: Any) -> Any:
    """The pre-#10 argument heuristic, kept only for methods that declare no types.

    Parses float then int then string. It is wrong for Boolean and every sized
    integer, which is what :func:`_input_argument_types` exists to fix; this
    remains because a method that publishes no InputArguments leaves nothing
    better to go on.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    try:
        return float(text)
    except ValueError:
        try:
            return int(text)
        except ValueError:
            return text


@mcp.tool(description=DESC["call_opcua_method"])
def call_opcua_method(
    object_node_id: str,
    method_node_id: str,
    ctx: Context,
    arguments: list[Any] | None = None,
) -> CallToolResult:
    """
    Call a method on an OPC UA object, with arguments of the types it declares.

    The declared types come from the method's own InputArguments definition
    (issue #10). Without it this parsed every argument float then int then string
    and forced Double or String, so a method expecting a Boolean or an Int32 was
    called with the wrong type and either failed or — worse — did something with
    a coerced value.

    Returns:
        CallToolResult: One record of ``resultShapes.methodResult``.
    """
    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        object_node = client.get_node(object_node_id)
        method_node = client.get_node(method_node_id)

        declared = _input_argument_types(client, method_node_id)
        method_args = []
        for index, argument in enumerate(arguments or []):
            if index < len(declared):
                variant_type, is_array = declared[index]
                method_args.append(
                    ua.Variant(convert_for_variant(argument, variant_type, is_array), variant_type)
                )
            else:
                method_args.append(_guess_variant(argument))

        # python-opcua exposes call_method on Node (not Client), and a string
        # methodid is treated as a child browse-name, so pass the resolved
        # method Node to call it by node id.
        outputs = object_node.call_method(method_node, *method_args)
        if outputs is None:
            outputs = []
        elif not isinstance(outputs, (list, tuple)):
            outputs = [outputs]

        return _object_result(
            {
                "object_node_id": canonical_node_id(object_node_id),
                "method_node_id": canonical_node_id(method_node_id),
                "status": "Good",
                "outputs": [scalar_to_json(output) for output in outputs],
            }
        )
    except Exception as e:
        raise ToolError(
            f"Failed to call method {method_node_id} on object {object_node_id}: {e!s}"
        ) from e


# --- data-change subscriptions ---------------------------------------------------


@mcp.tool(description=DESC["subscribe_opcua_nodes"])
async def subscribe_opcua_nodes(
    node_ids: list[str],
    publishing_interval: float = 1000,
    sampling_interval: float = 0,
    buffer_size: int = 20,
) -> list[dict]:
    """
    Watch one or more OPC UA nodes for value changes instead of polling them.

    Returns:
        list[dict]: One record per new subscription, shaped by
            ``resultShapes.subscriptionRecords``.
    """
    if not node_ids:
        raise ToolError("subscribe_opcua_nodes requires a non-empty node_ids array")
    records = []
    for node_id in node_ids:
        # `ToolError`, not a bare exception: the SDK forwards a ToolError's
        # message to the client and withholds anything else as a crash. A bad
        # node ID is the caller's to fix, so it has to reach them — worded as the
        # Node server words it.
        try:
            records.append(
                await asyncio.to_thread(
                    SUBSCRIPTIONS.subscribe,
                    node_id,
                    publishing_interval,
                    sampling_interval,
                    buffer_size,
                )
            )
        except Exception as e:
            raise ToolError(f"Failed to subscribe to node {node_id}: {e!s}") from e
    return records


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


@mcp.tool(description=DESC["unsubscribe_opcua_nodes"])
async def unsubscribe_opcua_nodes(subscription_ids: list[str]) -> list[dict]:
    """
    Cancel one or more subscriptions, reporting each as it was when cancelled.

    Every id is checked before any is cancelled: a list with one bad id would
    otherwise leave the caller unable to tell which of the others had already
    gone, and their buffered changes would be lost to a typo.

    Returns:
        list[dict]: The cancelled subscriptions, shaped by
            ``resultShapes.subscriptionRecords``, so anything still buffered can
            be read one last time.
    """
    if not subscription_ids:
        raise ToolError("unsubscribe_opcua_nodes requires a non-empty subscription_ids array")
    active = {record["subscription_id"] for record in SUBSCRIPTIONS.list()}
    unknown = [entry for entry in subscription_ids if entry not in active]
    if unknown:
        # Both runtimes word an unknown ID identically; see subscriptions.py.
        raise ToolError(unknown_subscriptions_message(unknown))

    records = []
    for subscription_id in subscription_ids:
        try:
            records.append(await asyncio.to_thread(SUBSCRIPTIONS.unsubscribe, subscription_id))
        except KeyError as e:
            raise ToolError(unknown_subscription_message(subscription_id)) from e
        except RuntimeError as e:
            # The OPC UA server refused the delete. Already worded for the caller
            # by `delete_failed_message`, and shared with the Node server.
            raise ToolError(str(e)) from e
    return records


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
    """The active subscriptions and their buffered changes, as JSON."""
    key = SUBSCRIPTIONS_RESOURCE["body"]["recordsKey"]
    return json.dumps({key: SUBSCRIPTIONS.list()}, indent=2)


# --- events and Alarms & Conditions -----------------------------------------------


@mcp.tool(description=DESC["subscribe_events"])
def subscribe_events(
    ctx: Context,
    node_id: str = events.DEFAULT_NOTIFIER,
    severity_min: int = events.DEFAULTS["severityMin"],
    buffer_size: int = events.DEFAULTS["bufferSize"],
) -> CallToolResult:
    """
    Start buffering OPC UA events from a notifier node.

    Returns:
        CallToolResult: One record of ``resultShapes.eventSubscription``, which
            reports the clamped values actually in force and whether an existing
            subscription was replaced.
    """
    # 0 means "unset" for a size, as it does everywhere else in both servers:
    # the Node side gets this from `||`, and a buffer that keeps nothing would be
    # a strange thing to have asked for.
    buffer_size = buffer_size or events.DEFAULTS["bufferSize"]
    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        replaced = _EVENTS.subscribe(client, node_id, severity_min, buffer_size)
    except Exception as e:
        raise ToolError(f"Failed to subscribe to events from node {node_id}: {e!s}") from e
    return _object_result(
        {
            "node_id": canonical_node_id(node_id),
            "severity_min": severity_min,
            "buffer_size": buffer_size,
            "replaced": replaced,
        }
    )


@mcp.tool(description=DESC["read_events"])
def read_events(
    node_id: str = events.DEFAULT_NOTIFIER,
    limit: int = events.DEFAULTS["readLimit"],
) -> CallToolResult:
    """
    Read and drain the events buffered by subscribe_events.

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
) -> CallToolResult:
    """
    Acknowledge an alarm or condition by the event_id that reported it.

    Returns:
        CallToolResult: One record of ``resultShapes.acknowledgement``.
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
    return _object_result(
        {
            "event_id": event_id,
            "condition_id": canonical_node_id(condition),
            "status": "Good",
        }
    )


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
        reconnect = reconnect_config()
    except ValueError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        raise SystemExit(1) from None

    print(f"Tool policy: {describe_policy(policy)}", file=sys.stderr)
    print(f"Connection resilience: {describe_reconnect(reconnect)}", file=sys.stderr)

    mcp.run(transport="stdio")
