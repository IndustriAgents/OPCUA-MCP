"""The running servers must report their manifest version in the MCP handshake.

The static half of this guard (manifests agreeing with each other, no hardcoded
literal in the Node source) is in unit/test_version_manifests.py and needs no
server.
"""

from __future__ import annotations

import json
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 only
    import tomli as tomllib

import pytest
from conftest import ROOT
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from test_mcp_e2e import NODE_BUILD, _server_params

NODE_PKG = ROOT / "packages" / "server-node" / "package.json"
PYTHON_PYPROJECT = ROOT / "packages" / "server-python" / "pyproject.toml"


def _expected_version(impl: str) -> str:
    if impl == "node":
        return json.loads(NODE_PKG.read_text(encoding="utf-8"))["version"]
    return tomllib.loads(PYTHON_PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_server_reports_manifest_version(impl, opcua_server):
    """Each running server reports its manifest version, under the shared name.

    Uses its own connection rather than the shared ``connect`` helper, because
    that helper discards the ``InitializeResult`` and a server may reject a
    second ``initialize`` on the same session.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")

    async with (
        stdio_client(_server_params(impl, opcua_server)) as (read, write),
        ClientSession(read, write) as session,
    ):
        info = (await session.initialize()).server_info

    assert info.name == "opcua-mcp-server", f"{impl} server identifies as {info.name!r}"
    assert info.version == _expected_version(impl), (
        f"{impl} reports {info.version!r}, manifest says {_expected_version(impl)!r}"
    )
