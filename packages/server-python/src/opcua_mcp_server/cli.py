"""The console-script entry point: dispatch CLI flags, or run the MCP server.

Importing :mod:`opcua_mcp_server.server` builds the whole MCP server — the SDK,
the OPC UA client library and every tool registration. That is the right thing
to do when starting the server and needless weight for ``--help``, ``--version``
or ``--install``. So that import happens here, late, and only on the serving
path.
"""

from __future__ import annotations

import sys

from .install import dispatch


def main() -> None:
    """Entry point for the ``opcua-mcp-server`` console script."""
    # Match Node byte output even through pipes on Windows or a non-UTF-8 locale.
    for stream in (sys.stdout, sys.stderr):
        configure = getattr(stream, "reconfigure", None)
        if configure is not None:
            configure(encoding="utf-8", newline="\n")
    code = dispatch()
    if code is not None:
        raise SystemExit(code)

    from .server import main as serve  # late: importing this probes the OPC UA server

    serve()


__all__ = ["main"]
