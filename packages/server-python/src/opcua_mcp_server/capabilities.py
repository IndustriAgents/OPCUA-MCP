"""Runtime probes for optional OPC UA server capabilities.

Tools that depend on a capability are only registered when the connected
server actually advertises it, mirroring the Node server's gating.
"""

from __future__ import annotations

from opcua import ua

from .aggregates import spec_aggregate_node_ids
from .contract import AGGREGATE_NODE_ID, HISTORY_NODE_ID
from .security import create_client


def client_supports_history(client) -> bool:
    """Read history support through an already-connected client."""
    try:
        return bool(client.get_node(HISTORY_NODE_ID).get_value())
    except Exception:
        return False


def client_aggregate_functions(client) -> dict[str, ua.NodeId]:
    """Read the allowlisted aggregate functions through an existing session."""
    spec = spec_aggregate_node_ids()
    try:
        node = client.get_node(AGGREGATE_NODE_ID)
        advertised = {}
        for child in node.get_referenced_nodes(
            refs=ua.ObjectIds.References,
            direction=ua.BrowseDirection.Forward,
        ):
            try:
                name = child.get_browse_name().Name
            except Exception:
                continue
            if name in spec and child.nodeid == spec[name]:
                advertised[name] = child.nodeid
        return advertised
    except Exception:
        return {}


def server_supports_history(url: str) -> bool:
    """Probe the server's AccessHistoryDataCapability (ns=0;i=11193).

    Used to expose `read_history_opcua_node` only when the server actually
    supports historical reads, matching the Node server's behaviour.
    """
    try:
        probe = create_client(url)
        probe.connect()
        try:
            return client_supports_history(probe)
        finally:
            probe.disconnect()
    except Exception:
        return False


def server_aggregate_functions(url: str) -> dict[str, ua.NodeId]:
    """The aggregate functions the server advertises, mapped to their node IDs.

    Browses ``Server/ServerCapabilities/AggregateFunctions`` and keeps only
    children whose browse name *and* node ID match a spec-defined aggregate, so a
    server exposing something unexpected under that folder cannot smuggle in a
    node ID we then send back in a request.

    Best-effort: any failure yields an empty mapping rather than an error, so a
    transient outage leaves the core tools advertised instead of breaking
    tools/list.
    """
    try:
        probe = create_client(url)
        probe.connect()
        try:
            return client_aggregate_functions(probe)
        finally:
            probe.disconnect()
    except Exception:
        return {}
