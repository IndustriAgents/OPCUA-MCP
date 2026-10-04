"""Existing application ports operate through maintained native services."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from opcua_mcp_server.adapters.asyncua_services import MaintainedClient
from opcua_mcp_server.adapters.asyncua_transport import BoundedProtocol
from opcua_mcp_server.adapters.opcua_browse import PythonOpcuaBrowsePort
from opcua_mcp_server.adapters.opcua_history import PythonOpcuaHistoryPort
from opcua_mcp_server.adapters.opcua_methods import PythonOpcuaMethodPort
from opcua_mcp_server.adapters.opcua_read import PythonOpcuaReadPort
from opcua_mcp_server.adapters.opcua_write import PythonOpcuaWritePort
from opcua_mcp_server.application.browse import browse_nodes
from opcua_mcp_server.application.history import read_history
from opcua_mcp_server.application.methods import call_method
from opcua_mcp_server.application.read import read_nodes
from opcua_mcp_server.application.write import write_nodes
from opcua_mcp_server.node_metadata import NodeMetadata
from opcua_mcp_server.subscriptions import Filter, SubscriptionManager


@asynccontextmanager
async def connected(url):
    client = MaintainedClient(url)
    try:
        await asyncio.to_thread(client.connect)
        assert isinstance(client.aio_obj.uaclient.protocol, BoundedProtocol)
        assert client.aio_obj._supervisor_task is None
        yield client
    finally:
        await asyncio.to_thread(client.disconnect)
    assert not client.tloop.is_alive()


@pytest.mark.asyncio
async def test_native_read_browse_engineering_and_inferred_write(opcua_server):
    async with connected(opcua_server) as client:
        metadata = NodeMetadata()
        records = await read_nodes(
            PythonOpcuaReadPort(client, metadata), ["ns=2;i=90", "ns=2;i=999999"], 100
        )
        assert records[0]["engineering"]["unit"] == "°C"
        assert records[0]["engineering"]["eu_range"] == {"low": 0.0, "high": 150.0}
        assert records[1]["status"] == "BadNodeIdUnknown"
        result = await browse_nodes(
            PythonOpcuaBrowsePort(client, {}),
            node_id="ns=2;i=1",
            browse_path=None,
            depth=2,
            node_class=None,
            name_filter=None,
            include_values=True,
            max_nodes=500,
        )
        assert any(record["node_id"] == "ns=2;i=3" for record in result["result"]["nodes"])
        changed = await write_nodes(
            PythonOpcuaWritePort(client, metadata),
            [{"node_id": "ns=2;i=41", "value": 12.5}],
            {"write": 100, "read": 100},
            {},
            False,
        )
        assert changed[0]["status"] == "Good"
        assert await asyncio.to_thread(client.get_node("ns=2;i=41").get_value) == 12.5


@pytest.mark.asyncio
async def test_native_method_metadata_and_raw_history(opcua_server):
    async with connected(opcua_server) as client:
        called = await call_method(
            PythonOpcuaMethodPort(client), "ns=2;i=27", "ns=2;s=EchoDuration", [2.5]
        )
        assert called["status"] == "Good"
        assert called["outputs"] == ["Double:2.5"]
        history = await read_history(
            PythonOpcuaHistoryPort(client, {}),
            {"node_id": "ns=2;i=3", "num_values": 5},
            [],
        )
        assert history["records"]
        assert len(history["records"]) <= 5
        assert all(record["status"] == "Good" for record in history["records"])


@pytest.mark.asyncio
async def test_native_filtered_subscription_can_be_sampled_and_deleted(opcua_server):
    async with connected(opcua_server) as client:
        manager = SubscriptionManager()
        manager.attach(client)
        try:
            record = await asyncio.to_thread(
                manager.subscribe,
                "ns=2;i=3",
                100.0,
                50.0,
                10,
                Filter(deadband_type="absolute", deadband_value=0.1),
            )
            deadline = asyncio.get_running_loop().time() + 5
            while not manager.list()[0]["changes"]:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.05)
            assert record["subscription_id"] == "sub-1"
            assert manager.list()[0]["changes"][0]["status"] == "Good"
        finally:
            await asyncio.to_thread(manager.close_all)
