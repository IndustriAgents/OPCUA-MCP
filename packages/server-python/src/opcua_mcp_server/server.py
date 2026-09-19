"""The MCP server: lifecycle, tool registration, and the stdio entry point."""

from __future__ import annotations

import asyncio
import copy
import json
import secrets
import sys
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
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
from .errors import message as error_message
from .limits import (
    MAX_NODES_PER_READ,
    MAX_SUBSCRIPTIONS,
    history_values,
    history_was_clipped,
)
from .node_ids import canonical_node_id
from .node_metadata import AnalogInfo, NodeMetadata
from .notices import notice
from .policy import (
    ValueBound,
    as_number,
    describe_policy,
    format_number,
    tool_policy,
    values_at,
)
from .records import history_records, scalar_to_json, variant_to_json
from .security import describe_security, security_config
from .subscriptions import (
    SUBSCRIPTIONS,
    unknown_subscription_message,
    unknown_subscriptions_message,
)
from .validation import validate_arguments
from .variant_codec import convert_for_variant
from .version import package_version

_CAPABILITIES: dict[str, Any] = {"history": False, "aggregate_functions": {}}

#: What each node published about its own number, for the life of one session.
#: Module-level for the same reason `_CAPABILITIES` is, and dropped by `_bind`
#: for the same reason: a restarted server may not be the same server.
_NODE_METADATA = NodeMetadata()


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


def new_call_id() -> str:
    """An id for one tool call, to tie its audit lines together.

    Every control call writes two lines — ``allowed`` before it, then
    ``completed`` or ``failed`` after — and without this there was nothing
    linking them. Both runtimes serve calls concurrently, so two overlapping
    writes produced four interleaved lines and no way to say which pairs; where
    the targets happened to match (the same node written twice) they were not even
    distinguishable by content. For a trail whose purpose is "which control call
    reached the plant and did it land", that was the one missing field.

    Random rather than a counter: it never needs to be meaningful or ordered, only
    unique within a process, and a counter would invite reading it as a total.
    """
    return secrets.token_hex(8)


@dataclass
class _Call:
    """One tools/call in flight, as the dispatcher and the audit trail see it.

    ``attempt`` is mutable and is the reason this is an object rather than a
    handful of parameters: the retry decision is taken several frames below the
    audit lines that have to report it. Before this, a call that died on its
    session and was re-sent wrote one ``allowed`` line for the first attempt and
    nothing at all about the second — an audit trail that under-counts what
    actually reached the plant (issue #105).
    """

    name: str
    arguments: dict[str, Any]
    spec: dict[str, Any]
    call_id: str
    attempt: int = 1
    #: Whether a refusal has already been recorded for this call. Keeps a denial
    #: to one line rather than two: the ``failed`` line would otherwise repeat
    #: its reason and read as though the plant had rejected the call.
    denied: bool = False


def describe_targets(spec: dict, arguments: dict[str, Any]) -> str:
    """What a call was aimed at, for a message a human will read.

    The same ``guard`` the audit record and the policy read, so the three cannot
    name different things. Targets only — never the values, for the same reason
    :func:`_audit_decision` withholds them.
    """
    targets = _audit_targets(spec, arguments)
    if not targets:
        return "unknown"
    parts = []
    for key, value in targets.items():
        rendered = ", ".join(str(item) for item in value) if isinstance(value, list) else value
        parts.append(f"{key}={rendered}")
    return "; ".join(parts)


def _audit_decision(
    name: str,
    arguments: dict[str, Any],
    decision: str,
    reason: str = "",
    call_id: str | None = None,
    attempt: int = 1,
) -> None:
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
        # Second, so it is next to the timestamp in the line an operator reads and
        # can be grepped for to pull one call's whole story out of a shipped log.
        "call_id": call_id,
        # Which physical attempt this line is about. One call can reach the plant
        # twice — the session dies, the connection is rebuilt, the request is
        # re-sent — and a trail whose purpose is "what reached the plant" has to
        # count those separately rather than fold them into one line.
        "attempt": attempt,
        "profile": tool_policy().config.profile,
        "tool": name,
        "decision": decision,
        **_audit_targets(spec, arguments),
    }
    if reason:
        record["reason"] = reason
    print(json.dumps(record, separators=(",", ":")), file=sys.stderr)


#: What ``Tool.run`` puts in front of a ToolError raised inside a tool body.
_SDK_TOOL_ERROR_PREFIX = "Error executing tool "


