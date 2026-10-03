"""How a node's facts are read: batched, chunked, cached per session, never fatal.

The rules the facts feed are pinned by the shared tables (``write-plan.json`` and
the rest); what those cannot show is the reading itself — that eight attributes
of a whole batch go out as one Read, cut to the server's MaxNodesPerRead; that a
DataType is walked once per session; that an enumeration's labels are found on
the variable first and its DataType second; and that a failure is reported as
"unknown" rather than as a failed tool call, and is not remembered. The fake in
``fake_address_space.py`` answers like a server and counts what it was asked.
"""

from __future__ import annotations

import pytest
from fake_address_space import FakeAddressSpace
from opcua import ua
from opcua_mcp_server.node_facts import NodeFacts

SCRATCH = "ns=2;i=40"
METHODS = "ns=2;i=27"


def _space() -> FakeAddressSpace:
    space = FakeAddressSpace()
    space.add(SCRATCH, (2, "Scratch"), ua.NodeClass.Object, parent="ns=0;i=85")
    space.add(METHODS, (2, "Methods"), ua.NodeClass.Object, parent="ns=0;i=85")
    space.variable("ns=2;i=41", "ScratchDouble", SCRATCH)
    space.variable("ns=2;i=3", "Temperature", SCRATCH, access_level=5, user_access_level=5)
    return space


def _enumeration(space: FakeAddressSpace) -> None:
    """ServerState: an enumerated DataType whose labels live on the DataType node."""
    space.add("ns=0;i=29", (0, "Enumeration"), ua.NodeClass.DataType)
    space.add("ns=0;i=852", (0, "ServerState"), ua.NodeClass.DataType)
    space.link("ns=0;i=29", ua.ObjectIds.HasSubtype, "ns=0;i=852")
    space.property(
        "ns=0;i=852",
        "ns=0;i=7612",
        "EnumStrings",
        [ua.LocalizedText("Running"), ua.LocalizedText("Failed")],
    )
    space.variable("ns=2;i=102", "MachineState", SCRATCH, data_type="ns=0;i=852", value=0)


def test_a_writable_double_is_described_in_full():
    facts = NodeFacts().for_nodes(_space().client(), ["ns=2;i=41"])["ns=2;i=41"]
    assert facts == {
        "status": "Good",
        "node_class": "Variable",
        "data_type": "Double",
        "data_type_id": "ns=0;i=11",
        "enumeration": False,
        "value_rank": -1,
        "array_dimensions": None,
        "access_level": 3,
        "user_access_level": 3,
        "executable": None,
        "user_executable": None,
        "states": None,
        "two_state": None,
    }


def test_an_unknown_node_carries_only_its_status():
    facts = NodeFacts().for_nodes(_space().client(), ["ns=2;i=999"])["ns=2;i=999"]
    assert facts["status"] == "BadNodeIdUnknown"
    assert all(facts[key] is None for key in facts if key not in {"status", "enumeration"})


def test_an_attribute_a_node_class_lacks_is_null_not_an_error():
    """An Object has no AccessLevel and no DataType; python-opcua answers Bad for each."""
    facts = NodeFacts().for_nodes(_space().client(), [METHODS])[METHODS]
    assert facts["node_class"] == "Object"
    assert facts["access_level"] is None and facts["data_type"] is None


def test_a_method_reports_whether_it_may_be_executed():
    space = _space()
    space.add(
        "ns=2;i=109",
        (2, "DisabledMethod"),
        ua.NodeClass.Method,
        parent=METHODS,
        reference=ua.ObjectIds.HasComponent,
        executable=False,
        user_executable=False,
    )
    facts = NodeFacts().for_nodes(space.client(), ["ns=2;i=109"])["ns=2;i=109"]
    assert (facts["node_class"], facts["executable"], facts["user_executable"]) == (
        "Method",
        False,
        False,
    )


def test_an_array_reports_its_rank_and_dimensions():
    space = _space()
    space.variable("ns=2;i=108", "ScratchArray", SCRATCH, value_rank=1, array_dimensions=[3])
    facts = NodeFacts().for_nodes(space.client(), ["ns=2;i=108"])["ns=2;i=108"]
    assert (facts["value_rank"], facts["array_dimensions"]) == (1, [3])


def test_empty_array_dimensions_say_nothing():
    space = _space()
    space.variable("ns=2;i=108", "ScratchArray", SCRATCH, value_rank=1, array_dimensions=[])
    assert (
        NodeFacts().for_nodes(space.client(), ["ns=2;i=108"])["ns=2;i=108"]["array_dimensions"]
        is None
    )


