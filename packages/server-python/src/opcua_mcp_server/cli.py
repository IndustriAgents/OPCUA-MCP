"""The console-script entry point: dispatch CLI flags, or run the MCP server.

Importing :mod:`opcua_mcp_server.server` connects to the configured OPC UA
endpoint to probe which capability-gated tools to register. That is the right
thing to do when starting the server and the wrong thing to do for ``--help``,
``--version`` or ``--install``, which would otherwise sit through a connection
timeout just to print a config file. So that import happens here, late, and only
on the serving path.
"""

from __future__ import annotations

from .install import dispatch


def main() -> None:
    """Entry point for the ``opcua-mcp-server`` console script."""
    code = dispatch()
    if code is not None:
        raise SystemExit(code)

    from .server import main as serve  # late: importing this probes the OPC UA server

    serve()


__all__ = ["main"]
