"""Instance-owned native connection binding, capability caches and cleanup."""

from __future__ import annotations

import sys

from ..capabilities import (
    CapabilityAnswers,
    answers_from,
    client_aggregate_functions,
    client_supports_history,
    client_supports_history_events,
    now_iso_utc,
)
from ..connection import OpcuaConnection, is_connection_error
from ..operation_limits import read_operation_limits
from ..state import ServerState


def _bind(state: ServerState, context: dict, client) -> None:
    """Point everything that holds a client at the one just established."""
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
    """Read what the server can do off a live client, and remember it."""
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
    """Ask the live server again, rebuilding the session first if it has died."""

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
    """Open the first connection and read its capabilities."""
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


def _release_opcua(state: ServerState) -> None:
    """Drop the subscriptions, then the session. In that order."""
    state.subscriptions.close_all()
    state.events.close_all()
    if state.connection is not None:
        state.connection.disconnect()
