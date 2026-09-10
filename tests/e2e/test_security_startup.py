"""Both servers refuse to start on a bad security configuration.

The unit suites cover the parsing rules; this covers the wiring around them —
that a misconfiguration reaches the process exit code and stderr instead of
being swallowed by a best-effort capability probe, which is all an MCP client
would have to go on.

No OPC UA server is needed: the configuration is rejected before connecting.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from conftest import ROOT

NODE_BUILD = ROOT / "packages" / "server-node" / "build" / "index.js"

# Asking for a policy without the certificate it needs — the misconfiguration an
# operator is most likely to hit when first turning security on.
BAD_ENV = {"OPCUA_SECURITY_POLICY": "Basic256Sha256"}

EXPECTED = (
    "Configuration error: OPCUA_SECURITY_POLICY=Basic256Sha256 requires OPCUA_CLIENT_CERT "
    "and OPCUA_CLIENT_KEY (paths to the client certificate and its private key)"
)


def _command(impl: str) -> list[str]:
    if impl == "python":
        return ["uv", "--directory", str(ROOT), "run", "--no-sync", "opcua-mcp-server"]
    if not NODE_BUILD.is_file():
        pytest.skip("Node server not built — run `npm run build` in packages/server-node")
    return ["node", str(NODE_BUILD)]


@pytest.mark.parametrize("impl", ["python", "node"])
def test_refuses_to_start_without_the_certificate_the_policy_needs(impl):
    result = subprocess.run(
        _command(impl),
        env={**os.environ, **BAD_ENV},
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode != 0
    # Both runtimes report it identically, and stdout stays clean for JSON-RPC.
    assert EXPECTED in result.stderr
    assert result.stdout == ""
