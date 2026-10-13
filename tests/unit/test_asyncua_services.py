"""Maintained service ownership and failures preserve the application boundary."""

from __future__ import annotations

import pytest
from asyncua import ua
from opcua import ua as legacy
from opcua_mcp_server.adapters import asyncua_services
from opcua_mcp_server.adapters.asyncua_client import ApplicationOwnedClient
from opcua_mcp_server.adapters.asyncua_services import MaintainedClient, _post
from opcua_mcp_server.connection import is_connection_error


def test_clients_own_distinct_sdk_loops_sessions_and_cleanup():
    first = MaintainedClient("opc.tcp://localhost:4840")
    second = MaintainedClient("opc.tcp://localhost:4841")
    try:
        assert first.tloop is not second.tloop
        assert first.aio_obj.uaclient.session is not second.aio_obj.uaclient.session
        assert first.uaclient.client is first.aio_obj.uaclient
        assert not first.tloop.is_alive()
        assert not second.tloop.is_alive()
        first.session_timeout = 10000
        first.secure_channel_timeout = 8000
        assert first.aio_obj.session_timeout == 10000
        assert first.aio_obj.secure_channel_timeout == 8000
        assert first.get_node("ns=2;i=3").server is first.uaclient
    finally:
        first.disconnect()
        second.disconnect()
    first.disconnect()
    assert not first.tloop.is_alive()
    assert not second.tloop.is_alive()


def test_failed_connect_preserves_status_cause_and_stops_owned_loop(monkeypatch):
    original = ua.UaStatusCodeError(ua.StatusCodes.BadSessionIdInvalid)

    class RefusingClient(ApplicationOwnedClient):
        async def connect(self, **kwargs):
            raise original

    monkeypatch.setattr(asyncua_services, "ApplicationOwnedClient", RefusingClient)
    client = MaintainedClient("opc.tcp://localhost:4840")
    with pytest.raises(legacy.UaStatusCodeError) as caught:
        client.connect()
    assert caught.value.code == original.code
    assert caught.value.__cause__ is original
    assert is_connection_error(caught.value)
    assert not client.tloop.is_alive()


@pytest.mark.parametrize(
    "code,dead", [(ua.StatusCodes.BadNodeIdUnknown, False), (ua.StatusCodes.BadSessionClosed, True)]
)
def test_numeric_native_status_translation_does_not_change_retry_policy(code, dead):
    client = MaintainedClient("opc.tcp://localhost:4840")

    async def failed():
        raise ua.UaStatusCodeError(code)

    try:
        with pytest.raises(legacy.UaStatusCodeError) as caught:
            _post(client.tloop, failed())
        assert caught.value.code == code
        assert is_connection_error(caught.value) is dead
    finally:
        client.disconnect()


def test_capability_browse_wraps_native_children_and_compares_ids_across_sdks(monkeypatch):
    from opcua_mcp_server.capabilities import client_aggregate_functions

    client = MaintainedClient("opc.tcp://localhost:4840")
    root = client.get_node("i=2997")
    child = client.aio_obj.get_node("i=2342")

    async def referenced(**kwargs):
        assert kwargs["direction"] is ua.BrowseDirection.Forward
        assert kwargs["refs"] == ua.ObjectIds.References
        return [child]

    async def name():
        return ua.QualifiedName("Average", 0)

    monkeypatch.setattr(root.aio_obj, "get_referenced_nodes", referenced)
    monkeypatch.setattr(child, "read_browse_name", name)
    monkeypatch.setattr(client, "get_node", lambda _: root)
    try:
        probe, functions = client_aggregate_functions(client)
        assert probe.support == "supported"
        assert functions["Average"].to_string() == "i=2342"
    finally:
        client.disconnect()
