"""Qualify native maintained-client lifecycle before selecting it for MCP tools."""

from __future__ import annotations

import asyncio

import pytest
from opcua_mcp_server.adapters.asyncua_client import ApplicationOwnedClient
from opcua_mcp_server.adapters.asyncua_transport import BoundedProtocol


@pytest.mark.asyncio
async def test_native_connect_and_read_without_autonomous_replacement(opcua_server):
    client = ApplicationOwnedClient(opcua_server)
    try:
        await client.connect()
        assert isinstance(client.uaclient.protocol, BoundedProtocol)
        assert client._supervisor_task is None
        assert client._stale_watchdog_task is None
        assert not client._auto_reconnect
        assert client._renew_channel_task is not None
        assert not client._renew_channel_task.done()
        assert isinstance(await client.get_node("ns=2;i=3").read_value(), float)
        namespaces = await client.get_namespace_array()
        assert namespaces[0] == "http://opcfoundation.org/UA/"
    finally:
        await client.disconnect()
    assert client._renew_channel_task is None
    assert not client.uaclient.has_transport


@pytest.mark.asyncio
async def test_native_secure_channel_renews_without_replacing_the_session(opcua_server):
    client = ApplicationOwnedClient(opcua_server)
    client.secure_channel_timeout = 1000
    client.session_timeout = 10000
    try:
        await client.connect()
        protocol = client.uaclient.protocol
        connection = protocol._connection
        token = connection.security_token.TokenId
        session = client.uaclient.session
        deadline = asyncio.get_running_loop().time() + 5
        while connection.security_token.TokenId <= token:
            assert asyncio.get_running_loop().time() < deadline, "channel did not renew"
            await asyncio.sleep(0.05)
        assert isinstance(await client.get_node("ns=2;i=3").read_value(), float)
        assert connection.security_token.TokenId > token
        assert client.uaclient.session is session
        assert client.uaclient.protocol is protocol
        assert client._supervisor_task is None
        assert client._stale_watchdog_task is None
    finally:
        await client.disconnect()
