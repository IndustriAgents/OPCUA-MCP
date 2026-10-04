"""Backend selection is explicit, fail-closed, and isolates legacy monkey patches."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from opcua import Client
from opcua_mcp_server import security
from opcua_mcp_server.adapters import asyncua_services
from opcua_mcp_server.adapters.asyncua_services import MaintainedClient
from opcua_mcp_server.python_backend import parse_python_backend


@pytest.mark.parametrize("backend", ["legacy", "asyncua"])
def test_selected_factory_preserves_identity_without_starting_a_thread(monkeypatch, backend):
    monkeypatch.setenv("OPCUA_PYTHON_BACKEND", backend)
    monkeypatch.setattr(
        security, "security_config", lambda: security.SecurityConfig("None", "None")
    )
    client = security.create_client("opc.tcp://localhost:4840")
    assert isinstance(client, MaintainedClient if backend == "asyncua" else Client)
    assert client.application_name == "OPC UA MCP Client"
    if backend == "asyncua":
        assert not client.tloop.is_alive()
        client.disconnect()


def test_invalid_backend_refuses_before_constructing_a_client(monkeypatch):
    monkeypatch.setenv("OPCUA_PYTHON_BACKEND", "typo")
    monkeypatch.setattr(security, "Client", lambda _: pytest.fail("constructed a client"))
    with pytest.raises(ValueError, match="Invalid OPCUA_PYTHON_BACKEND"):
        security.create_client("opc.tcp://localhost:4840")
    assert parse_python_backend({}) == "asyncua"


def test_security_setup_failure_closes_maintained_client_and_keeps_original_failure(monkeypatch):
    closed = []

    class RefusingClient:
        def __init__(self, url):
            pass

        def set_security(self, *args, **kwargs):
            raise RuntimeError("security setup refused")

        def disconnect(self):
            closed.append(True)

    monkeypatch.setenv("OPCUA_PYTHON_BACKEND", "asyncua")
    monkeypatch.setattr(asyncua_services, "MaintainedClient", RefusingClient)
    monkeypatch.setattr(
        security,
        "security_config",
        lambda: security.SecurityConfig(
            "Basic256Sha256", "SignAndEncrypt", "missing.pem", "missing.key"
        ),
    )
    with pytest.raises(RuntimeError, match="security setup refused"):
        security.create_client("opc.tcp://localhost:4840")
    assert closed == [True]


@pytest.mark.parametrize("backend,patched", [("legacy", True), ("asyncua", False)])
def test_only_rollback_factory_installs_legacy_receive_patch(backend, patched):
    program = """
from opcua_mcp_server import connection, security
from opcua.common.connection import SecureConnection
assert not hasattr(SecureConnection._receive, '__wrapped__')
client = security.create_client('opc.tcp://localhost:4840')
print(hasattr(SecureConnection._receive, '__wrapped__'))
"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("OPCUA_")}
    env["OPCUA_PYTHON_BACKEND"] = backend
    result = subprocess.run(
        [sys.executable, "-c", program], env=env, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(patched)


def test_legacy_trust_store_refuses_before_constructing_a_client(monkeypatch):
    monkeypatch.setenv("OPCUA_PYTHON_BACKEND", "legacy")
    monkeypatch.setattr(
        security,
        "security_config",
        lambda: security.SecurityConfig(
            "Basic256Sha256",
            "SignAndEncrypt",
            server_trust_store="/pki/trust",
            server_application_uri="urn:server",
        ),
    )
    monkeypatch.setattr(security, "Client", lambda _: pytest.fail("constructed a client"))
    with pytest.raises(ValueError, match="requires OPCUA_PYTHON_BACKEND=asyncua"):
        security.create_client("opc.tcp://localhost:4840")
