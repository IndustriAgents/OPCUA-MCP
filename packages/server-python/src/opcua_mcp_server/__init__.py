"""OPC UA MCP server (Python runtime).

Exposes OPC UA read/write/browse/method/history operations as MCP tools. The
tool surface is defined once in ``contract/tools.json`` and shared with the Node
runtime; see ``contract.py``.

Importing this package connects to the configured OPC UA server once, to probe
which capability-gated tools to register (see ``capabilities``).
"""

from __future__ import annotations

from .config import SERVER_URL
from .contract import CONTRACT, DESC, HISTORY_NODE_ID, load_contract
from .datetimes import format_iso_utc, parse_iso_datetime
from .records import history_record, history_records, scalar_to_json, variant_to_json
from .security import SecurityConfig, create_client, parse_security_config, security_config
from .server import main, mcp

__all__ = [
    "CONTRACT",
    "DESC",
    "HISTORY_NODE_ID",
    "SERVER_URL",
    "SecurityConfig",
    "create_client",
    "format_iso_utc",
    "history_record",
    "history_records",
    "load_contract",
    "main",
    "mcp",
    "parse_iso_datetime",
    "parse_security_config",
    "scalar_to_json",
    "security_config",
    "variant_to_json",
]
