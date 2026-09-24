"""What the connected OPC UA server says one service call may carry (issue #139).

``operation-limits.ts`` is the Node half. OPC UA Part 5 §6.3.11 has a server
publish its OperationLimits — how many nodes one Read, Write, Browse or
TranslateBrowsePathsToNodeIds may name — and a server is entitled to refuse a
request over them with BadTooManyOperations. Neither runtime asked, so a batch
that fitted this server's own caps could still be one the plant turned away
wholesale. The node ids are ``contract/tools.json`` -> ``operationLimits``.

Read once per session, beside the capability probes, and forgotten with the
session: a restarted server may be configured differently. What each tool does
with the answer — chunk a read, refuse a write — is the contract's
``operationLimits`` comment, and :func:`limits.effective_limit` is how the
server's number is combined with ours.
"""

from __future__ import annotations

from typing import Any

from opcua import ua

from .contract import CONTRACT
from .limits import MAX_NODES_PER_READ, MAX_NODES_PER_WRITE, effective_limit

_NODE_IDS: dict[str, str] = {
    key: value for key, value in CONTRACT["operationLimits"].items() if not key.startswith("$")
}

#: What is assumed before a session has been asked, and when asking fails.
UNSTATED: dict[str, int | None] = dict.fromkeys(_NODE_IDS)


def read_operation_limits(client: Any) -> dict[str, int | None]:
    """Ask the server, in one Read of the four nodes.

    Best-effort, like the capability probes: the nodes are optional in Part 5,
    and a server that does not publish one has stated no limit — which is
    ``None`` here, and which leaves the project limit in force. A failure of the
    read itself is the same answer, never a reason for a tool to fail.
    """
    limits = dict(UNSTATED)
    try:
        values = client.uaclient.get_attributes(
            [ua.NodeId.from_string(node_id) for node_id in _NODE_IDS.values()],
            ua.AttributeIds.Value,
        )
    except Exception:
        return limits
    for name, data_value in zip(_NODE_IDS, values, strict=False):
        status = getattr(data_value, "StatusCode", None)
        value = getattr(getattr(data_value, "Value", None), "Value", None)
        if (
            (status is None or status.is_good())
            and isinstance(value, int)
            and not isinstance(value, bool)
        ):
            limits[name] = value
    return limits


def read_chunk(server: dict[str, int | None], project: int = MAX_NODES_PER_READ) -> int:
    """How many nodes one Read may name, given what the server stated."""
    return effective_limit(project, server.get("maxNodesPerRead"))


def write_limit(server: dict[str, int | None]) -> int:
    """How many nodes one Write may name, given what the server stated."""
    return effective_limit(MAX_NODES_PER_WRITE, server.get("maxNodesPerWrite"))


def browse_chunk(server: dict[str, int | None], project: int) -> int:
    """How many nodes one Browse may name, given what the server stated."""
    return effective_limit(project, server.get("maxNodesPerBrowse"))


def translate_chunk(server: dict[str, int | None], project: int) -> int:
    """How many paths one TranslateBrowsePathsToNodeIds may carry."""
    return effective_limit(project, server.get("maxNodesPerTranslateBrowsePathsToNodeIds"))


__all__ = [
    "UNSTATED",
    "browse_chunk",
    "read_chunk",
    "read_operation_limits",
    "translate_chunk",
    "write_limit",
]
