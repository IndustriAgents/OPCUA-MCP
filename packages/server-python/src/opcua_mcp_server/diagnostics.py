"""The health/diagnostics report behind ``get_server_status``.

``contract/tools.json`` -> ``resultShapes.serverStatus`` is the specification;
this module is the Python implementation of it and ``src/diagnostics.ts`` is the
Node one. Both must produce the same record against the same OPC UA server: an
agent that has learned one runtime's answer to "are we connected, to what, and is
it healthy?" has to be able to read the other's.
"""

from __future__ import annotations

from typing import Any

from .contract import NAMESPACE_ARRAY_NODE_ID, SERVER_STATUS_NODE_ID
from .datetimes import format_iso_utc


def disconnected_status(endpoint_url: str, security: str, error: str | None) -> dict:
    """The report for a connection that is not up: configuration, and why."""
    return {
        "connected": False,
        "endpoint_url": endpoint_url,
        "security": security,
        "server_state": None,
        "current_time": None,
        "start_time": None,
        "build_info": None,
        "namespaces": [],
        "error": error,
    }


def _text(value: Any) -> str:
    """A field the server may have left unset, as the string the contract promises.

    python-opcua hands back a ``LocalizedText`` for ProductName and
    ManufacturerName where node-opcua unwraps to a plain string, so the text is
    taken out of it here rather than stringified into ``LocalizedText(...)``.
    """
    if value is None:
        return ""
    text = getattr(value, "Text", None)
    if text is not None:
        return str(text)
    return str(value)


def _state_name(value: Any) -> str | None:
    """OPC UA's own name for a ServerState, e.g. 'Running'.

    python-opcua decodes the field as a ``ua.ServerState``, node-opcua as its
    numeric enum value; both runtimes report the *name*, so the answer does not
    depend on which client library read it.
    """
    if value is None:
        return None
    name = getattr(value, "name", None)
    return str(name) if name is not None else "Unknown"


def _build_info(raw: Any) -> dict | None:
    if raw is None:
        return None
    return {
        "product_name": _text(getattr(raw, "ProductName", None)),
        "product_uri": _text(getattr(raw, "ProductUri", None)),
        "manufacturer_name": _text(getattr(raw, "ManufacturerName", None)),
        "software_version": _text(getattr(raw, "SoftwareVersion", None)),
        "build_number": _text(getattr(raw, "BuildNumber", None)),
        "build_date": format_iso_utc(getattr(raw, "BuildDate", None)),
    }


def read_server_status(client, endpoint_url: str, security: str) -> dict:
    """Read ServerStatus and the NamespaceArray over a live connection.

    Both are mandatory nodes in OPC UA Part 5, so no browsing is needed to find
    them — the node IDs come from the shared contract, which is also where the
    Node server gets them.
    """
    status = client.get_node(SERVER_STATUS_NODE_ID).get_value()
    uris = client.get_node(NAMESPACE_ARRAY_NODE_ID).get_value()
    namespaces = [{"index": index, "uri": _text(uri)} for index, uri in enumerate(uris or [])]

    return {
        "connected": True,
        "endpoint_url": endpoint_url,
        "security": security,
        "server_state": _state_name(getattr(status, "State", None)),
        "current_time": format_iso_utc(getattr(status, "CurrentTime", None)),
        "start_time": format_iso_utc(getattr(status, "StartTime", None)),
        "build_info": _build_info(getattr(status, "BuildInfo", None)),
        "namespaces": namespaces,
        "error": None,
    }
