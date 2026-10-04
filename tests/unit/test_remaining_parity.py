"""#157: actual capability, alarm and browse paths exercised without a server."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import ROOT
from opcua import ua
from opcua_mcp_server.adapters.opcua_browse import _fill_variable_detail
from opcua_mcp_server.capabilities import client_aggregate_functions
from opcua_mcp_server.events import alarm_action


def cases(name):
    return json.loads((ROOT / "tests/fixtures" / name).read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", cases("capability-probes.json"), ids=lambda c: c["name"])
def test_aggregate_discovery_checks_both_name_and_node_id(case):
    children = [
        SimpleNamespace(
            nodeid=ua.NodeId.from_string(ref["nodeId"]),
            get_browse_name=lambda ref=ref: ua.QualifiedName(ref["name"], 0),
        )
        for ref in case["references"]
    ]
    folder = SimpleNamespace(get_referenced_nodes=lambda **_: children)
    probe, functions = client_aggregate_functions(SimpleNamespace(get_node=lambda _: folder))
    assert list(functions) == case["expected"]
    assert probe.support == ("supported" if functions else "not_supported")


@pytest.mark.parametrize("case", cases("alarm-event-id.json"), ids=lambda c: c["name"])
def test_alarm_decodes_before_any_service_call(case, monkeypatch):
    import opcua_mcp_server.events as events

    sent = []
    node = SimpleNamespace(
        nodeid=ua.NodeId(7, 2),
        server=SimpleNamespace(
            call=lambda requests: (
                sent.extend(requests) or [SimpleNamespace(StatusCode=ua.StatusCode())]
            )
        ),
    )
    monkeypatch.setattr(
        events, "_action_method", lambda *_: SimpleNamespace(nodeid=ua.NodeId(9111))
    )
    touched = []

    def get(_):
        touched.append(True)
        return node

    client = SimpleNamespace(get_node=get)
    if "error" in case:
        with pytest.raises(ValueError) as error:
            alarm_action(client, "ns=2;i=7", case["eventId"], "acknowledge")
        assert str(error.value) == case["error"]
        assert not touched and not sent
    else:
        assert alarm_action(client, "ns=2;i=7", case["eventId"], "acknowledge") == "Good"
        assert sent[0].InputArguments[0].Value.hex() == case["hex"]


@pytest.mark.parametrize(
    "namespace,identifier,expected", [(0, 11, "Double"), (2, 11, None), (0, 9999, None)]
)
def test_browse_unreadable_value_keeps_only_known_standard_datatype(
    namespace, identifier, expected, monkeypatch
):
    import opcua_mcp_server.adapters.opcua_browse as browse_adapter

    def read(_client, _nodes, attribute, _chunk):
        if attribute == ua.AttributeIds.Value:
            return [ua.DataValue(None, ua.StatusCode(ua.StatusCodes.BadNotReadable))]
        if attribute == ua.AttributeIds.DataType:
            return [
                ua.DataValue(ua.Variant(ua.NodeId(identifier, namespace), ua.VariantType.NodeId))
            ]
        return [ua.DataValue(ua.Variant(ua.LocalizedText("sensor"), ua.VariantType.LocalizedText))]

    monkeypatch.setattr(browse_adapter, "_read_values", read)
    record = {
        "node_id": "ns=2;i=7",
        "node_class": "Variable",
        "value": None,
        "data_type": None,
        "description": None,
    }
    client = SimpleNamespace(get_node=lambda _: SimpleNamespace(nodeid=ua.NodeId(7, 2)))
    _fill_variable_detail(client, [record], {})
    assert record["value"] is None
    assert record["data_type"] == expected
    assert record["description"] == "sensor"
