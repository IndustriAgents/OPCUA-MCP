"""The health/diagnostics report behind ``get_server_status``.

``contract/tools.json`` -> ``resultShapes.serverStatus`` is the specification;
this module is the Python implementation of it and ``src/diagnostics.ts`` is the
Node one. Both must produce the same record against the same OPC UA server: an
agent that has learned one runtime's answer to "are we connected, to what, and is
it healthy?" has to be able to read the other's.
"""

from __future__ import annotations

from typing import Any

from .contract import CONTRACT, NAMESPACE_ARRAY_NODE_ID, SERVER_STATUS_NODE_ID
from .datetimes import format_iso_utc

_DIAGNOSTICS = CONTRACT["diagnostics"]

#: The record's field order, from the contract. `diagnostics.ts` builds the same
#: keys in the same order — twelve counters reported in two different orders by
#: two servers would be two records, not one shape.
DIAGNOSTICS_FIELDS: tuple[str, ...] = tuple(_DIAGNOSTICS["diagnosticsFields"])

#: python-opcua decodes ServerDiagnosticsSummaryDataType with OPC UA's own
#: PascalCase field names; the record uses snake_case. Derived rather than
#: written out, so the two can only disagree if the spec's own spelling changes.
_SUMMARY_ATTRIBUTES = {
    field: "".join(part.capitalize() for part in field.split("_")) for field in DIAGNOSTICS_FIELDS
}


def disconnected_status(
    endpoint_url: str, security: str, server_identity: dict, error: str | None
) -> dict:
    """The report for a connection that is not up: configuration, and why.

    ``server_identity`` is reported here too: it comes from configuration, and
    "why are the control tools missing?" is as likely a question while the
    connection is down as while it is up.
    """
    return {
        "connected": False,
        "endpoint_url": endpoint_url,
        "security": security,
        "server_identity": server_identity,
        "server_state": None,
        "current_time": None,
        "start_time": None,
        "build_info": None,
        "diagnostics": None,
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


def _diagnostics_summary(client) -> dict | None:
    """The server's own ServerDiagnosticsSummary, or None if it publishes none.

    Best-effort by design, and `None` is a real answer rather than a failure:
    Part 5 lets a server leave diagnostics switched off, and python-opcua's own
    mock creates the node but never populates it. A server that cannot say how
    many sessions it is holding is still a server worth talking to, so this must
    never be the reason `get_server_status` fails — which is the one tool that
    has to answer when everything else is going wrong.
    """
    try:
        summary = client.get_node(_DIAGNOSTICS["serverDiagnosticsSummaryNodeId"]).get_value()
    except Exception:
        return None
    if summary is None:
        return None
    record = {}
    for field, attribute in _SUMMARY_ATTRIBUTES.items():
        value = getattr(summary, attribute, None)
        if value is None:
            # A structure that decoded but is missing a counter is not a summary
            # this server can report honestly, and a record with holes in it is
            # worse than no record.
            return None
        record[field] = int(value)
    return record


def read_server_status(client, endpoint_url: str, security: str, server_identity: dict) -> dict:
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
        "server_identity": server_identity,
        "server_state": _state_name(getattr(status, "State", None)),
        "current_time": format_iso_utc(getattr(status, "CurrentTime", None)),
        "start_time": format_iso_utc(getattr(status, "StartTime", None)),
        "build_info": _build_info(getattr(status, "BuildInfo", None)),
        "diagnostics": _diagnostics_summary(client),
        "namespaces": namespaces,
        "error": None,
    }
