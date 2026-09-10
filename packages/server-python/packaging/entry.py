"""Entry script for the PyInstaller build — see opcua-mcp-server.spec.

PyInstaller freezes a *script*, not a console-script entry point, so this is the
one-line equivalent of the ``opcua-mcp-server`` command.
"""

from opcua_mcp_server.cli import main

main()
