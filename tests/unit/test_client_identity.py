"""Identity conflicts fail before any client connects; wire requests share a fixture."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import ROOT
from opcua_mcp_server import connection, security
from opcua_mcp_server.client_identity import CLIENT_APPLICATION_NAME, application_uri_problem
from opcua_mcp_server.config import ReconnectConfig

FIXTURE = json.loads((ROOT / "tests/fixtures/client-identity.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_uri_conflicts(case):
    assert application_uri_problem(case["configured"], case["certificateUri"]) == case["problem"]


def test_real_certificate_and_application_description(monkeypatch):
    cert = str(ROOT / FIXTURE["certificate"])
    assert security.certificate_application_uri(cert) == FIXTURE["certificateUri"]
    security.security_config.cache_clear()
    monkeypatch.setattr(
        security,
        "security_config",
        lambda: security.parse_security_config(
            {"OPCUA_CLIENT_CERT": cert, "OPCUA_CLIENT_KEY": cert}
        ),
    )
    client = security.create_client("opc.tcp://localhost:4840")
    assert client.application_name == CLIENT_APPLICATION_NAME == FIXTURE["applicationName"]
    assert client.application_uri == FIXTURE["certificateUri"]


def test_channel_and_session_lifetimes_are_requested_before_connect(monkeypatch):
    client = SimpleNamespace()

    def connect():
        raise RuntimeError("stop before opening a socket")

    client.connect = connect
    monkeypatch.setattr(connection, "create_client", lambda _: client)
    conn = connection.OpcuaConnection(
        "opc.tcp://localhost:4840",
        config=ReconnectConfig(session_timeout_ms=FIXTURE["sessionTimeoutMs"]),
    )
    with pytest.raises(RuntimeError, match="stop before opening a socket"):
        conn._open()
    assert client.session_timeout == FIXTURE["sessionTimeoutMs"]
    assert client.secure_channel_timeout == FIXTURE["secureChannelLifetimeMs"]
