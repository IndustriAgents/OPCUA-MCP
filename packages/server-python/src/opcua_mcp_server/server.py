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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from . import events
from .adapters.opcua_alarms import PythonOpcuaAlarmPort
from .adapters.opcua_browse import PythonOpcuaBrowsePort
from .adapters.opcua_diagnostics import PythonOpcuaDiagnosticsPort
from .adapters.opcua_events import PythonOpcuaEventPort
from .adapters.opcua_history import PythonOpcuaHistoryPort
from .adapters.opcua_methods import PythonOpcuaMethodPort
from .adapters.opcua_read import PythonOpcuaReadPort
from .adapters.opcua_subscriptions import PythonOpcuaSubscriptionPort
from .adapters.opcua_write import PythonOpcuaWritePort
from .application.alarms import act_on_alarm as act_on_alarm_use_case
from .application.alarms import list_alarms
from .application.browse import browse_nodes
from .application.diagnostics import get_server_status as get_server_status_use_case
from .application.events import read_event_history as read_event_history_use_case
from .application.events import read_events as read_events_use_case
from .application.events import subscribe_events as subscribe_events_use_case
from .application.execution import ExecutionCall as _Call
from .application.execution import execute_tool
from .application.history import read_history
from .application.invocation import invoke_tool
from .application.methods import call_method
from .application.read import read_nodes
from .application.subscriptions import list_subscriptions as list_subscriptions_use_case
from .application.subscriptions import subscribe_nodes, unsubscribe_nodes
from .application.write import write_nodes
from .audit import (
    AuditSink,
    AuditWriteError,
    build_record,
    describe_audit,
    operator_id,
    parse_audit_config,
)
from .capabilities import (
    CapabilityAnswers,
    answers_from,
    capability_status,
    client_aggregate_functions,
    client_supports_history,
    client_supports_history_events,
    now_iso_utc,
    refusal,
    requirements,
    verdict,
)
from .config import describe_reconnect, reconnect_config
from .connection import (
    OpcuaConnection,
    describe_error,
    is_connection_error,
    not_connected_message,
)
from .contract import CONTRACT, DESC, SUBSCRIPTIONS_RESOURCE
from .errors import AdapterFailure, ApplicationRefusal
from .errors import message as error_message
from .generated_contract import TOOL_NAMES
from .limits import (
    LimitExceeded,
)
from .notices import notice
from .operation_limits import (
    read_chunk,
    read_operation_limits,
    write_limit,
)
from .policy import (
    control_gate,
    describe_policy,
    server_identity_record,
    values_at,
)
from .result_text import normalize_result_text, pretty_json
from .security import describe_security, security_config
from .state import ServerState
from .subscriptions import (
    resolve_filter,
)
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

    Unexpected failures expose only the contract's tool-name frame. Their
    original cause remains internal for recovery classification and diagnosis.
    """
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


def _bind(state: ServerState, context: dict, client) -> None:
    """Point everything that holds a client at the one just established.

    Called by the connection whenever it produces a client — at startup and
    again after each reconnect. The lifespan state is *mutated* rather than
    replaced because the MCP server hands the same dict to every tool call, so
    this is what makes a tool that reads `lifespan_context["opcua_client"]` see
    the new session rather than the dead one.

    Probing here, with the new client in hand, is so ``get_server_status`` has
    an answer to report for the new session; a call that needs a capability
    re-reads anything from an older generation itself (#140).
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
    _probe_capabilities(state, client, state.connection)


def _probe_capabilities(
    state: ServerState, client, connection: OpcuaConnection | None
) -> CapabilityAnswers:
    """Read what the server can do off a live client, and remember it.

    Never raises: each probe reports what the server answered, or ``unknown``
    with the reason when it could not be asked. The answers are stamped with the
    generation of the session they were read on, captured before asking — so an
    answer that arrives after the session has been replaced is already stale
    when it is stored, and the next call asks again.
    """
    generation = connection.session_generation if connection is not None else None
    history = client_supports_history(client)
    history_events = client_supports_history_events(client)
    # Read with the capabilities because it changes when they do — on a new
    # session — and a tool that needs it can then use it without a round trip.
    state.operation_limits = read_operation_limits(client)
    state.node_metadata.server_limits = dict(state.operation_limits)
    aggregate, functions = client_aggregate_functions(client)
    for probe in (history, history_events, aggregate):
        if probe.support == "unknown":
            print(f"OPC UA capability probe failed: {probe.reason}", file=sys.stderr)
    state.capabilities = answers_from(
        generation,
        now_iso_utc(),
        {"history": history, "historyEvents": history_events, "aggregate": aggregate},
        functions,
    )
    return state.capabilities


def _fresh_capabilities(state: ServerState, connection: OpcuaConnection) -> CapabilityAnswers:
    """Ask the live server again, rebuilding the session first if it has died.

    What a refusal is decided on. python-opcua cannot tell a dead socket from a
    live one short of using it, and a refusal taken from the cache uses nothing:
    a server that restarted *with* history behind a cached "no" would go on being
    refused, on a session nobody noticed was gone, for as long as the client kept
    asking. So the probes go through :meth:`OpcuaConnection.run`, the same
    liveness check ``get_server_status`` relies on — a probe that lost the
    session raises its error, the session is rebuilt (which re-reads the answers
    for the new generation) and the probes are asked once more.
    """

    def ask() -> CapabilityAnswers:
        client = connection.client
        if client is None:
            raise RuntimeError("No OPC UA session available")
        answers = _probe_capabilities(state, client, connection)
        lost = next(
            (
                probe.error
                for probe in answers.probes.values()
                if probe.error is not None and is_connection_error(probe.error)
            ),
            None,
        )
        if lost is not None:
            raise lost
        return answers

    return connection.run(ask)


def _connect_and_probe(state: ServerState, connection: OpcuaConnection) -> None:
    """Open the first connection and read its capabilities.

    One connection attempt for both probes, not one each: against a server that
    is down, each would otherwise sit through the whole configured backoff on its
    own. `_bind` does the probing, through `on_client_replaced`.

    Called from the lifespan, and never from `tools/list` — see
    :meth:`PolicyMCPServer.list_tools` for why the catalogue does not depend on
    the connection at all.
    """
    state.forget_capabilities()
    try:
        connection.ensure_connected()
    except Exception:
        # Deliberately not fatal. An MCP client starts this server when *it*
        # starts, which may be long before the plant network is reachable; dying
        # here would mean a restart of the MCP client for every OPC UA outage.
        # The catalogue is the same either way (#140); every tool call retries
        # the connection, and `get_server_status` reports what is wrong in the
        # meantime.
        print(
            f"Starting without an OPC UA connection: {connection.last_error}. "
            f"Tools will retry on each call.",
            file=sys.stderr,
        )


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
    # `get_server_status` waits for it (`ServerState.await_warm_up`), and a tool
    # call that needs a session joins its round through `ensure_connected`.
    # `tools/list` does not: the catalogue is the contract whatever the plant is
    # doing (#140). The Node runtime starts its warm-up the same way.
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

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    for signum in (signal.SIGINT, signal.SIGTERM):
        if loop is not None and os.name == "posix":
            # add_signal_handler installs asyncio's wakeup fd. A signal may
            # arrive on an OPC UA or stdin thread while the main thread is
            # sleeping in the selector; a plain signal.signal handler alone
            # leaves the loop asleep indefinitely in that case (#173).
            loop.add_signal_handler(signum, handle, signum, None)
        else:
            signal.signal(signum, handle)


def _advertised_tool(tool, spec: dict):
    """One tool as advertised: the contract's own definition, never the derived one.

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

    Nothing about the connected server is folded in (#140). ``aggregate_function``
    used to be withheld from a server without aggregates and to carry the live
    function list otherwise, so the schema a client cached depended on when it
    listed; the server's own functions are reported by ``get_server_status``.
    """
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

    async def run_stdio_async(self) -> None:
        # Install again after the SDK's event loop exists, before it starts
        # stdin and OPC UA worker threads. Preserve callers' handlers on EOF.
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        _exit_on_signal(self.state)
        try:
            await super().run_stdio_async()
        finally:
            loop = asyncio.get_running_loop()
            for sig, handler in previous.items():
                if os.name == "posix":
                    loop.remove_signal_handler(sig)
                signal.signal(sig, handler)

    async def list_tools(self):
        """The catalogue: the contract, filtered by deployment policy only.

        No network I/O, no waiting, and the same answer for the life of the
        process whatever the plant is doing (#140). It used to depend on the
        capabilities of the session held — ``read_event_history`` absent,
        ``aggregate_function`` withheld — so a process started while the plant
        was down advertised less than one started while it was up, and nothing
        portable told a client that had listed once to list again. Clients and
        models cache tool definitions for the life of a session; what they cache
        now stays true. A capability the server lacks is reported by the call
        that needs it (:meth:`_ensure_capabilities`) and by ``get_server_status``.

        The policy is configuration, fixed when the process starts, so filtering
        on it does not make the catalogue move. No
        ``notifications/tools/list_changed`` is sent, on either runtime — see
        docs/architecture.md — and nothing depends on one.
        """
        policy = self.state.policy
        specs = {tool["name"]: tool for tool in CONTRACT["tools"]}
        listed = await super().list_tools()
        return [
            _advertised_tool(tool, specs[tool.name])
            for tool in listed
            if policy.is_visible(specs[tool.name])
        ]

    async def _ensure_capabilities(
        self, spec: dict, arguments: dict, connection: OpcuaConnection
    ) -> None:
        """Refuse a call the connected server cannot serve, before anything is sent.

        Called with a live session in hand, so it can ask rather than guess. A
        cached "yes" is trusted for the session generation it was read on: if it
        has gone stale, the server's own refusal of the request says so. Nothing
        else is taken from the cache. An answer from an older generation, an
        ``unknown``, and above all a "no" are asked again first — a "no" refuses
        without touching the network, so taken from the cache it could never find
        out that the server had come back with the feature. What is left is the
        server's own answer, and a refusal worded from the contract that says
        which capability, which session, and what to do instead (#140). The Node
        server's ``ensureCapabilities`` decides the same way.
        """
        groups = requirements(spec, arguments)
        if not groups:
            return
        answers = self.state.capabilities
        decided = verdict(groups, answers.support)
        if answers.generation != connection.session_generation or decided.outcome != "allowed":
            try:
                answers = await asyncio.to_thread(_fresh_capabilities, self.state, connection)
            except Exception as error:
                raise ToolError(
                    not_connected_message(connection.url, describe_error(error))
                ) from error
            decided = verdict(groups, answers.support)
        if decided.outcome != "allowed":
            raise ToolError(refusal(spec["name"], decided, answers, connection.url))

    async def call_tool(self, name, arguments, context=None):
        """Translate protocol errors around the shared execution envelope."""
        owner = self

        class Port:
            new_call_id = staticmethod(new_call_id)
            wait_for_connection = owner.state.await_connection_in_flight

            def authorize(self, name, arguments):
                owner.state.policy.authorize(name, arguments)

            normalize_failure = staticmethod(_without_sdk_prefix)
            normalize_result = staticmethod(normalize_result_text)

            def allowed(self, call):
                _audit_permission(
                    owner.state,
                    call.name,
                    call.arguments,
                    call_id=call.call_id,
                    attempt=call.attempt,
                )

            def after(self, call, decision, reason=""):
                _audit_after(
                    owner.state,
                    call.name,
                    call.arguments,
                    decision,
                    reason,
                    call_id=call.call_id,
                    attempt=call.attempt,
                )

            async def run(self, call):
                return await owner._run_tool(call, context)

        try:
            return await execute_tool(Port(), name, arguments or {})
        except (PermissionError, ValueError) as error:
            raise ToolError(str(error)) from error

    async def _run_tool(self, call: _Call, context):
        """Supply native and protocol callbacks to the shared recovery policy."""
        owner = self
        connection = self.state.connection
        dispatch = super().call_tool

        class Port:
            is_connection_error = staticmethod(is_connection_error)
            wait_for_warm_up = owner.state.await_warm_up

            def endpoint(self):
                return connection.url

            def session(self):
                return connection.session_id

            def has_connection(self):
                return connection is not None

            async def connect(self):
                await asyncio.to_thread(connection.ensure_connected)

            async def capabilities(self, call):
                await owner._ensure_capabilities(call.spec, call.arguments, connection)

            async def dispatch(self, call):
                return await dispatch(call.name, call.arguments, context)

            async def reconnect(self, session):
                await asyncio.to_thread(connection.reconnect, session)

            def log_recovery(self, resend):
                suffix = " and retrying once" if resend else ""
                print(
                    f"OPC UA call failed on a dead session; reconnecting{suffix}",
                    file=sys.stderr,
                )

            def targets(self, call):
                return describe_targets(call.spec, call.arguments)

            def authorize(self, call):
                owner.state.policy.authorize(call.name, call.arguments)

            def allowed(self, call):
                _audit_permission(
                    owner.state,
                    call.name,
                    call.arguments,
                    call_id=call.call_id,
                    attempt=call.attempt,
                )

            def denied(self, call, reason):
                _audit_after(
                    owner.state,
                    call.name,
                    call.arguments,
                    "denied",
                    reason,
                    call_id=call.call_id,
                    attempt=call.attempt,
                )

        return await invoke_tool(Port(), call)


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


async def read_opcua_nodes(node_ids: list[str], ctx: Context) -> list[dict]:
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
    """
    Read a node's stored history, raw or summarised by a server-side aggregate.

    The two used to be separate tools with separate implementations of the same
    framing. They differ in one request and share everything else, so they are
    one tool whose ``aggregate_function`` argument decides which is sent.

    Returns:
        CallToolResult: One record per reading or interval, shaped by
            ``resultShapes.historyRecords``, and ``completeness`` beside them.
    """
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


# Registered and advertised always; a call is checked against the capabilities
# of the session it rides on (#140). Nothing here touches the network at import.


async def read_event_history(
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
    port = PythonOpcuaEventPort(
        ctx.request_context.lifespan_context["opcua_client"], _state(ctx).events
    )
    try:
        result = await read_event_history_use_case(
            port,
            {
                "node_id": node_id,
                "start": start_time,
                "end": end_time,
                "num_values": num_values,
                "severity_min": severity_min,
            },
        )
        return _history_result(result["records"], result["completeness"], "eventHistoryTruncated")
    except (ApplicationRefusal, AdapterFailure, LimitExceeded) as error:
        raise ToolError(str(error)) from error


# Tool: Report the connection and what the OPC UA server says about itself.
async def get_server_status(ctx: Context) -> CallToolResult:
    """Report status without joining an existing connection round.

    Capabilities are sampled after the liveness read, which may replace the
    native session and update its generation. Connection failure is a status
    record rather than a tool error.
    """
    state = _state(ctx)
    connection = ctx.request_context.lifespan_context["opcua_connection"]
    port = PythonOpcuaDiagnosticsPort(connection, lambda: capability_status(state.capabilities))
    return _object_result(
        await get_server_status_use_case(
            port, describe_security(security_config()), server_identity_record(state.policy.config)
        )
    )


# --- browsing --------------------------------------------------------------------


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


# --- writing ---------------------------------------------------------------------


async def write_opcua_nodes(nodes: list[dict[str, Any]], ctx: Context) -> list[dict]:
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
    port = PythonOpcuaMethodPort(ctx.request_context.lifespan_context["opcua_client"])
    try:
        return _object_result(await call_method(port, object_node_id, method_node_id, arguments))
    except (ApplicationRefusal, AdapterFailure) as error:
        raise ToolError(str(error)) from error


# --- data-change subscriptions ---------------------------------------------------


def _subscription_port(ctx: Context):
    state = _state(ctx)
    return PythonOpcuaSubscriptionPort(
        lambda: ctx.request_context.lifespan_context["opcua_client"],
        state.subscriptions,
        state.node_metadata,
    )


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
    """Watch nodes for changes, validating percent ranges before creating subscriptions."""
    if not node_ids:
        raise ToolError(
            error_message("emptyArray", tool="subscribe_opcua_nodes", argument="node_ids")
        )
    try:
        data_filter = resolve_filter(deadband_type, deadband_value, data_change_trigger)
        result = await subscribe_nodes(
            _subscription_port(ctx),
            node_ids,
            {
                "publishingInterval": publishing_interval,
                "samplingInterval": sampling_interval,
                "bufferSize": buffer_size,
            },
            data_filter,
        )
        return _records_result(result["records"], result["completeness"])
    except (ValueError, AdapterFailure) as error:
        raise ToolError(str(error)) from error


def list_subscriptions(ctx: Context) -> CallToolResult:
    """Read existing buffers even when the OPC UA connection is unavailable."""
    result = list_subscriptions_use_case(_subscription_port(ctx))
    return _records_result(result["records"], result["completeness"])


async def unsubscribe_opcua_nodes(subscription_ids: list[str], ctx: Context) -> CallToolResult:
    """Validate every subscription ID before cancelling any subscription."""
    if not subscription_ids:
        raise ToolError(
            error_message("emptyArray", tool="unsubscribe_opcua_nodes", argument="subscription_ids")
        )
    try:
        result = await unsubscribe_nodes(_subscription_port(ctx), subscription_ids)
        return _records_result(result["records"], result["completeness"])
    except (ApplicationRefusal, AdapterFailure) as error:
        raise ToolError(str(error)) from error


# --- events and Alarms & Conditions -----------------------------------------------


async def subscribe_events(
    ctx: Context,
    node_id: str = events.DEFAULT_NOTIFIER,
    severity_min: int = events.DEFAULTS["severityMin"],
    buffer_size: int = events.DEFAULTS["bufferSize"],
) -> CallToolResult:
    """Start buffering OPC UA events from a notifier node."""
    port = PythonOpcuaEventPort(
        ctx.request_context.lifespan_context["opcua_client"], _state(ctx).events
    )
    try:
        return _object_result(
            await subscribe_events_use_case(port, node_id, severity_min, buffer_size)
        )
    except AdapterFailure as error:
        raise ToolError(str(error)) from error


async def read_events(
    ctx: Context, node_id: str = events.DEFAULT_NOTIFIER, limit: int = events.DEFAULTS["readLimit"]
) -> CallToolResult:
    """Drain buffered events and report truncation, overflow and reconnect gaps."""
    port = PythonOpcuaEventPort(
        ctx.request_context.lifespan_context["opcua_client"], _state(ctx).events
    )
    try:
        result = await read_events_use_case(port, node_id, limit or events.DEFAULTS["readLimit"])
    except ApplicationRefusal as error:
        raise ToolError(str(error)) from error
    response = _records_result(result["records"], result["completeness"])
    response.content.extend(TextContent(type="text", text=text) for text in result["notices"])
    return response


def _alarm_port(ctx: Context):
    return PythonOpcuaAlarmPort(
        lambda: ctx.request_context.lifespan_context["opcua_client"], lambda: _state(ctx).events
    )


async def list_active_alarms(
    ctx: Context,
    node_id: str = events.DEFAULT_NOTIFIER,
    timeout_seconds: float = events.DEFAULTS["refreshTimeoutSeconds"],
) -> list[dict]:
    """List retained alarm/condition instances and remember their EventIds."""
    try:
        return await list_alarms(_alarm_port(ctx), node_id, timeout_seconds)
    except AdapterFailure as error:
        raise ToolError(str(error)) from error


async def acknowledge_alarm(
    event_id: str, ctx: Context, comment: str = "", condition_id: str | None = None
) -> CallToolResult:
    """Acknowledge an alarm, preserving the dedicated acknowledgement result shape."""
    try:
        return _object_result(
            await act_on_alarm_use_case(
                _alarm_port(ctx), event_id, "acknowledge", comment, None, condition_id, True
            )
        )
    except (ApplicationRefusal, AdapterFailure) as error:
        raise ToolError(str(error)) from error


async def act_on_alarm(
    event_id: str,
    action: str,
    ctx: Context,
    comment: str = "",
    shelve_duration_ms: float | None = None,
    condition_id: str | None = None,
) -> CallToolResult:
    """Apply an alarm action, retaining the action tool's result shape and frame."""
    try:
        return _object_result(
            await act_on_alarm_use_case(
                _alarm_port(ctx), event_id, action, comment, shelve_duration_ms, condition_id, False
            )
        )
    except (ApplicationRefusal, AdapterFailure) as error:
        raise ToolError(str(error)) from error


# --- building a server ------------------------------------------------------------

# Registration names are generated from the canonical contract (#138).


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
        return pretty_json({key: mcp.state.subscriptions.list()})

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
