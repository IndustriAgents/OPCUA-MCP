"""The maintained SDK produces the existing contract values and request bytes."""

from __future__ import annotations

import re

import pytest
from asyncua import ua
from asyncua.ua import ua_binary
from opcua import ua as legacy
from opcua.ua import ua_binary as legacy_binary
from opcua_mcp_server.adapters.asyncua_values import native_request, standard_fields
from opcua_mcp_server.records import variant_to_json
from test_records import CASES, NATIVE


@pytest.mark.parametrize("name", sorted(CASES))
def test_native_values_match_shared_contract(name):
    if name == "extension_cycle":
        pair = ua.KeyValuePair(Key=ua.QualifiedName("target", 2))
        pair.Value = ua.Variant(pair, ua.VariantType.ExtensionObject)
        value = ua.Variant(pair, ua.VariantType.ExtensionObject)
    else:
        value = native_request(NATIVE[name])
    assert isinstance(value, ua.Variant)
    encoded = variant_to_json(value)
    case = CASES[name]
    if "expectedPattern" in case:
        assert isinstance(encoded, str) and re.fullmatch(case["expectedPattern"], encoded)
    else:
        assert encoded == case["expected"]


@pytest.mark.parametrize(
    "name",
    [
        "BrowseParameters",
        "BrowseNextParameters",
        "ReadParameters",
        "WriteParameters",
        "CallMethodRequest",
        "BrowsePath",
        "HistoryReadParameters",
        "ReadRawModifiedDetails",
        "ReadProcessedDetails",
        "ReadEventDetails",
        "EventFilter",
        "DataChangeFilter",
        "CreateSubscriptionParameters",
        "CreateMonitoredItemsParameters",
    ],
)
def test_request_dtos_preserve_wire_fields(name):
    request = getattr(legacy, name)()
    converted = native_request(request)
    assert type(converted) is getattr(ua, name)
    assert ua_binary.to_binary(type(converted), converted) == legacy_binary.to_binary(name, request)


def test_batch_node_ids_and_attribute_enum_are_native():
    values = native_request([legacy.NodeId(42, 2), legacy.NodeId("temperature", 3)])
    assert values == [ua.NodeId(42, 2), ua.NodeId("temperature", 3)]
    assert native_request(legacy.AttributeIds.Value) is ua.AttributeIds.Value
    assert native_request(values[0]) is values[0]


def test_native_structure_array_remains_bounded(monkeypatch):
    from opcua_mcp_server.structures import CONTRACT

    monkeypatch.setitem(CONTRACT["limits"], "maxArrayItems", 1)
    assert variant_to_json(native_request(NATIVE["extension_argument"])) == {
        "$opcua": "undecodableExtensionObject"
    }


def test_nested_native_structure_respects_depth_limit(monkeypatch):
    from opcua_mcp_server.structures import CONTRACT

    monkeypatch.setitem(CONTRACT["limits"], "maxNestingDepth", 1)
    assert variant_to_json(native_request(NATIVE["extension_axis_information"])) == {
        "$opcua": "undecodableExtensionObject"
    }


def test_nonempty_service_requests_keep_ids_values_filters_and_continuations():
    read = legacy.ReadParameters()
    item = legacy.ReadValueId()
    item.NodeId = legacy.NodeId("temperature", 2)
    item.AttributeId = legacy.AttributeIds.Value
    read.NodesToRead = [item]
    write = legacy.WriteParameters()
    change = legacy.WriteValue()
    change.NodeId = item.NodeId
    change.AttributeId = legacy.AttributeIds.Value
    change.Value = legacy.DataValue(NATIVE["int64_beyond_double"])
    write.NodesToWrite = [change]
    call = legacy.CallMethodRequest()
    call.ObjectId, call.MethodId = legacy.NodeId(1, 2), legacy.NodeId(2, 2)
    call.InputArguments = [NATIVE["boolean"], NATIVE["string"]]
    next_page = legacy.BrowseNextParameters()
    next_page.ReleaseContinuationPoints = True
    next_page.ContinuationPoints = [b"opaque\x00continuation"]
    history = legacy.HistoryReadParameters()
    history.HistoryReadDetails = legacy.ReadProcessedDetails()
    history.HistoryReadDetails.AggregateType = [legacy.NodeId(2342)]
    history.HistoryReadDetails.ProcessingInterval = 1000.0
    history.ReleaseContinuationPoints = True
    for request in (read, write, call, next_page, history):
        converted = native_request(request)
        assert ua_binary.to_binary(type(converted), converted) == legacy_binary.to_binary(
            type(request).__name__, request
        )
    assert native_request(write).NodesToWrite[0].Value.Value.Value == 2**53 + 1
    assert native_request(call).InputArguments[0].VariantType is ua.VariantType.Boolean
    assert native_request(next_page).ContinuationPoints == [b"opaque\x00continuation"]


def test_server_defined_class_cannot_impersonate_a_standard_structure():
    impostor = type("Range", (), {"Low": -50, "High": 250})()
    assert standard_fields(impostor) is None
    assert variant_to_json(ua.Variant(impostor, ua.VariantType.ExtensionObject)) == {
        "$opcua": "undecodableExtensionObject"
    }
    with pytest.raises(TypeError, match="Unsupported internal UA request type"):
        native_request(impostor)


def test_local_request_can_reuse_a_node_id_from_a_native_response():
    request = legacy.CallMethodRequest()
    request.ObjectId = ua.NodeId("condition", 2)
    request.MethodId = ua.NodeId(9111)
    request.InputArguments = [legacy.Variant("café 🙂", legacy.VariantType.String)]
    converted = native_request(request)
    assert converted.ObjectId == request.ObjectId
    assert converted.MethodId == request.MethodId
    assert converted.InputArguments[0].Value == "café 🙂"


def test_expanded_node_id_preserves_namespace_uri_and_server_index():
    node_id = legacy.NodeId(42, 2)
    node_id.NamespaceUri = "urn:plant:measurements"
    node_id.ServerIndex = 3
    converted = native_request(node_id)
    assert isinstance(converted, ua.ExpandedNodeId)
    assert converted.NamespaceUri == node_id.NamespaceUri
    assert converted.ServerIndex == 3
    assert converted.Identifier == 42


def test_cyclic_local_request_is_refused_before_native_serialization():
    with pytest.raises(ValueError, match="DTO nesting limit"):
        native_request(NATIVE["extension_cycle"])
