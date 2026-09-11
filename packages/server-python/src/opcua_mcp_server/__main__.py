"""``python -m opcua_mcp_server`` — the same entry point as the console script.

This is the form ``--install`` writes into a client config: it needs no ``PATH``
lookup, no executable bit and no active virtualenv, only the absolute path of the
interpreter the package is installed into.
"""

from __future__ import annotations

from .cli import main

main()
