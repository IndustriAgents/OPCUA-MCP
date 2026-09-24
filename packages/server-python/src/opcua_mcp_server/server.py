"""The MCP server: lifecycle, tool registration, and the stdio entry point."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import secrets
import signal
import sys
import threading
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from opcua import Node, ua

from . import events
from .aggregates import validate_aggregate_function
from .audit import (
    AuditSink,
    AuditWriteError,
    build_record,
    describe_audit,
    operator_id,
    parse_audit_config,
)
from .capabilities import (
    client_aggregate_functions,
    client_supports_history,
    client_supports_history_events,
)
from .completeness import (
    buffer_completeness,
    drain_completeness,
    history_completeness,
    traversal_completeness,
)
from .config import describe_reconnect, reconnect_config
from .connection import (
    OpcuaConnection,
    describe_error,
    is_connection_error,
    not_connected_message,
    still_connecting_message,
)
from .contract import CONTRACT, DESC, SUBSCRIPTIONS_RESOURCE
from .datetimes import format_iso_utc, parse_iso_datetime
from .diagnostics import disconnected_status, read_server_status
from .errors import message as error_message
from .history import continues, raw_details, release_continuation_point
from .limits import (
    MAX_HISTORY_VALUES,
    MAX_SUBSCRIPTIONS,
    LimitExceeded,
    aggregate_intervals,
    check_request_bounds,
    chunked,
    event_buffer_size,
    history_values,
)
from .method_arguments import built_in_type, guess_variant
from .node_ids import canonical_node_id
from .node_metadata import AnalogInfo
from .notices import notice
from .operation_limits import (
    browse_chunk,
    read_chunk,
    read_operation_limits,
    write_limit,
)
from .policy import (
    ValueBound,
    as_number,
    control_gate,
    describe_policy,
    format_number,
    server_identity_record,
    values_at,
)
from .records import history_data, history_records, variant_to_json
from .security import describe_security, security_config
from .state import ServerState
from .subscriptions import (
    resolve_filter,
    unknown_subscription_message,
    unknown_subscriptions_message,
)
from .validation import validate_arguments
from .variant_codec import convert_for_variant
from .version import package_version


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
    #: The connection's session id when this call was dispatched. Lets recovery
    #: skip a rebuild the connection has already had.
    session: str | None = None
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
    state: ServerState,
    name: str,
    arguments: dict[str, Any],
    decision: str,
    reason: str = "",
    call_id: str | None = None,
    attempt: int = 1,
) -> None:
    """Write one line of the control audit trail.

    Only ``control`` and ``alarm-action`` tools: an audit trail that also
    recorded every read would bury the four lines anyone is looking for.

    Never the *values* being written, only the targets. A setpoint is process
    data, and this stream is the one an MCP client shows the user and a log
    collector ships off the machine.

    The field order is part of the record, not an accident: ``audit.ts`` builds
    the same keys in the same order, and
    ``tests/e2e/test_policy_e2e.py::test_both_runtimes_write_the_same_record_shape``
    drives one call through both servers and compares them.
    """
    spec = next((tool for tool in CONTRACT["tools"] if tool["name"] == name), None)
    if spec is None or spec["accessClass"] not in {"control", "alarm-action"}:
        return
    connection = state.connection
    record = build_record(
        # Milliseconds, as `Date.toISOString` writes them, so the two runtimes'
        # timestamps are the same shape and not merely the same instant.
        timestamp=datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        call_id=call_id,
        attempt=attempt,
        endpoint=state.url,
        session=state.session_id,
        session_generation=connection.session_generation if connection is not None else None,
        # null when OPCUA_OPERATOR_ID is unset, which is honest: this server has
        # no notion of who is calling, and a name nothing verified would be worse
        # than none.
        operator_label=operator_id(),
        **state.audit.identity(),
        profile=state.policy.config.profile,
        # What let control through, or kept it out: `secured` for a verified
        # server, or which lab override was in force. An override that shows
        # up only in a startup line nobody kept is an override nobody can audit.
        control=control_gate(state.policy.config),
        tool=name,
        decision=decision,
        targets=_audit_targets(spec, arguments),
        reason=reason or None,
    )
    state.audit.write(record)


def _audit_after(state: ServerState, name: str, arguments: dict[str, Any], *args, **kwargs):
    """Record a denial or an outcome, reporting rather than raising if it is lost.

    Fail-closed is decided at :func:`_audit_permission`, before dispatch. A
    denial is refused whether or not its record lands. An outcome is recorded
    after the call reached the plant, and turning a write that happened into a
    reported failure would invite the model to send it again — so a sink that
    refuses it is reported on stderr, and the next control call's ``allowed``
    record is what refuses the next call.
    """
    try:
        _audit_decision(state, name, arguments, *args, **kwargs)
    except AuditWriteError as error:
        print(
            f"AUDIT FAILURE: the {args[0]} record for {name} (call {kwargs.get('call_id')}) "
            f"was not written: {error}",
            file=sys.stderr,
        )


def _audit_permission(
    state: ServerState, name: str, arguments: dict[str, Any], *, call_id: str, attempt: int
) -> None:
    """Record ``allowed`` before anything is sent — or refuse the call.

    The fail-closed half (#146): a control call whose permission could not be
    made durable never reaches the plant. Reads never get here, because they are
    not audited, so an audit outage does not take monitoring down with it.
    """
    try:
        _audit_decision(state, name, arguments, "allowed", call_id=call_id, attempt=attempt)
    except AuditWriteError as error:
        print(f"AUDIT FAILURE: refusing {name} (call {call_id}): {error}", file=sys.stderr)
        raise ToolError(error_message("auditUnavailable", tool=name, reason=str(error))) from error


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


def _bind(state: ServerState, context: dict, client) -> None:
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
    context["opcua_client"] = client
    state.subscriptions.reattach(client)
    # Event subscriptions belong to a session just the same. Not re-attaching
    # them left `read_events` draining a buffer nothing would ever fill again,
    # and answering `[]` — "nothing happened" — for as long as anyone asked.
    state.events.reattach(client)
    # A new session may be a restarted server, whose nodes are not necessarily
    # the nodes the old ids named. What each one said about its unit and its
    # range was true of the session that said it.
    state.node_metadata.forget()
    _probe_capabilities(state, client)


def _probe_capabilities(state: ServerState, client) -> bool:
    """Read the optional capabilities off a live client. True if they changed.

    Best-effort by design: any failure yields "not supported" rather than an
    error, because an optional capability must never break `tools/list`.
    """

    capabilities = state.capabilities

    def snapshot():
        return (
            capabilities["history"],
            capabilities["history_events"],
            tuple(sorted(capabilities["aggregate_functions"])),
        )

    before = snapshot()
    capabilities["history"] = _probe(client_supports_history, client, False)
    capabilities["history_events"] = _probe(client_supports_history_events, client, False)
    capabilities["aggregate_functions"] = _probe(client_aggregate_functions, client, {})
    # Read with the capabilities because it changes when they do — on a new
    # session — and a tool that needs it can then use it without a round trip.
    state.operation_limits = read_operation_limits(client)
    state.node_metadata.server_limits = dict(state.operation_limits)
    return before != snapshot()


def _connect_and_probe(state: ServerState, connection: OpcuaConnection) -> None:
    """Open the first connection and read its capabilities.

    One connection attempt for both probes, not one each: against a server that
    is down, each would otherwise sit through the whole configured backoff on its
    own. `_bind` does the probing, through `on_client_replaced`.

    Called from the lifespan, and never from `tools/list` — see
    :meth:`PolicyMCPServer.list_tools` for why a catalogue request must not wait
    on a socket.
    """
    state.forget_capabilities()
    try:
        connection.ensure_connected()
    except Exception:
        # Deliberately not fatal. An MCP client starts this server when *it*
        # starts, which may be long before the plant network is reachable; dying
        # here would mean a restart of the MCP client for every OPC UA outage. A
        # server that is down simply advertises the core tools until it comes
        # back, at which point `on_client_replaced` re-probes. Every tool call
        # retries the connection, and `get_server_status` reports what is wrong
        # in the meantime.
        print(
            f"Starting without an OPC UA connection: {connection.last_error}. "
            f"Tools will retry on each call.",
            file=sys.stderr,
        )


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
    """Handle OPC UA client connection lifecycle.

    ``server`` is the :class:`PolicyMCPServer` this lifespan belongs to, and its
    :attr:`~PolicyMCPServer.state` is what everything here reads and fills in.
    That is the whole of #116: the connection, the capability answers and the
    managers belong to one server instance rather than to the module, so a second
    instance in one process gets its own and a stopped one leaves nothing behind.

    The state is also put in the context dict, because the tools are handed a
    ``Context`` and no ``self``. It is the same object, not a copy.
    """
    state = server.state
    connection = OpcuaConnection(state.url, policy=state.policy)
    state.connection = connection
    context: dict = {
        "opcua_client": None,
        "opcua_connection": connection,
        "state": state,
    }
    connection.on_client_replaced = lambda client: _bind(state, context, client)

    # In a thread: python-opcua is synchronous, and for a secured connection even
    # building the client fetches the server's certificate from its endpoint
    # list, so this blocks on the network too. Connecting and probing are one
    # call so a server that is down costs one round of backoff, not two.
    #
    # Started, not awaited. The SDK answers nothing — not even `initialize` —
    # until this lifespan has yielded, so awaiting it held the whole protocol back
    # for a full connection round against a plant that was down (#136). Plant
    # connectivity is runtime state, reported by `get_server_status`; it does not
    # gate the protocol. What awaiting it bought is kept within a bound:
    # `tools/list` and `get_server_status` wait for it (`ServerState.await_warm_up`),
    # and a tool call that needs a session joins its round through
    # `ensure_connected`. The Node runtime starts its warm-up the same way.
    warm_up = state.start_warm_up(asyncio.to_thread(_connect_and_probe, state, connection))

    try:
        yield context
    finally:
        # Stop the warm-up's backoff and wait out the attempt on the wire, before
        # anything is disconnected: a round that completed after the disconnect
        # would leave a session open behind a server that has stopped.
        connection.close()
        await asyncio.wait({warm_up})
        state.warm_up = None
        await asyncio.to_thread(_release_opcua, state)
        state.forget_capabilities()
        state.connection = None


#: How long a server stopped by a signal waits for the OPC UA side to close
#: cleanly: `SHUTDOWN_GRACE_MS` in the Node runtime's `index.ts`.
SHUTDOWN_GRACE_SECONDS = 5.0


def _release_opcua(state: ServerState) -> None:
    """Drop the subscriptions, then the session. In that order.

    The event subscriptions as much as the data-change ones. Disconnecting first
    would leave the OPC UA server publishing to nobody until each subscription's
    lifetime expired. Shared by the lifespan, which runs when the client closes
    stdin, and by the signal handler, which runs when it does not.
    """
    state.subscriptions.close_all()
    state.events.close_all()
    if state.connection is not None:
        state.connection.disconnect()


def _exit_on_signal(state: ServerState) -> None:
    """Make SIGTERM and SIGINT release the OPC UA side, then exit 0.

    This runtime had no handler at all (#157). SIGTERM — what a supervisor or a
    container runtime sends — killed the process where it stood: no subscription
    deleted, no session closed, so the OPC UA server kept publishing into the void
    for each subscription's whole lifetime. The Node runtime has always cleaned up
    on both signals.

    The release runs on a thread of its own and the handler waits for it, but
    only for :data:`SHUTDOWN_GRACE_SECONDS`: disconnecting from a server that has
    gone quiet can wait on a request timeout, and the main thread may be the one
    holding whatever the release needs. ``os._exit`` then, because an orderly
    interpreter exit would wait for exactly the threads that are stuck.
    """
    stopping = threading.Event()

    def release() -> None:
        try:
            connection = state.connection
            if connection is not None:
                # What the lifespan does before it releases, for the same reason:
                # end the warm-up's backoff, and let an attempt already on the
                # wire land before disconnecting, so a round that completes
                # afterwards cannot leave a session open behind us (#136).
                connection.close()
                connection.settle()
            _release_opcua(state)
        except Exception as error:
            print(f"Error closing the OPC UA session: {describe_error(error)}", file=sys.stderr)

    def handle(_signum, _frame) -> None:
        if stopping.is_set():
            return
        stopping.set()
        worker = threading.Thread(target=release, name="opcua-shutdown", daemon=True)
        worker.start()
        worker.join(SHUTDOWN_GRACE_SECONDS)
        with contextlib.suppress(Exception):
            sys.stderr.flush()
        os._exit(0)

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, handle)


def _advertised_schema(state: ServerState, spec: dict) -> dict:
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

    functions = state.capabilities["aggregate_functions"]
    if not functions:
        properties.pop("aggregate_function", None)
        properties.pop("processing_interval", None)
        return schema

    properties["aggregate_function"]["description"] += f", one of: {', '.join(functions)}"
    return schema


class PolicyMCPServer(MCPServer):
    """MCPServer whose advertised and callable tools obey deployment policy.

    Owns a :class:`ServerState` (#116). ``list_tools`` and ``call_tool`` reach it
    through ``self``, which is the answer to the question the old module globals
    were the wrong answer to: they existed because ``list_tools`` is handed no
    ``Context``, and a method has ``self`` whether or not it has a context.
    """

    def __init__(self, *args, state: ServerState | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.state = state if state is not None else ServerState()

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
        # The one wait this does, and it is on the startup warm-up, bounded —
        # never on a connection of its own.
        await self.state.await_warm_up()
        policy = self.state.policy
        specs = {tool["name"]: tool for tool in CONTRACT["tools"]}
        listed = await super().list_tools()
        visible = []
        for tool in listed:
            spec = specs[tool.name]
            if not policy.is_visible(spec):
                continue
            if not self.state.capabilities_met(spec):
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
                # Beside `result`, for a tool that can return fewer records than
                # it was asked for (issue #137). `outputSchema` in tools.ts builds
                # the same object.
                if spec.get("reportsCompleteness"):
                    output_schema["properties"]["completeness"] = CONTRACT["completeness"]["schema"]
                    output_schema["required"] = ["result", "completeness"]
            visible.append(
                tool.model_copy(
                    update={
                        "annotations": annotations,
                        "output_schema": output_schema,
                        "input_schema": _advertised_schema(self.state, spec),
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
            #
            # Size before shape: the validator's work grows with the request, and
            # this stops at the first thing out of bounds (issue #139). A
            # `LimitExceeded` is a `ValueError`, so it is refused and audited
            # exactly as a malformed call is. See limits.py.
            check_request_bounds(name, arguments)
            validate_arguments(name, spec["inputSchema"], arguments)
            # Before the policy and the audit trail read the session, not merely
            # before the request goes out. `get_server_status` is the exception:
            # it reports on the connection, and waits for the warm-up only
            # boundedly.
            if name != "get_server_status":
                await self.state.await_connection_in_flight()
            # Catalog filtering is not authorization: clients may retain an old
            # tools/list result, so enforce the current policy again on every call.
            self.state.policy.authorize(name, arguments)
        except (PermissionError, ValueError) as exc:
            _audit_after(
                self.state, name, arguments, "denied", str(exc), call_id=call_id, attempt=1
            )
            raise ToolError(str(exc)) from exc
        _audit_permission(self.state, name, arguments, call_id=call_id, attempt=1)

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
                _audit_after(
                    self.state,
                    name,
                    arguments,
                    "failed",
                    describe_error(reported),
                    call_id=call_id,
                    attempt=call.attempt,
                )
            raise reported from error.__cause__
        _audit_after(
            self.state, name, arguments, "completed", call_id=call_id, attempt=call.attempt
        )
        return result

    async def _run_tool(self, call: _Call, context):
        name, arguments = call.name, call.arguments
        # The one tool that must answer while the connection is down: it exists
        # to say so, and reaches for the connection itself.
        connection = self.state.connection
        if name == "get_server_status" or connection is None:
            await self.state.await_warm_up()
            return await super().call_tool(name, arguments, context)

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
        # Which session this call is about to ride on, so recovery can tell "my
        # session died" from "someone else already replaced it".
        call.session = connection.session_id

        if not self.state.capabilities_met(call.spec):
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
        try:
            await asyncio.to_thread(connection.reconnect, call.session)
        except Exception as rebuild_failed:
            # The same failure the pre-dispatch path reports, worded the same way.
            # Left bare, this reached the model as "[Errno 61] Connection refused"
            # — the same outage the call before it had described as "Not connected
            # to the OPC UA server at …: … Call get_server_status for details", so
            # one server said two things about one event depending on where in the
            # request it happened to notice.
            raise ToolError(
                not_connected_message(connection.url, describe_error(rebuild_failed))
            ) from rebuild_failed

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
            self.state.policy.authorize(name, arguments)
        except (PermissionError, ValueError) as exc:
            call.denied = True
            _audit_after(
                self.state, name, arguments, "denied", str(exc), call_id=call.call_id, attempt=2
            )
            raise ToolError(str(exc)) from exc
        try:
            _audit_permission(self.state, name, arguments, call_id=call.call_id, attempt=2)
        except ToolError:
            # Refused before the second attempt went out, and recorded as nothing
            # more: a `failed` line would read as though the plant had answered.
            call.denied = True
            raise

        return await super().call_tool(name, arguments, context)


# --- helpers shared by the tool bodies ------------------------------------------


def _state(ctx: Context) -> ServerState:
    """This server's state, as a tool body reaches it.

    The tools are handed a ``Context`` and no ``self``, so they come at the state
    through the lifespan rather than through the instance. It is the same object
    :attr:`PolicyMCPServer.state` returns, put there by :func:`opcua_lifespan`.
    """
    return ctx.request_context.lifespan_context["state"]


_TRAVERSAL = CONTRACT["traversal"]

#: The standard Root folder, which an absolute browse path is written from.
_ROOT_FOLDER = "ns=0;i=84"


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


def _records_result(
    records: list[dict], completeness: dict | None = None, text: str | None = None
) -> CallToolResult:
    """Records, one text block each, and whether they are all of them.

    The same framing ``MCPServer`` gives a returned list — one block per record
    and ``{"result": [...]}`` — built by hand because ``completeness`` sits
    beside ``result`` (issue #137), never inside it: ``result`` stays the array
    every existing client already reads. ``recordBlocks`` in tools.ts is the
    Node half.

    ``text`` is a notice for a reader that only has the text, repeating what
    ``completeness`` says. Only for a loss the caller did not choose — this
    server's cap, the OPC UA server's, or a full buffer — never for a count the
    caller asked for and got: telling someone who asked for 10 readings that
    there may be more would be noise on every small read.
    """
    content = [TextContent(type="text", text=json.dumps(record, indent=2)) for record in records]
    if text is not None:
        content.append(TextContent(type="text", text=text))
    structured: dict[str, Any] = {"result": records}
    if completeness is not None:
        structured["completeness"] = completeness
    return CallToolResult(content=content, structured_content=structured)


def _history_result(records: list[dict], completeness: dict, cap_notice: str) -> CallToolResult:
    """History records (``resultShapes.historyRecords``), and whether they are all.

    The notice for a capped read predates ``completeness`` and is kept for
    text-only readers, and fires exactly when it always did: when this server's
    own cap was reached, which is ``contractLimit``. The count it names is the
    cap rather than the records returned — an event-history read reaches its cap
    on what the server sent, and ``severity_min`` may have kept fewer.
    """
    text = None
    if "contractLimit" in completeness["reasons"]:
        count = completeness["limit"] if completeness["limit"] is not None else len(records)
        text = notice(cap_notice, count=count)
    elif "serverLimit" in completeness["reasons"]:
        text = notice("serverTruncated", count=completeness["returned"])
    return _records_result(records, completeness, text)


def _forward_from(start: datetime | None, end: datetime | None, last: Any) -> str | None:
    """Where a truncated history read resumes, or None when its arguments cannot say.

    Only a forward read can be continued with ``start_time``: a start was given
    and the range runs up from it. Without one, an OPC UA server reads backwards
    from the end, newest first (Part 11 §6.4.3.2), and the rest of the answer is
    then *older* records — which no start_time asks for. The last record's own
    timestamp is the resume point, inclusive, so a boundary record repeats
    rather than being lost. ``forwardFrom`` in tools.ts is the other half.
    """
    if start is None or (end is not None and end <= start):
        return None
    return last if isinstance(last, str) else None


def _read_values(client: Any, node_ids: list[Any], attribute: Any, chunk: int) -> list[Any]:
    """One logical read, sent as consecutive Reads of at most ``chunk`` nodes.

    Sequential rather than in parallel: the chunking exists because the server
    said how much one request may carry, and firing every chunk at once would
    put the same load on it in a different envelope. The results are
    concatenated in the order asked, so each node keeps its own status in its
    own place (issue #139).
    """
    return [
        value
        for part in chunked(node_ids, chunk)
        for value in client.uaclient.get_attributes(part, attribute)
    ]


def _object_result(record: Any, completeness: dict | None = None) -> CallToolResult:
    """A result that is one object rather than a list of records.

    One text block and a ``result`` that is the object itself. Used by every
    shape where a list would be a lie about the answer's structure: a browse has
    one ``truncated`` flag for the whole walk, a method call has one result, and
    a status report is one report. The Node server frames these identically.
    """
    structured: dict[str, Any] = {"result": record}
    if completeness is not None:
        structured["completeness"] = completeness
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(record, indent=2))],
        structured_content=structured,
    )


# --- reading --------------------------------------------------------------------


def read_opcua_nodes(node_ids: list[str], ctx: Context) -> list[dict]:
    """
    Read the current value of one or more OPC UA nodes in a single request.

    Parameters:
        node_ids (list[str]): The node IDs to read. Example: ['ns=2;i=2', 'ns=2;i=3'].

    More than ``limits.maxNodesPerRead`` is refused by the input schema's
    ``maxItems`` before this runs: a short list of readings is indistinguishable
    from a complete one, so the list is never quietly cut. A server whose
    MaxNodesPerRead is lower gets the list in consecutive Reads instead.

    Returns:
        list[dict]: One record per node, shaped by `contract/tools.json` ->
            `resultShapes.nodeValues`. A node the server rejects is one 'Bad…'
            status among the others; only a failure of the whole operation is
            raised as a `ToolError`.
    """
    if not node_ids:
        raise ToolError(error_message("emptyArray", tool="read_opcua_nodes", argument="node_ids"))
    client = ctx.request_context.lifespan_context["opcua_client"]
    chunk = read_chunk(_state(ctx).operation_limits)
    try:
        nodes = [client.get_node(node_id) for node_id in node_ids]
        values = _read_values(client, [node.nodeid for node in nodes], ua.AttributeIds.Value, chunk)
        # Two extra round trips on a cold cache for the whole batch, none on a
        # warm one, and never a reason for the read to fail. See node_metadata.
        engineering = _state(ctx).node_metadata.for_nodes(client, node_ids)
        return [
            _node_value_record(node_id, data_value, engineering.get(node_id))
            for node_id, data_value in zip(node_ids, values, strict=True)
        ]
    except Exception as e:
        raise ToolError(error_message("readFailed", reason=describe_error(e))) from e


def read_opcua_history(
    node_id: str,
    ctx: Context,
    start_time: str | None = None,
    end_time: str | None = None,
    num_values: int = 0,
    aggregate_function: str | None = None,
    processing_interval: float = 0,
) -> CallToolResult:
    """
    Read a node's stored history, raw or summarised by a server-side aggregate.

    The two used to be separate tools with separate implementations of the same
    framing. They differ in one request and share everything else, so they are
    one tool whose ``aggregate_function`` argument decides which is sent.

    Returns:
        CallToolResult: One record per reading or interval, shaped by
            ``resultShapes.historyRecords``, and ``completeness`` beside them.
    """
    client = ctx.request_context.lifespan_context["opcua_client"]

    if aggregate_function is None:
        # `0` used to mean "every reading in the range", which against a node
        # historised at 100ms is a request that never returns — and the browse
        # caps beside it have always been refusals rather than tuning knobs.
        wanted = history_values(num_values)
        try:
            start = parse_iso_datetime(start_time)
            end = parse_iso_datetime(end_time)
            # What `Node.read_raw_history` sends, through the call that keeps the
            # continuation point it throws away; see history.py.
            details = raw_details(start, end, wanted)
            result = client.get_node(node_id).history_read(details)
            # Good severity, not `StatusCode.check()`'s plain Good: GoodNoData is
            # an empty range with completeness complete, not a failed read (#157).
            values = history_data(result, "Read history", "DataValues")
            continued = continues(result.ContinuationPoint)
            release_continuation_point(client, node_id, result.ContinuationPoint, details)
            records = history_records(values)
            return _history_result(
                records,
                history_completeness(
                    returned=len(records),
                    fetched=len(records),
                    wanted=wanted,
                    continuation_point=continued,
                    next_start=_forward_from(
                        start, end, records[-1]["timestamp"] if records else None
                    ),
                ),
                "historyTruncated",
            )
        except Exception as e:
            raise ToolError(
                error_message("historyFailed", node_id=node_id, reason=describe_error(e))
            ) from e

    if start_time is None:
        raise ToolError(error_message("aggregateNeedsStart"))

    aggregate_functions = _state(ctx).capabilities["aggregate_functions"]
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

        # The number of results is decided by `processing_interval` over the
        # range, which is the whole point of asking for one — it is how to see a
        # week without transferring a week. It is still a number of records,
        # though, and a millisecond interval over a year is billions of them; so
        # it is bounded by the same cap as a raw read, and refused before it is
        # sent.
        intervals = aggregate_intervals(
            details.StartTime.timestamp() * 1000,
            details.EndTime.timestamp() * 1000,
            processing_interval,
        )
        if intervals > MAX_HISTORY_VALUES:
            raise LimitExceeded(
                error_message(
                    "tooManyIntervals",
                    tool="read_opcua_history",
                    count=intervals,
                    processing_interval=format_number(processing_interval),
                    limit=MAX_HISTORY_VALUES,
                )
            )

        result = client.get_node(node_id).history_read(details)
        values = history_data(result, "Read aggregate", "DataValues")
        continued = continues(result.ContinuationPoint)
        release_continuation_point(client, node_id, result.ContinuationPoint, details)
        records = history_records(values)
        # No count was asked for, so only the server can have cut this short.
        # Where it resumes is the interval after the last one returned, which is
        # not a timestamp this server should compute and round on the caller's
        # behalf — so there is no `continuation`, and `serverTruncated` says to
        # narrow.
        return _history_result(
            records,
            history_completeness(
                returned=len(records),
                fetched=len(records),
                wanted=None,
                continuation_point=continued,
                next_start=None,
            ),
            "historyTruncated",
        )
    except LimitExceeded as e:
        # A refusal of the request never reached the server, so it did not fail
        # to be read — and wrapping it would say it had.
        raise ToolError(str(e)) from e
    except Exception as e:
        raise ToolError(
            error_message("historyFailed", node_id=node_id, reason=describe_error(e))
        ) from e


# Registered once; tools/list gates it using the capabilities read from the
# lifecycle's active session. This avoids network I/O during import and prevents
# startup from opening throwaway OPC UA sessions.


def read_event_history(
    ctx: Context,
    node_id: str = events.DEFAULT_NOTIFIER,
    start_time: str | None = None,
    end_time: str | None = None,
    num_values: int = 0,
    severity_min: int = events.DEFAULTS["severityMin"],
) -> CallToolResult:
    """
    Read the events the server stored, for a range that has already passed.

    ``subscribe_events`` only sees what arrives after it subscribes, so it
    cannot answer what fired before anyone was watching. This reads the server's
    own event archive instead, and returns the same records, so an alarm looks
    identical whether it was seen live or recovered afterwards.

    Returns:
        CallToolResult: One record per event, shaped by the shared
            ``resultShapes.eventRecords`` in ``contract/tools.json``, and
            ``completeness`` beside them.
    """
    client = ctx.request_context.lifespan_context["opcua_client"]
    # A refusal the model has to see, as the Node runtime words it: a bare
    # ValueError here escaped as the SDK's generic "Error executing tool".
    try:
        end = parse_iso_datetime(end_time) or datetime.now(timezone.utc)
        # An hour back, rather than the epoch: a range nobody bounded should be
        # the recent past, not the whole archive. `read_opcua_history` defaults
        # the same way and for the same reason.
        start = parse_iso_datetime(start_time) or end - timedelta(hours=1)
    except ValueError as e:
        raise ToolError(str(e)) from e
    # The same cap as a raw value read, and a refusal rather than a knob: an
    # alarm burst is tens of thousands of events, and "all of them" is a request
    # that never returns.
    wanted = history_values(num_values)
    try:
        page = events.read_event_history(client, node_id, start, end, wanted, severity_min)
    except Exception as e:
        raise ToolError(
            error_message("eventHistoryFailed", node_id=node_id, reason=describe_error(e))
        ) from e
    return _history_result(
        page.records,
        history_completeness(
            returned=len(page.records),
            fetched=page.fetched,
            wanted=wanted,
            continuation_point=page.continued,
            next_start=_forward_from(start, end, page.last_time),
        ),
        "eventHistoryTruncated",
    )


# Tool: Report the connection and what the OPC UA server says about itself.
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
    identity = server_identity_record(_state(ctx).policy.config)
    # A round someone else started is not joined. Against a plant that is down it
    # runs the whole configured backoff, and this is the report of why nothing is
    # connected — the one answer that must not wait for it (#136). The round
    # carries on; asking again reports how it ended.
    if connection.connecting:
        return _object_result(
            disconnected_status(
                connection.url,
                security,
                identity,
                still_connecting_message(connection.url, connection.last_error),
            )
        )
    try:
        # Through the same retry as every other read, so that asking for the
        # status also re-establishes a session that has silently died — which is
        # exactly the moment someone asks. python-opcua has no way to tell a
        # live socket from a dead one short of using it, so this read *is* the
        # liveness check.
        status = connection.run(
            lambda: read_server_status(connection.client, connection.url, security, identity)
        )
    except Exception as error:
        status = disconnected_status(connection.url, security, identity, describe_error(error))
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
        "type_definition": None,
    }


def type_definition_of(is_good: bool, browse_names: list[str]) -> str | None:
    """Which of a node's HasTypeDefinition references to report, if any.

    Split out from the browse and driven by ``tests/fixtures/type-definitions.json``
    because ``type-definitions.test.mjs`` has to answer identically: two clients
    browsing the same server must not disagree about what its nodes are.

    Exactly one, or nothing. OPC UA Part 3 §4.3 gives an Object or a Variable
    exactly one HasTypeDefinition, so:

    * none — a Method, a View or a type itself. That is an answer, not a failure.
    * two — a server no client can read correctly. Taking whichever came first
      would let the two runtimes report different types for the same node
      depending on how each library ordered the references, and would report the
      *base* type for a node that also declared a useful one. Saying nothing is
      the only answer that is both deterministic and never wrong.
    """
    if not is_good or len(browse_names) != 1:
        return None
    return browse_names[0] or None


def _fill_type_definitions(client, records: list[dict], server_limits: dict) -> None:
    """Fill in ``type_definition`` for ``records``, in one batched browse.

    ``HasTypeDefinition`` is non-hierarchical, so the traversal's own browse —
    forward hierarchical references only, deliberately, or every node would
    answer with its parent and its type instead of its children — never sees it.
    It takes a second browse, and that is why this is one request for the whole
    result rather than one per node: a 500-node walk would otherwise cost 500
    extra round trips to say what one already could.

    Best-effort, like the variable detail: a server that refuses this leaves the
    field null rather than failing a browse that succeeded.
    """
    if not records:
        return
    descriptions = []
    for record in records:
        description = ua.BrowseDescription()
        description.NodeId = ua.NodeId.from_string(record["node_id"])
        description.BrowseDirection = ua.BrowseDirection.Forward
        description.ReferenceTypeId = ua.NodeId.from_string(_TRAVERSAL["hasTypeDefinitionNodeId"])
        # No subtypes: HasTypeDefinition has none, and asking for them would let
        # an unrelated reference through on a server that has invented one.
        description.IncludeSubtypes = False
        description.NodeClassMask = ua.NodeClass.Unspecified
        description.ResultMask = ua.BrowseResultMask.All
        descriptions.append(description)

    # Chunked for the same reason the property reads are: MaxNodesPerBrowse is
    # an operational limit a conformant server may enforce, and the default walk
    # already returns up to 500 nodes. A server that states a lower one gets
    # smaller chunks.
    size = browse_chunk(server_limits, _TRAVERSAL["maxTypeDefinitionsPerRequest"])
    for start in range(0, len(descriptions), size):
        chunk = descriptions[start : start + size]
        params = ua.BrowseParameters()
        params.View.Timestamp = ua.get_win_epoch()
        params.NodesToBrowse = chunk
        params.RequestedMaxReferencesPerNode = 0
        try:
            results = client.uaclient.browse(params)
        except Exception:
            return
        for record, result in zip(records[start : start + size], results, strict=False):
            record["type_definition"] = type_definition_of(
                result.StatusCode.is_good(),
                [reference.BrowseName.Name for reference in result.References],
            )


def _fill_variable_detail(client, records: list[dict], server_limits: dict) -> None:
    """Fill in value, data type and description for the Variables among ``records``.

    One batched read of each attribute rather than three reads per node: a
    500-node inventory is otherwise 1500 round trips, which is the difference
    between a tool that answers and one that times out on real equipment.
    """
    variables = [record for record in records if record["node_class"] == "Variable"]
    if not variables:
        return
    node_ids = [client.get_node(record["node_id"]).nodeid for record in variables]
    chunk = read_chunk(server_limits)
    try:
        values = _read_values(client, node_ids, ua.AttributeIds.Value, chunk)
        descriptions = _read_values(client, node_ids, ua.AttributeIds.Description, chunk)
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
        unbrowsable = False

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
                    # its worst node. It is still a gap in the answer, and
                    # `completeness` says so rather than letting "could not list"
                    # pass for "has no children".
                    if current_id == root:
                        raise
                    unbrowsable = True
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
                        "type_definition": None,
                    }
                    if keep(record):
                        found.append(record)

                    # Descend through structure regardless of the class filter:
                    # what is being looked for is usually below an Object, not
                    # the Object.
                    if reference.NodeClass == ua.NodeClass.Object and current_depth + 1 < depth:
                        queue.append((child_id, current_depth + 1))

        # Unconditional, unlike the variable detail: the type is what the record
        # *is*, not extra reading about its value, and it costs one batched
        # browse however many nodes were found.
        server_limits = _state(ctx).operation_limits
        _fill_type_definitions(client, found, server_limits)
        if include_values:
            _fill_variable_detail(client, found, server_limits)
        return _object_result(
            {"nodes": found, "truncated": truncated, "inspected": inspected},
            traversal_completeness(
                returned=len(found),
                truncated=truncated,
                max_nodes=max_nodes,
                unbrowsable=unbrowsable,
            ),
        )
    except Exception as e:
        raise ToolError(
            error_message("browseFailed", node_id=root, reason=describe_error(e))
        ) from e


# --- writing ---------------------------------------------------------------------


def write_opcua_nodes(nodes: list[dict[str, Any]], ctx: Context) -> list[dict]:
    """
    Write a value to one or more OPC UA nodes.

    Nodes given an explicit ``data_type`` skip the read-first inference entirely,
    which is what makes a *write-only* node writable — reading it to learn its
    type is exactly what such a node refuses (issue #9). The rest are read first,
    in one batch, and converted to the type the server reports.

    The whole batch goes out as one Write, and that is a promise rather than an
    accident (issue #139). A batch over the server's MaxNodesPerWrite is refused
    here, before anything is read or sent, instead of being split: OPC UA lets
    one Write partially succeed already, and splitting would add a failure where
    the first part has moved the plant and the second never arrives — which
    ``uncertainOutcome`` could not then describe. ``limits.maxNodesPerWrite`` is
    the schema's ``maxItems``, enforced before this runs.

    Returns:
        list[dict]: One record per node, in the order asked, shaped by
            ``resultShapes.writeResults``. A node the server rejects is one
            status among them; only a failure of the whole operation is raised
            as a `ToolError`.
    """
    if not nodes:
        raise ToolError(error_message("emptyArray", tool="write_opcua_nodes", argument="nodes"))
    client = ctx.request_context.lifespan_context["opcua_client"]
    state = _state(ctx)
    limit = write_limit(state.operation_limits)
    if len(nodes) > limit:
        raise ToolError(
            error_message(
                "tooManyWritesForServer", tool="write_opcua_nodes", count=len(nodes), limit=limit
            )
        )
    policy = state.policy
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
            read = _read_values(
                client,
                [client.get_node(nodes[index]["node_id"]).nodeid for index in needs_current],
                ua.AttributeIds.Value,
                read_chunk(state.operation_limits),
            )
            current = dict(zip(needs_current, read, strict=True))

        # Before anything is sent, and raising rather than marking one record:
        # the whole batch is refused so it can never end up partially applied,
        # which is the property the identity allowlist already had.
        check_write_bounds(state, nodes, bounds, current, client)

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
            except LimitExceeded as e:
                # A value this server refuses to send at all — a ByteString over
                # limits.maxByteStringBytes — stops the batch rather than
                # becoming one node's status: nothing has been sent yet, and
                # nothing should be.
                raise ToolError(str(e)) from e
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
        raise ToolError(error_message("writeFailed", reason=describe_error(e))) from e


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
    state: ServerState,
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
        if state.policy.config.allow_out_of_range_writes
        else state.node_metadata.for_nodes(client, node_ids)
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
    """The declared type of each input argument, or [] when the method publishes none.

    A declared DataType that is not itself built in (``Duration``, ``UtcTime``,
    an enumeration) is resolved to the built-in type it is encoded as, and one
    that resolves to none raises: that is a method whose argument cannot be
    encoded, not one that declares nothing, and guessing would send it anyway.
    """
    try:
        arguments = client.get_node(method_node_id).get_child(["0:InputArguments"]).get_value()
    except Exception:
        # Not every method publishes InputArguments, and a method with no
        # arguments has nothing to publish. Fall back rather than refuse.
        return []

    def supertype_of(data_type: str) -> str | None:
        # Every inverse reference, filtered here rather than by the server:
        # python-opcua's own server answers a browse filtered to HasSubtype with
        # nothing at all, and `method-arguments.ts` does the same for that reason.
        references = client.get_node(data_type).get_references(direction=ua.BrowseDirection.Inverse)
        for reference in references:
            if reference.ReferenceTypeId == ua.NodeId(ua.ObjectIds.HasSubtype):
                return canonical_node_id(reference.NodeId.to_string())
        return None

    return [
        (
            built_in_type(canonical_node_id(argument.DataType.to_string()), supertype_of),
            argument.ValueRank >= 1,
        )
        for argument in arguments or []
    ]


def _call(node: Any, method_node: Any, arguments: list[Any]) -> Any:
    """Call a method and return its whole CallMethodResult.

    Not python-opcua's ``call_method``, which returns only the outputs — so the
    tool reported ``"Good"`` whatever the server said — and flattens a single
    array output into what look like several outputs.
    """
    request = ua.CallMethodRequest()
    request.ObjectId = node.nodeid
    request.MethodId = method_node.nodeid
    request.InputArguments = arguments
    result = node.server.call([request])[0]
    # Good *severity*, not plain Good: GoodClamped or GoodLocalOverride is a call
    # that happened, and refusing it would report as failed an action the plant
    # carried out. The subcode is reported in `status` instead.
    if not result.StatusCode.is_good():
        raise ValueError(f"Method call failed with status: {result.StatusCode.name}")
    return result


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
                method_args.append(guess_variant(argument, index))

        result = _call(object_node, method_node, method_args)
        return _object_result(
            {
                "object_node_id": canonical_node_id(object_node_id),
                "method_node_id": canonical_node_id(method_node_id),
                "status": str(result.StatusCode.name),
                "outputs": [variant_to_json(output) for output in result.OutputArguments],
            }
        )
    except LimitExceeded as e:
        # Refused before the call was sent, so it did not fail: wrapping it in
        # "Failed to call method" would say the plant had turned it down.
        raise ToolError(str(e)) from e
    except Exception as e:
        raise ToolError(
            error_message(
                "methodFailed",
                method_node_id=method_node_id,
                object_node_id=object_node_id,
                reason=describe_error(e),
            )
        ) from e


# --- data-change subscriptions ---------------------------------------------------


async def subscribe_opcua_nodes(
    node_ids: list[str],
    ctx: Context,
    publishing_interval: float = 1000,
    sampling_interval: float = 0,
    buffer_size: int = 20,
    deadband_type: str | None = None,
    deadband_value: float | None = None,
    data_change_trigger: str | None = None,
) -> CallToolResult:
    """
    Watch one or more OPC UA nodes for value changes instead of polling them.

    Returns:
        CallToolResult: One record per new subscription, shaped by
            ``resultShapes.subscriptionRecords``, and ``completeness``.
    """
    if not node_ids:
        raise ToolError(
            error_message("emptyArray", tool="subscribe_opcua_nodes", argument="node_ids")
        )
    try:
        data_filter = resolve_filter(deadband_type, deadband_value, data_change_trigger)
    except ValueError as error:
        raise ToolError(str(error)) from error
    # One OPC UA subscription per monitored node is what makes a single
    # unsubscribe take the whole thing down — and it is also what makes an
    # unbounded subscribe ask a PLC for one subscription per node, past whatever
    # it is willing to hold, with nothing here counting them.
    subscriptions = _state(ctx).subscriptions
    active = len(subscriptions.list())
    if active + len(node_ids) > MAX_SUBSCRIPTIONS:
        raise ToolError(
            error_message(
                "tooManySubscriptions",
                active=active,
                limit=MAX_SUBSCRIPTIONS,
                wanted=len(node_ids),
            )
        )
    if data_filter.deadband_type == "percent":
        # A percent deadband is a percentage *of the node's EURange*, so a node
        # that publishes none cannot have one. Checked here, before a single
        # subscription is created, so a batch is refused whole rather than
        # leaving some nodes monitored and some not.
        client = ctx.request_context.lifespan_context["opcua_client"]
        engineering = _state(ctx).node_metadata.for_nodes(client, node_ids)
        for node_id in node_ids:
            info = engineering.get(node_id)
            if info is None or info.eu_range is None:
                raise ToolError(error_message("percentDeadbandNeedsRange", node_id=node_id))

    records = []
    for node_id in node_ids:
        # `ToolError`, not a bare exception: the SDK forwards a ToolError's
        # message to the client and withholds anything else as a crash. A bad
        # node ID is the caller's to fix, so it has to reach them — worded as the
        # Node server words it.
        try:
            records.append(
                await asyncio.to_thread(
                    subscriptions.subscribe,
                    node_id,
                    publishing_interval,
                    sampling_interval,
                    buffer_size,
                    data_filter,
                )
            )
        except Exception as e:
            raise ToolError(
                error_message("subscribeFailed", node_id=node_id, reason=describe_error(e))
            ) from e
    return _subscription_result(records)


def _subscription_result(records: list[dict]) -> CallToolResult:
    """The subscription family's records, and the changes their buffers dropped.

    Each record carries its own ring buffer's ``dropped``; ``completeness``
    totals them so one field answers for the whole result (issue #137).
    """
    return _records_result(records, buffer_completeness(records))


def list_subscriptions(ctx: Context) -> CallToolResult:
    """
    List the active OPC UA data-change subscriptions and their buffered changes.

    Returns:
        CallToolResult: One record per active subscription, shaped by
            `contract/tools.json` -> `resultShapes.subscriptionRecords`, and
            ``completeness``. No records when nothing is subscribed.
    """
    # No thread hop and no OPC UA call: this reads buffers already filled by
    # python-opcua's publishing thread, so it answers even if the server is down.
    return _subscription_result(_state(ctx).subscriptions.list())


async def unsubscribe_opcua_nodes(subscription_ids: list[str], ctx: Context) -> CallToolResult:
    """
    Cancel one or more subscriptions, reporting each as it was when cancelled.

    Every id is checked before any is cancelled: a list with one bad id would
    otherwise leave the caller unable to tell which of the others had already
    gone, and their buffered changes would be lost to a typo.

    Returns:
        CallToolResult: The cancelled subscriptions, shaped by
            ``resultShapes.subscriptionRecords``, so anything still buffered can
            be read one last time, and ``completeness``.
    """
    if not subscription_ids:
        raise ToolError(
            error_message("emptyArray", tool="unsubscribe_opcua_nodes", argument="subscription_ids")
        )
    subscriptions = _state(ctx).subscriptions
    active = {record["subscription_id"] for record in subscriptions.list()}
    unknown = [entry for entry in subscription_ids if entry not in active]
    if unknown:
        # Both runtimes word an unknown ID identically; see subscriptions.py.
        raise ToolError(unknown_subscriptions_message(unknown))

    records = []
    for subscription_id in subscription_ids:
        try:
            records.append(await asyncio.to_thread(subscriptions.unsubscribe, subscription_id))
        except KeyError as e:
            raise ToolError(unknown_subscription_message(subscription_id)) from e
        except RuntimeError as e:
            # The OPC UA server refused the delete. Already worded for the caller
            # by `delete_failed_message`, and shared with the Node server.
            raise ToolError(str(e)) from e
    return _subscription_result(records)


# --- events and Alarms & Conditions -----------------------------------------------


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
    # 0 means "unset" for a size, as it does everywhere else in both servers,
    # and a buffer that keeps nothing would be a strange thing to have asked for.
    # Clamped, and reported as clamped: the buffer is memory this process holds
    # for as long as the subscription lives, and "as many as you like" was a
    # request with no ceiling at all (issue #139).
    buffer_size = event_buffer_size(buffer_size)
    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        replaced = _state(ctx).events.subscribe(client, node_id, severity_min, buffer_size)
    except Exception as e:
        raise ToolError(
            error_message("eventSubscribeFailed", node_id=node_id, reason=describe_error(e))
        ) from e
    return _object_result(
        {
            "node_id": canonical_node_id(node_id),
            "severity_min": severity_min,
            "buffer_size": buffer_size,
            "replaced": replaced,
        }
    )


def read_events(
    ctx: Context,
    node_id: str = events.DEFAULT_NOTIFIER,
    limit: int = events.DEFAULTS["readLimit"],
) -> CallToolResult:
    """
    Read and drain the events buffered by subscribe_events.

    Returns:
        CallToolResult: Event records in text and structured form, with
            ``completeness`` beside them, plus a plain-text compatibility notice
            when the buffer overflowed.
    """
    limit = limit or events.DEFAULTS["readLimit"]
    drained = _state(ctx).events.drain(node_id, limit)
    if drained is None:
        raise ToolError(error_message("notSubscribedToEvents", node_id=node_id))
    records, remaining, dropped, size, resubscribed = drained
    # In the response, not only on stderr: an agent that cannot tell a complete
    # event stream from one that lost alarms reads the gap as quiet. As a field
    # since issue #137, and as a sentence still for a reader of the text alone.
    result = _records_result(
        records,
        drain_completeness(
            returned=len(records), limit=limit, remaining=remaining, dropped=dropped
        ),
        events.dropped_events_message(dropped, size) if dropped else None,
    )
    if resubscribed:
        # The same reasoning for the gap a reconnect leaves: nothing was dropped
        # from the buffer, the events simply never arrived (#157).
        result.content.append(TextContent(type="text", text=notice("eventsResubscribed")))
    return result


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
        raise ToolError(
            error_message("alarmsFailed", node_id=node_id, reason=describe_error(e))
        ) from e
    _state(ctx).events.remember(alarms)
    return alarms


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
    condition = condition_id or _state(ctx).events.condition_for(event_id)
    if not condition:
        raise ToolError(error_message("unknownEventId", event_id=event_id))

    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        status = events.acknowledge_alarm(client, condition, event_id, comment)
    except Exception as e:
        raise ToolError(
            error_message("acknowledgeFailed", condition_id=condition, reason=describe_error(e))
        ) from e
    return _object_result(
        {
            "event_id": event_id,
            "condition_id": canonical_node_id(condition),
            "status": status,
        }
    )


def act_on_alarm(
    event_id: str,
    action: str,
    ctx: Context,
    comment: str = "",
    shelve_duration_ms: float | None = None,
    condition_id: str | None = None,
) -> CallToolResult:
    """
    Confirm, annotate or shelve an alarm — the rest of the operator workflow.

    Returns:
        CallToolResult: One record of ``resultShapes.alarmAction``.
    """
    # A relationship between two arguments, which the contract's own schema
    # cannot express: `shelveFor` is `shelve` plus a duration, and accepting one
    # on any other action would silently ignore it. Refusing says which action
    # the caller probably meant.
    if action == "shelveFor" and shelve_duration_ms is None:
        raise ToolError(error_message("shelveForNeedsDuration"))
    if action != "shelveFor" and shelve_duration_ms is not None:
        raise ToolError(error_message("shelveDurationNotAllowed", action=action))

    condition = condition_id or _state(ctx).events.condition_for(event_id)
    if not condition:
        raise ToolError(error_message("unknownEventId", event_id=event_id))

    client = ctx.request_context.lifespan_context["opcua_client"]
    try:
        status = events.alarm_action(
            client, condition, event_id, action, comment, shelve_duration_ms
        )
    except Exception as e:
        raise ToolError(
            error_message(
                "alarmActionFailed", action=action, condition_id=condition, reason=describe_error(e)
            )
        ) from e
    return _object_result(
        {
            "event_id": event_id,
            "condition_id": canonical_node_id(condition),
            "action": action,
            "status": status,
        }
    )


# --- building a server ------------------------------------------------------------

#: The tools, in the order they are registered. Names rather than the functions
#: themselves so the registration and the contract can be compared directly:
#: `tests/unit/test_contract.py` asserts this list against `contract/tools.json`,
#: which is what stops a tool being defined and never registered — a failure that
#: is otherwise invisible, because an unregistered tool is simply a tool the
#: server does not have.
TOOL_NAMES: tuple[str, ...] = (
    "read_opcua_nodes",
    "browse_opcua_nodes",
    "read_opcua_history",
    "read_event_history",
    "get_server_status",
    "list_subscriptions",
    "list_active_alarms",
    "write_opcua_nodes",
    "call_opcua_method",
    "subscribe_opcua_nodes",
    "unsubscribe_opcua_nodes",
    "subscribe_events",
    "read_events",
    "acknowledge_alarm",
    "act_on_alarm",
)


def create_server(state: ServerState | None = None) -> PolicyMCPServer:
    """One MCP server, with its own state and its own registered tools (#116).

    Registration happens here rather than through module-level decorators,
    because a decorator binds a tool to whichever instance existed at import.
    With one instance that is invisible; with two it means the second server has
    no tools. Threading the state through the lifespan was only half of making
    this instantiable twice — this is the other half.

    The server identity must match the Node server's, so both runtimes present
    themselves as the same product to MCP clients, and the version must be a real
    one rather than the null the Node server never reports.
    """
    mcp = PolicyMCPServer(
        "opcua-mcp-server",
        version=package_version(),
        lifespan=opcua_lifespan,
        state=state,
    )
    for name in TOOL_NAMES:
        mcp.tool(description=DESC[name])(globals()[name])

    # The same subscription records as `list_subscriptions`, re-readable without a
    # tool call. A closure rather than a module-level function because `MCPServer`
    # refuses to inject a `Context` into a static resource — which is exactly why
    # the subscription manager used to be module-level state. Closing over the
    # server gives it the one thing it needs without giving the module a
    # singleton.
    @mcp.resource(
        SUBSCRIPTIONS_RESOURCE["uri"],
        name=SUBSCRIPTIONS_RESOURCE["name"],
        description=SUBSCRIPTIONS_RESOURCE["description"],
        mime_type=SUBSCRIPTIONS_RESOURCE["mimeType"],
    )
    def subscriptions_resource() -> str:
        """The active subscriptions and their buffered changes, as JSON."""
        key = SUBSCRIPTIONS_RESOURCE["body"]["recordsKey"]
        return json.dumps({key: mcp.state.subscriptions.list()}, indent=2)

    return mcp


#: The instance the console script serves. One per process is what a stdio MCP
#: server is; what changed in #116 is that this is now an instance rather than the
#: only possible one.
mcp = create_server()


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
    #
    # Security, then policy, then reconnection, then the audit file — the same
    # order as `index.ts`, so one bad setting is the same first error on both.
    # The audit file goes last because opening it creates it.
    #
    # Any exception, not only ValueError: a policy file of an unexpected shape
    # used to escape as a KeyError or TypeError traceback, where the Node runtime
    # printed one "Configuration error:" line (#157).
    try:
        security_config()
        policy = mcp.state.policy
        reconnect = reconnect_config()
        # Opened here and not lazily: an operator who set OPCUA_AUDIT_FILE and
        # cannot be given one has to be told now, not at the first control call
        # they were relying on it to record. So does one whose target is unsafe
        # to write (a symlink, another account's file) or whose chain key cannot
        # be read.
        audit = AuditSink.from_config(parse_audit_config())
    except Exception as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    mcp.state.audit = audit

    print(f"Tool policy: {describe_policy(policy)}", file=sys.stderr)
    print(f"Control audit: {describe_audit(audit)}", file=sys.stderr)
    print(f"Connection resilience: {describe_reconnect(reconnect)}", file=sys.stderr)

    _exit_on_signal(mcp.state)
    mcp.run(transport="stdio")