def test_an_enumerations_labels_are_found_on_its_data_type():
    space = _space()
    _enumeration(space)
    facts = NodeFacts().for_nodes(space.client(), ["ns=2;i=102"])["ns=2;i=102"]
    assert (facts["data_type"], facts["enumeration"]) == ("Int32", True)
    assert facts["states"] == [{"value": 0, "label": "Running"}, {"value": 1, "label": "Failed"}]


def test_a_variables_own_labels_win_over_its_data_types():
    space = _space()
    _enumeration(space)
    space.property(
        "ns=2;i=102", "ns=2;i=900", "EnumStrings", [ua.LocalizedText("Idle"), ua.LocalizedText("")]
    )
    facts = NodeFacts().for_nodes(space.client(), ["ns=2;i=102"])["ns=2;i=102"]
    # The empty label names nothing a write could use, and is left out.
    assert facts["states"] == [{"value": 0, "label": "Idle"}]


def test_enum_values_keep_their_own_numbers():
    space = _space()
    gaps = []
    for value, label in ((1, "Low"), (5, "High")):
        item = ua.EnumValueType()
        item.Value = value
        item.DisplayName = ua.LocalizedText(label)
        gaps.append(item)
    space.variable("ns=2;i=103", "Mode", SCRATCH, data_type="ns=0;i=7", value=1)
    space.property("ns=2;i=103", "ns=2;i=901", "EnumValues", gaps)
    facts = NodeFacts().for_nodes(space.client(), ["ns=2;i=103"])["ns=2;i=103"]
    assert facts["states"] == [{"value": 1, "label": "Low"}, {"value": 5, "label": "High"}]


def test_a_two_state_node_names_both_states():
    space = _space()
    space.variable("ns=2;i=105", "DoorLock", SCRATCH, data_type="ns=0;i=1", value=False)
    space.property("ns=2;i=105", "ns=2;i=106", "TrueState", ua.LocalizedText("Locked"))
    space.property("ns=2;i=105", "ns=2;i=107", "FalseState", ua.LocalizedText("Unlocked"))
    facts = NodeFacts().for_nodes(space.client(), ["ns=2;i=105"])["ns=2;i=105"]
    assert facts["two_state"] == {"true": "Locked", "false": "Unlocked"}


def test_a_double_is_not_asked_for_enumeration_properties():
    """Only Boolean, integer and enumerated Variables can carry states."""
    space = _space()
    NodeFacts().for_nodes(space.client(), ["ns=2;i=41"])
    assert space.translate_sizes == []


def test_a_warm_cache_asks_nothing():
    space = _space()
    facts = NodeFacts()
    client = space.client()
    first = facts.for_nodes(client, ["ns=2;i=41", "ns=2;i=3"])
    asked = len(space.read_sizes)
    assert facts.for_nodes(client, ["ns=2;i=3", "ns=2;i=41"]) == {
        "ns=2;i=3": first["ns=2;i=3"],
        "ns=2;i=41": first["ns=2;i=41"],
    }
    assert len(space.read_sizes) == asked


def test_the_attribute_read_is_cut_to_the_servers_limit():
    space = _space()
    facts = NodeFacts()
    facts.server_limits = {**facts.server_limits, "maxNodesPerRead": 5}
    facts.for_nodes(space.client(), ["ns=2;i=41", "ns=2;i=3"])
    # Eight attributes for each of two nodes, five to a Read.
    assert space.read_sizes == [5, 5, 5, 1]


def test_a_data_type_is_walked_once_per_session():
    space = _space()
    _enumeration(space)
    space.variable("ns=2;i=110", "OtherState", SCRATCH, data_type="ns=0;i=852", value=0)
    facts = NodeFacts()
    facts.for_nodes(space.client(), ["ns=2;i=102"])
    facts.for_nodes(space.client(), ["ns=2;i=110"])
    assert space.supertype_lookups == ["ns=0;i=852"]


def test_forgetting_drops_everything():
    space = _space()
    facts = NodeFacts()
    facts.for_nodes(space.client(), ["ns=2;i=41"])
    facts.forget()
    assert facts.cached("ns=2;i=41") is None
    facts.for_nodes(space.client(), ["ns=2;i=41"])
    assert len(space.read_sizes) == 2


def test_a_failed_read_is_unknown_and_is_not_remembered(capsys):
    space = _space()
    space.fail["read"] = ConnectionResetError("the session died")
    facts = NodeFacts()
    assert facts.for_nodes(space.client(), ["ns=2;i=41"]) == {"ns=2;i=41": None}
    assert "Could not read node attributes" in capsys.readouterr().err
    del space.fail["read"]
    assert facts.for_nodes(space.client(), ["ns=2;i=41"])["ns=2;i=41"]["status"] == "Good"


