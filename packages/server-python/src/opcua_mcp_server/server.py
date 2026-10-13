"""The MCP server: lifecycle, tool registration, and the stdio entry point."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from .adapters.opcua_lifecycle import _bind, _connect_and_probe, _fresh_capabilities, _release_opcua
from .audit import (
    AuditSink,
    describe_audit,
    parse_audit_config,
)
from .config import describe_reconnect, reconnect_config
from .contract import DESC, SUBSCRIPTIONS_RESOURCE
from .generated_contract import TOOL_NAMES
from .infrastructure.control_audit import (
    describe_targets as describe_targets,
)
from .infrastructure.control_audit import (
    new_call_id as new_call_id,
)
from .policy import (
    describe_policy,
)
from .protocol.lifespan import opcua_lifespan as _protocol_lifespan
from .protocol.server import PolicyMCPServer as _ProtocolPolicyMCPServer
from .protocol.signals import _exit_on_signal as _protocol_signals
from .protocol.tools import (
    acknowledge_alarm as acknowledge_alarm,
)
from .protocol.tools import (
    act_on_alarm as act_on_alarm,
)
from .protocol.tools import (
    browse_opcua_nodes as browse_opcua_nodes,
)
from .protocol.tools import (
    call_opcua_method as call_opcua_method,
)
from .protocol.tools import (
    get_server_status as get_server_status,
)
from .protocol.tools import (
    handler_for,
)
from .protocol.tools import (
    list_active_alarms as list_active_alarms,
)
from .protocol.tools import (
    list_subscriptions as list_subscriptions,
)
from .protocol.tools import (
    read_event_history as read_event_history,
)
from .protocol.tools import (
    read_events as read_events,
)
from .protocol.tools import (
    read_opcua_history as read_opcua_history,
)
from .protocol.tools import (
    read_opcua_nodes as read_opcua_nodes,
)
from .protocol.tools import (
    subscribe_events as subscribe_events,
)
from .protocol.tools import (
    subscribe_opcua_nodes as subscribe_opcua_nodes,
)
from .protocol.tools import (
    unsubscribe_opcua_nodes as unsubscribe_opcua_nodes,
)
from .protocol.tools import (
    write_opcua_nodes as write_opcua_nodes,
)
from .python_backend import python_backend
from .result_text import pretty_json
from .security import security_config
from .state import ServerState
from .version import package_version

#: How long a server stopped by a signal waits for the OPC UA side to close
#: cleanly: `SHUTDOWN_GRACE_MS` in the Node runtime's `index.ts`.
SHUTDOWN_GRACE_SECONDS = 5.0


@asynccontextmanager
async def opcua_lifespan(server: MCPServer):
    """Compose protocol lifespan with this module's injectable native hooks."""
    async with _protocol_lifespan(server, _connect_and_probe, _bind, _release_opcua) as context:
        yield context


def _exit_on_signal(state: ServerState):
    """Compose bounded protocol shutdown with native cleanup."""
    return _protocol_signals(state, _release_opcua)


class PolicyMCPServer(_ProtocolPolicyMCPServer):
    """Public construction point keeps lifecycle hooks replaceable per instance."""

    def __init__(self, *args, state: ServerState | None = None, **kwargs):
        super().__init__(
            *args,
            state=state,
            signal_installer=lambda selected: _exit_on_signal(selected),
            capability_probe=lambda *args: _fresh_capabilities(*args),
            **kwargs,
        )


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
        mcp.tool(description=DESC[name])(handler_for(name))

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
        python_backend()
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
