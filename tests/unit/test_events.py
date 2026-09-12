"""Unit tests for the Python server's event logic — no OPC UA server needed.

The end-to-end tests prove the two runtimes agree against a real server; these
cover the parts that decide *what is asked for* and *what is kept*: the
EventFilter select clauses, the record mapping, and the buffer's severity floor
and overflow behaviour. The Node server's equivalents are asserted in
packages/server-node/test/unit.test.mjs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from conftest import ROOT
from opcua import ua
from opcua_mcp_server.events import (
    DEFAULTS,
    EventSubscriptions,
    _BufferingHandler,
    event_filter,
    event_record,
)

CONTRACT = json.loads((ROOT / "contract" / "tools.json").read_text())
EVENTS = CONTRACT["events"]
FIELD_KEYS = [field["key"] for field in EVENTS["fields"]]
PATH_OF = {field["key"]: field["path"] for field in EVENTS["fields"]}

TIMESTAMP = datetime(2026, 9, 9, 13, 36, 1, 468000, tzinfo=timezone.utc)


def variants(**values) -> list:
    """Event field values, in the contract's order, as python-opcua Variants.

    Anything not named is Null — which is exactly what a server sends for a
    condition field of an event that is not a condition.
    """
    return [values.get(key, ua.Variant(None, ua.VariantType.Null)) for key in FIELD_KEYS]


def event(**values):
    """A stand-in for python-opcua's Event — the one attribute the mapper reads."""
    return type("Event", (), {"event_fields": variants(**values)})()


def alarm_event(severity: int = 700, **overrides):
    """A condition-shaped event, of the kind an alarm server sends."""
    fields = {
        "event_id": ua.Variant(b"\x01\x02", ua.VariantType.ByteString),
        "event_type": ua.Variant(ua.NodeId(9341), ua.VariantType.NodeId),
        "source_node": ua.Variant(ua.NodeId(1001, 1), ua.VariantType.NodeId),
        "source_name": ua.Variant("Temperature", ua.VariantType.String),
        "time": ua.Variant(TIMESTAMP, ua.VariantType.DateTime),
        "message": ua.Variant(ua.LocalizedText("Condition is High"), ua.VariantType.LocalizedText),
        "severity": ua.Variant(severity, ua.VariantType.UInt16),
        "condition_id": ua.Variant(ua.NodeId(1002, 1), ua.VariantType.NodeId),
        "condition_name": ua.Variant("HighTemperatureAlarm", ua.VariantType.String),
        "active": ua.Variant(True, ua.VariantType.Boolean),
        "acked": ua.Variant(False, ua.VariantType.Boolean),
        "retain": ua.Variant(True, ua.VariantType.Boolean),
    }
    fields.update(overrides)
    return event(**fields)


# --- the filter ----------------------------------------------------------------


def test_one_select_clause_per_contract_field_in_order():
    """The record's field order *is* the select clause order; nothing may shift."""
    clauses = event_filter().SelectClauses
    assert len(clauses) == len(EVENTS["fields"])


def test_condition_id_is_the_node_id_attribute_of_the_condition_type():
    """Part 9's exception: ConditionId is not a component, it is the instance.

    Selecting it as a browse path — the obvious reading — returns null from every
    server, and `acknowledge_alarm` then has nothing to call the method on.
    """
    index = FIELD_KEYS.index("condition_id")
    clause = event_filter().SelectClauses[index]
    assert clause.BrowsePath == []
    assert clause.AttributeId == ua.AttributeIds.NodeId
    assert clause.TypeDefinitionId == ua.NodeId.from_string(EVENTS["conditionTypeNodeId"])


@pytest.mark.parametrize("key", [k for k in FIELD_KEYS if k != "condition_id"])
def test_every_other_field_is_a_value_browse_path_on_base_event_type(key):
    """BaseEventType, so the server resolves the path without regard to the type.

    That is what lets one filter select `AckedState/Id` from a condition and get
    null — rather than an error — from a plain event (Part 4 §7.4.4.5).
    """
    clause = event_filter().SelectClauses[FIELD_KEYS.index(key)]
    assert clause.AttributeId == ua.AttributeIds.Value
    assert clause.TypeDefinitionId == ua.NodeId.from_string(EVENTS["baseEventTypeNodeId"])
    assert [q.Name for q in clause.BrowsePath] == PATH_OF[key].split(".")


