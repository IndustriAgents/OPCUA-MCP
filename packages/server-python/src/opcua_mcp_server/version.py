"""The package version, read from the installed distribution metadata.

Single-sourced from ``pyproject.toml`` so it can never drift from what PyPI
publishes. Both the MCP handshake (``server``) and ``--version`` (``cli``) read
it from here.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

#: Reported when running from a source tree that was never installed.
UNKNOWN = "0.0.0+unknown"


def package_version() -> str:
    """Version of the installed distribution.

    Falls back to :data:`UNKNOWN` when the distribution metadata is absent, so
    importing never fails in a bare checkout.
    """
    try:
        return version("opcua-mcp-server")
    except PackageNotFoundError:
        return UNKNOWN
