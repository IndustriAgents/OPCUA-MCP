"""Read characterization and fake-port tests, independent of either transport."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import ROOT
from opcua import ua
from opcua_mcp_server.adapters.opcua_read import PythonOpcuaReadPort
from opcua_mcp_server.application.read import read_nodes
from opcua_mcp_server.connection import is_connection_error
from opcua_mcp_server.errors import AdapterFailure, ApplicationRefusal

FIXTURE = json.loads((ROOT / "tests/fixtures/read-port.json").read_text(encoding="utf-8"))


class FakeReadPort:
    def __init__(self):
        self.calls = []

    async def values(self, node_ids):
        self.calls.append(["values", node_ids])
        return [record for record in FIXTURE["records"] if record["node_id"] in node_ids]

    async def engineering(self, node_ids):
        self.calls.append(["engineering", node_ids])
        return FIXTURE["engineering"]


async def test_fake_port_preserves_batch_order_status_and_metadata():
    port = FakeReadPort()
    before = json.dumps(FIXTURE)
    actual = await read_nodes(port, FIXTURE["nodeIds"], FIXTURE["chunk"])
    expected = [
        dict(record, engineering=FIXTURE["engineering"].get(record["node_id"]))
        for record in FIXTURE["records"]
    ]
    assert actual == expected
    assert port.calls == FIXTURE["calls"]
    assert json.dumps(FIXTURE) == before


async def test_empty_reads_never_touch_the_port():
    port = FakeReadPort()
    with pytest.raises(ApplicationRefusal, match="node_ids"):
        await read_nodes(port, [], FIXTURE["chunk"])
    assert port.calls == []


async def test_adapter_translates_timeout_and_retains_retry_cause():
    error = TimeoutError("native timeout")

    def get_attributes(*_):
        raise error

    client = SimpleNamespace(
        get_node=lambda value: SimpleNamespace(nodeid=ua.NodeId.from_string(value)),
        uaclient=SimpleNamespace(get_attributes=get_attributes),
    )
    port = PythonOpcuaReadPort(client, None)
    with pytest.raises(AdapterFailure) as raised:
        await port.values(FIXTURE["nodeIds"])
    assert raised.value.operation == "read"
    assert raised.value.__cause__ is error
    assert is_connection_error(raised.value)
    assert str(raised.value) == "Failed to read nodes: native timeout"


async def test_absent_native_metadata_is_not_a_failed_read():
    metadata = SimpleNamespace(for_nodes=lambda *_: {FIXTURE["nodeIds"][0]: None})
    assert await PythonOpcuaReadPort(None, metadata).engineering(FIXTURE["nodeIds"]) == {
        FIXTURE["nodeIds"][0]: None
    }