def test_a_type_walk_that_fails_is_not_remembered():
    space = _space()
    _enumeration(space)
    space.fail["references"] = ConnectionResetError("the session died")
    facts = NodeFacts()
    assert facts.for_nodes(space.client(), ["ns=2;i=102"])["ns=2;i=102"]["data_type"] is None
    assert facts.cached("ns=2;i=102") is None
    del space.fail["references"]
    assert facts.for_nodes(space.client(), ["ns=2;i=102"])["ns=2;i=102"]["data_type"] == "Int32"


def test_a_server_that_cannot_translate_is_not_asked_again_this_session():
    space = _space()
    _enumeration(space)
    space.fail["translate"] = ValueError("BadServiceUnsupported")
    facts = NodeFacts()
    first = facts.for_nodes(space.client(), ["ns=2;i=102"])["ns=2;i=102"]
    assert first["states"] is None and first["data_type"] == "Int32"
    space.variable("ns=2;i=110", "OtherState", SCRATCH, data_type="ns=0;i=852", value=0)
    facts.for_nodes(space.client(), ["ns=2;i=110"])
    assert space.translate_sizes == []
    # A new session asks again.
    facts.forget()
    del space.fail["translate"]
    assert facts.for_nodes(space.client(), ["ns=2;i=102"])["ns=2;i=102"]["states"] is not None


def test_a_connection_error_while_translating_is_not_remembered_as_unanswerable():
    space = _space()
    _enumeration(space)
    space.fail["translate"] = ConnectionResetError("the session died")
    facts = NodeFacts()
    facts.for_nodes(space.client(), ["ns=2;i=102"])
    assert facts.cached("ns=2;i=102") is None
    del space.fail["translate"]
    assert facts.for_nodes(space.client(), ["ns=2;i=102"])["ns=2;i=102"]["states"] is not None


def test_a_string_that_is_not_a_node_id_gets_no_facts():
    facts = NodeFacts().for_nodes(_space().client(), ["not a node id"])
    assert facts == {"not a node id": None}


# --- whether a method belongs to an object ---------------------------------------


def _methods() -> FakeAddressSpace:
    space = _space()
    space.add("ns=0;i=58", (0, "BaseObjectType"), ua.NodeClass.ObjectType)
    space.add("ns=2;i=500", (2, "PumpType"), ua.NodeClass.ObjectType)
    space.link("ns=0;i=58", ua.ObjectIds.HasSubtype, "ns=2;i=500")
    space.add("ns=2;i=600", (2, "PumpSubType"), ua.NodeClass.ObjectType)
    space.link("ns=2;i=500", ua.ObjectIds.HasSubtype, "ns=2;i=600")
    space.add(
        "ns=2;i=28",
        (2, "StartProduction"),
        ua.NodeClass.Method,
        parent=METHODS,
        reference=ua.ObjectIds.HasComponent,
    )
    space.add(
        "ns=2;i=501",
        (2, "Prime"),
        ua.NodeClass.Method,
        parent="ns=2;i=500",
        reference=ua.ObjectIds.HasComponent,
    )
    space.add(
        "ns=2;i=7",
        (2, "Pump"),
        ua.NodeClass.Object,
        parent="ns=0;i=85",
        type_definition="ns=2;i=600",
    )
    return space


@pytest.mark.parametrize(
    ("object_id", "method_id", "expected"),
    [
        (METHODS, "ns=2;i=28", True),
        ("ns=2;i=7", "ns=2;i=501", True),
        (SCRATCH, "ns=2;i=28", False),
        ("ns=2;i=7", "ns=2;i=28", False),
    ],
    ids=["its own component", "its supertype's", "another object's", "not on the type either"],
)
def test_a_method_is_looked_for_on_the_object_and_up_its_type(object_id, method_id, expected):
    assert NodeFacts().on_object(_methods().client(), object_id, method_id) is expected


def test_ownership_that_could_not_be_browsed_is_unknown_and_not_remembered():
    space = _methods()
    space.browse_status["ns=2;i=7"] = "BadUserAccessDenied"
    facts = NodeFacts()
    assert facts.on_object(space.client(), "ns=2;i=7", "ns=2;i=501") is None
    del space.browse_status["ns=2;i=7"]
    assert facts.on_object(space.client(), "ns=2;i=7", "ns=2;i=501") is True


def test_ownership_is_asked_once_per_pair_per_session():
    space = _methods()
    facts = NodeFacts()
    facts.on_object(space.client(), METHODS, "ns=2;i=28")
    asked = len(space.browse_sizes)
    facts.on_object(space.client(), METHODS, "ns=2;i=28")
    assert len(space.browse_sizes) == asked


def test_ownership_follows_continuation_points():
    space = _methods()
    space.page_size = 1
    assert NodeFacts().on_object(space.client(), "ns=2;i=7", "ns=2;i=501") is True
