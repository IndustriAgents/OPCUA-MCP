"""One fake-tree table characterizes traversal without MCP or a network."""

from __future__ import annotations

import copy
import json

import pytest
from conftest import ROOT
from opcua_mcp_server.application.browse import browse_nodes
from opcua_mcp_server.errors import AdapterFailure, ApplicationRefusal

FIXTURE = json.loads((ROOT / "tests/fixtures/browse-port.json").read_text(encoding="utf-8"))


class FakeBrowsePort:
    def __init__(self, blocked=()):
        self.blocked = blocked
        self.enriched = []

    async def children(self, node_id):
        if node_id in self.blocked:
            raise RuntimeError("blocked")
        return copy.deepcopy(FIXTURE["graph"].get(node_id, []))

    async def describe(self, node_id, parent_node_id):
        ref = next(
            (r for refs in FIXTURE["graph"].values() for r in refs if r["nodeId"] == node_id),
            {"namespaceIndex": 2, "name": "Plant", "nodeClass": "Object"},
        )
        return {
            "node_id": node_id,
            "browse_name": f"{ref['namespaceIndex']}:{ref['name']}",
            "node_class": ref["nodeClass"],
            "parent_node_id": parent_node_id,
            "data_type": None,
            "value": None,
            "description": None,
            "type_definition": None,
        }

    async def enrich(self, records, include_values):
        self.enriched.append([r["node_id"] for r in records])
        for record in records:
            record["type_definition"] = "MockType"
            if include_values and record["node_class"] == "Variable":
                record.update(value=41.5, data_type="Double", description="Température °C")


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
async def test_shared_fake_tree(case):
    before = json.dumps(FIXTURE)
    port = FakeBrowsePort(case.get("blocked", []))
    request = case["request"]
    kwargs = dict(
        node_id=request["nodeId"],
        browse_path=request.get("browsePath"),
        depth=request.get("depth", 1),
        node_class=request.get("nodeClass"),
        name_filter=request.get("nameFilter"),
        include_values=request.get("includeValues", False),
        max_nodes=request.get("maxNodes", 500),
    )
    if "error" in case:
        with pytest.raises((AdapterFailure, ApplicationRefusal)) as raised:
            await browse_nodes(port, **kwargs)
        assert str(raised.value) == case["error"]
        assert not port.enriched
    else:
        actual = await browse_nodes(port, **kwargs)
        assert [r["node_id"] for r in actual["result"]["nodes"]] == case["nodeIds"]
        assert actual["result"]["inspected"] == case["inspected"]
        assert actual["result"]["truncated"] == case["truncated"]
        assert actual["completeness"]["reasons"] == case["reasons"]
        assert actual["completeness"]["complete"] == (not case["reasons"])
        assert actual["completeness"]["returned"] == len(case["nodeIds"])
        assert port.enriched == [case["nodeIds"]]
        if request.get("includeValues"):
            assert actual["result"]["nodes"][0]["description"] == "Température °C"
    assert json.dumps(FIXTURE) == before


async def test_native_timeout_cause_survives_thread_transfer():
    from types import SimpleNamespace

    from opcua_mcp_server.adapters.opcua_browse import PythonOpcuaBrowsePort
    from opcua_mcp_server.connection import is_connection_error

    error = TimeoutError("native timeout")

    def get_node(_):
        raise error

    with pytest.raises(AdapterFailure) as raised:
        await PythonOpcuaBrowsePort(SimpleNamespace(get_node=get_node), {}).children("ns=2;i=1")
    assert raised.value.__cause__ is error
    assert is_connection_error(raised.value)
