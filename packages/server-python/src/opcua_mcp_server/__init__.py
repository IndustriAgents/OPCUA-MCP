"""OPC UA MCP server (Python runtime).

Exposes OPC UA read/write/browse/method/history operations as MCP tools. The
tool surface is defined once in ``contract/tools.json`` and shared with the Node
runtime; see ``contract.py``.

Importing this package is side-effect free. ``main`` and ``mcp`` are resolved
lazily (PEP 562) because reaching them imports ``server``, and *that* connects to
the configured OPC UA server once to probe which capability-gated tools to
register (see ``capabilities``). Pure helpers — records, datetimes, the contract —
are importable without a server anywhere in sight.
"""

from __future__ import annotations

from .config import SERVER_URL
from .contract import CONTRACT, DESC, HISTORY_NODE_ID, load_contract
from .datetimes import format_iso_utc, parse_iso_datetime
from .records import history_record, history_records, scalar_to_json, variant_to_json
from .security import (
    SecurityConfig,
    certificate_application_uri,
    create_client,
    parse_security_config,
    security_config,
)
from .version import package_version

__all__ = [
    "CONTRACT",
    "DESC",
    "HISTORY_NODE_ID",
    "SERVER_URL",
    "SecurityConfig",
    "certificate_application_uri",
    "create_client",
    "format_iso_utc",
    "history_record",
    "history_records",
    "load_contract",
    "main",
    "mcp",
    "package_version",
    "parse_iso_datetime",
    "parse_security_config",
    "scalar_to_json",
    "security_config",
    "variant_to_json",
]


def __getattr__(name: str):
    """Resolve ``main`` and ``mcp`` on first use, not at import time.

    They live in ``server``, whose import probes the OPC UA endpoint. Keeping that
    behind an attribute lookup means ``import opcua_mcp_server`` costs nothing and
    the CLI can print ``--help`` without waiting on a connection timeout.
    """
    if name in ("main", "mcp"):
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