def _without_sdk_prefix(name: str, error: BaseException) -> BaseException:
    """A tool failure worded as the contract words it, not as the SDK frames it.

    ``Tool.run`` wraps every anticipated failure as
    ``Error executing tool <name>: <message>``. The Node server has no such
    wrapper, so the same refusal reached a model as two different sentences —
    "No such subscription: sub-1" there and "Error executing tool
    unsubscribe_opcua_nodes: No such subscription: sub-1" here. No test compared
    them, because the differential suite only checked that a failure *was* a
    failure. ``contract/tools.json`` -> ``errors`` now words both, and this is
    what stops the SDK re-framing one of them.

    ``UnexpectedToolError`` is left exactly as it is: its message deliberately
    carries nothing but the tool name, because the original was a crash and is
    withheld from the client on purpose.
    """
    if isinstance(error, UnexpectedToolError) or not isinstance(error, ToolError):
        return error
    prefix = f"{_SDK_TOOL_ERROR_PREFIX}{name}: "
    text = str(error)
    if not text.startswith(prefix):
        return error
    unwrapped = ToolError(text[len(prefix) :])
    unwrapped.__cause__ = error.__cause__
    return unwrapped


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

    Re-probing here is what lets `tools/list` stop waiting on the network: a new
    session is the only moment the answer can have changed, so the catalogue is
    recomputed exactly then rather than on every catalogue request.
    """
    state["opcua_client"] = client
    SUBSCRIPTIONS.reattach(client)
    # A new session may be a restarted server, whose nodes are not necessarily
    # the nodes the old ids named. What each one said about its unit and its
    # range was true of the session that said it.
    _NODE_METADATA.forget()
    _probe_capabilities(client)


def _probe_capabilities(client) -> bool:
    """Read the optional capabilities off a live client. True if they changed.

    Best-effort by design: any failure yields "not supported" rather than an
    error, because an optional capability must never break `tools/list`.
    """
    before = (_CAPABILITIES["history"], tuple(sorted(_CAPABILITIES["aggregate_functions"])))
    _CAPABILITIES["history"] = _probe(client_supports_history, client, False)
    _CAPABILITIES["aggregate_functions"] = _probe(client_aggregate_functions, client, {})
    after = (_CAPABILITIES["history"], tuple(sorted(_CAPABILITIES["aggregate_functions"])))
    return before != after


def _forget_capabilities() -> None:
    """Drop what was probed, because the session it was true of is gone."""
    _CAPABILITIES["history"] = False
    _CAPABILITIES["aggregate_functions"] = {}


def _connect_and_probe(connection: OpcuaConnection) -> None:
    """Open the first connection and read its capabilities.

    One connection attempt for both probes, not one each: against a server that
    is down, each would otherwise sit through the whole configured backoff on its
    own. `_bind` does the probing, through `on_client_replaced`.

    Called from the lifespan, and never from `tools/list` — see
    :meth:`PolicyMCPServer.list_tools` for why a catalogue request must not wait
    on a socket.
    """
    _forget_capabilities()
    try:
        connection.ensure_connected()
    except Exception:
        # `connect` has already said why on stderr. A server that is down simply
        # advertises the core tools until it comes back, at which point
        # `on_client_replaced` re-probes and the catalogue is announced again.
        return


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
    await asyncio.to_thread(_connect_and_probe, connection)
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


def _advertised_schema(spec: dict) -> dict:
    """A tool's input schema as advertised: the contract's own, capability-gated.

    The contract's schema, not the one ``MCPServer`` derives from the function
    signature. The derived one carries no per-argument descriptions at all and
    flattens every nested structure — ``write_opcua_nodes`` advertised its
    ``nodes`` argument as "an array of object" against a contract that names
    ``node_id``, ``value`` and the fifteen legal ``data_type`` spellings. Tool
    descriptions matched across the two runtimes while the parameter
    documentation a model needs in order to *call* the tool did not, and the
    parity test compared only top-level property names, so it passed.

    The derived schema remains what ``MCPServer`` validates the call against;
    it is strictly looser than this one, and :func:`validation.validate_arguments`
    applies the contract's own constraints before either of them sees the call.

    Capability gating then removes what this particular server cannot honour:
    ``read_opcua_history`` is advertised whenever the server reports
    HistoricalAccess, and its ``aggregate_function`` argument appears only if the
    server also advertises aggregates — carrying that server's *own* function
    list in its description, which is strictly more informative than documenting
    the argument as unsupported, because the list is the live one.
    """
    schema = copy.deepcopy(spec["inputSchema"])
    properties = schema.get("properties") or {}
    if "aggregate_function" not in properties:
        return schema

    functions = _CAPABILITIES["aggregate_functions"]
    if not functions:
        properties.pop("aggregate_function", None)
        properties.pop("processing_interval", None)
        return schema

    properties["aggregate_function"]["description"] += f", one of: {', '.join(functions)}"
    return schema


class PolicyMCPServer(MCPServer):
    """MCPServer whose advertised and callable tools obey deployment policy."""

    async def list_tools(self):
        """The catalogue, from what the *current* session was found to support.

        Deliberately does no network I/O. This used to open a connection before
        answering, and `OpcuaConnection.connect` holds its lock across the whole
        backoff loop — so against an unreachable plant every `tools/list` paid
        the full reconnect budget (7s by default, 32s with
        `OPCUA_RECONNECT_MAX_RETRY=-1`) and serialised every concurrent tool call
        behind it. Clients list at session start, which is exactly when a plant
        that is down is most likely to be down.

        The capabilities are probed where they can change instead — on every
        (re)connect, in `_bind`. Convergence is unchanged: a client that listed
        while the plant was down sees the core tools, any tool call brings the
        connection up and re-probes, and the next `tools/list` carries the full
        catalogue. What is gone is only the waiting.

        No `notifications/tools/list_changed` is sent, on either runtime, for the
        same reason no `notifications/resources/updated` is — see
        docs/architecture.md. The catalogue is re-listable at any time and this
        is what both runtimes can honestly promise.
        """
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
                        "input_schema": _advertised_schema(spec),
                    }
                )
            )
        return visible

    async def call_tool(self, name, arguments, context=None):
        arguments = arguments or {}
        call_id = new_call_id()
        spec = next((tool for tool in CONTRACT["tools"] if tool["name"] == name), None)
        try:
            if spec is None:
                raise ValueError(error_message("unknownTool", tool=name))
            # Shape before permission: a call that does not match the contract is
            # not a call this server can reason about, and the policy layer reads
            # the very arguments being checked here to decide what a write is
            # aimed at. `MCPServer` would validate later, against the looser
            # signature-derived schema, and word it differently from the Node
            # runtime; this is the contract's own schema on both.
            validate_arguments(name, spec["inputSchema"], arguments)
            # Catalog filtering is not authorization: clients may retain an old
            # tools/list result, so enforce the current policy again on every call.
            tool_policy().authorize(name, arguments)
            _audit_decision(name, arguments, "allowed", call_id=call_id, attempt=1)
        except (PermissionError, ValueError) as exc:
            _audit_decision(name, arguments, "denied", str(exc), call_id=call_id, attempt=1)
            raise ToolError(str(exc)) from exc

        # The outcome, not only the decision. "Permitted" and "happened" are
        # different facts, and the gap between them is where a control call that
        # reached the plant and then failed lives — which is the one an operator
        # most needs to find afterwards.
        call = _Call(name=name, arguments=arguments, spec=spec, call_id=call_id)
        try:
            result = await self._run_tool(call, context)
        except Exception as error:
            reported = _without_sdk_prefix(name, error)
            if not call.denied:
                _audit_decision(
                    name,
                    arguments,
                    "failed",
                    describe_error(reported),
                    call_id=call_id,
                    attempt=call.attempt,
                )
            raise reported from error.__cause__
        _audit_decision(name, arguments, "completed", call_id=call_id, attempt=call.attempt)
        return result

    async def _run_tool(self, call: _Call, context):
        name, arguments = call.name, call.arguments
        # The one tool that must answer while the connection is down: it exists
        # to say so, and reaches for the connection itself.
        if name == "get_server_status" or _CONNECTION is None:
            return await super().call_tool(name, arguments, context)

        connection = _CONNECTION
        # Connect *before* the capability gate, not after. The capability map is
        # filled in by the reconnect callback, so on a process that started while
        # the plant was unreachable it still holds its startup defaults — and
        # checking it first refused `read_opcua_history` as "the server advertises
        # none of: history" without ever asking the server. Unknown is not absent
        # (issue #108).
        try:
            await asyncio.to_thread(connection.ensure_connected)
        except Exception as error:
            raise ToolError(not_connected_message(connection.url, describe_error(error))) from error

        if not _capabilities_met(call.spec):
            raise ToolError(
                error_message(
                    "capabilityMissing", capabilities=", ".join(call.spec["capabilities"])
                )
            )

        try:
            return await super().call_tool(name, arguments, context)
        except Exception as error:
            if not is_connection_error(error):
                raise
            return await self._recover(call, context, connection, error)

    async def _recover(self, call: _Call, context, connection: OpcuaConnection, error: Exception):
        """Rebuild the session a call died on, and decide what may follow it.

        A connection can die between the check and the call: being connected a
        moment ago is all anything can ever know. What happens next is settled by
        the contract's own `retryPolicy` — *not* by `annotations.idempotentHint`,
        which both runtimes used to read for this. That annotation tells the model
        whether calling a tool twice is meaningful; this decides whether this
        server may put a second request on the wire after an outcome it does not
        know. `write_opcua_nodes` carries `idempotentHint: true` and must not be
        re-sent: Part 4 §5.11.4 lets a Write partially succeed and defines no
        operation order, so a lost response never proved the write had not landed
        (issue #106).

        The connection is rebuilt whatever the policy, so the next call finds a
        live session.
        """
        name, arguments = call.name, call.arguments
        policy = call.spec["retryPolicy"]
        suffix = " and retrying once" if policy == "resend" else ""
        print(
            f"OPC UA call failed on a dead session; reconnecting{suffix}",
            file=sys.stderr,
        )
        await asyncio.to_thread(connection.reconnect)

        if policy == "uncertainOutcome":
            raise ToolError(
                error_message(
                    "uncertainOutcome",
                    tool=name,
                    reason=describe_error(error),
                    targets=describe_targets(call.spec, arguments),
                )
            ) from error
        if policy != "resend":
            raise error

        # Re-authorize before the second attempt, and audit it as its own.
        #
        # `reconnect` has just re-read the server's NamespaceArray and re-bound it
        # into the policy, because a server that restarted may have loaded its
        # namespaces in a different order — which is the whole reason the `nsu=`
        # allowlist form exists. So the mapping this call was authorized against
        # is not necessarily the mapping the second attempt will resolve against,
        # and re-running the check is what stops a request reaching a node nobody
        # allowed (issue #105). It touches no network.
        call.attempt = 2
        try:
            tool_policy().authorize(name, arguments)
        except (PermissionError, ValueError) as exc:
            call.denied = True
            _audit_decision(name, arguments, "denied", str(exc), call_id=call.call_id, attempt=2)
            raise ToolError(str(exc)) from exc
        _audit_decision(name, arguments, "allowed", call_id=call.call_id, attempt=2)

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


def _node_value_record(
    node_id: str, data_value: Any, engineering: AnalogInfo | None = None
) -> dict:
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
        # What the plant says this number means. null for most nodes, because
        # only an AnalogItemType publishes it — but on the ones that do it is the
        # difference between "51.75" and "51.75 °C, normal range 0 to 150".
        "engineering": engineering.to_json() if engineering else None,
    }


def _history_result(records: list[dict], wanted: int) -> Any:
    """History records, with a notice when the call hit the per-call maximum.

    A trailing plain-text block rather than a field, because ``historyRecords``
    is an array of readings and a truncation flag is not a reading — the same
    shape and the same reason ``read_events`` reports dropped events this way.
    Outside ``structuredContent`` for the same reason.

    Only when the cap itself was reached: a caller who asked for 10 and got 10
    has what they asked for.
    """
    if not history_was_clipped(len(records), wanted):
        return records
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(record, indent=2)) for record in records]
        + [TextContent(type="text", text=notice("historyTruncated", count=len(records)))],
        structured_content={"result": records},
    )


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
        raise ToolError(error_message("emptyArray", tool="read_opcua_nodes", argument="node_ids"))
    if len(node_ids) > MAX_NODES_PER_READ:
        # Refused, not truncated: a short list of readings is indistinguishable
        # from a complete one, and dropping nodes from a read is the kind of
        # quiet wrong answer the browse caps exist to prevent.
        raise ToolError(
            error_message(
                "tooManyNodes",
                tool="read_opcua_nodes",
                limit=MAX_NODES_PER_READ,
                count=len(node_ids),
            )
        )
    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        nodes = [client.get_node(node_id) for node_id in node_ids]
        values = client.uaclient.get_attributes(
            [node.nodeid for node in nodes], ua.AttributeIds.Value
        )
        # Two extra round trips on a cold cache for the whole batch, none on a
        # warm one, and never a reason for the read to fail. See node_metadata.
        engineering = _NODE_METADATA.for_nodes(client, node_ids)
        return [
            _node_value_record(node_id, data_value, engineering.get(node_id))
            for node_id, data_value in zip(node_ids, values, strict=True)
        ]
    except Exception as e:
        raise ToolError(error_message("readFailed", reason=str(e))) from e


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
        # `0` used to mean "every reading in the range", which against a node
        # historised at 100ms is a request that never returns — and the browse
        # caps beside it have always been refusals rather than tuning knobs.
        wanted = history_values(num_values)
        try:
            values = client.get_node(node_id).read_raw_history(
                parse_iso_datetime(start_time),
                parse_iso_datetime(end_time),
                wanted,
            )
            return _history_result(history_records(values), wanted)
        except Exception as e:
            raise ToolError(error_message("historyFailed", node_id=node_id, reason=str(e))) from e

    if start_time is None:
        raise ToolError(error_message("aggregateNeedsStart"))

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

        # No cap on an aggregate read: the number of results is decided by
        # `processing_interval` over the range, which is the whole point of
        # asking for one — it is how to see a week without transferring a week.
        return history_records(result.HistoryData.DataValues)
    except Exception as e:
        raise ToolError(error_message("historyFailed", node_id=node_id, reason=str(e))) from e


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
        raise ToolError(error_message("browseFailed", node_id=root, reason=str(e))) from e


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
        raise ToolError(error_message("emptyArray", tool="write_opcua_nodes", argument="nodes"))
    client = ctx.request_context.lifespan_context["opcua_client"]
    policy = tool_policy()
    bounds = {
        index: policy.bound_for(str(node.get("node_id", ""))) for index, node in enumerate(nodes)
    }
    try:
        results: list[dict] = [
            {
                "node_id": canonical_node_id(str(node.get("node_id", ""))),
                "status": "Good",
                "error": None,
            }
            for node in nodes
        ]

        # A node needs its current value read for either of two reasons: its type
        # was not declared and has to be inferred, or it carries a `max_change`
        # bound, which is a bound on the *move* and so cannot be judged without
        # knowing where the node is now. One read covers both.
        inferred = [index for index, node in enumerate(nodes) if not node.get("data_type")]
        needs_current = sorted(
            set(inferred)
            | {
                index
                for index, bound in bounds.items()
                if bound is not None and bound.max_change is not None
            }
        )
        current: dict[int, Any] = {}
        if needs_current:
            read = client.uaclient.get_attributes(
                [client.get_node(nodes[index]["node_id"]).nodeid for index in needs_current],
                ua.AttributeIds.Value,
            )
            current = dict(zip(needs_current, read, strict=True))

        # Before anything is sent, and raising rather than marking one record:
        # the whole batch is refused so it can never end up partially applied,
        # which is the property the identity allowlist already had.
        check_write_bounds(nodes, bounds, current, client)

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
    except ToolError:
        # A refusal is already worded the way the contract words it, and it ends
        # in "Nothing was written". Wrapping it in "Failed to write nodes:" would
        # bury the reason under a framing that says the plant rejected the value
        # when in fact this server never sent it.
        raise
    except Exception as e:
        raise ToolError(error_message("writeFailed", reason=str(e))) from e


def _current_number(data_value: Any) -> float | None:
    """A node's present reading as a number, or None if there is not one to compare."""
    status = getattr(data_value, "StatusCode", None)
    if data_value is None or (status is not None and not status.is_good()):
        return None
    return as_number(variant_to_json(getattr(data_value, "Value", None)))


def check_eu_range(node_id: str, value: Any, info: AnalogInfo | None) -> None:
    """Refuse a value outside the range the OPC UA server itself published.

    This is the bound that needs no policy file at all, and it is the better one:
    the plant declared what the node is expected to hold in normal operation
    (Part 8 §5.3), so nobody has to retype it into a JSON file and keep it in
    step. An operator's ``min``/``max`` is checked separately, by the policy
    layer, and both apply — so a policy file can only ever *narrow* what the
    equipment already allows, never widen it.

    A non-numeric value is left alone: the variant codec is what judges whether a
    string or a boolean belongs on this node, and it says so better than a range
    comparison could.
    """
    if info is None or info.eu_range is None:
        return
    number = as_number(value)
    if number is None or info.eu_range.contains(number):
        return
    raise ToolError(
        error_message(
            "valueOutOfRange",
            value=format_number(number),
            node_id=node_id,
            low=format_number(info.eu_range.low),
            high=format_number(info.eu_range.high),
            unit=f" {info.unit}" if info.unit else "",
            source="the OPC UA server's own EURange",
        )
    )


def check_max_change(node_id: str, value: Any, bound: ValueBound, data_value: Any) -> None:
    """Refuse a move larger than the operator allows in one write.

    Scalars only. An array write has no single "how far did it move", and
    guessing one — the largest element-wise delta, say — would be a rule nobody
    could predict from the policy file, so it is refused instead.
    """
    present = _current_number(data_value)
    if present is None:
        reason = "the node returned no usable value"
        status = getattr(data_value, "StatusCode", None)
        if data_value is None:
            reason = "it could not be read"
        elif status is not None and not status.is_good():
            reason = str(status.name)
        raise ToolError(error_message("currentValueUnreadable", node_id=node_id, reason=reason))
    wanted = as_number(value)
    if wanted is None:
        raise ToolError(
            error_message("valueNotComparable", node_id=node_id, value=json.dumps(value))
        )
    change = abs(wanted - present)
    if change > bound.max_change:
        raise ToolError(
            error_message(
                "valueChangeTooLarge",
                node_id=node_id,
                current=format_number(present),
                value=format_number(wanted),
                change=format_number(change),
                limit=format_number(bound.max_change),
            )
        )


def check_write_bounds(
    nodes: list[dict[str, Any]],
    bounds: dict[int, ValueBound | None],
    current: dict[int, Any],
    client: Any,
) -> None:
    """Refuse the whole batch if any value is outside what its node may hold.

    Two bounds, from two places, and both apply. The operator's ``min``/``max``
    and ``enum`` were already checked by the policy layer, before the network was
    touched at all; what is left here is everything that needed a read — the
    server's own ``EURange``, and ``max_change``, which is a bound on the move.
    """
    node_ids = [str(node.get("node_id", "")) for node in nodes]
    engineering = (
        {}
        if tool_policy().config.allow_out_of_range_writes
        else _NODE_METADATA.for_nodes(client, node_ids)
    )
    for index, node in enumerate(nodes):
        node_id = node_ids[index]
        value = node.get("value")
        # An array write is checked element by element. Writing [0, 9999] to a
        # node whose range stops at 100 is writing 9999 to it.
        for element in value if isinstance(value, list) else [value]:
            check_eu_range(node_id, element, engineering.get(node_id))
        bound = bounds.get(index)
        if bound is not None and bound.max_change is not None:
            check_max_change(node_id, value, bound, current.get(index))


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
            error_message(
                "methodFailed",
                method_node_id=method_node_id,
                object_node_id=object_node_id,
                reason=str(e),
            )
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
        raise ToolError(
            error_message("emptyArray", tool="subscribe_opcua_nodes", argument="node_ids")
        )
    # One OPC UA subscription per monitored node is what makes a single
    # unsubscribe take the whole thing down — and it is also what makes an
    # unbounded subscribe ask a PLC for one subscription per node, past whatever
    # it is willing to hold, with nothing here counting them.
    active = len(SUBSCRIPTIONS.list())
    if active + len(node_ids) > MAX_SUBSCRIPTIONS:
        raise ToolError(
            error_message(
                "tooManySubscriptions",
                active=active,
                limit=MAX_SUBSCRIPTIONS,
                wanted=len(node_ids),
            )
        )
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
            raise ToolError(error_message("subscribeFailed", node_id=node_id, reason=str(e))) from e
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
        raise ToolError(
            error_message("emptyArray", tool="unsubscribe_opcua_nodes", argument="subscription_ids")
        )
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
        raise ToolError(
            error_message("eventSubscribeFailed", node_id=node_id, reason=str(e))
        ) from e
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
        raise ToolError(error_message("notSubscribedToEvents", node_id=node_id))
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
        raise ToolError(error_message("alarmsFailed", node_id=node_id, reason=str(e))) from e
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
        raise ToolError(error_message("unknownEventId", event_id=event_id))

    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        events.acknowledge_alarm(client, condition, event_id, comment)
    except Exception as e:
        raise ToolError(
            error_message("acknowledgeFailed", condition_id=condition, reason=str(e))
        ) from e
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
