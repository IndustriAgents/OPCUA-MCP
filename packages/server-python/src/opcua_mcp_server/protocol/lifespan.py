"""Non-blocking MCP lifespan, composed with injected native lifecycle callbacks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from ..connection import OpcuaConnection


@asynccontextmanager
async def opcua_lifespan(
    server: MCPServer, connect_and_probe, bind, release_opcua
) -> AsyncIterator[dict]:
    """Handle OPC UA client connection lifecycle."""
    state = server.state
    connection = OpcuaConnection(state.url, policy=state.policy)
    state.connection = connection
    context: dict = {
        "opcua_client": None,
        "opcua_connection": connection,
        "state": state,
    }
    connection.on_client_replaced = lambda client: bind(state, context, client)

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
    warm_up = state.start_warm_up(asyncio.to_thread(connect_and_probe, state, connection))

    try:
        yield context
    finally:
        # Stop the warm-up's backoff and wait out the attempt on the wire, before
        # anything is disconnected: a round that completed after the disconnect
        # would leave a session open behind a server that has stopped.
        connection.close()
        await asyncio.wait({warm_up})
        state.warm_up = None
        await asyncio.to_thread(release_opcua, state)
        state.forget_capabilities()
        state.connection = None