def test_a_two_step_path_stays_two_qualified_names():
    """`AckedState.Id` is the boolean; `AckedState` alone is the display text."""
    clause = event_filter().SelectClauses[FIELD_KEYS.index("acked")]
    assert [q.Name for q in clause.BrowsePath] == ["AckedState", "Id"]


# --- the record ----------------------------------------------------------------


def test_a_condition_event_maps_to_the_canonical_record():
    assert event_record(alarm_event().event_fields) == {
        "event_id": "AQI=",
        "event_type": "ns=0;i=9341",
        "source_node": "ns=1;i=1001",
        "source_name": "Temperature",
        "time": "2026-09-09T13:36:01.468000Z",
        "message": "Condition is High",
        "severity": 700,
        "condition_id": "ns=1;i=1002",
        "condition_name": "HighTemperatureAlarm",
        "active": True,
        "acked": False,
        "retain": True,
    }


def test_a_plain_event_still_carries_every_field():
    """Null, not absent: one record shape has to describe both kinds of event."""
    record = event_record(
        event(
            event_type=ua.Variant(ua.NodeId(2041), ua.VariantType.NodeId),
            severity=ua.Variant(500, ua.VariantType.UInt16),
        ).event_fields
    )
    assert set(record) == set(FIELD_KEYS)
    assert record["event_type"] == "ns=0;i=2041"
    assert record["condition_id"] is None
    assert record["acked"] is None


# --- the buffer ----------------------------------------------------------------


def test_events_come_back_oldest_first_and_only_once():
    handler = _BufferingHandler(severity_min=0, size=10)
    for _ in range(3):
        handler.event_notification(alarm_event())

    taken, remaining, dropped = handler.drain(2)
    assert (len(taken), remaining, dropped) == (2, 1, 0)
    assert handler.drain(10)[0], "the third event should still be waiting"
    assert handler.drain(10)[0] == [], "and nothing after that"


def test_the_severity_floor_is_applied_before_buffering():
    handler = _BufferingHandler(severity_min=500, size=10)
    handler.event_notification(alarm_event(severity=100))
    handler.event_notification(alarm_event(severity=900))

    kept, _remaining, _dropped = handler.drain(10)
    assert [record["severity"] for record in kept] == [900]


def test_a_full_buffer_drops_the_oldest_and_says_how_many():
    """Silently losing events is the one thing a fixed-size buffer must not do."""
    handler = _BufferingHandler(severity_min=0, size=2)
    for severity in (100, 200, 300):
        handler.event_notification(alarm_event(severity=severity))

    kept, _remaining, dropped = handler.drain(10)
    assert [record["severity"] for record in kept] == [200, 300]
    assert dropped == 1
    assert handler.drain(10)[2] == 0, "the overflow count is reported once, then reset"


@pytest.mark.parametrize("marker", ["refreshStartEventTypeNodeId", "refreshEndEventTypeNodeId"])
def test_condition_refresh_markers_never_reach_the_buffer(marker):
    """`list_active_alarms` triggers these; they are not plant events."""
    handler = _BufferingHandler(severity_min=0, size=10)
    marker_type = ua.Variant(ua.NodeId.from_string(EVENTS[marker]), ua.VariantType.NodeId)
    handler.event_notification(alarm_event(event_type=marker_type))
    assert handler.drain(10)[0] == []


# --- the event_id -> condition memory --------------------------------------------


def test_the_condition_behind_an_event_is_remembered_for_acknowledgement():
    """Why `acknowledge_alarm` needs only the event_id a model has just seen."""
    subscriptions = EventSubscriptions()
    subscriptions.remember([event_record(alarm_event().event_fields)])
    assert subscriptions.condition_for("AQI=") == "ns=1;i=1002"


def test_an_unseen_event_has_no_remembered_condition():
    assert EventSubscriptions().condition_for("AQI=") is None


def test_a_plain_event_leaves_nothing_to_acknowledge():
    subscriptions = EventSubscriptions()
    plain = event(event_id=ua.Variant(b"\x01\x02", ua.VariantType.ByteString))
    subscriptions.remember([event_record(plain.event_fields)])
    assert subscriptions.condition_for("AQI=") is None


# --- the promised defaults --------------------------------------------------------


def test_the_defaults_are_the_ones_the_contract_promises():
    """The tool descriptions state these numbers; both servers read them from there."""
    assert DEFAULTS["severityMin"] == 0
    assert DEFAULTS["bufferSize"] == 100
    assert DEFAULTS["readLimit"] == 50
    assert DEFAULTS["refreshTimeoutSeconds"] == 5
