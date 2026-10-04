"""One shared method-port table; preparation precedes one control service call."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import ROOT
from opcua import ua
from opcua_mcp_server.adapters.opcua_methods import PythonOpcuaMethodPort
from opcua_mcp_server.application.methods import call_method
from opcua_mcp_server.connection import is_connection_error
from opcua_mcp_server.errors import AdapterFailure, ApplicationRefusal

FIXTURE = json.loads((ROOT / "tests/fixtures/method-port.json").read_text(encoding="utf-8"))


class FakeMethodPort:
    def __init__(self, case):
        self.case = case
        self.calls = []

    async def input_types(self, node_id):
        self.calls.append(["metadata", node_id])
        if "metadataError" in self.case:
            raise RuntimeError(self.case["metadataError"])
        return self.case["declared"]

    async def call(self, object_id, method_id, arguments):
        self.calls.append(["call", object_id, method_id, arguments])
        if "refusal" in self.case:
            raise ApplicationRefusal(self.case["refusal"])
        if "callError" in self.case:
            raise TimeoutError(self.case["callError"])
        return {"status": self.case["status"], "outputs": self.case["outputs"]}


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
async def test_shared_method_port(case):
    port = FakeMethodPort(case)
    before = json.dumps(FIXTURE)
    if "error" in case:
        with pytest.raises((AdapterFailure, ApplicationRefusal)) as raised:
            await call_method(
                port, FIXTURE["objectNodeId"], FIXTURE["methodNodeId"], case["arguments"]
            )
        assert str(raised.value) == case["error"]
    else:
        result = await call_method(
            port, FIXTURE["objectNodeId"], FIXTURE["methodNodeId"], case["arguments"]
        )
        assert result == {
            "object_node_id": "ns=0;i=85",
            "method_node_id": FIXTURE["methodNodeId"],
            "status": case["status"],
            "outputs": case["outputs"],
        }
        assert port.calls[1][3] == case["typed"]
    assert port.calls[0] == ["metadata", FIXTURE["methodNodeId"]]
    assert len([c for c in port.calls if c[0] == "call"]) == (0 if "metadataError" in case else 1)
    assert json.dumps(FIXTURE) == before


async def test_native_timeout_retains_its_cause_without_another_call():
    error = TimeoutError("native timeout")
    calls = []

    def call(requests):
        calls.extend(requests)
        raise error

    client = SimpleNamespace(
        get_node=lambda value: SimpleNamespace(
            nodeid=ua.NodeId.from_string(value), server=SimpleNamespace(call=call)
        )
    )
    with pytest.raises(AdapterFailure) as raised:
        await PythonOpcuaMethodPort(client).call("i=85", "ns=2;i=20", [])
    assert raised.value.__cause__ is error
    assert is_connection_error(raised.value)
    assert len(calls) == 1
